"""Golden reference containers plus cache identity, failure and asynchronous regressions."""
from concurrent.futures import Future, CancelledError, ThreadPoolExecutor
from dataclasses import replace
import json
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from controls import ControlPanel
from lrc import LyricTimeline, display_document
from overlay import LyricsOverlay
from qqmusic import QQMusicPlayer, QQSnapshot
from qqmusic_cache import QQCacheReader, sanitized, expected_filenames
from qqmusic_qrc import MAX_ENCRYPTED, MAX_TEXT, _unmask, decode_qrc, parse_qrc
from qqmusic_lyrics import LyricResult
from settings import Preferences, SettingsStore, normalize_preferences
from presets import EFFECT_KEYS

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT/'tests/fixtures/qq-cache-synthetic.json').read_text(encoding='utf-8'))['samples']
APP = QApplication.instance() or QApplication([])


def data(name='qrc'):
    return bytes.fromhex(GOLDEN[name]['hex'])


def snapshot(title='缓存测试',artist='演示歌手',album='演示专辑',duration=3000):
    return QQSnapshot(title,artist,album,duration,500,False)


def write_cache(directory, song=None, name='qrc', translated=False):
    song = song or snapshot()
    filename = f'{sanitized(song.artist)} - {sanitized(song.title)} - {song.duration_ms//1000} - {sanitized(song.album)}_qm.qrc'
    path = Path(directory)/filename
    path.write_bytes(data(name))
    if translated:
        path.with_name(filename.replace('_qm.qrc','_qmts.qrc')).write_bytes(data('translation'))
    return path


class ContainerTests(unittest.TestCase):
    def test_qmc_reference_skip_boundaries_beyond_32k_and_64k(self):
        fixture = json.loads((ROOT/'tests/fixtures/qq-cache-synthetic.json').read_text(encoding='utf-8'))
        for sample in fixture['mask_boundaries']:
            self.assertEqual(hashlib.sha256(_unmask(bytes(sample['length']))).hexdigest(),sample['sha256'])

    def test_independent_reference_ciphertexts_decode_exactly(self):
        for name, sample in GOLDEN.items():
            with self.subTest(name=name):
                self.assertEqual(decode_qrc(bytes.fromhex(sample['hex'])),sample['text'])

    def test_truncated_corrupt_unknown_and_oversized_data_fail_closed(self):
        broken = bytearray(data()); broken[35] ^= 128
        for raw in (b'',data()[:30],bytes(broken),b'unknown binary data'*10,b'x'*(MAX_ENCRYPTED+1)):
            with self.subTest(size=len(raw)), self.assertRaises((ValueError,UnicodeError)):
                decode_qrc(raw)

    def test_nonzero_outer_offset_falls_back_without_guessing_sign(self):
        decoded = _unmask(data())
        changed = _unmask(decoded.replace(b'[offset:0]',b'[offset:1]',1))
        with self.assertRaisesRegex(ValueError,'偏移'):
            decode_qrc(changed)

    def test_decompression_output_is_bounded(self):
        import zlib
        bomb = zlib.compress(b'x'*(MAX_TEXT+1))
        with patch('qqmusic_qrc.decrypt_blocks',return_value=bomb), self.assertRaisesRegex(ValueError,'过大'):
            decode_qrc(data())

    def test_cancelled_decode_stops_before_full_des_loop(self):
        with self.assertRaises(CancelledError):
            decode_qrc(data(),lambda:True)

    def test_qrc_word_ranges_preserve_english_combining_emoji_and_blank_boundary(self):
        doc = parse_qrc(GOLDEN['qrc']['text'])
        self.assertEqual([line.text for line in doc.lines],['你好','Hello é🙂',''])
        units = [doc.lines[1].text[word.text_start:word.text_end] for word in doc.lines[1].words]
        self.assertEqual(units,['Hello ','é🙂'])
        self.assertEqual((doc.lines[1].words[1].start_ms,doc.lines[1].words[1].end_ms),(1300,1800))

    def test_zero_and_invalid_word_intervals_do_not_break_line_lyrics(self):
        doc = parse_qrc('[0,1000]甲(0,0)乙(200,300)\n[1000,500]丙(2000,100)')
        self.assertEqual(doc.lines[0].text,'甲乙')
        self.assertEqual(len(doc.lines[0].words),1)
        self.assertEqual(doc.lines[0].words[0].text_start,1)
        self.assertEqual(doc.lines[1].words,())
        self.assertTrue(doc.warnings)

    def test_xml_lrc_pure_music_and_untimed_fallback(self):
        xml = '<Qrc><Lyric_1 LyricContent="[0,1000]A&amp;B(0,500)"/></Qrc>'
        self.assertEqual(parse_qrc(xml).lines[0].text,'A&B')
        self.assertEqual(parse_qrc('[00:01.00]普通歌词').lines[0].words,())
        self.assertIsNone(parse_qrc(GOLDEN['pure']['text']))
        with self.assertRaises(ValueError):
            parse_qrc('没有时间的文字')

    def test_offset_is_applied_once_to_lines_and_word_ranges_with_user_delay(self):
        doc = parse_qrc('[offset:100]\n[1000,1000]演示(1000,400)')
        timeline = LyricTimeline(doc,delay_ms=250)
        self.assertEqual(timeline.lines[0].start_ms,1150)
        self.assertEqual(timeline.lines[0].words[0].start_ms,1150)
        self.assertEqual(timeline.lines[0].words[0].end_ms,1550)

    def test_real_qq_envelope_allows_unescaped_quotes_but_rejects_ambiguous_payloads(self):
        xml = '<QrcInfos><LyricInfo LyricCount="1"><Lyric_1 LyricType="1" LyricContent="[0,1000]A "B" &amp; C(0,500)"/></LyricInfo></QrcInfos>'
        doc = parse_qrc(xml)
        self.assertEqual(doc.lines[0].text,'A "B" & C')
        self.assertEqual(len(doc.lines[0].words),1)
        multiline = xml.replace('A "B" &amp; C(0,500)','第一行(0,200)\n第二行(200,300)')
        self.assertEqual(parse_qrc(multiline).lines[0].text,'第一行\n第二行')
        for invalid in (xml.replace('LyricCount="1"','LyricContent="other"'),
                        xml.replace('</QrcInfos>',''),
                        '<!DOCTYPE Qrc [<!ENTITY value "expanded">]><Qrc LyricContent="&value;"/>'):
            with self.assertRaises(ValueError):
                parse_qrc(invalid)

    def test_multiline_and_simultaneous_rows_preserve_text_without_fake_timing(self):
        doc = parse_qrc('[0,1000]第一行(0,300)\n第二行(300,400)\n[1200,500]甲(1200,100)\n[1200,500]乙(1300,100)')
        self.assertEqual(doc.lines[0].text,'第一行\n第二行')
        self.assertEqual(doc.lines[1].text,'甲\n乙')
        self.assertEqual(doc.lines[1].words,())


class CacheReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'中文 歌词缓存'
        self.path.mkdir()
        self.clock = [0.0]
        self.reader = QQCacheReader(str(self.path),clock=lambda:self.clock[0])

    def test_chinese_directory_offline_qrc_and_translation_priority(self):
        write_cache(self.path,translated=True)
        result = self.reader.load(snapshot())
        self.assertEqual(result.source,'cache')
        self.assertIn('本地缓存',result.message)
        self.assertEqual(result.document.lines[0].translation,'你好译文')
        self.assertEqual(display_document(result.document,True).lines[0].words,())

    def test_full_filename_matching_handles_separator_and_windows_replaced_characters(self):
        song = snapshot('A - B: 曲目','歌手/另一位','专辑 - 合集')
        write_cache(self.path,song)
        self.assertIsNotNone(self.reader.load(song))
        self.assertIsNone(self.reader.load(replace(song,title='A - B: 曲目 (Live)')))
        self.assertIsNone(self.reader.load(replace(song,artist='另一位')))
        self.assertIsNone(self.reader.load(replace(song,album='其他专辑')))
        self.assertIsNone(self.reader.load(replace(song,duration_ms=7000)))

    def test_missing_identity_and_ambiguous_duration_or_directory_are_rejected(self):
        write_cache(self.path)
        write_cache(self.path,replace(snapshot(),duration_ms=4000))
        self.assertIsNone(self.reader.load(snapshot()))
        self.assertIn('歧义',self.reader.status)
        self.assertFalse(expected_filenames(replace(snapshot(),album='')))
        self.assertFalse(expected_filenames(replace(snapshot(),artist='')))

    def test_spaces_within_identity_are_not_erased_to_match_another_song(self):
        write_cache(self.path,snapshot('AB'))
        self.assertIsNone(self.reader.load(snapshot('A B')))

    def test_missing_file_late_arrival_and_translation_updates_are_detected(self):
        self.assertIsNone(self.reader.load(snapshot()))
        path = write_cache(self.path)
        self.clock[0] = .6
        original = self.reader.load(snapshot())
        self.assertIsNotNone(original)
        self.assertIsNone(self.reader.load(snapshot(),known_signature=original.cache_signature))
        path.with_name(path.name.replace('_qm.qrc','_qmts.qrc')).write_bytes(data('translation'))
        updated = self.reader.load(snapshot(),known_signature=original.cache_signature)
        self.assertEqual(updated.document.lines[0].translation,'你好译文')

    def test_corrupt_original_falls_back_but_optional_bad_translation_keeps_original(self):
        path = write_cache(self.path,translated=True)
        translation = path.with_name(path.name.replace('_qm.qrc','_qmts.qrc'))
        translation.write_bytes(b'broken')
        self.assertIsNotNone(self.reader.load(snapshot()).document)
        path.write_bytes(b'broken')
        self.assertIsNone(self.reader.load(snapshot()))

    def test_stamp_change_and_read_permission_failure_never_return_partial_content(self):
        write_cache(self.path)
        with patch.object(self.reader,'_read',side_effect=PermissionError('no access')):
            self.assertIsNone(self.reader.load(snapshot()))
        with patch.object(self.reader,'_read',side_effect=ValueError('歌词缓存正在更新')):
            self.assertIsNone(self.reader.load(snapshot()))

    def test_saved_directory_takes_precedence_and_parent_folder_is_accepted(self):
        child = self.path/'QQMusicLyricNew'; child.mkdir()
        write_cache(child)
        finder = Mock(side_effect=AssertionError('explicit directory must not discover'))
        reader = QQCacheReader(str(self.path),finder=finder)
        self.assertIsNotNone(reader.load(snapshot()))
        finder.assert_not_called()

    def test_auto_discovery_retries_when_no_directory_existed(self):
        finder = Mock(side_effect=[[],[self.path]])
        reader = QQCacheReader(finder=finder,clock=lambda:self.clock[0])
        self.assertIsNone(reader.load(snapshot()))
        write_cache(self.path)
        self.clock[0] = 31
        self.assertIsNotNone(reader.load(snapshot()))

    def test_active_handle_discovery_wins_over_common_locations(self):
        import qqmusic_cache_paths as paths
        with patch.object(paths,'active_cache_directories',return_value=[self.path]):
            self.assertEqual(paths.discover_cache_directories(),[self.path])

    def test_pure_music_cache_prevents_network_fallback_and_cancel_is_propagated(self):
        write_cache(self.path,name='pure')
        result = self.reader.load(snapshot())
        self.assertIsNotNone(result)
        self.assertIsNone(result.document)
        with self.assertRaises(CancelledError):
            self.reader.load(snapshot(),cancelled=lambda:True)


class ImmediateExecutor:
    def submit(self,function,*args,**kwargs):
        future = Future()
        try: future.set_result(function(*args,**kwargs))
        except BaseException as error: future.set_exception(error)
        return future


class CachePlayerTests(unittest.TestCase):
    def make(self,reader,loader=None,executor=None):
        self.clock = [0.0]
        self.documents = []
        player = QQMusicPlayer(clock=lambda:self.clock[0],executor=executor or ImmediateExecutor(),
                               loader=loader or Mock(return_value=LyricResult(None,'网络无歌词')),
                               cache_reader=reader,cache_executor=ImmediateExecutor(),autostart=False)
        player.document_changed.connect(self.documents.append)
        self.addCleanup(player.stop)
        return player

    def test_cache_hit_never_calls_network_and_pause_is_frozen(self):
        result = LyricResult(parse_qrc(GOLDEN['qrc']['text']),'本地缓存',source='cache',cache_signature=('a',))
        reader = Mock(status='缓存目录',load=Mock(return_value=result))
        network = Mock()
        player = self.make(reader,network)
        player.apply_snapshot(snapshot()); player._poll()
        self.assertEqual(self.documents[-1],result.document)
        network.assert_not_called()
        self.clock[0] = 3
        player._poll()
        self.assertFalse(player.playing)
        self.assertEqual(player.position(),500)

    def test_miss_keeps_network_fallback_then_late_local_words_upgrade(self):
        local = LyricResult(parse_qrc(GOLDEN['qrc']['text']),'本地缓存',source='cache',cache_signature=('a',))
        reader = Mock(status='目录',load=Mock(return_value=None))
        network = Mock(return_value=LyricResult(parse_qrc('[00:00]网络歌词'),'网络'))
        player = self.make(reader,network)
        player.apply_snapshot(snapshot()); player._poll()
        self.assertEqual(self.documents[-1].lines[0].text,'网络歌词')
        reader.load.return_value = local
        self.clock[0] = .6; player._poll(); player._poll()
        self.assertEqual(self.documents[-1],local.document)
        self.assertEqual(network.call_count,1)

    def test_unexpected_local_failure_keeps_official_network_fallback(self):
        reader = Mock(status='目录',load=Mock(side_effect=RuntimeError('discovery unavailable')))
        network = Mock(return_value=LyricResult(parse_qrc('[00:00]网络歌词'),'网络'))
        player = self.make(reader,network)
        with self.assertLogs('qqmusic',level='WARNING'):
            player.apply_snapshot(snapshot()); player._poll()
        self.assertEqual(network.call_count,1)
        self.assertEqual(self.documents[-1].lines[0].text,'网络歌词')

    def test_late_local_cache_wins_while_network_is_blocked(self):
        entered,release = threading.Event(),threading.Event()
        pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(lambda:pool.shutdown(wait=True,cancel_futures=True))
        self.addCleanup(release.set)
        def network(_):
            entered.set(); release.wait(3)
            return LyricResult(parse_qrc('[00:00]旧网络结果'),'网络')
        reader = Mock(status='目录',load=Mock(return_value=None))
        player = self.make(reader,network,pool)
        player.apply_snapshot(snapshot())
        self.assertTrue(entered.wait(1))
        reader.load.return_value = LyricResult(parse_qrc('[00:00]本地结果'),'本地',source='cache',cache_signature=('new',))
        self.clock[0] = .6; player._poll(); player._poll()
        self.assertEqual(self.documents[-1].lines[0].text,'本地结果')
        release.set(); time.sleep(.02); player._poll()
        self.assertEqual(self.documents[-1].lines[0].text,'本地结果')

    def test_old_local_completion_is_discarded_after_track_change(self):
        player = self.make(Mock(status='目录',load=Mock(return_value=None)))
        player.apply_snapshot(snapshot()); player._poll()
        late = Future(); late.set_running_or_notify_cancel()
        player._local_future,player._local_meta = late,(player.song_id,player._generation)
        player.apply_snapshot(snapshot('新歌曲'))
        late.set_result(LyricResult(parse_qrc('[00:00]旧缓存'),'本地',source='cache'))
        player._poll()
        self.assertFalse(any(doc and doc.lines[0].text=='旧缓存' for doc in self.documents))

    def test_polling_is_bounded_and_manual_retry_reopens_window(self):
        reader = Mock(status='目录',load=Mock(return_value=None))
        player = self.make(reader)
        player.apply_snapshot(snapshot()); player._poll()
        self.clock[0] = 21
        for _ in range(20): player._poll()
        self.assertEqual(reader.load.call_count,1)
        player.retry_lyrics(); player._poll()
        self.assertEqual(reader.load.call_count,2)

    def test_cache_directory_ui_persists_without_affecting_playback_or_presets(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefs = Preferences()
            store = SettingsStore(Path(temporary)/'settings.ini')
            player = self.make(Mock(status='目录',load=Mock(return_value=None)))
            overlay = LyricsOverlay(player,prefs)
            panel = ControlPanel(player,overlay,prefs,store)
            player.apply_snapshot(snapshot()); player._poll()
            before = (player.song_id,player.position(),player.playing)
            child = Path(temporary)/'QQMusicLyricNew'; child.mkdir()
            try:
                with patch('controls.QFileDialog.getExistingDirectory',return_value=temporary):
                    panel.choose_qq_cache()
                self.assertEqual(store.load().qq_cache_directory,str(child))
                self.assertNotIn('qq_cache_directory',EFFECT_KEYS)
                self.assertEqual((player.song_id,player.position(),player.playing),before)
                panel._set_qq_cache_directory('')
                self.assertEqual(store.load().qq_cache_directory,'')
            finally:
                panel.refresh_timer.stop(); panel.tray.hide(); panel.hide()
                overlay.timer.stop(); overlay.hide(); panel.deleteLater(); overlay.deleteLater()
                APP.processEvents()

    def test_old_settings_default_to_auto_and_invalid_paths_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SettingsStore(Path(temporary)/'settings.ini')
            self.assertEqual(store.load().qq_cache_directory,'')
            self.assertEqual(normalize_preferences(Preferences(qq_cache_directory='bad\x00path')).qq_cache_directory,'')


if __name__=='__main__':
    unittest.main()
