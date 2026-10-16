import unittest
import tempfile
from pathlib import Path
from screens import screen_key, select_screen
from settings import Preferences, SettingsStore
from presets import EFFECT_KEYS


class Screen:
    def __init__(self, name, serial=""):
        self.label, self.serial = name, serial
    def name(self): return self.label
    def serialNumber(self): return self.serial
    def manufacturer(self): return "Test"
    def model(self): return "Display"


class ScreenTests(unittest.TestCase):
    def test_selection_unplug_reconnect_primary_and_no_screens(self):
        primary, other = Screen("DISPLAY1"), Screen("DISPLAY2", "second")
        preferred = screen_key(other)
        self.assertIs(select_screen([primary, other], primary, preferred), other)
        self.assertIs(select_screen([primary], primary, preferred), primary)
        renamed = Screen("DISPLAY3", "second")
        self.assertIs(select_screen([primary, renamed], primary, preferred), renamed)
        self.assertIs(select_screen([primary, other], other, "primary"), other)
        self.assertIsNone(select_screen([], None, preferred))

    def test_selection_persists_and_presets_do_not_change_screen(self):
        self.assertNotIn("display_screen", EFFECT_KEYS)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.ini"
            key = screen_key(Screen("DISPLAY2"))
            SettingsStore(path).save(Preferences(display_screen=key))
            self.assertEqual(SettingsStore(path).load().display_screen, key)
