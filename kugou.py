"""KuGou companion with recoverable readers and generation-safe lyric delivery."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
import math
import os
import re
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QProcess, QTimer
from netease import NeteasePlayer
from kugou_lyrics import LyricResult, load_song_lyrics
from kugou_reader import is_kugou
from kugou_clock import PlaybackClock
from settings import resource_path


@dataclass(frozen=True)
class KugouSnapshot:
    title: str
    artist: str
    album: str
    duration_ms: int
    position_ms: float
    playing: bool
    rate: float = 1.0
    mode: str = "smtc"
    lyric_hash: str = ""
    native_title: str = ""

    @property
    def key(self):
        identity = [self.title, self.artist, self.album, self.duration_ms]
        if self.lyric_hash:
            identity.append(self.lyric_hash)
        return json.dumps(identity, ensure_ascii=False)


def parse_snapshot(data):
    if not isinstance(data, dict) or not is_kugou(data.get("source")):
        return None
    for field in ("title", "artist", "album"):
        if not isinstance(data.get(field), str) or len(data[field]) > 1024 or any(c in data[field] for c in "\x00\r\n"):
            raise ValueError("酷狗歌曲信息无效")
    if not data["title"].strip():
        return None
    for field in ("duration_ms", "position_ms", "rate"):
        value = data.get(field, 1 if field == "rate" else None)
        upper = 4 if field == "rate" else 86400000
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= upper:
            raise ValueError("酷狗播放时间无效")
    if not isinstance(data.get("playing"), bool) or data.get("mode") not in ("smtc", "native", "ocr", "hybrid"):
        raise ValueError("酷狗播放状态无效")
    duration = int(data["duration_ms"])
    if not duration:
        return None
    lyric_hash, native_title = data.get("lyric_hash", ""), data.get("native_title", "")
    if not isinstance(lyric_hash, str) or lyric_hash and not re.fullmatch(r"[0-9a-fA-F]{32}", lyric_hash):
        raise ValueError("酷狗歌词标识无效")
    if not isinstance(native_title, str) or len(native_title) > 1024 or any(c in native_title for c in "\x00\r\n"):
        raise ValueError("酷狗原生曲名无效")
    return KugouSnapshot(data["title"].strip(), data["artist"].strip(), data["album"].strip(), duration,
                         min(duration, data["position_ms"]), data["playing"], data.get("rate", 1), data["mode"],
                         lyric_hash.casefold(), native_title)


class KugouPlayer(NeteasePlayer):
    source_id = "kugou"
    source_name = "酷狗音乐"
    source_short = "酷狗"
    connect_hint = "打开酷狗自带的桌面歌词，可尝试最小化同步；界面兼容模式需还原主窗口。"

    def __init__(self, parent=None, *, clock=time.monotonic, loader=load_song_lyrics, executor=None, autostart=True,
                 cache_directory="", replace_native=True):
        super().__init__(clock=clock, parent=parent)
        self.status = "等待酷狗音乐播放歌曲"
        self.compatibility_status = "自动检测系统媒体、原生歌词与界面兼容通道，无客户端版本号限制。"
        self.lyric_status = "等待酷狗歌词"
        self._snapshot = None
        self._timeline_clock = PlaybackClock()
        self._missing_since = None
        self._duration_candidate = None
        self._generation = 0
        self._rate = 1.0
        self._closed = False
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="kugou-lyrics")
        self._owns_executor = executor is None
        self._loader = loader
        self._cache_directory = cache_directory
        self._future = None
        self._future_key = ""
        self._future_generation = 0
        self._cancel = threading.Event()
        self._cache = OrderedDict()
        self._retry_at = 0
        self._last_packet = clock()
        self._restart_at = 0
        self._pending = b""
        self.replace_native = replace_native
        self.overlay_visible = True
        self._command_at = 0
        self._reader_pid = 0
        self._logged_channel = None
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.finished.connect(self._helper_failed)
        self.process.errorOccurred.connect(self._helper_failed)
        self.process.started.connect(lambda: setattr(self, "_reader_pid", int(self.process.processId())))
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        if autostart:
            self.timer.start()
            self._start_helper()

    def position(self):
        return self._timeline_clock.position(self._clock())

    def _start_helper(self):
        if self._closed or self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self._pending = b""
        self._last_packet = self._clock()
        args = [] if getattr(sys, "frozen", False) else [str(resource_path("main.py"))]
        self.process.start(sys.executable, args + ["--kugou-reader", "--parent-pid", str(os.getpid())])

    def configure_native_replacement(self, enabled):
        self.replace_native = bool(enabled)
        self._command_at = 0
        self._send_visibility()

    def set_overlay_visible(self, visible):
        self.overlay_visible = bool(visible)
        self._command_at = 0
        self._send_visibility()

    def _send_visibility(self):
        if self.process.state() != QProcess.ProcessState.Running or self._clock() < self._command_at:
            return
        enabled = (self.replace_native and self.overlay_visible and self.connected and self.lyrics_received
                   and self._snapshot is not None and self._snapshot.mode in ("native", "smtc"))
        self.process.write((json.dumps(dict(replace_native=enabled)) + "\n").encode("utf-8"))
        self._command_at = self._clock() + .5

    def _restore_native(self):
        pid = self._reader_pid or int(self.process.processId())
        if pid:
            from kugou_windows import NativeVisibility
            NativeVisibility(owner_pid=pid).close()

    def _helper_failed(self, *_):
        if not self._closed:
            self._restore_native()
            self.disconnect("酷狗读取助手暂不可用，5 秒后自动重连。")
            self._restart_at = self._clock() + 5

    def _read(self):
        self._pending += bytes(self.process.readAllStandardOutput())
        if len(self._pending) > 128 * 1024:
            self._pending = b""
            self.disconnect("酷狗播放信息过大，已丢弃。")
            return
        while b"\n" in self._pending:
            raw, self._pending = self._pending.split(b"\n", 1)
            try:
                packet = json.loads(raw)
                if not isinstance(packet, dict):
                    raise ValueError("酷狗信息无效")
                worker_pid = packet.get("reader_pid")
                if isinstance(worker_pid, int) and not isinstance(worker_pid, bool) and 0 < worker_pid <= 0xffffffff:
                    self._reader_pid = worker_pid
                self._last_packet = self._clock()
                snapshot = parse_snapshot(packet.get("snapshot"))
                message = packet.get("message", "")
                if not isinstance(message, str) or len(message) > 2048:
                    raise ValueError("酷狗诊断信息无效")
                channel = snapshot.mode if snapshot else "unavailable"
                if channel != self._logged_channel:
                    logging.info("KuGou channel=%s; status=%s", channel, message)
                    self._logged_channel = channel
                if snapshot is None:
                    self._handle_missing(message or "等待酷狗播放信息", packet.get("recoverable") is True)
                else:
                    self.apply_snapshot(snapshot, message)
            except (ValueError, TypeError, UnicodeError) as error:
                self._handle_missing(str(error), True)

    def _handle_missing(self, message, recoverable):
        if recoverable and self.connected:
            now = self._clock()
            if self._missing_since is None:
                self._missing_since = now
            if now - self._missing_since < 2:
                self.compatibility_status = message
                self.changed.emit()
                return
        self.disconnect(message)

    def apply_snapshot(self, snapshot, message=""):
        now = self._clock()
        coarse = snapshot.mode in ("ocr", "hybrid")
        if (coarse and self._snapshot is not None
                and (snapshot.title, snapshot.artist, snapshot.album) ==
                    (self.title, self.artist, self._snapshot.album)
                and snapshot.duration_ms != self.duration):
            previous = self._duration_candidate
            self._duration_candidate = (snapshot.duration_ms, now)
            if previous is None or previous[0] != snapshot.duration_ms or now - previous[1] > 2:
                return
        self._duration_candidate = None
        switched = snapshot.key != self.song_id
        accepted, jumped = self._timeline_clock.observe(snapshot.position_ms, now, snapshot.duration_ms,
                                                        snapshot.playing and self.enabled, snapshot.rate,
                                                        coarse, reset=switched)
        if not accepted:
            self.playing = snapshot.playing and self.enabled
            self.changed.emit()
            return
        self._missing_since = None
        if switched:
            self._cancel.set()
            self._generation += 1
            self._retry_at = 0
            self.document_changed.emit(None)
            self.lyrics_received = False
            self.lyric_status = "正在读取酷狗歌词…"
            self._energy = 0
        self._snapshot = snapshot
        self.song_id, self.title, self.artist = snapshot.key, snapshot.title, snapshot.artist
        self.duration = snapshot.duration_ms
        self._anchor, self._at, self._rate = self._timeline_clock.anchor, now, snapshot.rate
        self.playing, self.connected = snapshot.playing and self.enabled, True
        self.ended = self._anchor >= self.duration
        self.status = "已连接酷狗音乐 · 播放操作请在酷狗中进行。"
        self.compatibility_status = message or ({"smtc": "系统媒体接口可用。", "native": "原生歌词通道 · 支持最小化。"}.get(
            snapshot.mode, "界面兼容模式 · 约 1 秒精度，需还原主窗口。"))
        if jumped:
            self.discontinuity.emit()
        if switched and snapshot.key in self._cache:
            result = self._cache.pop(snapshot.key)
            self._cache[snapshot.key] = result
            self._apply_lyrics(result)
            self._retry_at = float("inf")
        self._schedule_lyrics()
        self.changed.emit()

    def _schedule_lyrics(self):
        if self._closed or not self.connected or not self._snapshot or self._future or self._clock() < self._retry_at:
            return
        self._cancel = threading.Event()
        self._future_key, self._future_generation = self.song_id, self._generation
        kwargs = dict(cancelled=self._cancel.is_set)
        if self._loader is load_song_lyrics and self._cache_directory:
            kwargs["directories"] = [Path(self._cache_directory)]
        self._future = self._executor.submit(self._loader, self._snapshot, **kwargs)
        self._retry_at = self._clock() + 15

    def _apply_lyrics(self, result):
        self.lyrics_received = result.document is not None
        self.lyric_status = result.message
        self.document_changed.emit(result.document)
        self.changed.emit()

    def retry_lyrics(self):
        self._cache.pop(self.song_id, None)
        self._retry_at = 0
        self._schedule_lyrics()

    @property
    def cache_status(self):
        return "歌词缓存：" + (self._cache_directory or "自动读取酷狗配置中的歌词目录")

    def configure_cache_directory(self, path):
        if path and not Path(path).is_dir():
            raise ValueError("歌词缓存目录不存在")
        self._cache_directory = path
        self._cancel.set()
        self._generation += 1
        self._cache.clear()
        self.retry_lyrics()

    def _poll(self):
        if self._closed:
            return
        if self._missing_since is not None and self._clock() - self._missing_since >= 2:
            self.disconnect("酷狗进度暂不可读，正在等待恢复。")
        if self._future is not None and self._future.done():
            future, key, generation = self._future, self._future_key, self._future_generation
            self._future = None
            try:
                result = future.result()
            except Exception:
                logging.exception("KuGou lyric fetch failed")
                result = LyricResult(None, "歌词读取失败，将自动重试；也可点击重新获取。")
            if generation == self._generation and key == self.song_id and self.connected:
                self._apply_lyrics(result)
                if result.document is not None:
                    self._cache[key] = result
                    while len(self._cache) > 24:
                        self._cache.popitem(last=False)
                    self._retry_at = float("inf")
        self._schedule_lyrics()
        self._send_visibility()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            if self._clock() - self._last_packet > 8:
                self.disconnect("酷狗读取超时，正在重建读取通道。")
                self._restore_native()
                self.process.kill()
        elif self._clock() >= self._restart_at:
            self._start_helper()

    def disconnect(self, message="等待酷狗音乐启动并播放歌曲。"):
        if self.connected:
            self._anchor = self.position()
            self._cancel.set()
            self._generation += 1
            self.song_id = self.title = self.artist = ""
            self._snapshot = None
            self.lyrics_received = False
            self.document_changed.emit(None)
            self.discontinuity.emit()
        self.playing = self.connected = False
        self._timeline_clock = PlaybackClock()
        self._missing_since = None
        self._duration_candidate = None
        self.status = self.compatibility_status = message
        self.changed.emit()

    def stop(self):
        self._closed = True
        self.timer.stop()
        self._cancel.set()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._restore_native()
            self.process.write(b'{"quit":true}\n')
            self.process.waitForBytesWritten(200)
            if not self.process.waitForFinished(1500):
                self._restore_native()
                self.process.kill()
                self.process.waitForFinished(1000)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self.disconnect("酷狗联动已停止")
