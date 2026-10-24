"""Sentence deadlines must include the exit and remain seek/pause deterministic."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import tempfile
import unittest
from pathlib import Path
from PySide6.QtWidgets import QApplication
from lrc import LyricDocument, LyricLine, LyricTimeline, parse_lrc, display_document
from settings import ANIMATION_STYLES, Preferences, SettingsStore
from presets import EFFECT_KEYS
from animation import _glyph_layout
from glyph_motion import glyph_states
from controls import FontPreview

APP = QApplication.instance() or QApplication([])


class DisplayLifetimeTests(unittest.TestCase):
    def test_default_round_trip_clamp_and_preset_independence(self):
        self.assertNotIn("max_display_seconds", EFFECT_KEYS)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.ini"
            self.assertEqual(SettingsStore(path).load().max_display_seconds, 6)
            for seconds in (0, 1, 6, 120):
                store = SettingsStore(path)
                store.save(Preferences(max_display_seconds=seconds))
                self.assertEqual(SettingsStore(path).load().max_display_seconds, seconds)
            for value, expected in ((-3, 0), (9999, 120), ("invalid", 6)):
                store.store.setValue("max_display_seconds", value)
                store.store.sync()
                self.assertEqual(SettingsStore(path).load().max_display_seconds, expected)

    def test_all_styles_finish_before_deadline_and_do_not_reappear(self):
        doc = parse_lrc("[00:01]甲乙丙丁戊\n[00:30]下一句\n[00:40]")
        for style in ANIMATION_STYLES:
            for scramble in (False, True):
                timeline = LyricTimeline(doc, animation_style=style, scramble_exit=scramble, max_display_ms=6000)
                item = timeline.visible(6800, 45000)[0]
                self.assertEqual(item.end_ms, 7000)
                if style == "classic" and not scramble:
                    self.assertGreater(item.opacity, 0)
                    self.assertLess(item.opacity, 1)
                else:
                    self.assertLess(item.exit_start_ms, 6800)
                    glyphs, _, _ = _glyph_layout(item.text, 32, 600)
                    prefs = Preferences(animation_style=style, scramble_exit=scramble)
                    early = glyph_states(glyphs, prefs, item, 6550, 0, 32)
                    late = glyph_states(glyphs, prefs, item, 6999.9, 0, 32)
                    self.assertLess(sum(s.opacity for s in late), sum(s.opacity for s in early))
                    self.assertLess(max(s.opacity for s in late), .01)
                for position in (7000, 10000, 29999):
                    self.assertFalse(timeline.visible(position, 45000))
                self.assertEqual(timeline.visible(6800, 45000)[0], item)

    def test_cap_does_not_extend_a_phrase_blank_or_song_end(self):
        docs = (parse_lrc("[00:01]甲乙\n[00:03]\n[00:30]下句"),
                LyricDocument([LyricLine(1000, "甲乙", end_ms=2000), LyricLine(30000, "下句")], []))
        for doc in docs:
            for style in ANIMATION_STYLES:
                timeline = LyricTimeline(doc, animation_style=style, max_display_ms=6000)
                self.assertFalse(timeline.visible(3000, 40000))
                self.assertFalse(timeline.visible(2500, 2500))

    def test_explicit_end_offset_bilingual_and_speed_compression(self):
        raw = LyricDocument([LyricLine(1000, "Long original", translation="长句译文", end_ms=26000),
                             LyricLine(30000, "Next")], [], offset_ms=300)
        doc = display_document(raw, mode="bilingual_translation")
        for style in ANIMATION_STYLES:
            timeline = LyricTimeline(doc, delay_ms=500, animation_style=style,
                                     entry_speed=25, exit_speed=25, max_display_ms=1000)
            item = timeline.visible(1300, 40000)[0]
            self.assertEqual((item.start_ms, item.end_ms), (1200, 2200))
            self.assertGreater(item.secondary_start, 0)
            if style != "classic":
                self.assertLessEqual(item.entry_end_ms, item.exit_start_ms)
            self.assertFalse(timeline.visible(2200, 40000))

    def test_zero_disables_cap_but_unknown_duration_still_has_end_fallback(self):
        for style in ANIMATION_STYLES:
            timeline = LyricTimeline(parse_lrc("[00:01]甲乙\n[00:30]下句"), animation_style=style, max_display_ms=0)
            self.assertTrue(timeline.visible(29000, 40000))
            last = LyricTimeline(parse_lrc("[00:01]最后一句"), animation_style=style, max_display_ms=0)
            self.assertTrue(last.visible(10000, 40000))
            self.assertFalse(last.visible(7000))

    def test_dense_cues_never_show_more_than_two_and_seek_is_repeatable(self):
        doc = parse_lrc("[00:01]甲\n[00:01.1]乙\n[00:01.2]丙\n[00:01.3]")
        for style in ANIMATION_STYLES:
            timeline = LyricTimeline(doc, animation_style=style, entry_speed=25, exit_speed=25, max_display_ms=1000)
            baseline = timeline.visible(1150)
            for position in range(1000, 1400):
                self.assertLessEqual(len(timeline.visible(position)), 2)
            self.assertEqual(timeline.visible(1150), baseline)
            self.assertFalse(timeline.visible(1300))

    def test_preview_uses_same_deadline_and_can_replay_short_caps(self):
        for style in ANIMATION_STYLES:
            preview = FontPreview(Preferences(animation_style=style, max_display_seconds=1))
            item = preview._preview_item()
            self.assertEqual(item.end_ms, 1000)
            self.assertLessEqual(item.entry_end_ms, item.exit_start_ms)
            preview._position = 900
            preview.replay()
            self.assertEqual(preview._position, 0)
            preview.close()

    def test_classic_preview_keeps_speed_scaled_fades_with_the_deadline(self):
        for speed in (25, 100, 300):
            preview = FontPreview(Preferences(entry_speed=speed, exit_speed=speed, max_display_seconds=0))
            item = preview._preview_item()
            self.assertEqual(item.entry_end_ms, min(300 * 100 / speed, 1900))
            self.assertEqual(item.end_ms - item.exit_start_ms, min(600 * 100 / speed, 1900))
            preview.close()
