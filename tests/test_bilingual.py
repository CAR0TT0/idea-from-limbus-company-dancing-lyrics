import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import tempfile
import unittest
from pathlib import Path
from PySide6.QtWidgets import QApplication
from animation import _glyph_layout
from lrc import LyricLine, LyricDocument, TimedWord, display_document, LyricTimeline
from glyph_motion import glyph_states
from settings import Preferences, SettingsStore, display_mode
from presets import EFFECT_KEYS

APP = QApplication.instance() or QApplication([])


class BilingualTests(unittest.TestCase):
    def document(self):
        return LyricDocument([LyricLine(1000, "Hello", (TimedWord(1000, 2000, 0, 5),), "你好"), LyricLine(4000, "Next")], [])

    def test_both_orders_keep_only_original_word_mapping_and_fallback(self):
        for mode, expected in (("bilingual_original", "Hello\n你好"), ("bilingual_translation", "你好\nHello")):
            document = display_document(self.document(), mode=mode)
            line = document.lines[0]
            self.assertEqual(line.text, expected)
            word = line.words[0]
            self.assertEqual(line.text[word.text_start:word.text_end], "Hello")
            self.assertEqual(document.lines[1].text, "Next")
            self.assertEqual(document.lines[1].secondary_start, -1)

    def test_secondary_geometry_and_animation_share_phase(self):
        document = display_document(self.document(), mode="bilingual_original")
        line = document.lines[0]
        glyphs, _, height = _glyph_layout(line.text, 40, 500, secondary_start=line.secondary_start)
        self.assertEqual({g.pixels for g in glyphs if g.language == 0}, {40})
        self.assertEqual({g.pixels for g in glyphs if g.language == 1}, {32})
        self.assertGreater(min(g.baseline for g in glyphs if g.language == 1), max(g.baseline for g in glyphs if g.language == 0))
        item = LyricTimeline(document, animation_style="ripple_wave").visible(1100)[0]
        states = glyph_states(glyphs, Preferences(animation_style="ripple_wave", jump=0), item, 1100, 0, 40)
        self.assertEqual(states[0].opacity, states[next(i for i,g in enumerate(glyphs) if g.language == 1)].opacity)

    def test_legacy_and_restart_language_is_independent_of_presets(self):
        self.assertNotIn("lyric_display_mode", EFFECT_KEYS)
        self.assertEqual(display_mode(Preferences(prefer_translation=True)), "translation")
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "prefs.ini")
            store.store.setValue("prefer_translation", True)
            self.assertEqual(display_mode(store.load()), "translation")
            store.save(Preferences(lyric_display_mode="bilingual_translation"))
            self.assertEqual(display_mode(SettingsStore(Path(directory) / "prefs.ini").load()), "bilingual_translation")

    def test_equal_translations_do_not_duplicate(self):
        document = LyricDocument([LyricLine(0, "你好", translation="你好")], [])
        self.assertEqual(display_document(document, mode="bilingual_original").lines[0].secondary_start, -1)
