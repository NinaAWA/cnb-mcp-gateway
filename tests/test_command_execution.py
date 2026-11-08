"""v1.1 命令执行真实回归：默认管理员模式、真实退出码、env/stdin、耗时与三入口一致。

直接导入 docker/server.py（测试环境已安装 docker/requirements.txt），调用真实的
_exec / run_command / run_command_background / run_command_rest，不做 mock。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="mcp-cmd-test-")
os.environ["MCP_TOKEN"] = "unit-test-token"
os.environ["MCP_ALLOWED_PREFIXES"] = ""
os.environ["AUDIT_DB_PATH"] = str(Path(TMP) / "audit.db")
os.environ["MCP_TASK_LOG_DIR"] = str(Path(TMP) / "task-logs")
os.environ["MCP_MAX_OUTPUT_BYTES"] = "200000"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "docker") not in sys.path:
    sys.path.insert(0, str(ROOT / "docker"))

_spec = importlib.util.spec_from_file_location("mcp_gateway_server_under_test", ROOT / "docker" / "server.py")
server = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(server)


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


class TestCommandExecution(unittest.TestCase):
    def setUp(self):
        server.ALLOWED_PREFIXES.clear()

    def test_default_admin_mode_allows_everyday_commands(self):
        self.assertEqual(server.ALLOWED_PREFIXES, [])
        for command in ["apt-get --version", "bash -c 'echo ok'", "python3 -V",
                        "docker ps", "git status", "pip list"]:
            with self.subTest(command=command):
                self.assertEqual(server._authorize(command, "/tmp", False), "")

    def test_real_exit_code_stdout_stderr_and_duration(self):
        out = server.run_command("printf 'hello'; echo boom >&2; exit 7", cwd="/tmp")
        self.assertIn("exit_code=7", out)
        self.assertIn("hello", out)
        self.assertIn("boom", out)
        self.assertIn("timed_out=false", out)
        self.assertRegex(out, r"duration_ms=\d+")

    def test_env_and_stdin(self):
        out = server.run_command("printf '%s' \"$MCP_TEST_VAR\"", cwd="/tmp",
                                 env={"MCP_TEST_VAR": "from-env"})
        self.assertIn("exit_code=0", out)
        self.assertIn("from-env", out)
        out = server.run_command("cat", cwd="/tmp", stdin="from-stdin\n")
        self.assertIn("from-stdin", out)

    def test_timeout_marks_convention_code(self):
        out = server.run_command("sleep 3", cwd="/tmp", timeout=1)
        self.assertIn("timed_out=true", out)
        self.assertIn("exit_code=124", out)
        self.assertIn("并非进程真实退出码", out)

    def test_catastrophic_and_destructive_gates(self):
        denied = server.run_command("git branch -D main", cwd="/tmp", confirm=True)
        self.assertIn("硬禁止", denied)
        need = server.run_command("rm -rf /tmp/mcp-does-not-exist-v11", cwd="/tmp")
        self.assertIn("confirm", need)
        ok = server.run_command("rm -rf /tmp/mcp-does-not-exist-v11", cwd="/tmp", confirm=True)
        self.assertIn("exit_code=0", ok)

    def test_encoded_and_interpreter_bypass_are_denied(self):
        for command in [
            "echo Z2l0IGJyYW5jaCAtRCBtYWlu | base64 -d | sh",
            "python3 -c \"import subprocess;subprocess.run(['git','branch','-D','main'])\"",
        ]:
            with self.subTest(command=command):
                self.assertIn("ERROR:", server._authorize(command, "/tmp", True))

    def test_dot_git_suffix_not_treated_as_git_dir_deletion(self):
        benign = [
            "rm -rf /tmp/mcp-v11/remote.git",
            "rm -rf /tmp/mcp-v11/git && git init -q --bare -b main /tmp/mcp-v11/git/remote.git",
            "git lfs pre-push origin https://example.invalid/team/repo.git",
        ]
        for command in benign:
            with self.subTest(command=command):
                self.assertNotEqual(server._classify(command), "catastrophic")
        self.assertEqual(server._classify("rm -rf /tmp/mcp-v11/.git"), "catastrophic")
        self.assertEqual(server._classify("rm -rf /tmp/mcp-v11/repo/.git/objects"), "catastrophic")

    def test_background_and_rest_share_the_same_gate(self):
        bg = server.run_command_background("git branch -D main", cwd="/tmp", confirm=True)
        self.assertIn("硬禁止", bg)
        out = asyncio.run(server.run_command_rest(
            _FakeRequest({"command": "git branch -D main", "confirm": True})))
        self.assertIn(b"\xe7\xa1\xac\xe7\xa6\x81\xe6\xad\xa2", out.body)  # 硬禁止

    def test_three_entry_points_allow_normal_and_lfs_commands(self):
        """同步 MCP / 后台 MCP / REST 共用同一闸门；正常命令与 LFS 都不被误拦。"""
        self.assertIn("three-entry-ok", server.run_command("echo three-entry-ok", cwd="/tmp"))
        body = asyncio.run(server.run_command_rest(
            _FakeRequest({"command": "echo three-entry-ok"}))).body.decode()
        self.assertIn("three-entry-ok", body)
        self.assertIn("exit_code=0", body)
        for command in ["git lfs install", "git lfs install --local",
                        "git lfs pull", "git lfs checkout", "git lfs version",
                        "apt-get --version", "docker compose version"]:
            with self.subTest(command=command):
                self.assertEqual(server._authorize(command, "/tmp", False), "")

    def test_lfs_destructive_forms_still_denied(self):
        for command in ["git lfs prune", "git lfs uninstall", "git lfs install --force"]:
            with self.subTest(command=command):
                self.assertIn("ERROR:", server._authorize(command, "/tmp", True))

    def test_rest_supports_env_and_stdin(self):
        out = asyncio.run(server.run_command_rest(_FakeRequest({
            "command": "cat; printf '%s' \"$MCP_REST_VAR\"",
            "env": {"MCP_REST_VAR": "rest-env"}, "stdin": "rest-stdin",
        })))
        body = out.body.decode()
        self.assertIn("exit_code=0", body)
        self.assertIn("rest-stdin", body)
        self.assertIn("rest-env", body)

    def test_background_stdin_and_separate_stderr(self):
        text = server.run_command_background(
            "cat; echo bg-stderr-line >&2; exit 6", cwd="/tmp",
            stdin="bg-stdin-line\n")
        self.assertIn("task_id=", text)
        task_id = text.split("task_id=", 1)[1].split("\n", 1)[0].strip()
        import time
        status = ""
        for _ in range(60):
            status = server.get_task_status(task_id)
            if "status=done" in status:
                break
            time.sleep(0.1)
        self.assertIn("exit_code=6", status)
        out = server.get_task_output(task_id, tail=0)
        self.assertIn("bg-stdin-line", out)
        self.assertIn("--- stderr ---", out)
        self.assertIn("bg-stderr-line", out)
        self.assertNotIn("bg-stderr-line",
                         (server._TASK_DIR / f"{task_id}.log").read_text())
        self.assertIn("bg-stderr-line",
                      (server._TASK_DIR / f"{task_id}.err.log").read_text())

    def test_tail_task_incremental_offset(self):
        import re
        text = server.run_command_background(
            "echo first-tick; sleep 0.5; echo second-tick", cwd="/tmp")
        task_id = text.split("task_id=", 1)[1].split("\n", 1)[0].strip()
        first = server.tail_task(task_id, offset_bytes=0, follow_seconds=5)
        self.assertIn("first-tick", first)
        offset = int(re.search(r"next_offset_bytes=(\d+)", first).group(1))
        second = server.tail_task(task_id, offset_bytes=offset, follow_seconds=5)
        self.assertNotIn("first-tick", second)
        self.assertIn("second-tick", second)

    def test_background_task_returns_id_and_real_log(self):
        text = server.run_command_background("echo bg-line; exit 5", cwd="/tmp")
        self.assertIn("task_id=", text)
        task_id = text.split("task_id=", 1)[1].split("\n", 1)[0].strip()
        import time
        for _ in range(40):
            status = server.get_task_status(task_id)
            if "status=done" in status:
                break
            time.sleep(0.1)
        self.assertIn("status=done", status)
        self.assertIn("exit_code=5", status)
        self.assertIn("bg-line", server.get_task_output(task_id))

    def test_optional_allowlist_still_tightens(self):
        server.ALLOWED_PREFIXES.extend(["git "])
        self.assertIn("ERROR", server._authorize("apt-get --version", "/tmp", False))
        self.assertEqual(server._authorize("git status", "/tmp", False), "")

    def test_output_is_capped(self):
        out = server.run_command("python3 -c \"print('x'*500000)\"", cwd="/tmp")
        self.assertIn("输出已截断", out)
        self.assertLess(len(out), 300000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
