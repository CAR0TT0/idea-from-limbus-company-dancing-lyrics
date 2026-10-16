import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import json
from unittest.mock import patch
import unittest
from PySide6.QtGui import QColor
import test_controls as fixture
from settings import display_mode


class UpgradeControlTests(unittest.TestCase):
    setUp = fixture.ControlPanelTests.setUp
    tearDown = fixture.ControlPanelTests.tearDown

    def test_gradient_color_dialog_and_middle_stop_save_without_transport_changes(self):
        self.player.clock, self.player.playing = 6500, False
        self.panel.text_style.setCurrentIndex(self.panel.text_style.findData("gradient_fill"))
        self.panel.gradient_middle.setChecked(True)
        with patch("controls.QColorDialog.getColor", return_value=QColor("#12ab34")):
            self.panel.choose_gradient_color(2)
        self.assertEqual(json.loads(self.store.load().gradient_colors)[-1], "#12ab34")
        self.assertEqual(len(json.loads(self.store.load().gradient_colors)), 3)
        self.assertEqual((self.player.clock, self.player.playing, self.player.seeks), (6500, False, []))

    def test_bilingual_scramble_and_instrumental_controls_persist_together(self):
        self.panel.language_combo.setCurrentIndex(self.panel.language_combo.findData("bilingual_translation"))
        self.panel.scramble_checkbox.setChecked(True)
        self.panel.instrumental_checkbox.setChecked(True)
        prefs = self.store.load()
        self.assertEqual(display_mode(prefs), "bilingual_translation")
        self.assertTrue(prefs.scramble_exit and prefs.hide_instrumental)
        item = self.panel.font_preview._preview_item()
        self.assertTrue(item.text.startswith("啊，对了！"))
        self.assertGreater(item.secondary_start, 0)

    def test_screen_control_persists_and_missing_screen_is_not_overwritten(self):
        self.panel.screen_combo.setCurrentIndex(1)
        self.assertEqual(self.store.load().display_screen, self.panel.screen_combo.currentData())
        self.prefs.display_screen = "name:disconnected-test-display"
        self.panel._reload_screens()
        self.assertEqual(self.panel.screen_combo.currentData(), self.prefs.display_screen)
