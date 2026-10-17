"""Native lyric visibility lease and a bounded pipe for its owning GUI."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
import time


PROPERTY = "DancingLyrics.KuGou.NativeLyrics.Owner.v1"
LYRIC_TITLE = "桌面歌词 - 酷狗音乐"


class Windows:
    def __init__(self):
        self.user = u = c.WinDLL("user32", use_last_error=True)
        self.kernel = k = c.WinDLL("kernel32", use_last_error=True)
        self.callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        u.EnumWindows.argtypes = [self.callback, w.LPARAM]
        u.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
        u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
        u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
        u.IsWindowVisible.argtypes = [w.HWND]
        u.ShowWindow.argtypes = [w.HWND, c.c_int]
        u.GetPropW.argtypes = [w.HWND, w.LPCWSTR]; u.GetPropW.restype = w.HANDLE
        u.SetPropW.argtypes = [w.HWND, w.LPCWSTR, w.HANDLE]
        u.RemovePropW.argtypes = [w.HWND, w.LPCWSTR]; u.RemovePropW.restype = w.HANDLE
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; k.OpenProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]

    def windows(self):
        result, paths = [], {}
        @self.callback
        def visit(hwnd, _):
            title, cls = c.create_unicode_buffer(1024), c.create_unicode_buffer(128)
            self.user.GetWindowTextW(hwnd, title, len(title))
            self.user.GetClassNameW(hwnd, cls, len(cls))
            if cls.value != "kugou_ui" or "酷狗" not in title.value:
                return True
            pid = w.DWORD(); self.user.GetWindowThreadProcessId(hwnd, c.byref(pid))
            if pid.value not in paths:
                handle = self.kernel.OpenProcess(0x1000, False, pid.value)
                path, size = c.create_unicode_buffer(32768), w.DWORD(32768)
                paths[pid.value] = False
                if handle:
                    try:
                        if self.kernel.QueryFullProcessImageNameW(handle, 0, path, c.byref(size)):
                            paths[pid.value] = path.value.replace("/", "\\").rsplit("\\", 1)[-1].casefold() == "kugou.exe"
                    finally:
                        self.kernel.CloseHandle(handle)
            if paths[pid.value]:
                result.append((hwnd, pid.value, title.value == LYRIC_TITLE))
            return True
        self.user.EnumWindows(visit, 0)
        return result

    def pid(self):
        pids = {pid for _, pid, _ in self.windows()}
        return next(iter(pids)) if len(pids) == 1 else None

    def alive(self, pid):
        handle = self.kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return self.kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            self.kernel.CloseHandle(handle)


class NativeVisibility:
    def __init__(self, windows=None, owner_pid=None):
        self.windows = windows or Windows()
        self.owner_pid = owner_pid or os.getpid()

    def replace(self, pid, enabled):
        api = self.windows.user
        hidden = 0
        for hwnd, player_pid, is_lyric in self.windows.windows():
            if not is_lyric:
                continue
            owner = api.GetPropW(hwnd, PROPERTY) or 0
            if owner and owner != self.owner_pid and not self.windows.alive(owner):
                api.ShowWindow(hwnd, 4)
                api.RemovePropW(hwnd, PROPERTY)
                owner = 0
            wanted = enabled and player_pid == pid
            if owner == self.owner_pid and not wanted:
                api.ShowWindow(hwnd, 4)
                api.RemovePropW(hwnd, PROPERTY)
            elif wanted and owner in (0, self.owner_pid):
                if owner == self.owner_pid or api.IsWindowVisible(hwnd):
                    if api.SetPropW(hwnd, PROPERTY, self.owner_pid):
                        api.ShowWindow(hwnd, 0)
                        hidden += 1
        return hidden

    def close(self):
        self.replace(None, False)


class CommandPipe:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.replace_native = False
        self.expires = 0
        self.quit = False
        self.pending = b""
        self.kernel = k = c.WinDLL("kernel32", use_last_error=True)
        k.GetStdHandle.argtypes = [w.DWORD]; k.GetStdHandle.restype = w.HANDLE
        k.PeekNamedPipe.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(w.DWORD), c.c_void_p]
        k.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        self.handle = k.GetStdHandle(w.DWORD(-10 & 0xffffffff))

    def feed(self, raw):
        self.pending += raw
        if len(self.pending) > 8192:
            self.pending = b""
            self.replace_native = False
            self.expires = 0
            return
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    continue
                if command.get("quit") is True:
                    self.quit = True
                if isinstance(command.get("replace_native"), bool):
                    self.replace_native = command["replace_native"]
                    self.expires = self.clock() + 2
            except (ValueError, UnicodeError):
                continue

    def poll(self):
        available = w.DWORD()
        if self.handle and self.kernel.PeekNamedPipe(self.handle, None, 0, None, c.byref(available), None) and available.value:
            size = min(4096, available.value)
            data, count = c.create_string_buffer(size), w.DWORD()
            if self.kernel.ReadFile(self.handle, data, size, c.byref(count), None):
                self.feed(data.raw[:count.value])
        return self.replace_native and self.clock() < self.expires
