"""Git 删除硬防护回归测试（仅依赖标准库，可用 unittest 独立运行）。

    python3 -m unittest discover -s tests -p 'test_git_guard.py' -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.git_guard import (  # noqa: E402
    check_argv, check_command, check_write_path, run_git, GitGuardError,
)

DENY_GIT_COMMANDS = [
    "git branch -D main",
    "git branch -d feature/x",
    "git branch -m main legacy",
    "git push origin --delete main",
    "git push origin :main",
    "git push origin main:",
    "git push --mirror",
    "git push --prune origin",
    "git push origin +main",
    "git update-ref -d refs/heads/main",
    "git worktree remove --force /workspace/wt",
    "git worktree prune",
    "git remote remove origin",
    "git remote prune origin",
    "git tag -d v1",
    "git fetch --prune",
    "git symbolic-ref --delete refs/remotes/origin/HEAD",
    "GIT_DIR=.git git branch -D main",
    "sudo git branch -D main",
    "env git push origin --delete main",
    "/usr/bin/git branch -D main",
    "git -C /workspace/repo branch -D main",
    "git -c alias.del=branch del -D main",
    "git config alias.del 'branch -D'",
    "echo ok; git branch -D main",
    "git status && git push origin :release",
    "bash -c \"git push origin --delete main\"",
    "sh -c 'rm -rf .git'",
    "python3 -c \"import subprocess;subprocess.run(['git','branch','-D','main'])\"",
    "python3 -c \"import shutil;shutil.rmtree('.git')\"",
    "rm -rf .git",
    "rm -rf ./myrepo/.git",
    "mv .git /tmp/gone",
    "find .git -name '*.lock' -delete",
    "echo 'x' > .git/refs/heads/main",
    "printf 'git branch -D main' > /tmp/s.sh && sh /tmp/s.sh",
    "echo Z2l0IGJyYW5jaCAtRCBtYWlu | base64 -d | sh",
]

ALLOW_GIT_COMMANDS = [
    "git status",
    "git checkout -b exp",
    "git switch main",
    "git commit -am 'experiment'",
    "git push origin main",
    "git branch -a",
    "git log --oneline -n 5",
    "git remote -v",
    "git fetch origin",
    "git diff --staged",
    "git reset --hard HEAD~1",  # 原系统按 destructive 需确认；不在硬禁止范围
    "git clean -fd",
    "python train.py --epochs 10",
    "python train.py --delete-old-checkpoints",  # 不含 git 上下文，不应误杀
    "python3 -c \"print('cleanup --delete flag')\"",
    "ls -la && pip install numpy",
]


class TestGitGuard(unittest.TestCase):
    def test_deny_delete_commands(self):
        for command in DENY_GIT_COMMANDS:
            with self.subTest(command=command):
                decision = check_command(command, cwd="/workspace")
                self.assertFalse(decision.allowed, f"应被拒绝: {command}")

    def test_allow_normal_commands(self):
        for command in ALLOW_GIT_COMMANDS:
            with self.subTest(command=command):
                decision = check_command(command, cwd="/workspace")
                self.assertTrue(decision.allowed, f"不应误杀: {command} -> {decision.reason}")

    def test_argv_matrix(self):
        deny = [
            ["branch", "-D", "main"], ["branch", "-d", "x"], ["push", "origin", ":main"],
            ["push", "origin", "main:"], ["push", "--delete", "origin", "main"],
            ["update-ref", "-d", "refs/heads/main"], ["remote", "prune", "origin"],
            ["worktree", "remove", "wt"], ["fetch", "--prune"],
            ["-C", "/repo", "branch", "-D", "main"], ["-c", "alias.x=branch", "x"],
            ["del", "-D", "main"], ["--exec-path=/tmp", "branch", "-D", "main"],
        ]
        allow = [
            ["status"], ["branch", "-a"], ["switch", "main"], ["push", "origin", "main"],
            ["push", "origin", "main:backup"], ["commit", "-m", "msg"],
            ["-C", "/repo", "status"], ["rev-parse", "--abbrev-ref", "HEAD"],
        ]
        for argv in deny:
            with self.subTest(argv=argv):
                self.assertFalse(check_argv(argv).allowed, argv)
        for argv in allow:
            with self.subTest(argv=argv):
                self.assertTrue(check_argv(argv).allowed, f"{argv} -> {check_argv(argv).reason}")

    def test_lfs_subcommands(self):
        allow = [
            ["lfs", "pre-push", "origin", "https://cnb.cool/webgamer-2026/h1.git"],
            ["lfs", "filter-process"],
            ["lfs", "smudge"],
            ["lfs", "clean"],
            ["lfs", "pointer", "--file", "a.bin"],
            ["lfs", "version"],
            ["lfs", "env"],
            ["lfs", "track", "*.psd"],
            ["lfs", "ls-files"],
            ["lfs", "status"],
            ["lfs", "fsck"],
            ["lfs", "push", "origin", "main"],
            ["lfs", "fetch", "origin"],
        ]
        deny = [
            ["lfs", "prune"],
            ["lfs", "uninstall"],
            ["lfs", "migrate", "import", "--everything"],
            ["lfs", "pre-push", "--delete", "origin"],
            ["lfs", "track", "--force"],
            ["lfs"],
        ]
        for argv in allow:
            with self.subTest(argv=argv):
                decision = check_argv(argv)
                self.assertTrue(decision.allowed, f"{argv} -> {decision.reason}")
        for argv in deny:
            with self.subTest(argv=argv):
                self.assertFalse(check_argv(argv).allowed, argv)
        self.assertTrue(check_command("git lfs version", cwd="/workspace").allowed)

    def test_repository_directory_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo"
            (repo / ".git").mkdir(parents=True)
            for command in [f"rm -rf {repo}", "rm -rf ./myrepo", "rm -rf .",
                            "mv ./myrepo /tmp/gone", "rm -rf *"]:
                with self.subTest(command=command):
                    decision = check_command(command, cwd=str(Path(tmp)))
                    self.assertFalse(decision.allowed, f"应拒绝删除仓库目录: {command}")

    def test_write_paths(self):
        for path in ["/workspace/repo/.git/config", ".git/refs/heads/main",
                     "/root/.ssh/authorized_keys", "/etc/shadow", "/app/.env"]:
            with self.subTest(path=path):
                self.assertFalse(check_write_path(path).allowed, path)
        for path in ["/workspace/main.py", "/workspace/results/plot.png", "/tmp/scratch.txt"]:
            with self.subTest(path=path):
                self.assertTrue(check_write_path(path).allowed, path)

    def test_structured_git_tools_have_no_delete_api(self):
        from gateway import git_guard

        class FakeMCP:
            def __init__(self):
                self.tools = {}

            def tool(self):
                def decorate(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return decorate

        fake = FakeMCP()
        git_guard.register_git_tools(fake, "/tmp", lambda _msg: None)
        self.assertEqual(len(fake.tools), 13)
        for name in ("git_status", "git_commit", "git_switch", "git_branch_create", "git_push"):
            self.assertIn(name, fake.tools)
        for name in fake.tools:
            self.assertNotIn("delete", name)
            self.assertNotIn("remove", name)

    @unittest.skipUnless(shutil.which("git"), "需要系统 git")
    def test_run_git_allows_commit_and_denies_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="t@example.com",
                       GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="t@example.com")
            subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
            (repo / "a.txt").write_text("hello\n")
            run_git(["add", "a.txt"], cwd=str(repo))
            run_git(["commit", "-m", "init"], cwd=str(repo))
            self.assertIn("init", run_git(["log", "--oneline"], cwd=str(repo)))
            run_git(["branch", "feature"], cwd=str(repo))
            self.assertTrue((repo / ".git" / "refs" / "heads" / "feature").exists()
                            or "feature" in run_git(["branch", "--list"], cwd=str(repo)))
            with self.assertRaises(GitGuardError):
                run_git(["branch", "-D", "feature"], cwd=str(repo))
            self.assertIn("feature", run_git(["branch", "--list"], cwd=str(repo)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
