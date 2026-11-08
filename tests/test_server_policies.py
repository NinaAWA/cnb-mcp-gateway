"""网关策略回归测试：在不导入 fastmcp 等运行依赖的前提下提取纯策略函数。

用 AST 提取 docker/server.py 中的分类、白名单、OAuth 与令牌函数，避免为了
跑单元测试而启动整套服务；CI 与本地都可独立执行。
"""
from __future__ import annotations

import ast
import hmac
import re
import sys
import time
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = (ROOT / "docker" / "server.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
WANTED = {
    "_normalize", "_classify", "CATASTROPHIC_PATTERNS", "DESTRUCTIVE_PATTERNS",
    "_prefix_allowed", "_redirect_allowed", "_token_valid", "_prune_oauth_state",
}


def _load_policies():
    chunks = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        else:
            continue
        if name in WANTED:
            chunks.append(ast.get_source_segment(SOURCE, node))
    namespace = {
        "re": re, "time": time, "hmac": hmac, "urllib": urllib,
        "TOKEN": "unit-test-token", "DENY_PREFIXES": [], "REQUIRE_CONFIRM": [],
        "ALLOWED_PREFIXES": [], "ISSUED_TOKENS": {}, "AUTH_CODES": {},
    }
    exec(compile("\n".join(chunks), "server-policies", "exec"), namespace)  # noqa: S102
    return namespace


NS = _load_policies()


class TestCommandClassification(unittest.TestCase):
    def test_git_deletion_is_hard_denied(self):
        commands = [
            "git branch -D main", "git branch -d feature/x", "git branch -m main old",
            "git push origin --delete main", "git push origin :main",
            "git push origin main:", "git update-ref -d refs/heads/main",
            "git worktree remove --force /workspace/wt", "git remote prune origin",
            "git fetch --prune", "git symbolic-ref --delete HEAD", "rm -rf .git",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(NS["_classify"](command), "catastrophic")

    def test_normal_work_is_not_blocked(self):
        for command in ["git status", "git commit -am 'exp'", "git switch main",
                        "git push origin main", "git branch -a", "python train.py"]:
            with self.subTest(command=command):
                self.assertEqual(NS["_classify"](command), "normal")
        self.assertEqual(NS["_classify"]("git push --force origin main"), "destructive")
        self.assertEqual(NS["_classify"]("git reset --hard"), "destructive")

    def test_allowlist(self):
        NS["ALLOWED_PREFIXES"] = ["git", "python"]
        try:
            self.assertTrue(NS["_prefix_allowed"]("git status"))
            self.assertFalse(NS["_prefix_allowed"]("curl https://example.com"))
        finally:
            NS["ALLOWED_PREFIXES"] = []

    def test_empty_allowlist_is_default_admin_mode(self):
        """v1.1：MCP_ALLOWED_PREFIXES 降级为可选的收紧开关，留空即放行全部命令。"""
        NS["ALLOWED_PREFIXES"] = []
        for command in ["apt-get update", "docker ps", "bash -c 'echo ok'",
                        "python3 train.py", "git status"]:
            with self.subTest(command=command):
                self.assertTrue(NS["_prefix_allowed"](command))


class TestOAuthPolicies(unittest.TestCase):
    def test_redirect_uri(self):
        allowed = NS["_redirect_allowed"]
        self.assertTrue(allowed("https://client.example/cb", []))
        self.assertTrue(allowed("http://127.0.0.1:8080/cb", []))
        self.assertFalse(allowed("http://evil.example/cb", []))
        self.assertFalse(allowed("myapp://callback", []))
        self.assertTrue(allowed("myapp://callback", ["myapp://callback"]))
        self.assertFalse(allowed("https://client.example/cb", ["https://other.example/cb"]))
        self.assertFalse(allowed("https://user:pass@client.example/cb", []))
        self.assertFalse(allowed("", ["https://client.example/cb"]))

    def test_token_validation_and_expiry(self):
        valid = NS["_token_valid"]
        NS["ISSUED_TOKENS"].clear()
        self.assertTrue(valid("unit-test-token"))
        self.assertFalse(valid("wrong-token"))
        self.assertFalse(valid(""))
        NS["ISSUED_TOKENS"]["expired"] = time.time() - 1
        self.assertFalse(valid("expired"))
        self.assertNotIn("expired", NS["ISSUED_TOKENS"])
        NS["ISSUED_TOKENS"]["fresh"] = time.time() + 60
        self.assertTrue(valid("fresh"))
        NS["ISSUED_TOKENS"].clear()

    def test_oauth_state_pruning(self):
        NS["AUTH_CODES"]["expired"] = {"expires": time.time() - 1}
        NS["AUTH_CODES"]["fresh"] = {"expires": time.time() + 60}
        NS["ISSUED_TOKENS"]["old"] = time.time() - 1
        NS["_prune_oauth_state"]()
        self.assertNotIn("expired", NS["AUTH_CODES"])
        self.assertIn("fresh", NS["AUTH_CODES"])
        self.assertNotIn("old", NS["ISSUED_TOKENS"])
        NS["AUTH_CODES"].clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
