"""Optional per-user KuGou watcher; never depends on the client install path."""
import ctypes
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

from qqmusic_startup import NamedMutex, WatcherState, program_command, spawn_hidden, user_key

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CityEchoesKugou"


def object_name(kind):
    return "Local\\CityEchoes.Kugou." + user_key() + "." + kind


def state_directory():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DancingLyrics-Kugou" / ".state"


class PanelSignal:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]; k.CreateEventW.restype = w.HANDLE
        k.SetEvent.argtypes = [w.HANDLE]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = k.CreateEventW(None, False, False, object_name("show"))
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def signal(self):
        self.kernel.SetEvent(self.handle)

    def consume(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Registry:
    def read(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = winreg.QueryValueEx(key, RUN_VALUE)
                return value if kind == winreg.REG_SZ else None
        except FileNotFoundError:
            return None

    def write(self, value):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)

    def remove(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            pass


class KugouStartupManager:
    def __init__(self, command=None, registry=None, launcher=None):
        self.command = command or program_command()
        self.root = Path(self.command[-1] if len(self.command) > 1 else self.command[0]).parent
        self.registry = registry or Registry()
        self.launcher = launcher or spawn_hidden
        self.watch_command = [*self.command, "--watch-kugou"]
        self.registration = subprocess.list2cmdline(self.watch_command)

    def enabled(self):
        return self.registry.read() == self.registration

    def set_enabled(self, enabled):
        previous = self.registry.read()
        if not enabled:
            if previous == self.registration:
                self.registry.remove()
            return
        if not all(Path(part).is_file() for part in self.command):
            raise OSError("程序已移动或文件缺失，请在新目录重新开启自动联动。")
        if len(self.registration.encode("utf-16le")) // 2 > 260:
            raise OSError("程序路径过长，请使用较短目录。")
        self.registry.write(self.registration)
        try:
            self.launcher(self.watch_command, self.root)
        except OSError:
            self.registry.remove() if previous is None else self.registry.write(previous)
            raise


def companion_running():
    mutex = NamedMutex(object_name("app"))
    running = not mutex.acquired
    mutex.close()
    return running


def kugou_processes():
    from process_audio import window_process_pid
    pid = window_process_pid("kugou.exe")
    return frozenset((pid,)) if pid else frozenset()


def run_watcher(manager=None, poll=2):
    manager = manager or KugouStartupManager()
    digest = hashlib.sha256(manager.registration.encode("utf-8")).hexdigest()[:16]
    mutex = NamedMutex(object_name("watch." + digest))
    if not mutex.acquired:
        mutex.close()
        return 0
    state = WatcherState()
    try:
        while manager.enabled():
            try:
                state.step(kugou_processes(), companion_running(), time.monotonic(),
                           lambda: manager.launcher([*manager.command, "--kugou", "--background"], manager.root))
            except OSError:
                pass
            time.sleep(poll)
    finally:
        mutex.close()
    return 0
