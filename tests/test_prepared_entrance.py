"""Cold exit materials must not block a long sentence's entrance frames."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import unittest
from unittest.mock import patch
import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from animation import _glyph_layout
from lrc import LyricTimeline, parse_lrc
from settings import Preferences
from glyph_motion import glyph_states
from overlay import LyricsOverlay
from text_effects import TextEffects, pixels_view

APP = QApplication.instance() or QApplication([])
LONG_TEXT = "In the quiet city we follow every echo through the night and find another morning."


class Player(QObject):
    changed, discontinuity = Signal(), Signal()
    playing, ended, duration, clock = False, False, 50000, 0
    def position(self):
        return self.clock
    def energy_at(self, _):
        return .5


class PreparedEntranceTests(unittest.TestCase):
    def test_incremental_base_can_enter_before_scramble_symbols_finish(self):
        glyphs, _, _ = _glyph_layout(LONG_TEXT, 32, 700)
        prefs = Preferences(animation_style="ripple_wave", scramble_exit=True, glow_variant="gradient", jump=0)
        renderer = TextEffects()
        steps = renderer.prepare_steps(glyphs, 32, prefs, 1.5)
        surface = next(ready for ready in steps if ready is not None)
        self.assertIsNone(surface.replacements)
        timeline = LyricTimeline(parse_lrc("[00:01]" + LONG_TEXT + "\n[00:08]Next"),
                                 animation_style=prefs.animation_style, scramble_exit=True, max_display_ms=6000)
        item = timeline.visible(1100)[0]
        sums = []
        for position in (1050, 1200, 1400, 1700):
            states = glyph_states(glyphs, prefs, item, position, 0, 32)
            renderer.render(surface, prefs, states=states)
            sums.append(int(pixels_view(surface.image)[:, :, 3].sum()))
        self.assertTrue(all(left < right for left, right in zip(sums, sums[1:])))
        for _ in steps:
            pass
        self.assertIsNotNone(surface.replacements)
        states = glyph_states(glyphs, prefs, item, 6800, 0, 32)
        renderer.render(surface, prefs, states=states)
        alpha = pixels_view(surface.image)[:, :, 3]
        self.assertFalse(np.any(alpha[0]) or np.any(alpha[-1]) or np.any(alpha[:, 0]) or np.any(alpha[:, -1]))

    def test_plain_symbols_share_materials_but_gradient_positions_do_not(self):
        glyphs, _, _ = _glyph_layout("WWii", 32, 500)
        prefs = Preferences(scramble_exit=True)
        surface = TextEffects().prepare(glyphs, 32, prefs, 1.25)
        self.assertIs(surface.replacements[0][0], surface.replacements[1][0])
        self.assertIs(surface.replacements[0][0], surface.replacements[2][0])
        prefs.glow_variant = "gradient"
        surface = TextEffects().prepare(glyphs, 32, prefs, 1.25)
        self.assertIsNot(surface.replacements[0][0], surface.replacements[1][0])

    def test_overlay_preloads_next_cue_without_blocking_paint_or_touching_clock(self):
        player = Player()
        prefs = Preferences(animation_style="ripple_wave", scramble_exit=True, glow_variant="gradient", angle=0)
        overlay = LyricsOverlay(player, prefs)
        overlay.set_document(parse_lrc("[00:01]Short\n[00:05]" + LONG_TEXT + "\n[00:30]Next"))
        overlay.show()
        try:
            # The paint event schedules preparation; it must not drain it.
            with patch("text_effects.TEXT_EFFECTS.prepare", side_effect=AssertionError("blocking paint preparation")):
                overlay.grab()
            self.assertIn(0, overlay._warm_jobs)
            for _ in range(200):
                if not overlay._warm_jobs:
                    break
                QTest.qWait(10)
            self.assertIsNotNone(overlay.layouts[0].surface)
            player.clock = 1200
            overlay.grab()
            for _ in range(200):
                if not overlay._warm_jobs:
                    break
                QTest.qWait(10)
            self.assertIsNotNone(overlay.layouts[1].surface)
            self.assertFalse(overlay.timer.isActive())
            player.clock = 5100
            frame = overlay.grab().toImage()
            QTest.qWait(50)
            self.assertEqual(frame, overlay.grab().toImage())
            self.assertLessEqual(len(overlay.layouts), 3)
            self.assertEqual(player.clock, 5100)
            player.discontinuity.emit()
            self.assertFalse(overlay._warm_jobs)
            self.assertFalse(overlay.layouts)
            prefs.hide_instrumental = True
            overlay.set_document(parse_lrc("[00:00]作曲：某某\n[00:01]纯音乐，请欣赏"))
            self.assertIsNone(overlay.timeline)
            self.assertFalse(overlay._warm_timer.isActive())
        finally:
            overlay.hide()
            overlay.deleteLater()
