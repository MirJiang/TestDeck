from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx
from ..db import get_db
from ..models import User, NotifyChannel, LLMConfig
from ..auth import current_user, require_admin
from .. import ai as A

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# ---------- 模型配置（多条，单条「使用中」） ----------

class LLMIn(BaseModel):
    name: str = ""
    vendor: str = ""
    url_type: str = "api"      # api | plan
    base_url: str
    api_key: str = ""          # 编辑时留空 = 保留已保存的 key
    model: str
    vision: bool = False       # 模型支持图片输入（AI 用例可发截图识别验证码）


def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 14 else "已配置"


def _validate_llm(body: LLMIn):
    if not body.base_url.strip().startswith(("http://", "https://")):
        raise HTTPException(400, "地址需以 http(s):// 开头")
    if not body.model.strip():
        raise HTTPException(400, "模型名不能为空")
    if body.url_type not in ("api", "plan", "plan2"):
        raise HTTPException(400, "接入方式不合法")


@router.get("/llm")
def list_llm(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = (db.query(LLMConfig)
            .order_by(LLMConfig.is_active.desc(), LLMConfig.id.desc()).all())
    return {"items": [{"id": r.id, "name": r.name or r.model, "vendor": r.vendor,
                       "url_type": r.url_type or "api", "base_url": r.base_url,
                       "model": r.model, "is_active": r.is_active, "vision": bool(r.vision),
                       "api_key_masked": _mask(r.api_key)} for r in rows],
            "available": A.llm_available(), "source": A.cfg_source(),
            "env_fallback": A.cfg_source() == "env"}


@router.post("/llm")
def create_llm(body: LLMIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _validate_llm(body)
    if not body.api_key.strip():
        raise HTTPException(400, "请填写 API Key")
    has_active = db.query(LLMConfig).filter(LLMConfig.is_active == True).count() > 0  # noqa
    row = LLMConfig(name=body.name.strip(), vendor=body.vendor.strip(),
                    url_type=body.url_type, base_url=body.base_url.strip().rstrip("/"),
                    api_key=body.api_key.strip(), model=body.model.strip(),
                    vision=body.vision,
                    is_active=not has_active)   # 首条自动设为使用中
    db.add(row); db.commit()
    return {"id": row.id, "is_active": row.is_active}


@router.put("/llm/{id}")
def update_llm(id: int, body: LLMIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    row = db.get(LLMConfig, id)
    if not row:
        raise HTTPException(404, "配置不存在")
    _validate_llm(body)
    row.name, row.vendor = body.name.strip(), body.vendor.strip()
    row.url_type, row.base_url = body.url_type, body.base_url.strip().rstrip("/")
    row.model = body.model.strip()
    row.vision = body.vision
    if body.api_key.strip():
        row.api_key = body.api_key.strip()
    db.commit()
    return {"ok": True}


@router.delete("/llm/{id}")
def delete_llm(id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    row = db.get(LLMConfig, id)
    if not row:
        raise HTTPException(404, "配置不存在")
    db.delete(row); db.commit()
    return {"ok": True}


@router.post("/llm/{id}/activate")
def activate_llm(id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    row = db.get(LLMConfig, id)
    if not row:
        raise HTTPException(404, "配置不存在")
    for r in db.query(LLMConfig).filter(LLMConfig.is_active == True):  # noqa
        r.is_active = False
    row.is_active = True
    db.commit()
    return {"ok": True}


class TestIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@router.post("/llm/test")
def test_llm(body: TestIn, user: User = Depends(require_admin)):
    """用给定参数做连接测试（未给全时回落到当前使用中的配置）。"""
    return A.test_connection(body.base_url, body.api_key, body.model)


@router.post("/llm/{id}/test")
def test_llm_by_id(id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """对某条已保存配置做连接测试（key 在服务端，前端拿不到明文）。"""
    row = db.get(LLMConfig, id)
    if not row:
        raise HTTPException(404, "配置不存在")
    return A.test_connection(row.base_url, row.api_key, row.model)


class ModelsIn(BaseModel):
    base_url: str
    api_key: str = ""      # 留空 = 用指定 id / 使用中配置的已存 key
    id: int = 0


@router.post("/llm/models")
def fetch_models(body: ModelsIn, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """拉取厂商的可用模型列表（OpenAI 兼容 GET /models），免去手工收集模型名。"""
    base = body.base_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, "地址需以 http(s):// 开头")
    key = body.api_key.strip()
    if not key and body.id:
        row = db.get(LLMConfig, body.id)
        key = row.api_key if row else ""
    if not key:
        act = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()  # noqa
        key = (act.api_key if act else "") or A.fallback_llm()[1]
    try:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"},
                      timeout=15, trust_env=False)
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        models = sorted({m.get("id") for m in (items or []) if isinstance(m, dict) and m.get("id")})
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "models": []}


class ChannelIn(BaseModel):
    name: str
    url: str
    on_fail: bool = True
    enabled: bool = True


@router.get("/notify")
def list_channels(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [{"id": c.id, "name": c.name, "url": c.url[:40] + "…" if len(c.url) > 40 else c.url,
             "on_fail": c.on_fail, "enabled": c.enabled, "last_status": c.last_status}
            for c in db.query(NotifyChannel).all()]


@router.post("/notify")
def create_channel(body: ChannelIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if not body.url.startswith("http"):
        raise HTTPException(400, "webhook 地址不合法")
    c = NotifyChannel(**body.model_dump())
    db.add(c); db.commit()
    return {"id": c.id}


@router.put("/notify/{cid}")
def update_channel(cid: str, body: ChannelIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    c = db.get(NotifyChannel, cid)
    if not c:
        raise HTTPException(404, "通知渠道不存在")
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/notify/{cid}")
def delete_channel(cid: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    c = db.get(NotifyChannel, cid)
    if not c:
        raise HTTPException(404, "通知渠道不存在")
    db.delete(c); db.commit()
    return {"ok": True}


@router.post("/notify/{cid}/test")
async def test_channel(cid: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    import httpx
    c = db.get(NotifyChannel, cid)
    if not c:
        raise HTTPException(404, "通知渠道不存在")
    try:
        r = await httpx.AsyncClient(timeout=10).post(
            c.url, json={"msgtype": "text", "text": {"content": "[TestDeck] 这是一条测试通知，配置成功 ✓"}})
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "status": str(e)[:100]}
