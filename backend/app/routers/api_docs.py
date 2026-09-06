"""API 文档导入：Swagger 2.0 / OpenAPI 3.x / Postman Collection（Apifox 导出 OpenAPI 亦可）。
支持直接粘贴内容或给 URL 由平台拉取。"""
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, ApiDoc, ApiEndpoint
from ..auth import current_user
from ..perms import check_project_access
from ..engine.api_docs import parse_spec

router = APIRouter(prefix="/api/v1/projects/{pid}/api-docs", tags=["api-docs"])


class ImportIn(BaseModel):
    name: str = ""
    content: str = ""
    url: str = ""          # 平台从该地址拉取文档（如 http://host/v3/api-docs）


@router.post("/import")
def import_doc(pid: str, body: ImportIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    content = body.content or ""
    url = body.url.strip()
    if url:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "URL 需以 http(s):// 开头")
        try:
            r = httpx.get(url, timeout=20, trust_env=False, follow_redirects=True)
        except Exception as e:
            raise HTTPException(400, f"拉取失败：{type(e).__name__}: {e}"[:200])
        if r.status_code != 200:
            raise HTTPException(400, f"拉取失败：HTTP {r.status_code}")
        content = r.text
    if not content.strip():
        raise HTTPException(400, "请提供文档内容或 URL")
    if len(content) > 5_000_000:
        raise HTTPException(400, "文档过大（>5MB）")
    try:
        parsed = parse_spec(content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    doc = ApiDoc(project_id=pid, name=body.name.strip() or parsed["title"] or "未命名文档",
                 format=parsed["format"], base_url=parsed.get("base_url", ""),
                 spec={"endpoints": parsed["endpoints"],
                       "source_url": url if url else ""},
                 endpoints_count=len(parsed["endpoints"]))
    db.add(doc)
    db.commit()
    seen, eps = set(), []
    for e in parsed["endpoints"]:
        key = (e["method"], e["path"])
        if key in seen:
            continue
        seen.add(key)
        eps.append(ApiEndpoint(project_id=pid, doc_id=doc.id, method=e["method"],
                               path=e["path"], summary=e.get("summary", ""), params=e.get("params") or {}))
    if eps:
        db.add_all(eps)
        db.commit()
    return {"id": doc.id, "name": doc.name, "format": doc.format,
            "base_url": doc.base_url, "count": len(eps)}


@router.get("")
def list_docs(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    rows = (db.query(ApiDoc).filter(ApiDoc.project_id == pid)
            .order_by(ApiDoc.created_at.desc()).all())
    return [{"id": r.id, "name": r.name, "format": r.format, "base_url": r.base_url,
             "count": r.endpoints_count, "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/{did}/endpoints")
def list_endpoints(pid: str, did: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    rows = (db.query(ApiEndpoint)
            .filter(ApiEndpoint.project_id == pid, ApiEndpoint.doc_id == did)
            .order_by(ApiEndpoint.path, ApiEndpoint.method).all())
    return [{"id": r.id, "method": r.method, "path": r.path, "summary": r.summary,
             "params": r.params or {}} for r in rows]


@router.post("/{did}/refresh")
def refresh_doc(pid: str, did: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """按导入时的 URL 重新拉取并覆盖更新（仅 URL 导入的文档可刷新）。"""
    check_project_access(pid, user, db)
    doc = db.get(ApiDoc, did)
    if not doc or doc.project_id != pid:
        raise HTTPException(404, "文档不存在")
    if not doc.spec or not doc.spec.get("source_url"):
        raise HTTPException(400, "该文档非 URL 导入，无法刷新")
    url = doc.spec["source_url"]
    try:
        r = httpx.get(url, timeout=20, trust_env=False, follow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(400, f"拉取失败：HTTP {r.status_code}")
        parsed = parse_spec(r.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"拉取失败：{type(e).__name__}: {e}"[:200])
    for e in db.query(ApiEndpoint).filter(ApiEndpoint.doc_id == did):
        db.delete(e)
    seen, eps = set(), []
    for e in parsed["endpoints"]:
        key = (e["method"], e["path"])
        if key in seen:
            continue
        seen.add(key)
        eps.append(ApiEndpoint(project_id=pid, doc_id=did, method=e["method"],
                               path=e["path"], summary=e.get("summary", ""), params=e.get("params") or {}))
    if eps:
        db.add_all(eps)
    doc.format = parsed["format"]
    doc.base_url = parsed.get("base_url", "")
    doc.spec = {"endpoints": parsed["endpoints"], "source_url": url}
    doc.endpoints_count = len(eps)
    db.commit()
    return {"ok": True, "count": len(eps)}


@router.delete("/{did}")
def delete_doc(pid: str, did: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    doc = db.get(ApiDoc, did)
    if not doc or doc.project_id != pid:
        raise HTTPException(404, "文档不存在")
    for e in db.query(ApiEndpoint).filter(ApiEndpoint.doc_id == did):
        db.delete(e)
    db.delete(doc)
    db.commit()
    return {"ok": True}
