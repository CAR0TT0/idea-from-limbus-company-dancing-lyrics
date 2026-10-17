"""Read-only KuGou Mac adapter. Never alters another application's windows.

media-control (BSD-3-Clause) is installed separately, not bundled. Its get
schema and timestamp units are documented by ungive/mediaremote-adapter.
"""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

from kugou_lyrics import load_song_lyrics
from macos_netease import MacNeteasePlayer, NowPlaying

KUGOU_BUNDLE = "com.kugou.mac.Music"
MAX_PAYLOAD = 65536


def find_media_control():
    candidates = (os.environ.get("MEDIA_CONTROL"), shutil.which("media-control"),
                  "/opt/homebrew/bin/media-control", "/usr/local/bin/media-control")
    return next((str(Path(p)) for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), None)


def _number(data, key, maximum):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"系统播放字段缺失或无效：{key}")
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"系统播放字段超出范围：{key}")
    return value


def parse_media_info(data, *, epoch=None):
    """Require the actual source, explicit playback state and a real timeline.

    Elapsed time is an anchor at timestampEpochMicros, NOT the process receipt
    time. Re-reading an unchanged anchor must not reset the lyric progress.
    """
    if not isinstance(data, dict) or data.get("bundleIdentifier") != KUGOU_BUNDLE:
        return None
    title, artist = data.get("title"), data.get("artist", "") or ""
    if (not isinstance(title, str) or not title.strip() or not isinstance(artist, str)
            or max(len(title), len(artist)) > 1024 or type(data.get("playing")) is not bool):
        return None
    now = time.time() if epoch is None else epoch
    duration = _number(data, "durationMicros", 86400 * 1000000) / 1000
    elapsed = _number(data, "elapsedTimeMicros", 86400 * 1000000) / 1000
    stamp = _number(data, "timestampEpochMicros", (now + 2) * 1000000) / 1000000
    if not duration or not 0 <= now - stamp <= 86400:
        raise ValueError("酷狗尚未提供有效的系统播放时间轴")
    playing = data["playing"]
    rate = _number({"rate": data.get("playbackRate", 1 if playing else 0)}, "rate", 4)
    if playing and not rate:
        raise ValueError("酷狗播放状态与播放速率不一致")
    position = elapsed + max(0, now - stamp) * 1000 * rate if playing else elapsed
    return NowPlaying("playing" if playing else "paused", title.strip(), artist.strip(),
                      round(duration), round(min(duration, position)), rate=rate)


def query_media_control(cli):
    if not cli:
        return None
    result = subprocess.run([cli, "get", "--micros", "--no-artwork"], capture_output=True,
                            text=True, encoding="utf-8", timeout=2, check=True)
    if len(result.stdout.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("系统播放信息过大")
    text = result.stdout.strip()
    return parse_media_info(json.loads(text)) if text and text != "null" else None


class MacKugouPlayer(MacNeteasePlayer):
    source_name = "酷狗音乐"
    source_short = "酷狗"
    reader_name = "media-control"
    connect_hint = "在酷狗 Mac 版播放歌曲；插件读取系统当前播放信息。"
    find_reader = staticmethod(find_media_control)
    query_reader = staticmethod(query_media_control)

    def __init__(self, parent=None, *, cache_directory="", loader=None, **kwargs):
        self._cache_directory = str(cache_directory)
        self._speed = 0.0
        self._raw_position = None
        if loader is None:
            def loader(info):
                # Capture the selection per job; changes invalidate old jobs.
                directories = (Path(self._cache_directory),) if self._cache_directory else ()
                return load_song_lyrics(info, directories=directories).document
        super().__init__(parent, loader=loader, **kwargs)

    def _observe_position(self, info, now, switched):
        current = self.position()
        error = info.position_ms - current
        paused_seek = (not self.playing and info.state != "playing"
                       and self._raw_position is not None and abs(info.position_ms - self._raw_position) > 100)
        jumped = switched or abs(error) > 750 or paused_seek
        self._anchor = info.position_ms if jumped else current
        self._at = now
        self._raw_position = info.position_ms
        playing = info.state == "playing"
        self._speed = (info.rate + max(-info.rate * .2, min(info.rate * .2, error / 2500))) if playing else 0
        if jumped and playing:
            self._speed = info.rate
        return jumped

    def position(self):
        elapsed = max(0, min(self._clock(), self._last_poll + 1.5) - self._at) * 1000 if self.playing else 0
        value = self._anchor + elapsed * self._speed
        return max(0, min(value, self.duration)) if self.duration else max(0, value)

    @property
    def cache_status(self):
        return (f"歌词目录：{self._cache_directory}" if self._cache_directory else
                "未选择本地歌词目录；根据歌名、歌手与时长匹配酷狗在线歌词。")

    def configure_cache_directory(self, path):
        self._cache_directory = str(path)
        self._cache.clear()
        self.retry_lyrics()

    def retry_lyrics(self):
        if self.connected:
            self._generation += 1
            self._cache.pop(self.song_id, None)
            self._needs_lyrics = True
            self._retry_at = 0
            self.status = "正在重新获取酷狗歌词…"
            self.changed.emit()
