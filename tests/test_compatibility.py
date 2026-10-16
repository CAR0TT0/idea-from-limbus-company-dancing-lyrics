"""Compatibility policy, scoped consent, reversible installation and runtime diagnostics."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from PySide6.QtWidgets import QApplication, QMessageBox
import compatibility as cp
import test_distribution as distribution
from installer_ui import InstallerWindow
import netease_install as ni
from netease import validate_snapshot
from test_netease import packet
from qqmusic import media_diagnostic

ROOT = Path(__file__).resolve().parents[1]
APP = QApplication.instance() or QApplication([])


def policy(revision=2, status="verified"):
    return {"schema_version": 1, "revision": revision, "netease": [
        {"version": "3.1.42.123456", "status": status, "reason": "测试夹具，不代表真实兼容"}]}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_bundled_versions_match_release_and_unknown_is_not_promoted(self):
        store = cp.CompatibilityPolicy(self.directory)
        self.assertEqual({e["version"] for e in store.data["netease"]}, set(ni.CLIENT_VERSIONS))
        self.assertEqual(store.assess("3.1.42.123456").status, "unverified")
        for version in ni.CLIENT_VERSIONS:
            self.assertEqual(store.assess(version).status, "verified")

    def test_refresh_persists_and_restart_reuses_valid_cache(self):
        store = cp.CompatibilityPolicy(self.directory, fetcher=policy, clock=lambda: 1000)
        self.assertTrue(store.refresh())
        self.assertEqual(store.assess("3.1.42.123456").status, "verified")
        restarted = cp.CompatibilityPolicy(self.directory, clock=lambda: 1001)
        self.assertEqual(restarted.assess("3.1.42.123456").status, "verified")
        self.assertEqual(restarted.assess(ni.CLIENT_VERSION).status, "verified")

    def test_blocked_override_expires_without_promoting_unverified_versions(self):
        data = policy(status="blocked")
        data["netease"].append({"version": ni.CLIENT_VERSION, "status": "blocked", "reason": "fixture"})
        store = cp.CompatibilityPolicy(self.directory, fetcher=lambda: data, clock=lambda: 1000)
        self.assertTrue(store.refresh())
        self.assertEqual(store.assess(ni.CLIENT_VERSION).status, "blocked")
        expired = cp.CompatibilityPolicy(self.directory, clock=lambda: 1000 + cp.CACHE_AGE + 1)
        self.assertEqual(expired.assess(ni.CLIENT_VERSION).status, "blocked")
        self.assertEqual(expired.assess("3.1.42.123456").status, "blocked")
        data["netease"][0]["status"] = "verified"
        self.assertTrue(store.refresh())
        expired = cp.CompatibilityPolicy(self.directory, clock=lambda: 1000 + cp.CACHE_AGE + 1)
        self.assertEqual(expired.assess("3.1.42.123456").status, "unverified")

    def test_network_corruption_and_downgrade_keep_previous_file_and_policy(self):
        store = cp.CompatibilityPolicy(self.directory, fetcher=policy)
        self.assertTrue(store.refresh())
        original = store.path.read_bytes()
        for fetcher in (lambda: policy(1), lambda: {"schema_version": 99},
                        lambda: (_ for _ in ()).throw(OSError("offline"))):
            store.fetcher = fetcher
            self.assertFalse(store.refresh())
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(store.assess("3.1.42.123456").status, "verified")
        store.path.write_text("broken", encoding="utf-8")
        self.assertEqual(cp.CompatibilityPolicy(self.directory).assess("3.1.42.123456").status, "unverified")
        self.assertEqual(store.path.read_text(), "broken")

    def test_policy_rejects_code_urls_duplicates_and_bad_types(self):
        bad = []
        for key, value in (("version", "../../evil"), ("status", "force"), ("reason", 123)):
            item = policy()
            item["netease"][0][key] = value
            bad.append(item)
        item = policy()
        item["netease"][0]["script"] = "https://evil.test/code.js"
        bad.extend((item, policy(True), {**policy(), "schema_version": True},
                    {**policy(), "netease": policy()["netease"] * 2}))
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                cp.validate_policy(data)

    def test_https_read_is_bounded_and_redirects_rejected(self):
        with patch.object(cp.urllib.request, "urlopen") as opening:
            response = opening.return_value.__enter__.return_value
            response.geturl.return_value = cp.POLICY_URL
            response.read.return_value = json.dumps(policy()).encode()
            self.assertEqual(cp.fetch_policy()["revision"], 2)
            self.assertEqual(opening.call_args.kwargs["timeout"], 5)
            response.read.assert_called_with(cp.MAX_BYTES + 1)
            response.geturl.return_value = "http://other.test/file"
            with self.assertRaises(ValueError):
                cp.fetch_policy()
            response.geturl.return_value = cp.POLICY_URL
            response.read.return_value = b"x" * (cp.MAX_BYTES + 1)
            with self.assertRaises(ValueError):
                cp.fetch_policy()

    def test_grant_is_scoped_and_packet_cannot_authorize_itself(self):
        config = {"compatibility": {"version": "3.1.42.123456", "status": "unverified"}}
        family = cp.bridge_client(config)
        self.assertEqual(family, "3.1.42")
        new = packet(client=family, compatibility=config["compatibility"])
        with self.assertRaises(ValueError):
            validate_snapshot(new)
        self.assertEqual(validate_snapshot(new, family)["client"], family)
        with self.assertRaises(ValueError):
            validate_snapshot(packet(client="3.1.43"), family)
        self.assertIsNone(cp.bridge_client({}))
        self.assertIsNone(cp.bridge_client({"compatibility": {"status": "blocked", "version": "3.1.42.123456"}}))


class CompatibilityInstallTests(unittest.TestCase):
    setUp = distribution.InstallerTests.setUp
    plugin = distribution.InstallerTests.plugin
    install = distribution.InstallerTests.install

    def test_unverified_needs_consent_and_generates_scoped_plugin_grant(self):
        self.installer.version_reader = lambda _: "3.1.42.123456"
        with self.assertRaisesRegex(ni.InstallError, "实验性安装"):
            self.install()
        self.assertFalse(self.plugin.exists())
        result = self.installer.install(self.client, self.profile, self.framework, experimental=True)
        self.assertEqual(result["compatibility"]["status"], "unverified")
        with zipfile.ZipFile(self.plugin) as archive:
            self.assertEqual(json.loads(archive.read("manifest.json"))["ncm-version-req"], ">= 3.1.42 < 3.1.43")
            self.assertEqual(cp.bridge_client(json.loads(archive.read("bridge-config.json"))), "3.1.42")

    def test_online_verified_new_version_installs_without_experimental_flag(self):
        self.installer.policy = cp.CompatibilityPolicy(self.app / ".state", bundled=policy())
        self.installer.version_reader = lambda _: "3.1.42.123456"
        self.assertEqual(self.install()["compatibility"]["status"], "verified")

    def test_experimental_never_overrides_known_block_running_architecture_or_hash(self):
        self.installer.version_reader = lambda _: "3.1.42.123456"
        self.installer.policy = cp.CompatibilityPolicy(self.app / ".state", bundled=policy(status="blocked"))
        with self.assertRaisesRegex(ni.InstallError, "已知不兼容"):
            self.installer.install(self.client, self.profile, self.framework, experimental=True)
        self.installer.policy = cp.CompatibilityPolicy(self.app / ".state")
        self.installer.running = lambda: True
        with self.assertRaisesRegex(ni.InstallError, "完全退出"):
            self.installer.install(self.client, self.profile, self.framework, experimental=True)
        self.installer.running = lambda: False
        path = self.client / "cloudmusic.exe"
        original = path.read_bytes()
        path.write_bytes(original[:68] + b"\x4c\x01" + original[70:])
        with self.assertRaisesRegex(ni.InstallError, "64 位"):
            self.installer.install(self.client, self.profile, self.framework, experimental=True)
        path.write_bytes(original)
        self.framework.write_bytes(b"invalid")
        with self.assertRaisesRegex(ni.InstallError, "SHA-256"):
            self.installer.install(self.client, self.profile, self.framework, experimental=True)
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.client / "msimg32.dll").exists())

    def test_restore_first_install_removes_only_created_files_and_preserves_settings(self):
        personal = self.installer.state / "settings.ini"
        personal.parent.mkdir()
        personal.write_bytes(b"my preferences")
        self.install()
        self.assertTrue(self.installer.restore(self.client, self.profile)["removedCreatedFramework"])
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.client / "msimg32.dll").exists())
        self.assertEqual(personal.read_bytes(), b"my preferences")

    def test_restore_update_restores_old_plugin_token_receipt_and_shared_framework(self):
        self.install()
        paths = [self.plugin, self.installer.state / "netease-bridge.json", self.installer.state / "netease-install.json"]
        originals = {path: path.read_bytes() for path in paths}
        self.installer.version_reader = lambda _: "3.1.42.123456"
        self.installer.install(self.client, self.profile, self.framework, experimental=True)
        self.assertFalse(self.installer.restore(self.client, self.profile)["removedCreatedFramework"])
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual((self.client / "msimg32.dll").read_bytes(), self.framework.read_bytes())

    def test_restore_refuses_changed_files_other_installation_and_shared_plugins(self):
        self.install()
        before = self.plugin.read_bytes()
        self.plugin.write_bytes(b"modified by somebody")
        with self.assertRaisesRegex(ni.InstallError, "文件已变化"):
            self.installer.restore(self.client, self.profile)
        self.assertEqual(self.plugin.read_bytes(), b"modified by somebody")
        self.plugin.write_bytes(before)
        with self.assertRaisesRegex(ni.InstallError, "目录与安装记录不符"):
            self.installer.restore(self.client, self.profile / "other")
        (self.plugin.parent / "another.plugin").write_bytes(b"shared")
        with self.assertRaisesRegex(ni.InstallError, "共用插件"):
            self.installer.restore(self.client, self.profile)
        self.assertEqual(self.plugin.read_bytes(), before)

    def test_restore_failure_rolls_back_to_installed_state(self):
        self.install()
        paths = [self.plugin, self.installer.state / "netease-bridge.json", self.installer.state / "netease-install.json"]
        originals = {path: path.read_bytes() for path in paths}
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if path == self.client / "msimg32.dll":
                raise PermissionError("fixture")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail), self.assertRaises(PermissionError):
            self.installer.restore(self.client, self.profile)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_restore_rejects_corrupt_backup_metadata_before_changes(self):
        result = self.install()
        backup = self.installer.state / "netease-backups" / result["backupId"] / "backup.json"
        original = self.plugin.read_bytes()
        backup.write_text('{"records":[null,null,null]}', encoding="utf-8")
        with self.assertRaisesRegex(ni.InstallError, "备份"):
            self.installer.restore(self.client, self.profile)
        self.assertEqual(self.plugin.read_bytes(), original)

    def window(self):
        args = SimpleNamespace(uninstall_netease=False, client_directory=self.client,
                               profile_directory=self.profile, framework_dll=self.framework, installer_elevated=False)
        window = InstallerWindow(self.installer, args)
        self.addCleanup(window.close)
        return window

    def test_ui_requires_explicit_confirmation_each_time_and_uses_worker(self):
        self.installer.version_reader = lambda _: "3.1.42.123456"
        window = self.window()
        window.start()
        self.assertIsNone(window.worker)
        self.assertIn("实验性安装", window.status.text())
        window.experimental.setChecked(True)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            window.start()
        self.assertFalse(self.plugin.exists())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            window.start()
        self.assertTrue(window.worker.wait(10000))
        APP.processEvents()
        self.assertIn("安装完成", window.status.text())

    def test_ui_shows_blocked_and_refresh_failure_without_enabling_install(self):
        self.installer.version_reader = lambda _: "3.1.42.123456"
        self.installer.policy = cp.CompatibilityPolicy(self.app / ".state", bundled=policy(status="blocked"),
            fetcher=lambda: (_ for _ in ()).throw(OSError("offline")))
        window = self.window()
        self.assertFalse(window.apply.isEnabled())
        self.assertIn("已知不兼容", window.compatibility_status.text())
        window.refresh_policy()
        self.assertTrue(window.worker.wait(10000))
        APP.processEvents()
        self.assertIn("更新失败", window.status.text())
        self.assertFalse(window.apply.isEnabled())


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_qq_diagnostics_distinguish_session_and_opened_without_version_gate(self):
        self.assertIn("尚未发现", media_diagnostic(None))
        self.assertIn("等待歌曲与有效进度", media_diagnostic({"source": "QQMusic.exe", "state": "Opened"}))
        self.assertIn("没有播放歌曲", media_diagnostic({"source": "QQMusic.exe", "state": "Stopped"}))

    @unittest.skipUnless(shutil.which("node"), "Node.js for capability probes")
    def test_js_capability_selection_and_scoped_runtime_consent(self):
        result = subprocess.run(["node", str(ROOT / "tests/test_compatibility.cjs")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_guides_explain_experimental_scope_rollback_and_online_fallback(self):
        for name in ("README.md", "NETEASE.md", "release/netease/使用说明.txt"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for phrase in ("实验性安装", "未验证", "恢复上次安装", "联网刷新兼容列表", "不能保证"):
                self.assertIn(phrase, text)
        guide = (ROOT / "docs/客户端兼容维护.md").read_text(encoding="utf-8")
        for phrase in ("revision", "7 天", "blocked", "不下载或执行代码", "3.1.42.123456"):
            self.assertIn(phrase, guide)
        manifest = json.loads((ROOT / "plugins/netease/manifest.json").read_text(encoding="utf-8"))
        netease_guide = (ROOT / "NETEASE.md").read_text(encoding="utf-8")
        self.assertIn(f"插件版本为 {manifest['version']}", netease_guide)
        self.assertIn("随 QQ 音乐自动启动（v0.7.0-beta.5 起）", (ROOT / "QQMUSIC.md").read_text(encoding="utf-8"))
