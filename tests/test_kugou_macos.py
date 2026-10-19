"""Mac KuGou fixtures and distribution boundaries; no live Mac claim."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from concurrent.futures import Future
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
import main_mac
import macos_kugou as mac
from macos_netease import NowPlaying, OffsetStore
from lrc import LyricDocument, LyricLine
from tools.package_macos import KUGOU_APP_BUNDLE, archive_name, audit_archive

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = Future()
        self.jobs.append((function, args, future))
        return future

    def finish(self, index, value=None, error=None):
        future = self.jobs[index][2]
        future.set_exception(error) if error else future.set_result(value)


def info(title="Song", position=1000, playing=True):
    return NowPlaying("playing" if playing else "paused", title, "Artist", 180000, position,
                      rate=1 if playing else 0)


def payload(**updates):
    data = dict(bundleIdentifier=mac.KUGOU_BUNDLE, title="Song", artist="Artist", playing=True,
                durationMicros=180000000, elapsedTimeMicros=1000000,
                timestampEpochMicros=1700000000000000, playbackRate=1)
    data.update(updates)
    return data


class MacKugouParsingTests(unittest.TestCase):
    def test_timestamp_anchor_repeated_read_progresses_and_uses_playback_rate(self):
        data = payload(playbackRate=1.25)
        first = mac.parse_media_info(data, epoch=1700000000.5)
        later = mac.parse_media_info(data, epoch=1700000001.5)
        self.assertEqual(first.position_ms, 1625)
        self.assertEqual(later.position_ms, 2875)
        self.assertEqual(later.rate, 1.25)

    def test_pause_never_extrapolates_and_position_stays_inside_song(self):
        paused = mac.parse_media_info(payload(playing=False, playbackRate=1), epoch=1700000100)
        self.assertEqual((paused.state, paused.position_ms), ("paused", 1000))
        self.assertEqual(mac.parse_media_info(payload(), epoch=1700000500).position_ms, 180000)

    def test_source_unknown_other_player_and_missing_state_are_rejected(self):
        for data in (None, [], {}, payload(bundleIdentifier=None), payload(bundleIdentifier="com.apple.Music"),
                     payload(playing=1), payload(playing=None), payload(title="")):
            with self.subTest(data=data):
                self.assertIsNone(mac.parse_media_info(data, epoch=1700000001))
        data = payload(bundleIdentifier="com.apple.Music", parentApplicationBundleIdentifier=mac.KUGOU_BUNDLE)
        self.assertIsNone(mac.parse_media_info(data, epoch=1700000001))

    def test_missing_or_invalid_timeline_does_not_create_a_synthetic_clock(self):
        for key, value in (("durationMicros", 0), ("durationMicros", None), ("elapsedTimeMicros", None),
                           ("elapsedTimeMicros", True), ("elapsedTimeMicros", -1),
                           ("elapsedTimeMicros", float("nan")), ("playbackRate", float("inf")),
                           ("playbackRate", 0), ("timestampEpochMicros", 0),
                           ("timestampEpochMicros", 1800000000000000)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                mac.parse_media_info(payload(**{key: value}), epoch=1700000001)

    def test_query_is_only_read_command_with_timeout_and_no_shell(self):
        with patch.object(mac.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(payload()))) as run, \
                patch.object(mac.time, "time", return_value=1700000001):
            self.assertEqual(mac.query_media_control("/中文 path/media-control").position_ms, 2000)
            self.assertEqual(run.call_args.args[0], ["/中文 path/media-control", "get", "--micros", "--no-artwork"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertNotIn("shell", run.call_args.kwargs)
        for text in ("null", " "):
            with patch.object(mac.subprocess, "run", return_value=SimpleNamespace(stdout=text)):
                self.assertIsNone(mac.query_media_control("tool"))

    def test_timeout_oversized_and_malformed_payloads_fail_explicitly(self):
        for value in ("X" * (mac.MAX_PAYLOAD + 1), "not json"):
            with patch.object(mac.subprocess, "run", return_value=SimpleNamespace(stdout=value)), self.assertRaises(ValueError):
                mac.query_media_control("tool")
        with patch.object(mac.subprocess, "run", side_effect=subprocess.TimeoutExpired("tool", 2)), \
                self.assertRaises(subprocess.TimeoutExpired):
            mac.query_media_control("tool")

    def test_reader_discovery_supports_both_homebrew_locations_without_launching_anything(self):
        for path in ("/opt/homebrew/bin/media-control", "/usr/local/bin/media-control"):
            with patch.dict(os.environ, {"MEDIA_CONTROL": path}), patch.object(mac.Path, "is_file", return_value=True), \
                    patch.object(mac.os, "access", return_value=True), patch.object(mac.subprocess, "run") as run:
                self.assertEqual(mac.find_media_control(), str(Path(path)))
                run.assert_not_called()


class MacKugouPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.executor = ManualExecutor()
        self.player = mac.MacKugouPlayer(query=lambda: None, executor=self.executor,
                                        clock=lambda: self.now[0], autostart=False)
        self.docs = []
        self.jumps = []
        self.player.document_changed.connect(self.docs.append)
        self.player.discontinuity.connect(lambda: self.jumps.append(self.now[0]))

    def tearDown(self):
        self.player.stop()

    def test_normal_poll_jitter_does_not_rewind_or_repeat_sentence_boundaries(self):
        self.player.apply(info(position=0))
        samples = []
        for frame in range(1, 301):
            self.now[0] = frame / 60
            if frame % 30 == 0:
                raw = round(self.now[0] * 1000) + (-90 if frame % 60 else 90)
                self.player.apply(info(position=raw))
            samples.append(self.player.position())
        self.assertTrue(all(b >= a for a, b in zip(samples, samples[1:])))
        self.assertLess(abs(samples[-1] - 5000), 150)
        self.assertEqual(len(self.jumps), 1)
        self.assertEqual(sum(a < 2000 <= b for a, b in zip(samples, samples[1:])), 1)

    def test_pause_freezes_seek_realigns_and_lost_reader_cannot_coast_forever(self):
        self.player.apply(info())
        self.now[0] = .5
        self.player.apply(info(position=1500, playing=False))
        paused = self.player.position()
        self.now[0] = 20
        self.assertEqual(self.player.position(), paused)
        self.player.apply(info(position=19000, playing=False))
        self.assertEqual(self.player.position(), 19000)
        self.player.apply(info(position=19000))
        self.now[0] = 100
        self.assertLessEqual(self.player.position(), 20500)
        self.player.apply(None)
        self.assertFalse(self.player.playing)
        self.assertIsNone(self.docs[-1])

    def test_missing_helper_uses_kugou_message_and_has_no_native_window_interface(self):
        with patch.object(mac.MacKugouPlayer, "find_reader", return_value=None):
            other = mac.MacKugouPlayer(autostart=False)
            try:
                self.assertIn("brew install media-control", other.status)
                self.assertNotIn("网易云", other.status)
                self.assertFalse(hasattr(other, "configure_native_replacement"))
                self.assertFalse(hasattr(other, "set_overlay_visible"))
            finally:
                other.stop()

    def test_read_failure_clears_lyrics_and_identifies_correct_helper(self):
        self.player.apply(info())
        self.player._tick()
        self.executor.finish(0, error=ValueError("missing timeline"))
        with self.assertLogs("macos_netease", level="WARNING"):
            self.player._tick()
        self.assertFalse(self.player.connected)
        self.assertIn("media-control", self.player.status)
        self.assertIsNone(self.docs[-1])

    def test_switch_discards_late_lyrics_and_retry_can_recover_previously_missing_lyrics(self):
        old = LyricDocument([LyricLine(0, "old")], [])
        current = LyricDocument([LyricLine(0, "current")], [])
        self.player.apply(info("old"))
        self.player._tick()
        self.player.apply(info("current"))
        self.executor.finish(1, old)
        self.player._tick()
        self.assertNotIn(old, self.docs)
        self.executor.finish(2, None)
        self.player._tick()
        self.assertIn("保持空白", self.player.status)
        self.player.retry_lyrics()
        self.player._tick()
        self.executor.finish(3, current)
        self.player._tick()
        self.assertIs(self.docs[-1], current)
        self.assertIn("酷狗", self.player.status)

    def test_real_krc_intervals_and_line_ends_are_reused_from_selected_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            from kugou_lyrics import KRC_KEY
            import zlib
            compressed = zlib.compress(b"[1000,800]<0,300,0>Hello<300,500,0> world")
            encrypted = bytes(v ^ KRC_KEY[i % len(KRC_KEY)] for i, v in enumerate(compressed))
            (Path(temporary) / "Artist - Song.krc").write_bytes(b"krc1" + encrypted)
            self.player.configure_cache_directory(temporary)
            doc = self.player._loader(info())
            self.assertEqual(doc.lines[0].end_ms, 1800)
            self.assertEqual([(w.start_ms, w.end_ms) for w in doc.lines[0].words], [(1000, 1300), (1300, 1800)])
            self.assertIn(temporary, self.player.cache_status)

    def test_offsets_and_controls_never_send_transport_or_window_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.player.set_offset_store(OffsetStore(Path(temporary) / "offsets.json"))
            self.player.apply(info())
            with patch.object(mac.subprocess, "run") as run:
                self.player.adjust_offset(200)
                self.player.toggle()
                self.player.seek(15000)
                self.player.set_volume(0)
                self.assertEqual((self.player.position(), self.player.lyric_offset_ms), (1000, 200))
                self.assertEqual(self.player.energy_at(), 0)
                run.assert_not_called()
            self.player.stop()
            self.player.apply(info("ignored"))
            self.assertEqual(self.player.title, "Song")

    def test_mac_panel_offers_cache_retry_but_no_native_replacement_or_windows_startup(self):
        import controls
        from overlay import LyricsOverlay
        from settings import Preferences, SettingsStore
        with tempfile.TemporaryDirectory() as temporary:
            prefs = Preferences(motion="wave")
            overlay = LyricsOverlay(self.player, prefs)
            with patch.object(controls, "sys", SimpleNamespace(platform="darwin")):
                panel = controls.ControlPanel(self.player, overlay, prefs, SettingsStore(Path(temporary) / "settings.ini"))
            try:
                self.assertEqual(panel.source_short, "酷狗")
                self.assertFalse(hasattr(panel, "kugou_replace_checkbox"))
                self.assertIsNone(panel.startup_manager)
                self.assertTrue(panel.progress.display_only)
                self.assertFalse(panel.qq_cache_controls.isHidden())
                self.assertFalse(panel.retry_lyrics_button.isHidden())
                self.assertTrue(overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)
                self.player.apply(info())
                with patch.object(mac.subprocess, "run") as run:
                    panel.theme_combo.setCurrentIndex(panel.theme_combo.findData("dark"))
                    panel._refresh_position()
                    run.assert_not_called()
                self.assertEqual(self.player.position(), 1000)
            finally:
                panel.refresh_timer.stop()
                panel.font_preview.timer.stop()
                panel.font_library.close()
                panel.tray.hide()
                panel.hide()
                panel.deleteLater()
                overlay.close()
                overlay.deleteLater()


class MacKugouDistributionTests(unittest.TestCase):
    def test_state_is_separate_from_netease_and_never_inside_the_bundle(self):
        with patch.object(main_mac.Path, "home", return_value=Path("/Users/example")):
            self.assertEqual(main_mac.default_state_directory(True),
                             Path("/Users/example/Library/Application Support/DancingLyrics-Kugou"))

    def test_entry_rejects_windows_and_check_reports_no_live_claim(self):
        with patch.object(main_mac.sys, "platform", "win32"), redirect_stderr(StringIO()):
            self.assertEqual(main_mac.main(["--kugou"]), 2)
        output = StringIO()
        with patch.object(main_mac.sys, "platform", "darwin"), \
                patch.object(mac, "find_media_control", return_value="helper"), redirect_stdout(output):
            self.assertEqual(main_mac.main(["--kugou", "--check"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["player"], "kugou")
        self.assertFalse(report["live_playback_verified"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main_mac.main(["--kugou", "--local"])

    def test_distinct_archives_and_native_build_guards(self):
        for arch in ("arm64", "x86_64"):
            self.assertIn("kugou-macos.1", archive_name(arch, "kugou"))
            self.assertNotEqual(archive_name(arch), archive_name(arch, "kugou"))
        from tools.build_macos import build
        from tools.verify_macos import verify
        with patch("sys.platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                build("unused", "kugou")
            with self.assertRaisesRegex(RuntimeError, "原生 macOS"):
                verify("unused.zip", "unused", "kugou")

    def test_source_archive_has_portable_imports_permissions_and_no_personal_state(self):
        from tools.package_kugou_macos_source import package, PREFIX
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = package(temporary)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertFalse(any(set(Path(n).parts) & {".state", ".venv", "artifacts", ".git"} for n in names))
                self.assertFalse(any(n.endswith((".exe", ".dll", ".pyd")) for n in names))
                for name in names:
                    if name.endswith(".command"):
                        self.assertTrue(archive.getinfo(name).external_attr >> 16 & 0o111)
                        self.assertNotIn(b"\r\n", archive.read(name))
                manifest = json.loads(archive.read(f"{PREFIX}/source-info.json"))
                self.assertFalse(manifest["native_app_included"])
                self.assertFalse(manifest["macos_live_verified"])
                archive.extractall(Path(temporary) / "extract")
            code = ("import main_mac_kugou, macos_kugou, controls, overlay, player; import sys; "
                    "assert 'kugou_native' not in sys.modules; assert 'kugou_reader' not in sys.modules")
            result = subprocess.run([__import__("sys").executable, "-c", code],
                                    cwd=Path(temporary) / "extract" / PREFIX, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_extracted_source_can_build_without_git_metadata(self):
        from tools.package_macos import source_commit
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "source-info.json"
            manifest.write_text(json.dumps({"source_commit": "a" * 40}))
            with patch("tools.package_macos.subprocess.check_output") as git:
                self.assertEqual(source_commit(temporary), "a" * 40)
                git.assert_not_called()
            manifest.write_text(json.dumps({"source_commit": "unknown"}))
            with self.assertRaises(ValueError):
                source_commit(temporary)

    def test_macos_archive_audits_correct_player_bundle_and_runtime(self):
        prefix = f"CityEchoes/{KUGOU_APP_BUNDLE}"
        names = [f"{prefix}/Contents/MacOS/CityEchoes", f"{prefix}/Contents/Info.plist"]
        names += [f"{prefix}/Contents/Frameworks/PySide6/{m}.abi3.so" for m in ("QtCore", "QtGui", "QtWidgets", "QtMultimedia")]
        names += [f"{prefix}/Contents/Frameworks/libqcocoa.dylib", f"{prefix}/Contents/Frameworks/_multiarray_umath.so",
                  f"{prefix}/Contents/Resources/base_library.zip"]
        names += [f"CityEchoes/{n}" for n in ("使用说明.txt", "安装播放读取工具.command", "LICENSE", "THIRD_PARTY_NOTICES.md",
                  "build-info.json", "licenses/Python-LICENSE.txt", "licenses/NumPy-LICENSE.txt", "licenses/LGPL-3.0.txt")]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in names:
                    entry = zipfile.ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFREG | 0o755) << 16
                    archive.writestr(entry, b"fixture")
            self.assertGreater(audit_archive(path, "kugou")["entries"], 10)
            with self.assertRaisesRegex(ValueError, "缺少"):
                audit_archive(path)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("CityEchoes/media-control", b"not bundled")
            with self.assertRaisesRegex(ValueError, "禁止发布"):
                audit_archive(path, "kugou")

    def test_guides_distinguish_source_binary_and_unverified_background(self):
        guide = (ROOT / "KUGOU-MACOS.md").read_text(encoding="utf-8")
        for phrase in ("尚未生成 Mac 成品", "arm64", "x86_64", "media-control", "真实音频强弱", "不自动接替",
                       "最小化", "关闭与恢复", "待 Mac 真机验收", "DancingLyrics-Kugou", "私有框架"):
            self.assertIn(phrase, guide)
        workflow = (ROOT / ".github/workflows/kugou-macos-package.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        self.assertIn("--player kugou", workflow)
        evidence = json.loads((ROOT / "docs/research/kugou-macos-20261017.json").read_text(encoding="utf-8"))
        self.assertFalse(evidence["validation"]["macos_app_built"])
        self.assertFalse(evidence["validation"]["live_kugou_mac_tested"])
        self.assertFalse(evidence["validation"]["background_minimized_verified"])
        self.assertFalse(evidence["validation"]["close_and_reopen_verified"])
        self.assertEqual(evidence["client_window_commands"], 0)
        self.assertEqual(evidence["validation"]["new_adapter_tests"], 24)

    def test_diagnostic_is_mac_only_and_does_not_collect_track_names(self):
        from tools.probe_kugou_macos import probe
        with patch("sys.platform", "darwin"), patch.object(mac, "find_media_control", return_value="helper"), \
                patch.object(mac, "query_media_control", return_value=info()):
            result = probe()
        self.assertTrue(result["playback_detected"])
        self.assertFalse(result["background_verified"])
        self.assertFalse(result["window_close_verified"])
        self.assertNotIn("title", result)
        self.assertNotIn("artist", result)

    def test_shell_syntax_and_windows_install_guard(self):
        git = shutil.which("git")
        git_bash = Path(git).resolve().parents[1] / "bin/bash.exe" if git else None
        bash = str(git_bash) if git_bash and git_bash.is_file() else ("/bin/bash" if Path("/bin/bash").is_file() else None)
        if not bash:
            self.skipTest("Bash unavailable")
        scripts = list(ROOT.glob("*酷狗Mac.command")) + list((ROOT / "release/kugou-macos").glob("*.command"))
        for script in scripts:
            result = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        if os.name == "nt":
            result = subprocess.run([bash, str(ROOT / "安装酷狗Mac.command")], capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
