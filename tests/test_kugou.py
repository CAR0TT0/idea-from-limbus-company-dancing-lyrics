import base64
from concurrent.futures import Future
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PySide6.QtWidgets import QApplication
from kugou import KugouPlayer, KugouSnapshot, parse_snapshot
from kugou_lyrics import (KRC_KEY, LyricResult, choose_candidate, decode_krc, load_song_lyrics,
                          local_lyrics, parse_krc)
from kugou_reader import is_kugou, ocr_metadata
from kugou_startup import KugouStartupManager, object_name
from lrc import display_document
from settings import Preferences, SettingsStore, normalize_preferences

ROOT = Path(__file__).resolve().parents[1]
TEXT = "[offset:20]\n[1000,2000]<0,800,0>Hello <800,1000,0>world\n[4000,1000]<0,500,0>Next"


def krc(text=TEXT):
    data = zlib.compress(text.encode("utf-8"))
    return b"krc1" + bytes(v ^ KRC_KEY[i % len(KRC_KEY)] for i, v in enumerate(data))


def song(title="Example", position=1000, playing=True):
    return KugouSnapshot(title, "Singer", "", 6000, position, playing)


class Executor:
    def __init__(self):
        self.futures = []
    def submit(self, *_args, **_kwargs):
        result = Future()
        self.futures.append(result)
        return result


class KugouTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_source_ids_and_reject_other_players(self):
        for source in ("kugou", "KuGou.exe", r"D:\new-version\KuGou.exe"):
            self.assertTrue(is_kugou(source))
        for source in ("QQMusic.exe", "cloudmusic.exe", "fakekugou.exe", "kugou-browser", None):
            self.assertFalse(is_kugou(source))

    def test_snapshot_validation_and_modes(self):
        packet = dict(source="kugou", title="Song", artist="Artist", album="", duration_ms=2000,
                      position_ms=1000, playing=True, mode="hybrid")
        self.assertEqual(parse_snapshot(packet).title, "Song")
        for key, value in (("duration_ms", True), ("position_ms", float("nan")), ("rate", float("inf")),
                           ("playing", 1), ("title", "bad\x00"), ("mode", "unknown")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_snapshot({**packet, key: value})
        self.assertIsNone(parse_snapshot({**packet, "source": "browser.exe"}))

    def test_krc_unicode_word_indices_and_offsets(self):
        doc = parse_krc(decode_krc(krc("[offset:-30]\n[1000,1000]<0,500,0>😀你<500,500,0>好")))
        self.assertEqual(doc.offset_ms, -30)
        self.assertEqual(doc.lines[0].text, "😀你好")
        self.assertEqual((doc.lines[0].words[0].text_start, doc.lines[0].words[0].text_end), (0, 2))
        self.assertEqual(doc.lines[0].words[1].start_ms, 1500)

    def test_krc_real_translation_supports_all_display_modes(self):
        language = base64.b64encode(json.dumps({"content": [{"type": 1, "lyricContent": [["你好"], ["下一句"]]}]}).encode()).decode()
        doc = parse_krc(TEXT + "\n[language:" + language + "]")
        self.assertEqual(doc.lines[0].translation, "你好")
        self.assertEqual(display_document(doc, mode="translation").lines[0].text, "你好")
        self.assertIn("\n", display_document(doc, mode="bilingual_original").lines[0].text)
        self.assertGreater(display_document(doc, mode="bilingual_translation").lines[0].words[0].text_start, 0)

    def test_malformed_optional_translation_does_not_drop_lyrics(self):
        self.assertEqual(len(parse_krc(TEXT + "\n[language:aaaa]").lines), 2)

    def test_decoder_rejects_invalid_truncated_and_zip_bomb(self):
        for raw in (b"bad", krc()[:-2], krc("x" * (2 * 1024 * 1024 + 1))):
            with self.subTest(size=len(raw)), self.assertRaises((ValueError, zlib.error)):
                decode_krc(raw)

    def test_local_kugou_cache_filename_with_hash_id_and_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("Singer - Example-" + "a" * 32 + "-12345678-00000000.krc")
            path.write_bytes(krc())
            result = local_lyrics(song(), [directory])
            self.assertEqual(result.source, "cache")
            self.assertEqual(len(result.document.lines), 2)
            self.assertIsNone(local_lyrics(song("Different"), [directory]))
            self.assertIsNone(local_lyrics(song(), [directory], cancelled=lambda: True))

    def test_candidate_matches_title_artist_duration(self):
        good = dict(id="123", song="Example", singer="Singer", duration=6000, accesskey="a" * 32)
        self.assertEqual(choose_candidate([good], song()), good)
        for changes in (dict(song="Other"), dict(singer="Other"), dict(duration=12000), dict(duration=True), dict(id="../bad")):
            self.assertIsNone(choose_candidate([{**good, **changes}], song()))
        self.assertIsNone(choose_candidate([good, {**good, "id": "456"}], song()))

    def test_network_roundtrip_without_audio_or_third_party_host(self):
        candidate = dict(id="123", song="Example", singer="Singer", duration=6000, accesskey="a" * 32)
        calls = []
        def request(path, params):
            calls.append(path)
            return {"candidates": [candidate]} if path == "/search" else {"content": base64.b64encode(krc()).decode()}
        result = load_song_lyrics(song(), request=request, directories=[])
        self.assertEqual(result.source, "network")
        self.assertEqual(calls, ["/search", "/download"])

    def test_ocr_fullwidth_and_spaced_timestamp(self):
        lines = [dict(text="０１ ： ２３ / ０２ ： ５６", x=100, y=100)]
        result = ocr_metadata(lines, 1000, timing_only=True)
        self.assertEqual((result["position_ms"], result["duration_ms"]), (83000, 176000))
        self.assertIsNone(ocr_metadata([dict(text="99:99/00:00", x=100, y=100)], 1000, True))

    def test_ocr_requires_valid_timeline_not_song_list_duration(self):
        self.assertIsNone(ocr_metadata([dict(text="03:42", x=100, y=100)], 1000))

    def test_song_switch_discards_old_inflight_lyrics(self):
        executor = Executor()
        player = KugouPlayer(executor=executor, autostart=False)
        player._start_helper = lambda: None
        received = []
        player.document_changed.connect(received.append)
        try:
            player.apply_snapshot(song("A"))
            player.apply_snapshot(song("B"))
            executor.futures[0].set_result(LyricResult(parse_krc(TEXT), "stale"))
            player._poll()
            self.assertNotIn("stale", player.lyric_status)
            self.assertFalse(player.lyrics_received)
            self.assertEqual(len(executor.futures), 2)
            executor.futures[1].set_result(LyricResult(parse_krc(TEXT), "current"))
            player._poll()
            self.assertEqual(player.lyric_status, "current")
            self.assertTrue(player.lyrics_received)
        finally:
            player.stop()

    def test_pause_freezes_and_disconnect_clears_lyrics(self):
        tick = [10.0]
        player = KugouPlayer(clock=lambda: tick[0], executor=Executor(), autostart=False)
        try:
            player.apply_snapshot(song(position=2000, playing=False))
            tick[0] += 20
            self.assertEqual(player.position(), 2000)
            player.disconnect("missing")
            self.assertFalse(player.playing)
            self.assertFalse(player.connected)
            self.assertFalse(player.lyrics_received)
        finally:
            player.stop()

    def test_kugou_state_path_independent_of_package_and_client_versions(self):
        from kugou_startup import state_directory
        self.assertEqual(state_directory().parts[-2:], ("DancingLyrics-Kugou", ".state"))
        self.assertNotIn("QQ", object_name("app"))

    def test_cache_settings_roundtrip_and_invalid_path_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs = Preferences(kugou_cache_directory=directory)
            store = SettingsStore(Path(directory) / "settings.ini")
            store.save(prefs)
            self.assertEqual(store.load().kugou_cache_directory, directory)
        self.assertEqual(normalize_preferences(Preferences(kugou_cache_directory="bad\x00")).kugou_cache_directory, "")

    def test_startup_opt_in_rollback_and_independent_registration(self):
        class Registry:
            value = None
            def read(self): return self.value
            def write(self, value): self.value = value
            def remove(self): self.value = None
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "new-version.exe"; exe.touch()
            registry = Registry()
            launches = []
            manager = KugouStartupManager(command=[str(exe)], registry=registry, launcher=lambda *args: launches.append(args))
            self.assertFalse(manager.enabled())
            manager.set_enabled(True)
            self.assertTrue(manager.enabled())
            self.assertEqual(launches[0][0][-1], "--watch-kugou")
            manager.set_enabled(False)
            self.assertIsNone(registry.value)
            def failed(*_): raise OSError("failed")
            manager.launcher = failed
            with self.assertRaises(OSError): manager.set_enabled(True)
            self.assertIsNone(registry.value)

    def test_installer_does_not_inspect_or_write_music_client(self):
        from kugou_install import shortcut_script
        script = shortcut_script()
        self.assertIn("SpecialFolders", script)
        self.assertIn("--kugou", script)
        self.assertNotIn("KuGou.exe", script)
        self.assertNotIn("Version", script)
        self.assertNotIn("HKLM", script)

    def test_documented_features_and_limitations(self):
        guide = (ROOT / "release/kugou-guide.txt").read_text(encoding="utf-8")
        for phrase in ("0.8.0-beta.1", "渐变", "双语", "乱码退场", "纯音乐隐藏", "屏幕", "最小化", "约 1 秒", "不承诺", "无需", "LOCALAPPDATA"):
            self.assertIn(phrase, guide)
        self.assertTrue((ROOT / "KUGOU.md").is_file())

    @unittest.skipUnless(sys.platform == "win32", "Windows native reader")
    def test_reader_pipe_uses_utf8_even_when_windows_locale_differs(self):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "0"
        result = subprocess.run([sys.executable, "main.py", "--kugou-reader", "--once"], cwd=ROOT,
                                env=env, capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        packet = json.loads(result.stdout.decode("utf-8"))
        self.assertIn("snapshot", packet)

    def test_packager_keeps_original_licenses_and_excludes_private_state(self):
        text = (ROOT / "tools/package_kugou.py").read_text(encoding="utf-8")
        self.assertIn('parts[1] == "licenses"', text)
        self.assertIn('(".state", "artifacts", "__pycache__")', text)
        self.assertIn('"--kugou"', text)
        self.assertIn("zip.sha256", text)


if __name__ == "__main__":
    unittest.main()
