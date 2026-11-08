"""Capability-report regression tests: truthful privilege scope, no secret leakage."""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "docker/server.py").read_text("utf-8")
MODULE = ast.parse(SOURCE)
FUNCTION = next(
    node for node in MODULE.body
    if isinstance(node, ast.FunctionDef) and node.name == "get_capabilities"
)


class TestCapabilities(unittest.TestCase):
    def test_report_is_json_and_does_not_expose_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            class FileService:
                ROOT = Path(tmp)

            seen = []
            namespace = {
                "os": os,
                "sys": sys,
                "json": json,
                "Path": Path,
                "file_service": FileService,
                "WORKDIR": tmp,
                "_TASK_DIR": Path(tmp) / "tasks",
                "ALLOWED_PREFIXES": [],
                "audit": seen.append,
            }
            module = ast.Module(body=[FUNCTION], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), "capabilities", "exec"), namespace)
            old = os.environ.get("MCP_TEST_SECRET")
            os.environ["MCP_TEST_SECRET"] = "never-report-this-secret"
            try:
                report_text = namespace["get_capabilities"]()
            finally:
                if old is None:
                    os.environ.pop("MCP_TEST_SECRET", None)
                else:
                    os.environ["MCP_TEST_SECRET"] = old

            report = json.loads(report_text)
            self.assertNotIn("never-report-this-secret", report_text)
            self.assertFalse(report["host_root_claimed"])
            self.assertTrue(report["commands_unrestricted_by_prefix"])
            self.assertEqual(report["workdir"], tmp)
            self.assertEqual(report["file_root"], tmp)
            self.assertEqual(report["task_logs"], str(Path(tmp) / "tasks"))
            self.assertIn("git-lfs", report["binaries"])
            self.assertIn("mujoco", report["scientific_imports_in_gateway_python"])
            self.assertTrue(seen)

    def test_allowlist_is_reported_without_claiming_command_execution(self):
        namespace = {
            "os": os, "sys": sys, "json": json, "Path": Path,
            "WORKDIR": "/workspace", "_TASK_DIR": Path("/tmp/tasks"),
            "ALLOWED_PREFIXES": ["git status"], "audit": lambda _: None,
            "file_service": type("FS", (), {"ROOT": Path("/tmp/files")})(),
        }
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[FUNCTION], type_ignores=[])), "capabilities", "exec"), namespace)
        result = json.loads(namespace["get_capabilities"]())
        self.assertFalse(result["commands_unrestricted_by_prefix"])
        self.assertEqual(result["commands_allowlist"], ["git status"])
        self.assertFalse(result["host_root_claimed"])


if __name__ == "__main__":
    unittest.main()
