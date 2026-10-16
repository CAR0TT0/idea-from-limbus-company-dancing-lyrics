import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import unittest
from dataclasses import replace
from PySide6.QtWidgets import QApplication
from animation import _glyph_layout
from lrc import parse_lrc, LyricTimeline
from glyph_motion import glyph_states
from settings import Preferences, ANIMATION_STYLES
from text_effects import TextEffects, pixels_view
import numpy as np

APP = QApplication.instance() or QApplication([])


class ScrambleTests(unittest.TestCase):
    def test_left_to_right_and_same_time_repeats_for_every_animation(self):
        doc = parse_lrc("[00:01]去一趟图书馆吧。[00:04]Next\n[00:07]")
        glyphs, _, _ = _glyph_layout(doc.lines[0].text, 32, 700)
        for style in ANIMATION_STYLES:
            prefs = Preferences(animation_style=style, scramble_exit=True, jump=0)
            timeline = LyricTimeline(doc, animation_style=style, scramble_exit=True)
            item = timeline.visible(4250)[0]
            states = glyph_states(glyphs, prefs, item, 4250, 0, 32, 730)
            self.assertEqual(states, glyph_states(glyphs, prefs, item, 4250, 0, 32, 730))
            self.assertEqual(states[0].opacity, 0)
            self.assertEqual(states[-1].opacity, 1)
            self.assertTrue(any(state.replacement >= 0 for state in states))
            self.assertTrue(all(state.rotation == 0 for state in states))

    def test_blank_end_single_character_and_dense_intervals_finish(self):
        for text, end in (("[00:00]字\n[00:01]", 1000), ("[00:00]长句有很多字\n[00:00.1]下一句\n[00:00.2]", 200)):
            timeline = LyricTimeline(parse_lrc(text), scramble_exit=True)
            self.assertFalse(timeline.visible(end, end))
            for time in (0, end / 2, end-1):
                self.assertLessEqual(len(timeline.visible(time, end)), 2)

    def test_replacements_reuse_cache_and_have_clear_edges_for_gradient_and_bilingual(self):
        glyphs, _, _ = _glyph_layout("Hello\n你好", 32, 500, secondary_start=6)
        for style in ("glow", "gradient_fill", "solid"):
            prefs = Preferences(text_style=style, glow_variant="gradient", scramble_exit=True, jump=0)
            renderer = TextEffects()
            surface = renderer.prepare(glyphs, 32, prefs, 1.5)
            from lrc import ActiveLine
            item = ActiveLine(0, "Hello\n你好", 0, 1600, 1, 500, 1000, secondary_start=6)
            states = glyph_states(glyphs, prefs, item, 1250, 0, 32, 42)
            before = renderer.misses
            image = renderer.render(surface, prefs, states=states)
            expected = bytes(image.constBits())
            renderer.render(surface, prefs, states=states)
            self.assertEqual(bytes(image.constBits()), expected)
            self.assertEqual(renderer.misses, before)
            self.assertLessEqual(renderer.cache_bytes, renderer.limit_bytes)
            alpha = pixels_view(image)[:, :, 3]
            self.assertFalse(np.any(alpha[0]) or np.any(alpha[-1]) or np.any(alpha[:, 0]) or np.any(alpha[:, -1]))
