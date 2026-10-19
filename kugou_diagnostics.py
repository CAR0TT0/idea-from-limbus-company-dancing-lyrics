"""Small local compatibility report without titles, lyrics, paths or memory."""
import asyncio
import json
from pathlib import Path
import platform


def summarize(snapshot, message, native, rows, windows):
    return {
        "schema": 1,
        "adapter_revision": "kugou.5",
        "windows_version": platform.version(),
        "client_version_directory": native.module_version,
        "native_status": native.status,
        "channel": snapshot.get("mode") if snapshot else None,
        "playing": bool(snapshot and snapshot.get("playing")),
        "main_window_visible": any(windows.user.IsWindowVisible(h) for h, _, lyric in rows if not lyric),
        "desktop_lyric_window_present": any(lyric for _, _, lyric in rows),
        "message": message,
    }


async def collect():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    from kugou_native import NativeLyricsReader
    from kugou_windows import Windows
    from kugou_reader import sample_channels
    native, windows = NativeLyricsReader(), Windows()
    try:
        manager = await asyncio.wait_for(Manager.request_async(), 3)
        window = None
        for _ in range(3):
            try:
                snapshot, message, window = await sample_channels(manager, native, windows, window)
            except Exception as error:
                from kugou_reader import NATIVE_HINTS
                snapshot = None
                message = NATIVE_HINTS.get(native.status, "后台状态未知") + "；界面读取失败（" + type(error).__name__ + "）。"
            await asyncio.sleep(.25)
        return summarize(snapshot, message, native, windows.windows(), windows)
    finally:
        native.close()


def run(args):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(args)
    try:
        report = asyncio.run(collect())
    except Exception as error:
        report = {"schema": 1, "error_type": type(error).__name__, "message": "诊断读取失败，请提供此文件及设置页通道提示。"}
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if "error_type" not in report else 1
