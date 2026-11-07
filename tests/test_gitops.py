"""阶段 C 结构化 Git：8 工具、临时仓库、dry_run、凭据保护与审计。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gateway.gitops import tools as git


def _repo(tmp_path: Path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    env = {**__import__("os").environ,
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org"}
    def run(*args):
        return subprocess.run(["git", "-C", str(root), *args], env=env,
                              capture_output=True, text=True, check=True)

    run("init", "-q")
    run("config", "user.name", "Test")
    run("config", "user.email", "test@example.org")
    # CNB 环境全局强制 gpg 签名会拒绝测试仓库提交；仓库级显式关闭。
    run("config", "commit.gpgsign", "false")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "-q", "-m", "initial")
    return str(root)


def test_all_eight_tools_on_temporary_repo(tmp_path, isolated_storage):
    repo = _repo(tmp_path)
    inspect = git.git_inspect(repo, "req-inspect")
    assert inspect["ok"] and inspect["branch"] in ("master", "main")
    assert inspect["repo_label"]

    assert git.git_branch(repo, "feature-x", "create", request_id="r")["ok"]
    assert "feature-x" in " ".join(
        git.git_branch(repo, action="list")["stdout"].split())
    assert git.git_branch(repo, "feature-x", "switch", request_id="r")["ok"]

    (tmp_path / "repo" / "new.txt").write_text("data", encoding="utf-8")
    assert git.git_stage(repo, "new.txt", request_id="r")["ok"]
    committed = git.git_commit(repo, "Add new file with token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                               dry_run=False, request_id="r")
    assert committed["ok"] and committed.get("commit_sha")

    assert git.git_sync(repo, "fetch", dry_run=True)["status"] == "preview"
    assert git.git_recover(repo, dry_run=True)["status"] == "preview"
    assert "reflog" in git.git_recover(repo, dry_run=True)["note"]
    assert git.git_worktree(repo, "list", request_id="r")["ok"]
    assert git.git_clone("https://github.com/example/repo.git",
                         str(tmp_path / "clone"), dry_run=True)["status"] == "preview"


def test_clone_rejects_embedded_credentials_and_no_shell(monkeypatch):
    result = git.git_clone("https://user:pat@example.com/r.git", "/tmp/clone",
                           dry_run=False)
    assert not result["ok"] and "URL" in result["stderr"]
    calls = []
    original_run = subprocess.run
    monkeypatch.setattr(git.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)) or
                        original_run(["true"], capture_output=True, text=True))
    git.git_stage("/workspace", "README.md", dry_run=False)
    assert calls and calls[0][0][0] == "git"
    assert calls[0][1]["shell"] is False, "必须参数列表执行"


def test_high_risk_operations_default_to_preview(tmp_path):
    repo = _repo(tmp_path)
    assert git.git_clone("https://github.com/example/repo.git",
                         str(tmp_path / "clone"))["dry_run"] is True
    assert git.git_commit(repo, "message")["dry_run"] is True
    assert git.git_sync(repo, "push")["dry_run"] is True
    assert git.git_recover(repo)["dry_run"] is True
    forced = git.git_sync(repo, "push", force=True, dry_run=True)
    assert "--force-with-lease" in forced["argv"]
    assert git.git_branch(repo, "bad branch; echo pwned", "create")["ok"] is False


def test_git_audit_rows_are_redacted(tmp_path, isolated_storage):
    repo = _repo(tmp_path)
    git.git_commit(repo, "secret ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                   dry_run=False, request_id="req-git")
    rows = isolated_storage._query(
        "SELECT * FROM git_operations WHERE request_id=?", ["req-git"])
    assert rows
    assert "ghp_" not in str(rows[0]), "提交信息/审计字段必须脱敏"
    assert rows[0]["operation"] == "commit"
    assert rows[0]["repo_name"]
