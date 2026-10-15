import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath, QColor
from PySide6.QtWidgets import QApplication
from animation import Glyph
from glyph_motion import GlyphState
from settings import Preferences, SettingsStore
from presets import PresetStore
from text_effects import TextEffects, pixels_view

APP = QApplication.instance() or QApplication([])


class GradientTests(unittest.TestCase):
    def render(self, style="glow", direction="horizontal", dpr=1):
        self.prefs = Preferences(text_style=style, glow_variant="gradient", gradient_colors='["#ff0000","#0000ff"]',
                                 gradient_direction=direction, glow_strength=0, jump=0)
        path = QPainterPath()
        path.addRect(QRectF(0, 0, 20, 20))
        self.glyphs = [Glyph(path, 0, 0, 0), Glyph(path, 40, 0, 1)]
        self.effects = TextEffects()
        self.surface = self.effects.prepare(self.glyphs, 32, self.prefs, dpr)
        self.effects.render(self.surface, self.prefs)

    def sample(self, x, y):
        s = self.surface
        dpr = s.image.devicePixelRatio()
        return s.image.pixelColor(round((x-s.origin.x())*dpr), round((y-s.origin.y())*dpr))

    def test_outline_keeps_white_core_and_changes_color_across_original_layout(self):
        self.render()
        self.assertEqual(self.sample(10, 10), QColor("white"))
        self.assertEqual(self.sample(50, 10), QColor("white"))
        self.assertGreater(self.sample(-1, 10).red(), self.sample(-1, 10).blue())
        self.assertGreater(self.sample(61, 10).blue(), self.sample(61, 10).red())

    def test_fill_gradient_directions_dpi_and_no_clipped_alpha(self):
        for direction in ("horizontal", "vertical", "diagonal"):
            for dpr in (1, 1.25, 1.5):
                self.render("gradient_fill", direction, dpr)
                first = self.sample(2, 2)
                last = self.sample(58, 18)
                self.assertGreater(first.red(), last.red())
                self.assertGreater(last.blue(), first.blue())
                alpha = pixels_view(self.surface.image)[:, :, 3]
                self.assertFalse(np.any(alpha[0]) or np.any(alpha[-1]) or np.any(alpha[:, 0]) or np.any(alpha[:, -1]))

    def test_fading_neighbour_does_not_reassign_gradient(self):
        self.render("gradient_fill")
        expected = self.sample(50, 10)
        states = [GlyphState(0, 0, 0), GlyphState(40, 0)]
        self.effects.render(self.surface, self.prefs, states=states)
        self.assertEqual(self.sample(50, 10), expected)
        self.assertEqual(self.sample(10, 10).alpha(), 0)

    def test_restart_presets_and_bad_colors_fall_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SettingsStore(root / "settings.ini")
            prefs = Preferences(text_style="gradient_fill", gradient_colors='["#ff0000","#00ff00","#0000ff"]', gradient_direction="diagonal")
            store.save(prefs)
            self.assertEqual(json.loads(SettingsStore(root / "settings.ini").load().gradient_colors), json.loads(prefs.gradient_colors))
            presets = PresetStore(root / "presets.json")
            saved = presets.save("渐变", prefs)
            self.assertEqual(PresetStore(root / "presets.json").get(saved.id).values["gradient_direction"], "diagonal")
            store.store.setValue("gradient_colors", '["bad","#00ff00"]')
            self.assertEqual(json.loads(store.load().gradient_colors), ["#ff78bb", "#68b6f5"])
