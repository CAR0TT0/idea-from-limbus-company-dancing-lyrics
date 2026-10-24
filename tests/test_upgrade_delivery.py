"""Keep new usage, actual media and the public verification boundary reviewable."""
import hashlib
import json
from pathlib import Path
import unittest
from PySide6.QtGui import QImageReader
from app_info import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


class UpgradeDeliveryTests(unittest.TestCase):
    def test_usage_and_version_are_consistent_and_new_media_are_real_animations(self):
        for name in ('README.md', 'NETEASE.md', 'QQMUSIC.md',
                     'release/netease/使用说明.txt', 'release/qqmusic/使用说明.txt'):
            text = (ROOT/name).read_text(encoding='utf-8')
            self.assertIn(APP_VERSION, text)
            for feature in ('渐变描边', '渐变字芯', '双语·原文在上', '双语·译文在上',
                            '80%', '乱码退场', '显示屏幕', '纯音乐时不显示', '滚轮'):
                self.assertIn(feature, text)
        reader = QImageReader(str(ROOT/'docs/images/scramble-bilingual-demo.gif'))
        self.assertTrue(reader.supportsAnimation())
        self.assertEqual(reader.loopCount(), -1)
        self.assertEqual(reader.imageCount(), 120)
        duration, distinct, phases = 0, set(), set()
        for index in range(reader.imageCount()):
            picture = reader.read()
            self.assertFalse(picture.isNull())
            duration += reader.nextImageDelay()
            fingerprint = hashlib.sha256(picture.constBits()).digest()
            distinct.add(fingerprint)
            if index in (0, 5, 10, 20, 88, 94, 100, 106, 112, 119):
                phases.add(fingerprint)
        self.assertEqual(duration, 4000)
        # Holding may repeat frames; entry and scramble-exit phases must change.
        self.assertGreater(len(distinct), 20)
        self.assertGreaterEqual(len(phases), 8)

    def test_verification_distinguishes_real_playback_from_demo_and_hardware_limits(self):
        data = json.loads((ROOT/'docs/research/lyric-upgrade-20261016.json').read_text(encoding='utf-8'))
        self.assertEqual(data['app_version'], '0.8.0-beta.1')  # Historical 2026-10-16 evidence.
        self.assertTrue(data['passed'])
        self.assertEqual(len(data['benchmarks']), 16)
        for row in data['benchmarks']:
            self.assertLessEqual(row['p95_ms'], 33)
            self.assertLessEqual(row['cache_bytes'], 64*1024*1024)
            self.assertTrue(row['warm_blur_reused'])
        for item in data['real_playback'].values():
            self.assertTrue(item['passed'])
            self.assertFalse(item['simulated_data'])
            self.assertTrue(item['source_harness'])
            self.assertEqual(item['playback_commands'], [])
            self.assertFalse(item['real_pause_seek_switch_verified'])
        self.assertTrue(data['recording']['synthetic_lyric_times'])
        self.assertTrue(data['recording']['nativeWindowVisible'])
        self.assertIn('second_clean_machine', data['not_verified'])
        self.assertIn('physical_monitor_hotplug', data['not_verified'])
