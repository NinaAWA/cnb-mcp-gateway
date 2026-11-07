"""容器入口脚本回归测试（仅依赖标准库）。

曾因 `string.ascii_letters+digits`（漏写 string.）抛 NameError，
导致镜像启动即退出、restart 策略下无限重启。这里直接执行脚本里
内嵌的 Python 片段，避免同类问题再次进入制品。
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "entrypoint.sh"


def _snippets() -> list[str]:
    return re.findall(r"python -c '([^']*)'", SCRIPT.read_text(encoding="utf-8"))


class TestEntrypoint(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_python_snippets_execute(self):
        snippets = _snippets()
        self.assertEqual(len(snippets), 1, "入口脚本的 Python 片段数量变化，请同步更新本测试")
        for code in snippets:
            proc = subprocess.run([sys.executable, "-c", code],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{code!r} -> {proc.stderr}")
            self.assertTrue(proc.stdout.strip())

    def test_admin_password_defaults_and_ignores_stale_file(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('ADMIN_PASSWORD="admin"', text,
                      "未配置 ADMIN_PASSWORD 时必须回落到固定默认口令")
        self.assertNotIn('cat "$PASS_FILE"', text,
                         "升级后不得读取旧卷里残留的随机口令")
        self.assertIn('> "$PASS_FILE"', text,
                      "仍须把生效口令写入受保护文件，兼容既有取口令命令")

    def test_generated_token_is_48_alnum(self):
        token_code = [s for s in _snippets() if "ascii_letters" in s]
        self.assertEqual(len(token_code), 1)
        proc = subprocess.run([sys.executable, "-c", token_code[0]],
                              capture_output=True, text=True, check=True)
        value = proc.stdout.strip()
        self.assertEqual(len(value), 48)
        self.assertTrue(value.isalnum())


if __name__ == "__main__":
    unittest.main(verbosity=2)
