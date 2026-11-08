"""v1.1 Git 真实行为测试：显式路径暂存、推送远端核验、新结构化工具。

使用临时真实 Git 仓库与本地 bare 远端，不做 mock；不依赖网络。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import git_guard  # noqa: E402
from gateway.git_guard import run_git_checked, verify_remote_push  # noqa: E402


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


class TestGitV11(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-git-v11-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        def restore(key: str, previous: str | None) -> None:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

        for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                    "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
            self.addCleanup(restore, key, os.environ.get(key))
        # 宿主 git 可能配置了强制 GPG 签名（CNB 签名钩子要求特定作者），
        # 测试仓库用环境注入关闭签名，保证测量的是 v1.1 代码而不是宿主策略。
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = "commit.gpgsign"
        os.environ["GIT_CONFIG_VALUE_0"] = "false"
        os.environ["GIT_CONFIG_KEY_1"] = "tag.gpgsign"
        os.environ["GIT_CONFIG_VALUE_1"] = "false"
        os.environ["GIT_AUTHOR_NAME"] = "v11-test"
        os.environ["GIT_AUTHOR_EMAIL"] = "v11@example.com"
        os.environ["GIT_COMMITTER_NAME"] = "v11-test"
        os.environ["GIT_COMMITTER_EMAIL"] = "v11@example.com"
        self.repo = base / "repo"
        self.remote = base / "remote.git"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                       check=True)
        _git("remote", "add", "origin", str(self.remote), cwd=str(self.repo))
        (self.repo / "a.txt").write_text("one\n")
        (self.repo / "b.log").write_text("skip\n")
        self.mcp = FakeMCP()
        git_guard.register_git_tools(self.mcp, str(self.repo), lambda _msg: None)
        self.tools = self.mcp.tools

    def _stage_commit(self, message: str = "init") -> str:
        _git("add", "a.txt", cwd=str(self.repo))
        _git("commit", "-q", "-m", message, cwd=str(self.repo))
        return _git("rev-parse", "HEAD", cwd=str(self.repo))

    def test_git_add_requires_explicit_path_array(self):
        for bad in (["."], ["*"], ["*.txt"], ["-A"], [":/"], ["a.txt", ".."]):
            with self.subTest(bad=bad):
                self.assertIn("禁止", self.tools["git_add"](paths=bad, cwd=str(self.repo)))
        out = self.tools["git_add"](paths=["a.txt"], cwd=str(self.repo))
        self.assertIn("exit=0", out)
        status = self.tools["git_status"](cwd=str(self.repo))
        self.assertIn("A  a.txt", status)   # 只有显式指定的文件被暂存
        self.assertIn("?? b.log", status)   # 无关文件仍是未跟踪，未混入暂存区
        # 向后兼容：旧的字符串入参仍按显式路径解析，但 "." 依旧拒绝。
        self.assertIn("禁止", self.tools["git_add"](paths=".", cwd=str(self.repo)))
        self.assertIn("exit=0", self.tools["git_add"](paths="b.log", cwd=str(self.repo)))

    def test_push_verifies_remote_head_against_local_commit(self):
        head = self._stage_commit("first")
        out = self.tools["git_push"](cwd=str(self.repo), remote="origin", branch="main")
        self.assertIn("exit_code=0", out)
        self.assertIn("remote_verification=PASS", out)
        self.assertIn(head, out)
        ls = _git("ls-remote", "origin", "refs/heads/main", cwd=str(self.repo))
        self.assertEqual(ls.split()[0], head)
        ok, detail = verify_remote_push(str(self.repo), "origin", "main")
        self.assertTrue(ok, detail)

    def test_verify_remote_push_detects_unpushed_local_commit(self):
        self._stage_commit("first")
        self.tools["git_push"](cwd=str(self.repo), remote="origin", branch="main")
        (self.repo / "a.txt").write_text("two\n")
        self._stage_commit("second-not-pushed")
        ok, detail = verify_remote_push(str(self.repo), "origin", "main")
        self.assertFalse(ok)
        self.assertIn("不一致", detail)

    def test_push_failure_reports_real_exit_code(self):
        self._stage_commit("first")
        _git("remote", "remove", "origin", cwd=str(self.repo))
        _git("remote", "add", "origin", str(Path(self.tmp.name) / "missing.git"),
             cwd=str(self.repo))
        out = self.tools["git_push"](cwd=str(self.repo), remote="origin", branch="main")
        self.assertNotIn("remote_verification=PASS", out)
        self.assertIn("未进行远端核验", out)
        self.assertRegex(out, r"exit_code=(?!0\b)\d+")

    def test_new_structured_tools_run_real_git(self):
        self._stage_commit("first")
        self.assertIn("exit=0", self.tools["git_show"](ref="HEAD", cwd=str(self.repo)))
        self.assertIn("exit=0", self.tools["git_branch_create"](
            branch="side", cwd=str(self.repo)))
        self.assertIn("exit=0", self.tools["git_checkout"](ref="side", cwd=str(self.repo)))
        (self.repo / "c.txt").write_text("side\n")
        self.assertIn("exit=0", self.tools["git_add"](paths=["c.txt"], cwd=str(self.repo)))
        self.assertIn("exit=0", self.tools["git_commit"](
            message="side work", cwd=str(self.repo)))
        side_sha = _git("rev-parse", "HEAD", cwd=str(self.repo))
        self.assertIn("exit=0", self.tools["git_checkout"](ref="main", cwd=str(self.repo)))
        self.assertIn("exit=0", self.tools["git_cherry_pick"](
            commit=side_sha, cwd=str(self.repo)))
        self.assertTrue((self.repo / "c.txt").is_file())
        self.assertIn("exit=0", self.tools["git_rebase"](ref="main", cwd=str(self.repo)))
        # 脏工作区 -> stash push -> stash list 可见
        (self.repo / "a.txt").write_text("dirty\n")
        self.assertIn("exit=0", self.tools["git_stash"](
            action="push", message="wip", cwd=str(self.repo)))
        self.assertIn("wip", self.tools["git_stash"](action="list", cwd=str(self.repo)))
        self.assertIn("禁止", self.tools["git_stash"](action="clear", cwd=str(self.repo)))

    def test_run_git_checked_keeps_real_exit_code(self):
        rc, out, err = run_git_checked(["rev-parse", "HEAD"], cwd=str(self.repo))
        self.assertEqual(rc, 128)  # 尚无提交，真实退出码非 0
        self.assertTrue(err)       # 错误必须出现在 stderr，绝不包装成成功
        self._stage_commit("first")
        rc, out, _ = run_git_checked(["rev-parse", "HEAD"], cwd=str(self.repo))
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip()), 40)

    def test_deletion_guard_still_blocks_argv_and_shell(self):
        self.assertFalse(git_guard.check_argv(["branch", "-D", "main"]).allowed)
        self.assertFalse(git_guard.check_command(
            "sh -c 'git branch -D main'", cwd=str(self.repo)).allowed)
        self.assertFalse(git_guard.check_command(
            "rm -rf .git", cwd=str(self.repo)).allowed)
        # 新工具没有删除型接口
        self.assertNotIn("git_branch_delete", self.tools)
        self.assertNotIn("git_push_force", self.tools)


if __name__ == "__main__":
    unittest.main(verbosity=2)
