"""Install only per-user shortcuts; never open the music client's directory."""
import os
from pathlib import Path
import subprocess


def shortcut_script(remove=False):
    return r'''
$ErrorActionPreference = 'Stop'
$appTarget = [IO.Path]::GetFullPath($env:CITYECHOES_KUGOU_EXE)
if (-not (Test-Path -LiteralPath $appTarget -PathType Leaf)) { throw 'The complete application directory is required.' }
$shortcutShell = New-Object -ComObject WScript.Shell
$shortcutFolders = @($shortcutShell.SpecialFolders.Item('Desktop'), $shortcutShell.SpecialFolders.Item('Programs'))
foreach ($shortcutFolder in $shortcutFolders) {
    $shortcutPath = Join-Path $shortcutFolder 'DancingLyrics-Kugou.lnk'
    if ($env:CITYECHOES_KUGOU_REMOVE -eq '1') {
        if (Test-Path -LiteralPath $shortcutPath) {
            $existingShortcut = $shortcutShell.CreateShortcut($shortcutPath)
            if ($existingShortcut.TargetPath -ieq $appTarget) { Remove-Item -LiteralPath $shortcutPath }
        }
    } else {
        $appShortcut = $shortcutShell.CreateShortcut($shortcutPath)
        $appShortcut.TargetPath = $appTarget
        $appShortcut.Arguments = '--kugou'
        $appShortcut.WorkingDirectory = [IO.Path]::GetDirectoryName($appTarget)
        $appShortcut.IconLocation = $appTarget + ',0'
        $appShortcut.Save()
    }
}
'''


def install(root, remove=False):
    root = Path(root).resolve()
    exe = root / "DancingLyrics-Kugou.exe"
    if not exe.is_file() or not (root / "_internal").is_dir():
        raise OSError("请完整解压酷狗版压缩包后再运行安装入口。")
    env = os.environ.copy()
    env["CITYECHOES_KUGOU_EXE"] = str(exe)
    env["CITYECHOES_KUGOU_REMOVE"] = "1" if remove else "0"
    if remove:
        from kugou_startup import KugouStartupManager
        KugouStartupManager().set_enabled(False)
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", shortcut_script(remove)],
                            env=env, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError("快捷方式创建或移除失败，请确认桌面和开始菜单目录可写。")


def run_installer(root, remove=False):
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    try:
        install(root, remove)
    except (OSError, subprocess.TimeoutExpired) as error:
        QMessageBox.critical(None, "酷狗联动", str(error))
        return 1
    message = ("已移除本目录的快捷方式和自动联动设置，个人歌词效果设置已保留。\n正在运行时，请从托盘退出。"
               if remove else "已创建桌面和开始菜单快捷方式。\n打开酷狗并播放歌曲，再打开 DancingLyrics-Kugou 即可。\n无需管理员权限；酷狗更新后无需重新安装。")
    QMessageBox.information(None, "酷狗联动", message)
    return 0
