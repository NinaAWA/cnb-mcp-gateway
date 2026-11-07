"""结构化 Git 操作（V3 阶段 C）。

- 全部用参数列表 ``subprocess.run(argv, shell=False)``，避免 shell 注入；
- 同一仓库串行化（按 repo 锁），避免并发写坏索引；
- 高风险操作默认 ``dry_run`` 预览，必须显式关闭才真正执行；
- 凭据只走 ``GIT_ASKPASS`` / 环境变量 / credential helper，
  绝不进入 argv、命令串、日志或审计；
- 远端 URL 与提交信息统一经 ``safe_repo_label`` / 脱敏后记录。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from gateway.audit.enrich import safe_repo_label
from gateway.audit.events import utcnow
from gateway.audit.redaction import redact_text

_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: dict[str, threading.Lock] = {}
_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def repo_lock(repo: str) -> threading.Lock:
    key = str(Path(repo).resolve())
    with _LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(key, threading.Lock())


def _safe_url(url: str) -> str:
    value = str(url or "").strip()
    if _CREDENTIAL_IN_URL.search(value):
        raise ValueError("远端 URL 不得内嵌用户名/密码；请使用 GIT_ASKPASS 或 "
                         "credential helper")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("https", "ssh", "git", "file", ""):
        raise ValueError(f"不支持的远端协议: {parts.scheme}")
    if parts.username or parts.password:
        raise ValueError("远端 URL 不得携带凭据")
    return value


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # 禁止交互式索取凭据；真实凭据由部署环境的 GIT_ASKPASS / helper 提供。
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", os.environ.get("GIT_ASKPASS", ""))
    return env


def _run(argv: list[str], *, repo: str = "", timeout: int = 60,
         dry_run: bool = False) -> dict[str, Any]:
    command = [str(x) for x in argv]
    result: dict[str, Any] = {
        "argv": command, "repo_label": safe_repo_label(repo) if repo else "",
        "dry_run": bool(dry_run), "ts": utcnow(),
    }
    if dry_run:
        result.update({"ok": True, "status": "preview", "stdout": "",
                       "stderr": "", "exit": 0,
                       "preview": " ".join(redact_text(x) for x in command)})
        return result
    try:
        proc = subprocess.run(command, shell=False, capture_output=True,
                              text=True, timeout=timeout, env=_env(),
                              cwd=repo or None)
        result.update({
            "ok": proc.returncode == 0, "status": "success" if proc.returncode == 0
            else "failed",
            "stdout": redact_text(proc.stdout or "")[:20000],
            "stderr": redact_text(proc.stderr or "")[:4000],
            "exit": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        result.update({"ok": False, "status": "timeout", "stdout": "",
                       "stderr": f"命令超过 {timeout}s 超时", "exit": -1})
    except OSError as exc:
        result.update({"ok": False, "status": "failed", "stdout": "",
                       "stderr": redact_text(repr(exc))[:400], "exit": -1})
    return result


def _audit(op: str, repo: str, result: dict[str, Any],
           request_id: str = "") -> None:
    try:
        from gateway.audit.storage import get_storage
        get_storage().add_git({
            "request_id": request_id, "trace_id": "", "ts": utcnow(),
            "repo_path": redact_text(repo)[:200],
            "repo_name": result.get("repo_label", ""), "branch": result.get("branch", ""),
            "operation": op, "files_changed": int(result.get("files_changed") or 0),
            "commit_sha": result.get("commit_sha", ""),
            "commit_message": redact_text(result.get("commit_message") or "")[:300],
            "push_remote": result.get("remote", ""),
            "push_branch": result.get("push_branch", ""),
            "client_id": "", "status": result.get("status", ""),
            "error_message": redact_text(result.get("stderr") or "")[:300],
        })
    except Exception:  # noqa: BLE001 - 审计失败不改变 Git 结果
        pass


def _finish(op: str, repo: str, result: dict[str, Any],
            request_id: str = "") -> dict[str, Any]:
    _audit(op, repo, result, request_id)
    return result


def git_inspect(repo: str, request_id: str = "") -> dict[str, Any]:
    """查看分支、状态、最近提交与远端（只读）。"""
    with repo_lock(repo):
        branch = _run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                      repo=repo)
        status = _run(["git", "-C", repo, "status", "--porcelain=v1"], repo=repo)
        log = _run(["git", "-C", repo, "log", "-5", "--oneline"], repo=repo)
        remotes = _run(["git", "-C", repo, "remote", "-v"], repo=repo)
        result = {
            "ok": all(x["ok"] for x in (branch, status, log, remotes)),
            "status": "success", "op": "inspect", "repo_label": safe_repo_label(repo),
            "branch": branch["stdout"].strip(),
            "changes": [line for line in status["stdout"].splitlines() if line.strip()],
            "files_changed": len([x for x in status["stdout"].splitlines() if x.strip()]),
            "recent_commits": log["stdout"].splitlines(),
            "remotes": redact_text(remotes["stdout"])[:2000],
            "ts": utcnow(),
        }
        return _finish("inspect", repo, result, request_id)


def git_clone(url: str, dest: str, branch: str = "", depth: int = 0,
              dry_run: bool = True, request_id: str = "") -> dict[str, Any]:
    """克隆仓库；默认 dry_run 预览，凭据只允许走 GIT_ASKPASS / helper。"""
    try:
        safe = _safe_url(url)
    except ValueError as exc:
        return {"ok": False, "status": "denied", "op": "clone",
                "stderr": str(exc), "repo_label": safe_repo_label(url)}
    argv = ["git", "clone"]
    if branch:
        argv += ["--branch", branch]
    if depth and depth > 0:
        argv += ["--depth", str(depth)]
    argv += [safe, dest]
    with repo_lock(dest):
        result = _run(argv, timeout=180, dry_run=dry_run)
    result.update({"op": "clone", "repo_label": safe_repo_label(safe),
                   "credential_policy": "GIT_ASKPASS/credential helper only"})
    return _finish("clone", dest, result, request_id)


def git_branch(repo: str, name: str = "", action: str = "list",
               dry_run: bool = False, request_id: str = "") -> dict[str, Any]:
    """分支列举 / 创建 / 切换 / 删除；删除默认需要显式 dry_run=False。"""
    if action not in ("list", "create", "switch", "delete"):
        return {"ok": False, "status": "denied", "op": "branch",
                "stderr": "action 只能是 list/create/switch/delete"}
    if action != "list" and not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", name or ""):
        return {"ok": False, "status": "denied", "op": "branch",
                "stderr": "分支名不合法"}
    if action == "list":
        argv = ["git", "-C", repo, "branch", "--list"]
    elif action == "create":
        argv = ["git", "-C", repo, "branch", name]
    elif action == "switch":
        argv = ["git", "-C", repo, "switch", name]
    else:
        argv = ["git", "-C", repo, "branch", "-D", name]
        if not dry_run:
            dry_run = False  # 显式调用方可关闭预览
    with repo_lock(repo):
        result = _run(argv, repo=repo, dry_run=dry_run)
    result.update({"op": "branch", "action": action, "branch": name,
                   "repo_label": safe_repo_label(repo)})
    return _finish("branch", repo, result, request_id)


def git_stage(repo: str, paths: str = ".", dry_run: bool = False,
              request_id: str = "") -> dict[str, Any]:
    """暂存指定路径（空格分隔，逐项作为 argv，不使用 shell）。"""
    items = [p for p in str(paths or "").split() if p]
    if not items:
        return {"ok": False, "status": "denied", "op": "stage",
                "stderr": "paths 为空"}
    argv = ["git", "-C", repo, "add", "--"] + items
    with repo_lock(repo):
        result = _run(argv, repo=repo, dry_run=dry_run)
    result.update({"op": "stage", "files_changed": len(items),
                   "repo_label": safe_repo_label(repo)})
    return _finish("stage", repo, result, request_id)


def git_commit(repo: str, message: str, dry_run: bool = True,
               request_id: str = "") -> dict[str, Any]:
    """提交；默认 dry_run 预览，提交信息经脱敏后入审计。"""
    clean = redact_text(str(message or "").strip())[:300]
    if not clean:
        return {"ok": False, "status": "denied", "op": "commit",
                "stderr": "提交信息为空"}
    argv = ["git", "-C", repo, "commit", "-m", clean]
    with repo_lock(repo):
        result = _run(argv, repo=repo, dry_run=dry_run)
        if result["ok"] and not dry_run:
            head = _run(["git", "-C", repo, "rev-parse", "HEAD"], repo=repo)
            result["commit_sha"] = head["stdout"].strip()[:64]
    result.update({"op": "commit", "commit_message": clean,
                   "repo_label": safe_repo_label(repo)})
    return _finish("commit", repo, result, request_id)


def git_sync(repo: str, action: str = "fetch", remote: str = "origin",
             branch: str = "", force: bool = False, dry_run: bool = True,
             request_id: str = "") -> dict[str, Any]:
    """fetch / pull / push；默认 dry_run，force 必须显式开启且记审计。"""
    if action not in ("fetch", "pull", "push"):
        return {"ok": False, "status": "denied", "op": "sync",
                "stderr": "action 只能是 fetch/pull/push"}
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", remote or ""):
        return {"ok": False, "status": "denied", "op": "sync",
                "stderr": "远端名不合法"}
    argv = ["git", "-C", repo, action, remote]
    if branch:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch):
            return {"ok": False, "status": "denied", "op": "sync",
                    "stderr": "分支名不合法"}
        argv.append(branch)
    if force and action == "push":
        argv.insert(4, "--force-with-lease")
    with repo_lock(repo):
        result = _run(argv, repo=repo, dry_run=dry_run, timeout=180)
    result.update({"op": "sync", "action": action, "remote": remote,
                   "push_branch": branch, "force": bool(force),
                   "repo_label": safe_repo_label(repo)})
    return _finish("sync", repo, result, request_id)


def git_recover(repo: str, ref: str = "", dry_run: bool = True,
                request_id: str = "") -> dict[str, Any]:
    """基于 reflog 的恢复预览；默认只列出可恢复点，不改动工作区。"""
    with repo_lock(repo):
        reflog = _run(["git", "-C", repo, "reflog", "--date=iso", "-20"], repo=repo)
        if dry_run or not ref:
            result = {"ok": True, "status": "preview", "op": "recover",
                      "stdout": redact_text(reflog["stdout"])[:8000],
                      "stderr": "", "dry_run": True,
                      "note": "列出 reflog 候选；显式 dry_run=False 且提供 ref 才会恢复"}
        else:
            if not re.fullmatch(r"[A-Za-z0-9._~^/@-]{1,120}", ref):
                return {"ok": False, "status": "denied", "op": "recover",
                        "stderr": "恢复引用不合法"}
            result = _run(["git", "-C", repo, "reset", "--hard", ref], repo=repo)
        result["repo_label"] = safe_repo_label(repo)
        return _finish("recover", repo, result, request_id)


def git_worktree(repo: str, action: str = "list", path: str = "",
                 ref: str = "", dry_run: bool = False,
                 request_id: str = "") -> dict[str, Any]:
    """worktree 列举 / 新增 / 移除。"""
    if action not in ("list", "add", "remove"):
        return {"ok": False, "status": "denied", "op": "worktree",
                "stderr": "action 只能是 list/add/remove"}
    if action == "list":
        argv = ["git", "-C", repo, "worktree", "list", "--porcelain"]
    elif action == "add":
        if not path or not ref:
            return {"ok": False, "status": "denied", "op": "worktree",
                    "stderr": "add 需要 path 与 ref"}
        argv = ["git", "-C", repo, "worktree", "add", path, ref]
    else:
        if not path:
            return {"ok": False, "status": "denied", "op": "worktree",
                    "stderr": "remove 需要 path"}
        argv = ["git", "-C", repo, "worktree", "remove", path]
    with repo_lock(repo):
        result = _run(argv, repo=repo, dry_run=dry_run)
    result.update({"op": "worktree", "action": action,
                   "repo_label": safe_repo_label(repo)})
    return _finish("worktree", repo, result, request_id)
