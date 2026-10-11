"""Opt-in, per-user QQ startup watcher. No Qt, audio, or playback controls.

The Windows Run registration is authoritative and independent of effect presets.
"""
import ctypes
from ctypes import wintypes
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CityEchoesQQMusic"
PANEL_PROPERTY = "CityEchoes.QQMusic.Panel"


def user_key():
    identity = os.environ.get("USERPROFILE", str(Path.home())).casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def object_name(kind):
    return "Local\\CityEchoes.QQ." + user_key() + "." + kind


def _kernel():
    return ctypes.WinDLL("kernel32", use_last_error=True)


class NamedMutex:
    def __init__(self, name):
        kernel = _kernel()
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        self.handle = kernel.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.acquired = ctypes.get_last_error() != 183

    def close(self):
        if self.handle:
            kernel = _kernel()
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self.handle)
            self.handle = None


def companion_running():
    kernel = _kernel()
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenMutexW(0x100000, False, object_name("app"))
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error not in (0, 2):
        raise ctypes.WinError(error)
    return False


class ShowPanelEvent:
    def __init__(self):
        kernel = _kernel()
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        self.handle = kernel.CreateEventW(None, False, False, object_name("show"))
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def signal(self):
        kernel = _kernel()
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        if not kernel.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def consume(self):
        kernel = _kernel()
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return kernel.WaitForSingleObject(self.handle, 0) == 0

    def close(self):
        if self.handle:
            kernel = _kernel()
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self.handle)
            self.handle = None


def mark_panel(hwnd):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    if not user.SetPropW(hwnd, PANEL_PROPERTY, 1):
        raise ctypes.WinError(ctypes.get_last_error())


def qq_processes():
    """Stable QQ root process IDs, including windows minimized to the tray."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_void_p),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("filename", wintypes.WCHAR * 260)]
    kernel = _kernel()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    found = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.filename.casefold() == "qqmusic.exe":
                found[entry.pid] = entry.parent
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error not in (0, 18):
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(snapshot)
    return frozenset(pid for pid, parent in found.items() if parent not in found)


def program_command(root=None, frozen=None, executable=None):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = Path(executable or sys.executable).resolve()
    root = Path(root or (executable.parent if frozen else Path(__file__).parent)).resolve()
    if frozen:
        return [str(executable)]
    pythonw = executable.with_name("pythonw.exe")
    return [str(pythonw if pythonw.is_file() else executable), str(root / "main.py")]


def spawn_hidden(command, cwd):
    return subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, shell=False)


class RunRegistry:
    def read(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = winreg.QueryValueEx(key, RUN_VALUE)
                return value if kind == winreg.REG_SZ and isinstance(value, str) else None
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


class QQStartupManager:
    def __init__(self, root=None, command=None, registry=None, launcher=None):
        self.command = command or program_command(root)
        self.root = Path(root or (Path(self.command[0]).parent if getattr(sys, "frozen", False)
                                  else Path(__file__).parent)).resolve()
        self.registry = registry or RunRegistry()
        self.launcher = launcher or spawn_hidden
        self.watch_command = [*self.command, "--watch-qqmusic"]
        self.registration = subprocess.list2cmdline(self.watch_command)

    def enabled(self):
        return self.registry.read() == self.registration

    def set_enabled(self, enabled):
        if sys.platform != "win32":
            raise OSError("随 QQ 音乐启动仅支持 Windows。")
        previous = self.registry.read()
        if not enabled:
            if previous == self.registration:
                self.registry.remove()
            return
        if not all(Path(path).is_file() for path in self.command):
            raise OSError("启动程序已移动或缺失，请从完整程序目录重新开启。")
        if len(self.registration.encode("utf-16-le")) // 2 > 260:
            raise OSError("程序路径过长，Windows 登录启动命令限 260 字符；请移至较短目录后重试。")
        self.registry.write(self.registration)
        try:
            self.launcher(self.watch_command, self.root)
        except OSError:
            if previous is None:
                self.registry.remove()
            else:
                self.registry.write(previous)
            raise


class WatcherState:
    """Respect manual exit until the next QQ session; bounded retries on spawn failure."""
    def __init__(self):
        self.session = frozenset()
        self.handled = False
        self.retries = 0
        self.retry_at = 0.0

    def step(self, processes, running, now, launch):
        session = frozenset(processes)
        if session != self.session:
            self.session, self.handled, self.retries, self.retry_at = session, False, 0, 0.0
        if not session:
            return
        if running:
            self.handled = True
        if self.handled or self.retries >= 3 or now < self.retry_at:
            return
        try:
            launch()
        except OSError:
            self.retries += 1
            self.retry_at = now + 10
            raise
        self.handled = True


def run_watcher(manager=None, poll=2.0):
    if sys.platform != "win32":
        return 1
    manager = manager or QQStartupManager()
    digest = hashlib.sha256(manager.registration.encode("utf-8")).hexdigest()[:16]
    mutex = NamedMutex(object_name("watch." + digest))
    if not mutex.acquired:
        mutex.close()
        return 0
    state_dir = manager.root / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("qq-autostart")
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(state_dir / "qq-autostart.log", maxBytes=262144,
                                 backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    state = WatcherState()
    log.info("Watcher started; pid=%s", os.getpid())
    try:
        while manager.enabled():
            try:
                before = state.handled
                state.step(qq_processes(), companion_running(), time.monotonic(),
                           lambda: manager.launcher([*manager.command, "--qqmusic", "--background"], manager.root))
                if state.handled and not before:
                    log.info("QQ session handled")
            except OSError as error:
                log.warning("Startup check failed: %s", error)
            time.sleep(poll)
    except OSError as error:
        log.error("Startup registration cannot be read: %s", error)
        return 1
    finally:
        log.info("Watcher stopped")
        log.removeHandler(handler)
        handler.close()
        mutex.close()
    return 0
