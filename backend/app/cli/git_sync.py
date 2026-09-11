"""git-sync 命令行：内网无 webhook 环境，从本地 git 仓库同步提交到平台。

用法：
  python -m app.cli.git_sync --repo /path/to/repo --webhook http://platform:8000/api/v1/integrations/git/webhook/<secret> [--branch main] [--limit 20]

建议放进 CI：每次构建/部署时执行一次，把最近提交推给平台分析与回归触发。
"""
import argparse
import subprocess
import sys
import httpx


def git(repo, *args):
    out = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description="把本地 git 提交同步到 TestDeck")
    ap.add_argument("--repo", required=True, help="本地仓库路径")
    ap.add_argument("--webhook", required=True, help="平台 webhook 地址")
    ap.add_argument("--branch", default=None, help="分支名（默认取当前分支）")
    ap.add_argument("--limit", type=int, default=20, help="最多同步最近 N 条提交")
    a = ap.parse_args()

    branch = a.branch or git(a.repo, "rev-parse", "--abbrev-ref", "HEAD")
    log = git(a.repo, "log", f"-{a.limit}", "--pretty=format:%H%x1f%an%x1f%s")
    commits = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            sha, author, msg = parts
            files = git(a.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
            commits.append({"id": sha, "message": msg, "author": {"name": author},
                            "added": files[:20], "modified": [], "removed": []})
    if not commits:
        print("没有可同步的提交")
        return

    r = httpx.post(a.webhook, json={"ref": f"refs/heads/{branch}", "branch": branch, "commits": commits}, timeout=30)
    body = r.json()
    print(f"已同步 {body.get('ingested', 0)} 条提交（{branch}）"
          + (f"，触发计划 {len(body.get('triggered_plans', []))} 个" if body.get("triggered_plans") else ""))


if __name__ == "__main__":
    main()
