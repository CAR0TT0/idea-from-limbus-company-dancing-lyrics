"""Installer waits, offline selection and cancellation before/inside file transactions."""
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtTest import QTest
import netease_install as ni
from installer_ui import InstallerWindow
import test_distribution as distribution

APP = distribution.APP
REAL_FRAMEWORK_VALIDATOR = ni.framework_valid


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class InstallerWaitTests(unittest.TestCase):
    setUp = distribution.InstallerTests.setUp
    plugin = distribution.InstallerTests.plugin
    install = distribution.InstallerTests.install

    def window(self):
        args = SimpleNamespace(uninstall_netease=False, client_directory=self.client,
            profile_directory=self.profile, framework_dll=self.framework, installer_elevated=False)
        window = InstallerWindow(self.installer, args)
        self.addCleanup(window.close)
        return window

    def test_selected_local_framework_never_downloads(self):
        with patch.object(ni.urllib.request, "urlopen", side_effect=AssertionError("No framework network")):
            self.assertTrue(self.install()["installed"])

    def test_missing_selected_local_file_reports_error_without_network(self):
        missing = self.framework.with_name("missing.dll")
        with patch.object(ni.urllib.request, "urlopen", side_effect=AssertionError("No framework network")):
            with self.assertRaisesRegex(ni.InstallError, "本地框架 DLL 不存在"):
                self.installer.install(self.client, self.profile, missing)
        self.assertFalse((self.client / "msimg32.dll").exists())
        self.assertFalse(self.plugin.exists())

    def test_oversized_local_file_is_rejected_without_loading_whole_file(self):
        with self.framework.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        with patch.object(type(self.framework), "read_bytes", side_effect=AssertionError("Do not load oversized file")):
            self.assertFalse(REAL_FRAMEWORK_VALIDATOR(self.framework))

    def test_cancellation_after_checks_leaves_client_and_token_untouched(self):
        stop = threading.Event()
        def progress(message):
            if "保存安装前备份" in message:
                stop.set()
        with self.assertRaises(ni.InstallCancelled):
            self.installer.install(self.client, self.profile, self.framework,
                operation=ni.InstallOperation(progress, stop.is_set))
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.client / "msimg32.dll").exists())
        self.assertFalse((self.installer.state / "netease-bridge.json").exists())

    def test_cancellation_during_commit_finishes_transaction(self):
        stop = threading.Event()
        def progress(message):
            if "正在安装联动" in message:
                stop.set()
        result = self.installer.install(self.client, self.profile, self.framework,
            operation=ni.InstallOperation(progress, stop.is_set))
        self.assertTrue(stop.is_set())
        self.assertTrue(result["installed"])
        self.assertTrue(self.plugin.exists())
        self.assertTrue((self.installer.state / "netease-install.json").exists())

    def test_slow_drip_has_total_download_deadline(self):
        elapsed = [0]
        class Slow(Response):
            def read(self, size):
                elapsed[0] += 10
                return b"part"
        with patch.object(ni.urllib.request, "urlopen", return_value=Slow()) as fetch:
            with self.assertRaisesRegex(ni.InstallError, "超过 45 秒"):
                ni.download_framework(self.framework, clock=lambda: elapsed[0])
        self.assertEqual(fetch.call_args.kwargs["timeout"], 5)
        self.assertLessEqual(elapsed[0], 50)

    def test_download_cancellation_before_and_after_read(self):
        stop = threading.Event()
        stop.set()
        operation = ni.InstallOperation(cancelled=stop.is_set)
        with patch.object(ni.urllib.request, "urlopen") as fetch:
            with self.assertRaises(ni.InstallCancelled):
                ni.download_framework(self.framework, operation=operation)
            fetch.assert_not_called()
        stop.clear()
        class CancelledRead(Response):
            def read(self, size):
                stop.set()
                return b"unused"
        with patch.object(ni.urllib.request, "urlopen", return_value=CancelledRead()):
            with self.assertRaises(ni.InstallCancelled):
                ni.download_framework(self.framework, operation=operation)

    def test_process_check_timeout_has_specific_message(self):
        with patch.object(ni.subprocess, "run", side_effect=subprocess.TimeoutExpired("tasklist", 15)):
            with self.assertRaisesRegex(ni.InstallError, "进程检查超过 15 秒"):
                ni.client_running()

    def start_blocked_check(self, window):
        entered, release = threading.Event(), threading.Event()
        def running():
            entered.set()
            release.wait(5)
            return False
        self.installer.running = running
        window.show()
        window.start()
        self.assertTrue(entered.wait(2))
        return release

    def finish(self, window, release):
        release.set()
        self.assertTrue(window.worker.wait(3000))
        APP.processEvents()

    def test_ui_cancel_keeps_window_and_allows_retry_without_blocking_wait(self):
        window = self.window()
        release = self.start_blocked_check(window)
        try:
            window.cancel_or_close()
            self.assertIn("正在取消", window.status.text())
        finally:
            self.finish(window, release)
        self.assertTrue(window.isVisible())
        self.assertIn("已取消", window.status.text())
        self.assertTrue(window.apply.isEnabled())
        self.assertFalse(self.plugin.exists())
        self.installer.running = lambda: False
        window.start()
        self.assertTrue(window.worker.wait(3000))
        with patch.object(window.worker, "wait", side_effect=AssertionError("GUI must not block")):
            APP.processEvents()
        self.assertIn("安装完成", window.status.text())

    def test_ui_close_and_escape_cancel_then_close_safely(self):
        for escape in (False, True):
            with self.subTest(escape=escape):
                window = self.window()
                release = self.start_blocked_check(window)
                try:
                    window.reject() if escape else window.close()
                    self.assertTrue(window.close_pending)
                    self.assertTrue(window.worker.isInterruptionRequested())
                    self.assertTrue(window.isVisible())
                finally:
                    self.finish(window, release)
                self.assertFalse(window.isVisible())
                self.assertFalse(self.plugin.exists())

    def test_ui_shows_stage_and_elapsed_while_check_waits(self):
        window = self.window()
        release = self.start_blocked_check(window)
        try:
            QTest.qWait(1050)
            self.assertIn("进程检查最多 15 秒", window.status.text())
            self.assertRegex(window.status.text(), r"已用 [1-9]\d* 秒")
            self.assertTrue(window.cancel.isEnabled())
        finally:
            window.cancel_or_close()
            self.finish(window, release)

    def test_ui_close_during_write_finishes_or_rolls_back_before_close(self):
        window = self.window()
        entered, release = threading.Event(), threading.Event()
        original = ni.atomic_write
        def write(path, data):
            if path == self.plugin:
                entered.set()
                release.wait(5)
            original(path, data)
        with patch.object(ni, "atomic_write", side_effect=write):
            window.show()
            window.start()
            self.assertTrue(entered.wait(2))
            try:
                window.close()
                self.assertIn("完成文件事务", window.status.text())
                self.assertTrue(window.isVisible())
            finally:
                self.finish(window, release)
        self.assertIsNone(window.worker.error)
        self.assertTrue(self.plugin.exists())
        self.assertFalse(window.isVisible())

    def test_wait_guides_explain_cancel_limits_and_actual_local_file_selection(self):
        for name in ("README.md", "NETEASE.md", "release/netease/使用说明.txt", "release/release-notes.md"):
            text = (distribution.ROOT / name).read_text(encoding="utf-8")
            for phrase in ("取消操作", "45 秒", "5 秒", "15 秒", "文件路径", "回滚"):
                self.assertIn(phrase, text)
