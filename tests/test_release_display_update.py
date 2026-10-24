"""The Windows downloads must advertise the fixes included in this release."""
import hashlib
import json
from pathlib import Path
import unittest
from app_info import APP_VERSION, KUGOU_VERSION

ROOT = Path(__file__).resolve().parents[1]


class DisplayUpdateReleaseTests(unittest.TestCase):
    def test_all_player_guides_explain_deadline_migration_and_independence(self):
        for name in ("README.md", "NETEASE.md", "QQMUSIC.md", "KUGOU.md", "release/使用说明.txt",
                     "release/netease/使用说明.txt", "release/qqmusic/使用说明.txt", "release/kugou-guide.txt",
                     "release/release-notes.md", "release/kugou-release-notes.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for phrase in ("默认 6 秒", "0–120 秒", "不额外限制", "退场", "长句", "预设"):
                self.assertIn(phrase, text, name)
        self.assertTrue(KUGOU_VERSION.startswith(APP_VERSION))
        self.assertEqual(APP_VERSION, "0.8.0-beta.2")

    def test_shared_engine_dependencies_ship_exact_license_bytes(self):
        records = json.loads((ROOT / "release/licenses/sources.json").read_text(encoding="utf-8"))
        for name in ("winrt-runtime", "winrt-Windows.Media.Control", "winrt-Windows.Media.Ocr", "pefile", "typing_extensions"):
            found = [row for row in records if row.get("distribution") == name]
            self.assertTrue(found, name)
            for row in found:
                self.assertEqual(hashlib.sha256((ROOT / "release/licenses" / row["file"]).read_bytes()).hexdigest(), row["sha256"])
