"""Regress the real Mili/ミリー mismatch without weakening song identity."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from kugou_identity import artists_match
from kugou_native import NativeLyricsReader
from kugou_reader import sample_channels
from kugou_lyrics import local_lyrics, choose_candidate
from test_kugou import krc, song


class BackgroundAliasTests(unittest.TestCase):
    def test_cross_script_artist_aliases_are_symmetric_and_conservative(self):
        for original, alias in (("Mili", "Mili（ミリー）"), ("Linkin Park", "Linkin Park (林肯公园)"),
                                ("ヨルシカ", "ヨルシカ (夜鹿)")):
            self.assertTrue(artists_match(original, alias))
            self.assertTrue(artists_match(alias, original))
        for other in ("Other", "Mili & Other", "Mili (feat. Other)", "Mili (合作歌手)",
                      "Mili (Live)", "Mili (Cover)", "Mili (现场版)", "Mili (ミリー & 他)", ""):
            self.assertFalse(artists_match("Mili", other), other)

    def test_aliased_native_view_advances_and_never_initializes_window_ocr(self):
        tick = [10.]
        reader = NativeLyricsReader(lambda: tick[0])
        reader.process = NS(pid=42)
        reader.objects = [(100, None)]
        reader.retry_at = 100
        media = dict(title="String Theocracy", artist="Mili", album="", duration_ms=0, playing=True)
        row = dict(title="String Theocracy (《废墟图书馆》游戏主题曲)", artist="Mili (ミリー)",
                   album="", duration_ms=162920, position_ms=20000, document=200)
        with patch("kugou_native.read_object", side_effect=lambda *_:dict(row)), patch(
                "kugou_reader.smtc_snapshot", new=AsyncMock(return_value=media)), patch("kugou_reader.WindowReader") as ocr:
            first = asyncio.run(sample_channels(None, reader, NS(pid=lambda:42)))[0]
            tick[0] += .2
            row['position_ms'] += 200
            second = asyncio.run(sample_channels(None, reader, NS(pid=lambda:42)))[0]
            self.assertEqual((first['mode'], second['mode']), ('native','native'))
            self.assertTrue(second['playing'])
            self.assertGreater(second['position_ms'], first['position_ms'])
            ocr.assert_not_called()

    def test_late_smtc_metadata_accepts_artist_alias_but_rejects_different_singer(self):
        packet = dict(title="String Theocracy", artist="Mili (ミリー)", album="", duration_ms=162920,
                      position_ms=20000, playing=True, mode="native")
        native = NS(read=Mock(return_value=packet))
        for artist, accepted in (("Mili", True), ("Other", False)):
            media = dict(title="String Theocracy", artist=artist, playing=True, rate=1)
            with patch("kugou_reader.smtc_snapshot", new=AsyncMock(side_effect=[None, media])):
                result = asyncio.run(sample_channels(None, native, NS(pid=lambda:42)))[0]
                self.assertEqual(result is not None, accepted)

    def test_local_and_network_lyrics_match_alias_without_mixing_versions(self):
        snapshot = replace(song(title="String Theocracy"), artist="Mili")
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/"Mili (ミリー) - String Theocracy.krc").write_bytes(krc("[1000,2000]<0,1900,0>Correct"))
            (Path(directory)/"Other - String Theocracy.krc").write_bytes(krc("[1000,2000]<0,1900,0>Wrong"))
            result = local_lyrics(snapshot, [directory])
            self.assertEqual(result.document.lines[0].text, 'Correct')
        candidate = dict(song="String Theocracy (游戏主题曲)", singer="Mili (ミリー)",
                         duration=snapshot.duration_ms, id='123', accesskey='abcdefgh')
        self.assertIs(choose_candidate([candidate], snapshot), candidate)
        self.assertIsNone(choose_candidate([{**candidate,'singer':'Mili & Other'}], snapshot))
        self.assertIsNone(choose_candidate([{**candidate,'song':'String Theocracy (Live)'}], snapshot))
