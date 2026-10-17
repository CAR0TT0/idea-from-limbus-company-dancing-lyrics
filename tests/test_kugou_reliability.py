"""Regressions from real translated titles, tray hiding and KRC boundaries."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from kugou_identity import titles_match
from kugou_native import NativeLyricsReader
from kugou_clock import PlaybackClock
from kugou_lyrics import local_lyrics, parse_krc
from lrc import LyricTimeline, display_document
from test_kugou import krc, song


class ReliabilityTests(unittest.TestCase):
    def test_real_japanese_title_with_client_chinese_alias_matches_native(self):
        self.assertTrue(titles_match("アスノヨゾラ哨戒班", "アスノヨゾラ哨戒班 (明日的夜空哨戒班)"))
        self.assertTrue(titles_match("Fly Me to the Moon", "Fly Me to the Moon（飞向月球）"))
        self.assertTrue(titles_match("Title", " TITLE "))

    def test_alias_matching_keeps_recording_versions_and_different_songs_distinct(self):
        for title in ("Title (Live)", "Title (现场版)", "Title (Remix)", "Title (伴奏)", "Other (标题)"):
            self.assertFalse(titles_match("Title", title), title)

    def test_native_alias_survives_foreground_independent_matching_and_buffering(self):
        tick=[10.0];reader=NativeLyricsReader(lambda:tick[0])
        reader.process=NS(pid=42);reader.objects=[(100,None)];reader.retry_at=100
        row=dict(title="アスノヨゾラ哨戒班",artist="ゆある",album="",duration_ms=160000,
                 position_ms=12345,document=300,lyric_hash="a"*32)
        media=dict(title="アスノヨゾラ哨戒班 (明日的夜空哨戒班)",artist="ゆある",album="",playing=True)
        with patch("kugou_native.read_object",return_value=row):
            packet=reader.read(42,media)
            self.assertIsNotNone(packet);self.assertEqual(packet["mode"],"native")
            self.assertEqual(packet["lyric_hash"],"a"*32)
            self.assertEqual(packet["title"],media["title"])
            tick[0]+=3
            self.assertFalse(reader.read(42,media)["playing"])
            row["position_ms"]+=200;tick[0]+=.2
            self.assertTrue(reader.read(42,media)["playing"])
            self.assertIsNone(reader.read(42,{**media,"artist":"Other"}))

    def test_native_hash_selects_current_lyric_version_over_newer_wrong_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            good=folder/("Singer - Example-"+"1"*32+"-123-00000000.krc")
            wrong=folder/("Singer - Example-"+"2"*32+"-456-00000000.krc")
            good.write_bytes(krc("[hash:"+"a"*32+"]\n[1000,9000]<0,8000,0>Correct"))
            wrong.write_bytes(krc("[hash:"+"b"*32+"]\n[2000,1000]<0,900,0>Wrong"))
            packet=NS(title="Example",artist="Singer",lyric_hash="a"*32,native_title="Example")
            result=local_lyrics(packet,[folder])
            self.assertEqual(result.document.lines[0].text,"Correct")
            self.assertEqual(result.document.lines[0].end_ms,10000)
            packet.lyric_hash="c"*32
            self.assertIsNone(local_lyrics(packet,[folder]))

    def test_cache_accepts_client_translation_alias_with_native_lyric_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/"ゆある - アスノヨゾラ哨戒班.krc").write_bytes(krc("[hash:"+"a"*32+"]\n[1000,1000]<0,800,0>Text"))
            packet=NS(title="アスノヨゾラ哨戒班 (明日的夜空哨戒班)",artist="ゆある",
                      lyric_hash="a"*32,native_title="アスノヨゾラ哨戒班")
            self.assertIsNotNone(local_lyrics(packet,[directory]))

    def test_krc_long_phrase_does_not_disappear_at_six_seconds(self):
        doc=parse_krc("[1000,9500]<0,9400,0>Long phrase\n[13000,1000]<0,900,0>Next")
        for style,scramble in (("classic",False),("classic",True),("fall_wave",False)):
            timeline=LyricTimeline(doc,animation_style=style,scramble_exit=scramble)
            with self.subTest(style=style,scramble=scramble):
                self.assertEqual(timeline.visible(10000,20000)[0].text,"Long phrase")
                self.assertEqual(timeline.visible(12000,20000),[])

    def test_krc_instrumental_gap_does_not_hold_sentence_until_next_cue(self):
        doc=parse_krc("[1000,2000]<0,1900,0>First\n[14000,1000]<0,900,0>Next")
        for style,scramble in (("classic",False),("classic",True),("ripple_shake",False)):
            timeline=LyricTimeline(doc,animation_style=style,scramble_exit=scramble)
            self.assertTrue(timeline.visible(2900,18000))
            self.assertEqual(timeline.visible(4000,18000),[])
            self.assertEqual(timeline.visible(13000,18000),[])

    def test_explicit_end_preserves_offsets_translation_and_word_times(self):
        doc=parse_krc("[offset:200]\n[1000,9000]<0,8000,0>First")
        doc.lines[0]=replace(doc.lines[0],translation="第一句")
        translated=display_document(doc,mode="bilingual_original")
        timeline=LyricTimeline(translated,delay_ms=500)
        self.assertEqual(timeline.lines[0].start_ms,1300)
        self.assertEqual(timeline.lines[0].end_ms,10300)
        self.assertEqual(timeline.lines[0].words[0].end_ms,9300)

    def test_precise_native_clock_does_not_coast_through_buffering_seconds(self):
        clock=PlaybackClock();clock.observe(10000,10.,100000,True,1.,False)
        for tick in (10.15,10.3,10.45,10.6,11.,12.):
            clock.observe(10000,tick,100000,True,1.,False)
        self.assertLessEqual(clock.position(12.1),10251)

    def test_precise_pause_does_not_reenter_previous_sentence(self):
        clock=PlaybackClock();clock.observe(1000,10.,100000,True,1.,False)
        before=clock.position(10.2)
        clock.observe(1000,10.2,100000,False,1.,False)
        self.assertAlmostEqual(clock.position(15.),before)

    def test_switching_from_ocr_to_native_does_not_rewind_interpolation(self):
        clock=PlaybackClock();clock.observe(1000,10.,100000,True,1.,True)
        clock.observe(1000,10.4,100000,True,1.,True)
        before=clock.position(10.6)
        clock.observe(2000,10.6,100000,True,1.,False)
        self.assertGreaterEqual(clock.position(10.6),before)

    def test_small_seek_while_paused_still_updates_precise_native_clock(self):
        clock=PlaybackClock();clock.observe(1000,10.,100000,False,1.,False)
        accepted,jumped=clock.observe(1800,10.2,100000,False,1.,False)
        self.assertTrue(accepted);self.assertTrue(jumped)
        self.assertEqual(clock.position(15.),1800)

    def test_native_hash_is_validated_and_changes_song_cache_identity(self):
        from kugou import parse_snapshot
        packet=dict(source="kugou",title="Song",artist="Singer",album="",position_ms=1234,duration_ms=60000,
                    playing=True,mode="native",lyric_hash="a"*32,native_title="Song")
        first=parse_snapshot(packet);second=parse_snapshot({**packet,"lyric_hash":"b"*32})
        self.assertNotEqual(first.key,second.key)
        with self.assertRaises(ValueError):parse_snapshot({**packet,"lyric_hash":"bad"})

    def test_access_denied_does_not_reclaim_a_live_helpers_native_window(self):
        from kugou_windows import Windows
        windows=Windows.__new__(Windows)
        windows.kernel=NS(OpenProcess=lambda *_:0)
        with patch("kugou_windows.c.get_last_error",return_value=5):
            self.assertTrue(windows.alive(12345))
        with patch("kugou_windows.c.get_last_error",return_value=87):
            self.assertFalse(windows.alive(12345))

    def test_reader_worker_pid_overrides_windows_python_redirector_pid(self):
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication
        from kugou import KugouPlayer
        from test_kugou import Executor
        app=QApplication.instance() or QApplication([])
        player=KugouPlayer(executor=Executor(),autostart=False)
        original=player.process
        packet=dict(snapshot=None,message="waiting",reader_pid=23456)
        player.process=NS(readAllStandardOutput=lambda:(json.dumps(packet)+"\n").encode(),processId=lambda:12345)
        try:
            player._read()
            self.assertEqual(player._reader_pid,23456)
            with patch("kugou_windows.NativeVisibility") as visibility:
                player._restore_native()
                visibility.assert_called_once_with(owner_pid=23456)
        finally:
            player.process=original;player.stop()

    def test_fix_guides_and_live_evidence_cover_the_reported_failures(self):
        root=Path(__file__).resolve().parents[1]
        for name in ("KUGOU.md","release/kugou-guide.txt"):
            text=(root/name).read_text(encoding="utf-8")
            for phrase in ("kugou.4","括注","结束时间","托盘"):
                self.assertIn(phrase,text)
        data=json.loads((root/"docs/research/kugou-reliability-20261017.json").read_text(encoding="utf-8"))
        self.assertTrue(data["repro"]["old_native_channel_rejected_current_song"])
        self.assertTrue(data["repro"]["krc_line_end_was_discarded"])
        self.assertTrue(data["packaged_tray"]["passed"])
        self.assertTrue(data["launcher"]["passed"])
        self.assertLessEqual(data["live"]["max_native_clock_error_ms"],350)
        self.assertGreater(data["live"]["max_explicit_end_lines"],0)


if __name__ == "__main__":unittest.main()
