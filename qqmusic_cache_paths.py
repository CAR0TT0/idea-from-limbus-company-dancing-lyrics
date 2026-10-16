"""Discover lyric-only directories; never read QQ config, cookies or process memory."""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import sys


def active_cache_directories():
    if sys.platform!='win32':
        return []
    try:
        from process_audio import qqmusic_pid
        pid = qqmusic_pid()
        return _disk_handle_directories(pid) if pid else []
    except (OSError,ValueError,AttributeError):
        return []


def _disk_handle_directories(pid):
    kernel,native = c.WinDLL('kernel32',use_last_error=True),c.WinDLL('ntdll')
    class Entry(c.Structure):
        _fields_ = [('object',c.c_void_p),('pid',c.c_size_t),('handle',c.c_size_t),
                    ('access',w.ULONG),('trace',w.USHORT),('type',w.USHORT),('attrs',w.ULONG),('reserved',w.ULONG)]
    class Header(c.Structure):
        _fields_ = [('count',c.c_size_t),('reserved',c.c_size_t)]
    kernel.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.DuplicateHandle.argtypes = [w.HANDLE,w.HANDLE,w.HANDLE,c.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
    kernel.DuplicateHandle.restype = w.BOOL
    kernel.GetFileType.argtypes = [w.HANDLE]
    kernel.GetFinalPathNameByHandleW.argtypes = [w.HANDLE,w.LPWSTR,w.DWORD,w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    native.NtQuerySystemInformation.argtypes = [w.ULONG,c.c_void_p,w.ULONG,c.POINTER(w.ULONG)]
    native.NtQuerySystemInformation.restype = c.c_long
    size,needed = 1024*1024,w.ULONG()
    while size<=64*1024*1024:
        data = c.create_string_buffer(size)
        status = native.NtQuerySystemInformation(64,data,size,c.byref(needed))
        if status==0:
            break
        if status!=-1073741820:
            return []
        size = max(size*2,needed.value+65536)
    else:
        return []
    count = Header.from_buffer(data).count
    if c.sizeof(Header)+count*c.sizeof(Entry)>size:
        return []
    process = kernel.OpenProcess(0x40,False,pid)  # PROCESS_DUP_HANDLE only.
    if not process:
        return []
    directories,seen = set(),0
    try:
        for index in range(count):
            entry = Entry.from_buffer(data,c.sizeof(Header)+index*c.sizeof(Entry))
            if entry.pid!=pid:
                continue
            seen += 1
            if seen>10000:
                break
            duplicate = w.HANDLE()
            if not kernel.DuplicateHandle(process,w.HANDLE(entry.handle),kernel.GetCurrentProcess(),c.byref(duplicate),0,False,2):
                continue
            try:
                if kernel.GetFileType(duplicate)!=1:
                    continue
                name = c.create_unicode_buffer(4096)
                length = kernel.GetFinalPathNameByHandleW(duplicate,name,len(name),8)  # FILE_NAME_OPENED.
                if not 0<length<len(name):
                    continue
                path = Path(name.value.removeprefix('\\\\?\\'))
                if path.name.casefold() in ('lyricinfo','lyricinfo.crc') and path.parent.name.casefold()=='qqmusiclyricnew':
                    directories.add(path.parent)
            finally:
                kernel.CloseHandle(duplicate)
    finally:
        kernel.CloseHandle(process)
    return sorted(directories)


def discover_cache_directories():
    active = active_cache_directories()
    if active:
        return active
    candidates = [Path(os.environ.get(key,''))/'Tencent/QQMusic/QQMusicLyricNew'
                  for key in ('APPDATA','LOCALAPPDATA') if os.environ.get(key)]
    # Bounded direct path checks, not a recursive or whole-drive scan.
    if sys.platform=='win32':
        candidates += [Path(f'{drive}:/QQMusicCache/QQMusicLyricNew') for drive in 'CDE']
    return [path for path in candidates if path.is_dir()]
