import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from qqmusic_startup import (NamedMutex, QQStartupManager, ShowPanelEvent, WatcherState,
                             object_name, program_command)

ROOT = Path(__file__).resolve().parents[1]


class FakeRegistry:
    def __init__(self, value=None):
        self.value = value

    def read(self):
        return self.value

    def write(self, value):
        self.value = value

    def remove(self):
        self.value = None


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / '中文 音乐 & files'
        self.root.mkdir()
        self.exe = self.root / 'FloatingLyrics.exe'
        self.exe.write_bytes(b'test executable')
        self.registry = FakeRegistry()
        self.launcher = Mock()
        self.manager = QQStartupManager(self.root, [str(self.exe)], self.registry, self.launcher)

    def test_default_off_and_chinese_space_path_is_quoted_without_shell(self):
        self.assertFalse(self.manager.enabled())
        with patch('qqmusic_startup.sys.platform', 'win32'):
            self.manager.set_enabled(True)
        self.assertEqual(self.registry.value, f'"{self.exe}" --watch-qqmusic')
        self.launcher.assert_called_once_with([str(self.exe), '--watch-qqmusic'], self.root)
        self.assertTrue(self.manager.enabled())

    def test_repeat_enable_and_disable_only_owns_one_run_value(self):
        with patch('qqmusic_startup.sys.platform', 'win32'):
            self.manager.set_enabled(True)
            self.manager.set_enabled(True)
            self.manager.set_enabled(False)
            self.manager.set_enabled(False)
        self.assertIsNone(self.registry.value)
        self.assertFalse(self.manager.enabled())

    def test_update_location_does_not_let_old_install_remove_new_registration(self):
        other = QQStartupManager(self.root, [str(self.exe)], self.registry, self.launcher)
        other.registration = '"D:\\New app\\FloatingLyrics.exe" --watch-qqmusic'
        self.registry.value = other.registration
        with patch('qqmusic_startup.sys.platform', 'win32'):
            self.manager.set_enabled(False)
        self.assertTrue(other.enabled())
        self.assertFalse(self.manager.enabled())

    def test_failed_spawn_restores_previous_registration_or_removes_new_one(self):
        self.launcher.side_effect = OSError('无法创建进程')
        for previous in (None, 'previous install command'):
            self.registry.value = previous
            with patch('qqmusic_startup.sys.platform', 'win32'), self.assertRaises(OSError):
                self.manager.set_enabled(True)
            self.assertEqual(self.registry.value, previous)

    def test_missing_program_or_registry_permission_failure_does_not_claim_enabled(self):
        self.exe.unlink()
        with patch('qqmusic_startup.sys.platform', 'win32'), self.assertRaises(OSError):
            self.manager.set_enabled(True)
        self.assertIsNone(self.registry.value)
        self.exe.write_bytes(b'test')
        with patch.object(self.registry, 'write', side_effect=PermissionError('denied')):
            with patch('qqmusic_startup.sys.platform', 'win32'), self.assertRaises(PermissionError):
                self.manager.set_enabled(True)
        self.launcher.assert_not_called()

    def test_source_and_frozen_commands_preserve_paths(self):
        self.assertEqual(program_command(self.root, True, self.exe), [str(self.exe)])
        python = self.root / 'python.exe'
        python.write_bytes(b'test')
        pythonw = self.root / 'pythonw.exe'
        pythonw.write_bytes(b'test')
        self.assertEqual(program_command(self.root, False, python), [str(pythonw), str(self.root/'main.py')])

    def test_long_login_command_is_rejected_before_registry_is_written(self):
        self.manager.registration = 'a'*261
        with patch('qqmusic_startup.sys.platform', 'win32'), self.assertRaisesRegex(OSError, '260'):
            self.manager.set_enabled(True)
        self.assertIsNone(self.registry.value)
        self.launcher.assert_not_called()


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.state, self.launch = WatcherState(), Mock()

    def test_waits_for_qq_then_launches_only_once_and_respects_manual_exit(self):
        self.state.step([], False, 0, self.launch)
        self.launch.assert_not_called()
        self.state.step([10], False, 2, self.launch)
        self.state.step([10], True, 4, self.launch)
        for now in range(6, 100, 2):
            self.state.step([10], False, now, self.launch)
        self.launch.assert_called_once_with()

    def test_already_open_companion_is_reused_and_next_qq_session_rearms(self):
        self.state.step([10], True, 0, self.launch)
        self.state.step([10], False, 2, self.launch)
        self.launch.assert_not_called()
        self.state.step([], False, 4, self.launch)
        self.state.step([20], False, 6, self.launch)
        self.launch.assert_called_once_with()

    def test_fast_restart_with_new_pid_rearms_even_without_observed_absence(self):
        self.state.step([10], False, 0, self.launch)
        self.state.step([20], False, 2, self.launch)
        self.assertEqual(self.launch.call_count, 2)

    def test_spawn_failures_back_off_and_are_bounded_per_session(self):
        self.launch.side_effect = OSError('failed')
        for now in (0, 10, 20):
            with self.assertRaises(OSError):
                self.state.step([10], False, now, self.launch)
            self.state.step([10], False, now+1, self.launch)
        for now in (30, 40, 1000):
            self.state.step([10], False, now, self.launch)
        self.assertEqual(self.launch.call_count, 3)

    def test_watcher_early_entry_has_no_qt_and_hidden_process_has_no_shell(self):
        tree = ast.parse((ROOT/'main.py').read_text(encoding='utf-8-sig'))
        early = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.If))
        qt = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.ImportFrom)
                  and node.module.startswith('PySide6'))
        self.assertLess(early, qt)
        with patch('qqmusic_startup.subprocess.Popen') as popen, \
             patch('qqmusic_startup.subprocess.CREATE_NO_WINDOW', 0x08000000, create=True):
            from qqmusic_startup import spawn_hidden
            spawn_hidden(['app.exe', '--watch-qqmusic'], ROOT)
        self.assertFalse(popen.call_args.kwargs['shell'])
        self.assertEqual(popen.call_args.kwargs['creationflags'], 0x08000000)

    def test_watcher_exits_when_registration_changes_and_closes_resources(self):
        from qqmusic_startup import run_watcher
        with tempfile.TemporaryDirectory() as tmp:
            manager = Mock(root=Path(tmp), registration='test')
            manager.enabled.side_effect = [True, False]
            mutex = Mock(acquired=True)
            with patch('qqmusic_startup.sys.platform', 'win32'), \
                 patch('qqmusic_startup.NamedMutex', return_value=mutex), \
                 patch('qqmusic_startup.qq_processes', return_value=frozenset()), \
                 patch('qqmusic_startup.companion_running', return_value=False), \
                 patch('qqmusic_startup.time.sleep') as sleep:
                self.assertEqual(run_watcher(manager), 0)
            mutex.close.assert_called_once_with()
            manager.launcher.assert_not_called()
            sleep.assert_called_once_with(2.0)

    def test_duplicate_watcher_returns_without_polling_or_starting_companion(self):
        from qqmusic_startup import run_watcher
        manager, mutex = Mock(registration='test'), Mock(acquired=False)
        with patch('qqmusic_startup.sys.platform', 'win32'), \
             patch('qqmusic_startup.NamedMutex', return_value=mutex):
            self.assertEqual(run_watcher(manager), 0)
        manager.enabled.assert_not_called()
        manager.launcher.assert_not_called()
        mutex.close.assert_called_once_with()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows kernel objects')
    def test_native_mutex_and_event_prevent_duplicate_and_request_show(self):
        with patch('qqmusic_startup.object_name', side_effect=lambda kind: object_name(uuid.uuid4().hex+kind)):
            event = ShowPanelEvent()
        self.addCleanup(event.close)
        self.assertFalse(event.consume())
        event.signal()
        self.assertTrue(event.consume())
        self.assertFalse(event.consume())
        name = object_name('test.'+uuid.uuid4().hex)
        first, second = NamedMutex(name), NamedMutex(name)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertTrue(first.acquired)
        self.assertFalse(second.acquired)


class StartupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def make_panel(self, player, manager):
        from tests.test_controls import FakeOverlay
        from controls import ControlPanel
        from settings import Preferences, SettingsStore
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        panel = ControlPanel(player, FakeOverlay(), Preferences(),
                             SettingsStore(Path(temporary.name)/'settings.ini'), startup_manager=manager)
        def cleanup():
            panel.refresh_timer.stop()
            panel.font_preview._stop_preview()
            panel.tray.hide()
            panel.hide()
            panel.deleteLater()
            self.app.processEvents()
        self.addCleanup(cleanup)
        return panel

    def test_checkbox_changes_registration_without_changing_music_or_presets(self):
        from tests.test_controls import FakePlayer
        from presets import EFFECT_KEYS
        player, manager = FakePlayer(), Mock()
        player.source_short = 'QQ 音乐'
        manager.enabled.side_effect = [False, True, False]
        panel = self.make_panel(player, manager)
        before = (player.position(), player.playing, list(player.seeks), panel.prefs.__dict__.copy())
        panel.qq_startup_checkbox.setChecked(True)
        manager.set_enabled.assert_called_with(True)
        self.assertIn('已开启', panel.qq_startup_status.text())
        panel.qq_startup_checkbox.setChecked(False)
        self.assertIn('未开启', panel.qq_startup_status.text())
        self.assertEqual(before, (player.position(), player.playing, player.seeks, panel.prefs.__dict__))
        self.assertNotIn('qq_autostart', EFFECT_KEYS)

    def test_failed_registration_reverts_checkbox_and_shows_persistent_error(self):
        from tests.test_controls import FakePlayer
        manager = Mock(enabled=Mock(return_value=False), set_enabled=Mock(side_effect=OSError('denied')))
        panel = self.make_panel(FakePlayer(), manager)
        panel.qq_startup_checkbox.setChecked(True)
        self.assertFalse(panel.qq_startup_checkbox.isChecked())
        self.assertIn('失败', panel.qq_startup_status.text())
        self.assertIn('denied', panel.qq_startup_status.text())

    def test_local_and_netease_do_not_show_qq_startup_card(self):
        from tests.test_controls import FakePlayer
        for source in ('本地音乐', '网易云'):
            player = FakePlayer()
            player.source_short = source
            panel = self.make_panel(player, None)
            self.assertTrue(panel.qq_startup_card.isHidden())

    def test_explicit_exit_bypasses_close_to_tray_but_normal_close_still_hides(self):
        from tests.test_controls import FakePlayer
        panel = self.make_panel(FakePlayer(), None)
        panel.tray_available = True
        event = Mock()
        panel.closeEvent(event)
        event.ignore.assert_called_once_with()
        application = Mock()
        with patch('controls.QApplication.instance', return_value=application):
            panel.quit_application()
        application.exit.assert_called_once_with(0)
        event = Mock()
        panel.closeEvent(event)
        event.accept.assert_called_once_with()
        event.ignore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
