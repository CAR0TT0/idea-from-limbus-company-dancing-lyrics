"""KuGou companion with recoverable readers and generation-safe lyric delivery."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QProcess, QTimer
from netease import NeteasePlayer
from kugou_lyrics import LyricResult, load_song_lyrics
from kugou_reader import is_kugou
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

    @property
    def key(self):
        return json.dumps([self.title, self.artist, self.album, self.duration_ms], ensure_ascii=False)


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
    if not isinstance(data.get("playing"), bool) or data.get("mode") not in ("smtc", "ocr", "hybrid"):
        raise ValueError("酷狗播放状态无效")
    duration = int(data["duration_ms"])
    if not duration:
        return None
    return KugouSnapshot(data["title"].strip(), data["artist"].strip(), data["album"].strip(), duration,
                         min(duration, data["position_ms"]), data["playing"], data.get("rate", 1), data["mode"])


class KugouPlayer(NeteasePlayer):
    source_id = "kugou"
    source_name = "酷狗音乐"
    source_short = "酷狗"
    connect_hint = "打开酷狗并播放歌曲；界面兼容模式请保持酷狗主窗口还原。"

    def __init__(self, parent=None, *, clock=time.monotonic, loader=load_song_lyrics, executor=None, autostart=True,
                 cache_directory=""):
        super().__init__(clock=clock, parent=parent)
        self.status = "等待酷狗音乐播放歌曲"
        self.compatibility_status = "自动检测系统媒体接口及界面兼容通道，无客户端版本号限制。"
        self.lyric_status = "等待酷狗歌词"
        self._snapshot = None
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
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.finished.connect(self._helper_failed)
        self.process.errorOccurred.connect(self._helper_failed)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        if autostart:
            self.timer.start()
            self._start_helper()

    def position(self):
        if not self.duration:
            return 0
        limit = 500 if self._snapshot and self._snapshot.mode in ("ocr", "hybrid") else 1500
        elapsed = min(limit, max(0, (self._clock() - self._at) * 1000)) * self._rate if self.playing else 0
        return min(self.duration, self._anchor + elapsed)

    def _start_helper(self):
        if self._closed or self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self._pending = b""
        self._last_packet = self._clock()
        args = [] if getattr(sys, "frozen", False) else [str(resource_path("main.py"))]
        self.process.start(sys.executable, args + ["--kugou-reader"])

    def _helper_failed(self, *_):
        if not self._closed:
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
                self._last_packet = self._clock()
                snapshot = parse_snapshot(packet.get("snapshot"))
                message = packet.get("message", "")
                if not isinstance(message, str) or len(message) > 2048:
                    raise ValueError("酷狗诊断信息无效")
                if snapshot is None:
                    self.disconnect(message or "等待酷狗播放信息")
                else:
                    self.apply_snapshot(snapshot, message)
            except (ValueError, TypeError, UnicodeError) as error:
                self.disconnect(str(error))

    def apply_snapshot(self, snapshot, message=""):
        switched = snapshot.key != self.song_id
        jumped = switched or abs(snapshot.position_ms - self.position()) > 1500
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
        self._anchor, self._at, self._rate = snapshot.position_ms, self._clock(), snapshot.rate
        self.playing, self.connected = snapshot.playing and self.enabled, True
        self.ended = self._anchor >= self.duration
        self.status = "已连接酷狗音乐 · 播放操作请在酷狗中进行。"
        self.compatibility_status = message or ("系统媒体接口可用。" if snapshot.mode == "smtc" else "界面兼容模式 · 约 1 秒精度，需还原主窗口。")
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
        if self.process.state() != QProcess.ProcessState.NotRunning:
            if self._clock() - self._last_packet > 8:
                self.disconnect("酷狗读取超时，正在重建读取通道。")
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
        self.status = self.compatibility_status = message
        self.changed.emit()

    def stop(self):
        self._closed = True
        self.timer.stop()
        self._cancel.set()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(1000)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self.disconnect("酷狗联动已停止")
