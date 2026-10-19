import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PySide6.QtWidgets import QApplication
from animation import _glyph_layout
from glyph_motion import glyph_states
from kugou_diagnostics import summarize
from kugou_native import NativeLyricsReader, NativeUnavailable
from kugou_reader import sample_channels
from lrc import ActiveLine
from settings import ANIMATION_STYLES, Preferences, SettingsStore, normalize_preferences
from presets import EFFECT_KEYS
from text_effects import TextEffects

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class FriendRegressionTests(unittest.TestCase):
    def test_quiet_default_migrates_old_kugou_settings_but_preserves_explicit_strength(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            store.store.setValue("jump", 17)
            self.assertEqual(store.load().motion_strength, 100)
            self.assertEqual(store.load(kugou=True).motion_strength, 30)
            self.assertEqual(store.load(kugou=True).jump, 17)
            prefs = store.load(kugou=True)
            prefs.motion_strength = 0
            store.save(prefs)
            self.assertEqual(store.load(kugou=True).motion_strength, 0)
        for value, expected in ((-5, 0), (110, 100)):
            self.assertEqual(normalize_preferences(Preferences(motion_strength=value)).motion_strength, expected)
        self.assertNotIn("motion_strength", EFFECT_KEYS)

    def test_all_character_styles_scale_motion_without_changing_fades_or_symbols(self):
        glyphs, _, _ = _glyph_layout("Motion", 32, 500)
        item = ActiveLine(0, "Motion", 1000, 4600, 1, 1700, 4000)
        for style in ANIMATION_STYLES:
            for scramble in (False, True):
                prefs = Preferences(animation_style=style, scramble_exit=scramble)
                for position in (1300, 2300, 4250):
                    full = glyph_states(glyphs, prefs, item, position, .8, 32, 42, 12)
                    low = glyph_states(glyphs, replace(prefs, motion_strength=30), item, position, .8, 32, 42, 12)
                    zero = glyph_states(glyphs, replace(prefs, motion_strength=0), item, position, .8, 32, 42, 12)
                    for glyph, a, b, c in zip(glyphs, full, low, zero):
                        self.assertAlmostEqual(b.x - glyph.x, (a.x - glyph.x) * .3)
                        self.assertAlmostEqual(b.y - glyph.baseline, (a.y - glyph.baseline) * .3)
                        self.assertEqual((c.x, c.y, c.rotation, c.scale), (glyph.x, glyph.baseline, 0, 1))
                        self.assertEqual((a.opacity, a.replacement), (b.opacity, b.replacement))

    def test_classic_fast_and_gradient_renderers_are_stationary_at_zero(self):
        glyphs, _, _ = _glyph_layout("Motion", 32, 500)
        for style in ("solid", "glow", "gradient_fill"):
            prefs = Preferences(text_style=style, motion_strength=0)
            effects = TextEffects()
            surface = effects.prepare(glyphs, 32, prefs, 1, prefs.jump)
            first = effects.render(surface, prefs, seconds=1, energy=.8, moving=True).copy()
            second = effects.render(surface, prefs, seconds=1.2, energy=.1, moving=True).copy()
            self.assertEqual(first, second)

    def test_frozen_main_view_rediscovers_new_desktop_view_after_minimize(self):
        tick = [10.]
        reader = NativeLyricsReader(lambda: tick[0])
        scan = Mock(return_value=[(100, None), (200, None)])
        reader.process = NS(pid=42, objects=scan)
        reader.layouts = []
        reader.objects = [(100, None)]
        reader.retry_at = 15
        row = dict(title="Song", artist="Singer", album="", duration_ms=60000, document=300)
        media = dict(title="Song", artist="Singer", album="", playing=True)
        def read(_process, obj, _layout):
            return dict(row, position_ms=1000 if obj == 100 else int(tick[0] * 1000))
        with patch("kugou_native.read_object", side_effect=read):
            reader.read(42, media)
            tick[0] = 11
            self.assertFalse(reader.read(42, media)["playing"])
            scan.assert_not_called()
            tick[0] = 16
            reader.read(42, media)
            scan.assert_called_once()
            tick[0] = 16.2
            result = reader.read(42, media)
            self.assertEqual(result["position_ms"], 16200)
            self.assertTrue(result["playing"])

    def test_paused_playback_does_not_trigger_expensive_rescan(self):
        reader = NativeLyricsReader(lambda: 100.)
        scan = Mock()
        reader.process = NS(pid=42, objects=scan)
        reader.objects = [(100, None)]
        row = dict(title="Song", artist="Singer", album="", duration_ms=60000, document=300, position_ms=1000)
        with patch("kugou_native.read_object", return_value=row):
            self.assertFalse(reader.read(42, dict(title="Song", artist="Singer", album="", playing=False))["playing"])
            scan.assert_not_called()

    def test_native_layout_failure_remains_explained_during_retry_backoff(self):
        reader = NativeLyricsReader(lambda: 10.)
        process = NS(pid=42, ui_module=lambda: (1234, Path("20.1.51.27967/ui.dll")), close=Mock())
        with patch("kugou_native.ReadOnlyProcess", return_value=process), patch(
                "kugou_native.discover_layouts", side_effect=NativeUnavailable("client_x86_unsupported")):
            process.read = Mock()
            with self.assertRaises(ValueError):
                reader.read(42)
            self.assertEqual(reader.status, "client_x86_unsupported")
            self.assertIsNone(reader.read(42))
            self.assertEqual(reader.status, "client_x86_unsupported")

    def test_interface_fallback_reports_32_bit_reason_instead_of_repeated_desktop_advice(self):
        native = NS(status="client_x86_unsupported", read=Mock(return_value=None))
        window = NS(read=AsyncMock(return_value=(None, "请还原酷狗主窗口。")))
        with patch("kugou_reader.smtc_snapshot", new=AsyncMock(return_value=None)):
            snapshot, message, _ = asyncio.run(sample_channels(None, native, NS(pid=lambda: 42), window))
        self.assertIsNone(snapshot)
        self.assertIn("32 位", message)

    def test_diagnostic_report_omits_song_and_personal_fields(self):
        snapshot = dict(mode="native", title="Private song", artist="Private artist", playing=True, lyric_hash="secret")
        report = summarize(snapshot, "原生歌词通道", NS(status="ready", module_version="20.1.51.27967"),
                           [(1, 42, False), (2, 42, True)], NS(user=NS(IsWindowVisible=lambda _: False)))
        raw = json.dumps(report)
        for secret in ("Private song", "Private artist", "secret", "lyric_hash"):
            self.assertNotIn(secret, raw)
        self.assertTrue(report["desktop_lyric_window_present"])
        self.assertFalse(report["main_window_visible"])

    def test_release_explains_defaults_diagnosis_and_unverified_friend(self):
        for name in ("KUGOU.md", "release/kugou-guide.txt"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for phrase in ("30%", "0%", "导出兼容诊断.bat", "朋友", "尚未"):
                self.assertIn(phrase, text)
        evidence = json.loads((ROOT / "docs/research/kugou-friend-20261019.json").read_text(encoding="utf-8"))
        self.assertFalse(evidence["friend_minimized_playback_verified"])
        self.assertTrue(evidence["regression"]["synthetic_native_objects"])
        self.assertTrue(evidence["packaged_windows_smoke"]["passed"])
        self.assertTrue(evidence["packaged_windows_smoke"]["actual_bat_entry"])


if __name__ == "__main__":
    unittest.main()
