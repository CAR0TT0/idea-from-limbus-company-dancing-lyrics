import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import unittest
import tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from lrc import LyricDocument, LyricLine, display_document, instrumental_document
from settings import Preferences, SettingsStore
from presets import EFFECT_KEYS
from netease import NeteasePlayer
from overlay import LyricsOverlay

APP = QApplication.instance() or QApplication([])


class InstrumentalTests(unittest.TestCase):
    def test_marker_with_credits_in_separate_or_merged_lines_is_instrumental(self):
        from lrc import parse_lrc
        for text in ("[00:00]作曲: かねこちはる / t+pazolite\n[00:05]纯音乐，请欣赏",
                     "[00:00]编曲：某某\n[00:00]Composer: Example\n[00:00]纯音乐，请欣赏。",
                     "[00:00]Composed by: Example\n[00:05]纯音乐，请您欣赏\n[00:10]"):
            document = parse_lrc(text)
            self.assertTrue(instrumental_document(document))
            for mode in ("original", "translation", "bilingual_original", "bilingual_translation"):
                self.assertIsNone(display_document(document, mode=mode, hide_instrumental=True))
            self.assertIs(display_document(document), document)

    def test_credits_alone_or_real_lyrics_alongside_marker_are_not_instrumental(self):
        from lrc import parse_lrc
        for text in ("[00:00]作曲: Example", "[00:00]作曲: Example\n[00:05]我想听纯音乐\n[00:10]纯音乐，请欣赏",
                     "[00:00]纯音乐，请欣赏\n[00:05]This is a real lyric",
                     "[00:00]这是纯音乐，请欣赏我的故事"):
            self.assertFalse(instrumental_document(parse_lrc(text)))

    def test_clear_markers_can_be_hidden_but_default_preserves_them(self):
        for text in ("纯音乐，请欣赏", "纯音乐，请您欣赏。", "此歌曲为没有填词的纯音乐"):
            document = LyricDocument([LyricLine(0, text)], [])
            self.assertTrue(instrumental_document(document))
            self.assertIs(display_document(document), document)
            self.assertIsNone(display_document(document, hide_instrumental=True))

    def test_missing_lyrics_and_real_lyrics_are_not_guessed(self):
        self.assertFalse(instrumental_document(None))
        document = LyricDocument([LyricLine(0, "我想听纯音乐"), LyricLine(2000, "纯音乐，请欣赏")], [])
        self.assertIs(display_document(document, hide_instrumental=True), document)
        self.assertIsNone(display_document(LyricDocument([LyricLine(0, "提示")], [], instrumental=True), hide_instrumental=True))

    def test_settings_survive_restart_and_presets_do_not_override(self):
        self.assertNotIn("hide_instrumental", EFFECT_KEYS)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefs.ini"
            SettingsStore(path).save(Preferences(hide_instrumental=True))
            self.assertTrue(SettingsStore(path).load().hide_instrumental)

    def test_switch_rebuilds_paused_overlay_and_keeps_original_document(self):
        player, prefs = NeteasePlayer(), Preferences()
        overlay = LyricsOverlay(player, prefs)
        try:
            document = LyricDocument([LyricLine(0, "纯音乐，请欣赏")], [])
            overlay.set_document(document)
            self.assertIsNotNone(overlay.timeline)
            position = player.position()
            prefs.hide_instrumental = True
            overlay.refresh_preferences()
            self.assertIsNone(overlay.timeline)
            self.assertIs(overlay.document, document)
            self.assertFalse(overlay.timer.isActive())
            self.assertEqual(player.position(), position)
            prefs.hide_instrumental = False
            overlay.refresh_preferences()
            self.assertIsNotNone(overlay.timeline)
        finally:
            overlay.hide()
            overlay.deleteLater()
