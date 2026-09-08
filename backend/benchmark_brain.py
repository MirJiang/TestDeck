"""B4 基准对比：真实用例分别跑新旧两条 brain 路径，对比成功率 / 平均 token / 耗时。

平台排期（docs/PLAN.md 批次 B4）：双路径共存仅限本对比窗口——达标后在 .env 设
TD_BRAIN=agentscope 切默认，然后删除 ai_runner 里的手搓循环。

用法（需在配置好「模型配置」与项目环境地址的真实环境跑，会真实操作被测系统）：
    cd backend
    .venv/Scripts/python benchmark_brain.py <case_id> [case_id ...] [-n 每路径轮数]

对比口径：
  - 成功率：status == passed 的轮次占比
  - 平均 token：llm_logs 中 kind=agent-step 的 prompt+completion 增量 / 轮数
  - 平均耗时：run_ai_case 返回的 duration 均值
"""
import argparse
import sys
import time

from app import config
from app.db import SessionLocal
from app.models import TestCase, Env, LLMLog


def _token_cursor(db) -> int:
    row = db.query(LLMLog).order_by(LLMLog.id.desc()).first()
    return row.id if row else 0


def _tokens_since(db, cursor: int) -> int:
    rows = db.query(LLMLog).filter(LLMLog.id > cursor, LLMLog.kind == "agent-step").all()
    return sum((r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in rows)


def run_one(case, env, brain: str, idx: int) -> dict:
    from app.engine.ai_runner import run_ai_case
    if brain == "agentscope":
        config.set("TD_BRAIN", "agentscope")
    else:
        config.set("TD_BRAIN", "legacy")
    db = SessionLocal()
    try:
        cursor = _token_cursor(db)
        run_id = f"bench-{brain}-{case.id[:8]}-{idx}-{int(time.time())}"
        t0 = time.time()
        r = run_ai_case(case, env, run_id)
        wall = round(time.time() - t0, 2)
        tokens = _tokens_since(db, cursor)
    finally:
        db.close()
    return {"brain": brain, "case": case.name, "round": idx,
            "status": r.get("status"), "summary": (r.get("summary") or "")[:80],
            "duration": r.get("duration") or wall, "tokens": tokens,
            "pass_n": r.get("pass_n", 0), "fail_n": r.get("fail_n", 0)}


def main():
    ap = argparse.ArgumentParser(description="新旧 brain 基准对比（PLAN 批次 B4）")
    ap.add_argument("case_ids", nargs="+", help="AI 用例 id（可多个）")
    ap.add_argument("-n", "--rounds", type=int, default=3, help="每条路径每个用例跑几轮（默认 3）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        cases = []
        for cid in args.case_ids:
            c = db.get(TestCase, cid)
            if not c:
                print(f"用例不存在：{cid}", file=sys.stderr)
                return 2
            env = db.query(Env).filter(Env.project_id == c.project_id).first()
            if not env or not (env.base_url or "").startswith(("http://", "https://")):
                print(f"用例 {c.name} 的项目未配置有效环境地址", file=sys.stderr)
                return 2
            cases.append((c, env))

        results = []
        for brain in ("legacy", "agentscope"):     # 先旧后新：同一环境时段尽量接近
            for c, env in cases:
                for i in range(1, args.rounds + 1):
                    print(f"[{brain}] {c.name} 第 {i}/{args.rounds} 轮 …", flush=True)
                    row = run_one(c, env, brain, i)
                    results.append(row)
                    print(f"  → {row['status']} · {row['duration']}s · {row['tokens']} tokens · {row['summary']}")
    finally:
        config.set("TD_BRAIN", "legacy")           # 恢复默认，避免影响后续手动执行
        db.close()

    print("\n========== 基准对比汇总 ==========")
    print(f"{'路径':<12}{'轮数':>4}{'成功率':>8}{'平均耗时':>10}{'平均token':>10}")
    agg = {}
    for r in results:
        a = agg.setdefault(r["brain"], {"n": 0, "ok": 0, "dur": 0.0, "tok": 0})
        a["n"] += 1
        a["ok"] += 1 if r["status"] == "passed" else 0
        a["dur"] += r["duration"] or 0
        a["tok"] += r["tokens"]
    for brain in ("legacy", "agentscope"):
        a = agg.get(brain)
        if not a:
            continue
        print(f"{brain:<12}{a['n']:>4}{a['ok'] / a['n']:>7.0%}"
              f"{a['dur'] / a['n']:>9.1f}s{a['tok'] // a['n']:>10}")
    la, aa = agg.get("legacy"), agg.get("agentscope")
    if la and aa:
        print("\n判读（PLAN B4）：agentscope 成功率不低于 legacy、token 与耗时可接受 → "
              ".env 设 TD_BRAIN=agentscope 切默认，然后删除旧循环。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
