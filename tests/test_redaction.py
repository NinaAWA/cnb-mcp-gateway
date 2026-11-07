"""脱敏与大输出引用完整性回归测试（仅依赖标准库）。"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.audit.redaction import (  # noqa: E402
    REDACTED, ArtifactStore, redact_json, redact_text, sha256_bytes,
)


class TestRedaction(unittest.TestCase):
    def test_connection_string_credentials(self):
        samples = [
            "AUDIT_DB_DSN=postgresql://mcp_audit:s3cr3t@postgres:5432/mcp_audit",
            "database_url=mysql://root:hunter2@db.internal/app",
            "export SECRET_TOKEN=abcdefghijklmnopqrstuvwxyz",
            "redis://default:password123@cache:6379/0",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                masked = redact_text(sample)
                self.assertNotIn("s3cr3t", masked)
                self.assertNotIn("hunter2", masked)
                self.assertNotIn("password123", masked)
                self.assertNotIn("abcdefghijklmnopqrstuvwxyz", masked)
                self.assertIn(REDACTED, masked)

    def test_sensitive_keys(self):
        masked = redact_json({"dsn": "postgresql://u:p@h/db", "command": "git push origin main"})
        self.assertIn(REDACTED, masked)
        self.assertIn("git push origin main", masked)
        self.assertNotIn("postgresql://u:p@h/db", masked)

    def test_artifact_checksum_matches_stored_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp), threshold=1024, max_bytes=4096)
            stored = store.store("x" * 20000)
            self.assertTrue(stored["ref"])
            path = Path(tmp) / stored["ref"].removeprefix("artifact://")
            on_disk = path.read_bytes()
            self.assertEqual(len(on_disk), stored["stored_size"])
            self.assertEqual(hashlib.sha256(on_disk).hexdigest(), stored["sha256"])
            self.assertEqual(store.read(stored["ref"], stored["sha256"]), on_disk.decode())
            path.write_bytes(b"tampered")
            self.assertIsNone(store.read(stored["ref"], stored["sha256"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
