"""Small native installer window, also available from the frozen application."""
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time
from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QLabel, QLineEdit,
                              QPushButton, QFileDialog, QHBoxLayout, QCheckBox, QMessageBox, QScrollArea, QWidget)
from app_info import APP_NAME, APP_VERSION
from netease_install import NeteaseInstaller, find_client, default_profile, atomic_write, InstallOperation


class InstallWorker(QThread):
    progress = Signal(str)

    def __init__(self, installer, action, client, profile, framework, parent=None, *, experimental=False):
        super().__init__(parent)
        self.installer, self.action = installer, action
        self.client, self.profile, self.framework = client, profile, framework
        self.experimental = experimental
        self.result = self.error = None
        self.operation = InstallOperation(self.progress.emit, self.isInterruptionRequested)

    def run(self):
        try:
            if self.action == "refresh":
                self.operation.stage("正在刷新在线兼容列表（单次网络等待最多 5 秒）")
                result = self.installer.policy.refresh()
                self.operation.check()
            elif self.action == "restore":
                result = self.installer.restore(self.client, self.profile, operation=self.operation)
            elif self.action == "install":
                result = self.installer.install(self.client, self.profile, self.framework, experimental=self.experimental,
                                                operation=self.operation)
            else:
                result = self.installer.uninstall(self.client, self.profile, operation=self.operation)
            self.result = result
        except Exception as error:
            self.error = error


class InstallerWindow(QDialog):
    def __init__(self, installer, args):
        super().__init__()
        self.installer, self.args = installer, args
        self.worker = None
        self.close_pending = False
        self.cancel_pending = False
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self.show_elapsed)
        self.action = "restore" if getattr(args, "restore_netease", False) else "uninstall" if args.uninstall_netease else "install"
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} · 网易云联动")
        self.resize(640, 680)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        heading = QLabel({"install": "安装网易云联动", "uninstall": "卸载网易云联动", "restore": "恢复上次安装"}[self.action])
        heading.setStyleSheet("font-size:22px;font-weight:bold")
        layout.addWidget(heading)
        tip = QLabel("自动读取当前客户端版本，并在连接后检测播放与歌词接口。\n安装、卸载或恢复前请完全退出网易云（包括托盘）。\n请将完整程序放在长期保留且可写的目录，安装后不要移动它。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        try:
            saved = json.loads((installer.state / "netease-install.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        self.client = QLineEdit(str(args.client_directory or saved.get("client") or find_client() or ""))
        self.add_path(layout, "网易云目录", self.client, True)
        self.compatibility_status = QLabel()
        self.compatibility_status.setWordWrap(True)
        self.compatibility_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.compatibility_status)
        self.refresh = QPushButton("联网刷新兼容列表")
        self.refresh.clicked.connect(self.refresh_policy)
        layout.addWidget(self.refresh)
        self.experimental = QCheckBox("实验性安装（未验证版本）")
        self.experimental.setChecked(getattr(args, "experimental_netease", False))
        self.experimental.setToolTip("仅跳过版本白名单；保留 x64、哈希和文件冲突检查。每次安装仍需确认。")
        self.experimental.setVisible(self.action == "install")
        layout.addWidget(self.experimental)
        self.profile = QLineEdit(str(args.profile_directory or saved.get("profile") or default_profile()))
        self.profile.setToolTip("建议保留默认 C:\\betterncm；插件数据目录请使用英文路径。歌词程序目录可含中文。")
        self.add_path(layout, "BetterNCM 数据目录", self.profile, True)
        self.framework = QLineEdit(str(args.framework_dll or ""))
        self.framework.setPlaceholderText("可选；离线安装请点“选择”，确认此处显示 DLL 文件路径")
        if not args.uninstall_netease:
            self.add_path(layout, "本地框架 DLL（可选）", self.framework, False)
        self.status = QLabel("卸载仅移除本插件，保留个人设置、字体、预设及共用框架。" if args.uninstall_netease else "准备就绪。安装失败可在此查看原因并重试。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.apply = QPushButton({"install": "安装联动", "uninstall": "卸载联动", "restore": "恢复上次安装"}[self.action])
        self.apply.clicked.connect(lambda: self.start())
        outer.addWidget(self.apply)
        self.restore_button = QPushButton("恢复上次安装")
        self.restore_button.setVisible(self.action == "install")
        self.restore_button.clicked.connect(lambda: self.start("restore"))
        outer.addWidget(self.restore_button)
        self.cancel = QPushButton("关闭")
        self.cancel.clicked.connect(self.cancel_or_close)
        outer.addWidget(self.cancel)
        self.admin = QPushButton("以管理员身份重试")
        self.admin.clicked.connect(self.elevate)
        self.admin.hide()
        layout.addWidget(self.admin)
        self.setStyleSheet(self.styleSheet() + "QPushButton{padding:8px} QLineEdit{padding:6px}")
        self.client.textChanged.connect(self.detect)
        self.detect()
        screen = QApplication.primaryScreen()
        if screen:
            self.resize(min(640, screen.availableGeometry().width() - 40), min(680, screen.availableGeometry().height() - 60))

    def detect(self):
        try:
            support = self.installer.assess_client(self.client.text())
            message = f"网易云 {support.version} · {support.label}\n{support.reason}"
            allowed = support.status != "blocked"
        except (OSError, ValueError) as error:
            message, allowed = str(error), False
        self.compatibility_status.setText(message + "\n" + self.installer.policy.message)
        if self.worker is None or not self.worker.isRunning():
            self.apply.setEnabled(allowed or self.action != "install")

    def refresh_policy(self):
        if self.worker and self.worker.isRunning():
            return
        self._run("refresh")

    def add_path(self, layout, title, editor, directory):
        layout.addWidget(QLabel(title))
        row = QHBoxLayout()
        row.addWidget(editor)
        browse = QPushButton("选择")
        def select():
            value = (QFileDialog.getExistingDirectory(self, title, editor.text()) if directory else
                     QFileDialog.getOpenFileName(self, title, editor.text(), "DLL (*.dll)")[0])
            if value:
                editor.setText(value)
        browse.clicked.connect(select)
        row.addWidget(browse)
        layout.addLayout(row)

    def start(self, action=None):
        if self.worker and self.worker.isRunning():
            return
        action = action or self.action
        if not self.client.text().strip() or not self.profile.text().strip():
            self.status.setText("请选择网易云目录及 BetterNCM 数据目录。")
            return
        try:
            if action == "install":
                support = self.installer.assess_client(self.client.text())
                if support.status == "blocked" or (support.status == "unverified" and not self.experimental.isChecked()):
                    self.installer.check_client(self.client.text())
                if support.status == "unverified" and QMessageBox.question(self, "实验性安装确认",
                        f"网易云 {support.version} 尚未验证。BetterNCM 不匹配可能导致网易云无法启动。\n"
                        "本次会保存安装备份；出现问题请完全退出网易云后选择“恢复上次安装”。\n继续本次安装？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
            if action == "restore" and QMessageBox.question(self, "恢复上次安装",
                    "恢复本目录最近一次安装前的插件和连接配置，仅移除该次新建的框架。\n"
                    "个人设置、字体及预设保留。继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self._run(action)

    def _run(self, action):
        self.pending_action = action
        self.close_pending = self.cancel_pending = False
        self.started_at = time.monotonic()
        self.phase = "正在准备操作"
        self.apply.setEnabled(False)
        self.admin.hide()
        self.client.setEnabled(False)
        self.profile.setEnabled(False)
        self.framework.setEnabled(False)
        self.experimental.setEnabled(False)
        self.refresh.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.cancel.setText("取消操作")
        self.cancel.setEnabled(True)
        self.show_elapsed()
        self.worker = InstallWorker(self.installer, action,
                                    Path(self.client.text()), Path(self.profile.text()),
                                    Path(self.framework.text()) if self.framework.text() else None, self,
                                    experimental=self.experimental.isChecked())
        self.worker.progress.connect(self.set_phase)
        self.worker.finished.connect(lambda worker=self.worker: self.complete(worker.result, worker.error))
        self.elapsed_timer.start()
        self.worker.start()

    def set_phase(self, message):
        if not self.cancel_pending:
            self.phase = message
            self.show_elapsed()

    def show_elapsed(self):
        self.status.setText(f"{self.phase}\n已用 {int(time.monotonic() - self.started_at)} 秒")

    def cancel_or_close(self):
        if self.worker and self.worker.isRunning():
            self.cancel_pending = True
            self.worker.requestInterruption()
            self.cancel.setEnabled(False)
            self.phase = ("正在完成文件事务，无法中途打断；请稍候" if self.worker.operation.committing else
                          "正在取消，等待当前检查返回（进程检查最多 15 秒，网络单次等待最多 5 秒）")
            self.show_elapsed()
        else:
            self.close()

    def complete(self, result, error):
        self.elapsed_timer.stop()
        self.cancel.setText("关闭")
        self.cancel.setEnabled(True)
        self.apply.setEnabled(True)
        for editor in (self.client, self.profile, self.framework):
            editor.setEnabled(True)
        self.experimental.setEnabled(True)
        self.refresh.setEnabled(True)
        self.restore_button.setEnabled(True)
        if error:
            self.status.setText(f"{self.phase}\n{error}" + ("\n目录需要写入权限，请点击管理员重试。" if isinstance(error, PermissionError) else ""))
            self.admin.setVisible(isinstance(error, PermissionError) and not self.args.installer_elevated)
        else:
            self.status.setText({"refresh": self.installer.policy.message, "restore": "恢复完成。请重新启动网易云。个人设置已保留。",
                "uninstall": "卸载完成。请重新启动网易云。个人设置和共用框架已保留。",
                "install": "安装完成！请重新启动网易云，在播放栏点击“都市回响”，右键或点击“设置”调整效果。"}[self.pending_action])
        self.detect()
        if self.close_pending:
            self.close()

    def elevate(self):
        arguments = [{"install": "--install-netease", "uninstall": "--uninstall-netease", "restore": "--restore-netease"}[self.pending_action],
                     "--installer-elevated", "--client-directory", self.client.text(),
                     "--profile-directory", self.profile.text()]
        if self.framework.text():
            arguments += ["--framework-dll", self.framework.text()]
        if self.experimental.isChecked():
            arguments += ["--experimental-netease"]
        if not getattr(sys, "frozen", False):
            arguments.insert(0, str(Path(__file__).parent / "main.py"))
        shell = ctypes.windll.shell32.ShellExecuteW
        shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell.restype = ctypes.c_void_p
        result = shell(None, "runas", sys.executable, subprocess.list2cmdline(arguments), str(self.installer.app_dir), 1)
        if result and result > 32:
            self.accept()
        else:
            self.status.setText("管理员授权未完成。你可以再次重试，或将程序解压到个人可写目录。")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.close_pending = True
            self.cancel_or_close()
            event.ignore()
        else:
            event.accept()

    def reject(self):
        # QDialog Escape must follow the same safe cancellation path as the close button.
        self.close()


def run_installer(args, app_dir, templates):
    installer = NeteaseInstaller(app_dir, templates)
    # Explicit automation mode for isolated native verification; normal BAT always shows the window.
    if args.installer_report:
        try:
            if not args.client_directory or not args.profile_directory:
                raise ValueError("自动验证需要明确指定两个目录。")
            if getattr(args, "restore_netease", False):
                result = installer.restore(args.client_directory, args.profile_directory)
            elif args.uninstall_netease:
                result = installer.uninstall(args.client_directory, args.profile_directory)
            else:
                result = installer.install(args.client_directory, args.profile_directory, args.framework_dll,
                                           experimental=getattr(args, "experimental_netease", False))
            report, code = {"passed": True, "result": result}, 0
        except Exception as error:
            report, code = {"passed": False, "error": str(error)}, 1
        atomic_write(args.installer_report, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        return code
    app = QApplication(sys.argv[:1])
    from controls import app_icon
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(True)
    window = InstallerWindow(installer, args)
    window.show()
    if window.action == "install":
        QTimer.singleShot(0, window.refresh_policy)
    return app.exec()
