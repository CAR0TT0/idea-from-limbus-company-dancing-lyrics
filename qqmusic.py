"""Windows QQ Music companion: read SMTC, fetch matching lyrics off the GUI thread."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, CancelledError
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import PureWindowsPath
import time
import threading

from PySide6.QtCore import QProcess, QTimer
from netease import NeteasePlayer
from qqmusic_lyrics import LyricResult, load_song_lyrics
from settings import resource_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QQSnapshot:
    title: str
    artist: str
    album: str
    duration_ms: int
    position_ms: float
    playing: bool
    rate: float = 1.0

    @property
    def key(self):
        return json.dumps([self.title, self.artist, self.album, self.duration_ms], ensure_ascii=False)


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("QQ 音乐播放时间无效")
    return value


def parse_snapshot(data):
    if not isinstance(data, dict) or PureWindowsPath(str(data.get("source", ""))).name.lower() != "qqmusic.exe":
        return None
    for key in ("title", "artist", "album"):
        if not isinstance(data.get(key), str) or len(data[key]) > 1024 or "\x00" in data[key]:
            raise ValueError("QQ 音乐歌曲信息无效")
    if not data["title"].strip():
        return None
    state = data.get("state")
    if state not in ("Playing", "Paused", "Stopped", "Closed", "Opened", "Changing"):
        raise ValueError("QQ 音乐播放状态无效")
    if state in ("Closed", "Stopped", "Opened", "Changing"):
        return None
    duration = int(_number(data.get("duration_ms"), 0, 86400000))
    position = _number(data.get("position_ms"), 0, 86400000)
    updated = _number(data.get("updated_ms"), 1, 1e14)
    observed = _number(data.get("observed_ms"), 1, 1e14)
    rate = _number(data.get("rate", 1), 0, 4)
    if not duration:
        raise ValueError("QQ 音乐尚未提供时间轴，请在 QQ 音乐中播放一首歌曲。")
    # Windows returns an anchored timeline, not necessarily a freshly sampled position.
    if state == "Playing":
        age = observed - updated
        if age < -2000 or age > 86400000:
            raise ValueError("QQ 音乐时间轴已失效，请重新播放歌曲。")
        position += max(0, age) * rate
    return QQSnapshot(data["title"].strip(), data["artist"].strip(), data["album"].strip(),
                      duration, min(duration, position), state == "Playing", rate)


def media_diagnostic(data):
    if not isinstance(data, dict) or PureWindowsPath(str(data.get("source", ""))).name.lower() != "qqmusic.exe":
        return "兼容检测：尚未发现 QQ 音乐媒体会话，请开启系统媒体控制并播放歌曲。"
    if data.get("state") in ("Opened", "Changing"):
        return "兼容检测：已发现 QQ 音乐，等待歌曲与有效进度；请开始播放。"
    if data.get("state") in ("Stopped", "Closed"):
        return "兼容检测：QQ 音乐当前没有播放歌曲。"
    return "兼容检测：等待有效歌曲与进度。"


class QQMusicPlayer(NeteasePlayer):
    source_id = "qqmusic"
    source_name = source_short = "QQ 音乐"
    connect_hint = "打开 QQ 音乐并播放歌曲；若未连接，请开启 QQ 音乐的系统媒体控制。"

    def __init__(self, parent=None, *, clock=time.monotonic, executor=None, loader=None, autostart=True,
                 cache_reader=None, cache_directory="", cache_executor=None):
        super().__init__(clock=clock, parent=parent)
        self.status = "等待 QQ 音乐播放歌曲"
        self.lyric_status = "等待 QQ 音乐歌词"
        self.compatibility_status = media_diagnostic(None)
        self._rate = 1.0
        self._snapshot = None
        self._generation = 0
        self._cache = OrderedDict()
        self._future = None
        self._future_key = None
        self._future_generation = 0
        self._requested_key = None
        self._cancel = threading.Event()
        self._retired = []
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="qq-lyrics")
        self._owns_executor = executor is None
        self._loader = loader or load_song_lyrics
        self._default_loader = loader is None
        self._use_local = cache_reader is not None or loader is None
        from qqmusic_cache import QQCacheReader
        self._cache_reader = cache_reader or (QQCacheReader(cache_directory) if self._use_local else None)
        self._cache_executor = cache_executor or (ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-cache") if self._use_local else None)
        self._owns_cache_executor = cache_executor is None
        self._local_future = None
        self._local_meta = None
        self._local_signature = ()
        self._local_deadline = self._cache_probe_at = 0
        self._closed = False
        self._last_packet = clock()
        self._restart_at = 0
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.errorOccurred.connect(lambda _: self._helper_failed())
        self.process.finished.connect(lambda *_: self._helper_failed())
        self._pending = b""
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        if autostart:
            self.timer.start()
            self._start_helper()

    def position(self):
        elapsed = max(0, (self._clock() - self._at) * 1000) * self._rate if self.playing else 0
        return min(self.duration, self._anchor + min(2500, elapsed)) if self.duration else 0

    def _start_helper(self):
        if self._closed or self.process.state() != QProcess.ProcessState.NotRunning:
            return
        program = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                               "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        self._last_packet = self._clock()
        self._pending = b""
        self.process.start(program, ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(resource_path("native/qqmusic_smtc.ps1"))])

    def _helper_failed(self):
        if not self._closed:
            self.disconnect("系统媒体读取暂不可用；需要 Windows 10 1809 或更新版本，将自动重试。")
            self._restart_at = self._clock() + 5

    def _read(self):
        self._pending += bytes(self.process.readAllStandardOutput())
        if len(self._pending) > 128 * 1024:
            self._pending = b""
            self.disconnect("QQ 音乐播放信息过大，已停止本次读取。")
            return
        while b"\n" in self._pending:
            raw, self._pending = self._pending.split(b"\n", 1)
            try:
                packet = json.loads(raw)
                if not isinstance(packet, dict):
                    raise ValueError("QQ 音乐播放信息无效")
                self._last_packet = self._clock()
                snapshot = parse_snapshot(packet.get("snapshot"))
                if packet.get("error"):
                    self.disconnect("Windows 媒体会话读取失败，正在重试。")
                elif snapshot is None:
                    self.disconnect()
                    self.compatibility_status = media_diagnostic(packet.get("snapshot"))
                    self.changed.emit()
                else:
                    self.apply_snapshot(snapshot)
            except (ValueError, TypeError, UnicodeError) as error:
                self.disconnect(str(error))

    def apply_snapshot(self, snapshot):
        switched = snapshot.key != self.song_id
        jumped = switched or abs(snapshot.position_ms - self.position()) > 750
        if switched:
            self._cancel_lyrics()
            self._generation += 1
            self._requested_key = None
            self._local_signature = ()
            self._local_deadline = self._clock() + 20
            self._cache_probe_at = self._clock() + .5
            self.lyrics_received = False
            self.lyric_status = "正在获取 QQ 音乐歌词…"
            self.document_changed.emit(None)
            self._energy = 0
        self._snapshot = snapshot
        self.song_id, self.title, self.artist = snapshot.key, snapshot.title, snapshot.artist
        self.duration = snapshot.duration_ms
        self._anchor, self._at, self._rate = snapshot.position_ms, self._clock(), snapshot.rate
        self.playing, self.connected = snapshot.playing, True
        self.ended = self._anchor >= self.duration
        self.status = "已连接 QQ 音乐 · 播放、暂停与切歌请在 QQ 音乐中操作。"
        self.compatibility_status = "兼容检测通过 · 系统媒体歌曲与进度可用；歌词采用本地缓存优先、联网兜底。"
        if jumped:
            self.discontinuity.emit()
        if switched and snapshot.key in self._cache:
            result = self._cache.pop(snapshot.key)
            self._cache[snapshot.key] = result
            self._requested_key = snapshot.key
            self._apply_lyrics(result)
        self._schedule_lyrics()
        self.changed.emit()

    def _schedule_lyrics(self):
        if self._closed or not self.connected or self._snapshot is None or self._future is not None:
            return
        if self._requested_key == self.song_id:
            return
        self._requested_key = self._future_key = self.song_id
        self._future_generation = self._generation
        self._cancel = threading.Event()
        self._future = self._executor.submit(self._fetch_lyrics, self._snapshot, self._cancel.is_set)

    def _fetch_lyrics(self, snapshot, cancelled):
        if self._use_local:
            try:
                result = self._cache_reader.load(snapshot, cancelled=cancelled)
            except CancelledError:
                raise
            except Exception as error:
                logger.warning("QQ local cache unavailable: %s", type(error).__name__)
                result = None
            if result is not None:
                return result
        if self._default_loader:
            return self._loader(snapshot, cancelled=cancelled)
        return self._loader(snapshot)

    def _cancel_lyrics(self):
        self._cancel.set()
        if self._future is not None:
            if not self._future.cancel():
                self._retired.append(self._future)
            self._future = None
        if self._local_future is not None:
            if not self._local_future.cancel():
                self._retired.append(self._local_future)
            self._local_future = None
        self._retired = [future for future in self._retired if not future.done()]

    def _apply_lyrics(self, result):
        self.lyrics_received = True
        self.lyric_status = result.message
        self.compatibility_status = ("兼容检测通过 · 系统媒体歌曲与进度可用 · " +
                                     ("本地歌词缓存可用" if result.source == "cache" else "歌词使用联网结果或正常回退"))
        if result.source == "cache":
            self._local_signature = result.cache_signature
        self.document_changed.emit(result.document)

    def retry_lyrics(self):
        if self.connected:
            self._cancel_lyrics()
            self._cache.pop(self.song_id, None)
            self._generation += 1
            self._requested_key = None
            self._local_signature = ()
            self._local_deadline = self._clock() + 20
            self._cache_probe_at = self._clock() + .5
            self.lyric_status = "正在重新获取 QQ 音乐歌词…"
            self._schedule_lyrics()
            self.changed.emit()

    @property
    def cache_status(self):
        return self._cache_reader.status if self._cache_reader else "歌词缓存：自动检测"

    def configure_cache_directory(self, directory):
        from qqmusic_cache import QQCacheReader
        self._cache_reader = QQCacheReader(directory)
        self._use_local = True
        if self._cache_executor is None:
            self._cache_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-cache")
            self._owns_cache_executor = True
        self._cache.clear()
        self.retry_lyrics()
        self.changed.emit()

    def _poll_local_cache(self):
        if self._local_future is not None and self._local_future.done():
            future, (key, generation) = self._local_future, self._local_meta
            self._local_future = None
            try:
                result = future.result()
            except Exception:
                result = None  # Cache availability must never interrupt network lyrics.
            if result is not None and key == self.song_id and generation == self._generation and self.connected:
                self._cancel_lyrics()
                self._cancel = threading.Event()
                self._requested_key = self.song_id
                self._apply_lyrics(result)
                if result.document is not None:
                    self._cache[key] = result
                    while len(self._cache) > 32:
                        self._cache.popitem(last=False)
                self.changed.emit()
        now = self._clock()
        if (self._use_local and self.connected and self._snapshot is not None and self._local_future is None
                and self._cache_probe_at <= now < self._local_deadline):
            self._cache_probe_at = now + .5
            self._local_meta = (self.song_id, self._generation)
            self._local_future = self._cache_executor.submit(
                self._cache_reader.load, self._snapshot, cancelled=self._cancel.is_set,
                known_signature=self._local_signature)

    def _poll(self):
        if self._closed:
            return
        self._retired = [future for future in self._retired if not future.done()]
        self._poll_local_cache()
        if self._future is not None and self._future.done():
            future, key, generation = self._future, self._future_key, self._future_generation
            self._future = None
            try:
                result = future.result()
            except Exception:
                logger.exception("QQ Music lyric read failed")
                result = LyricResult(None, "QQ 音乐歌词获取失败，请检查网络后点击“重新获取歌词”。")
            if key == self.song_id and generation == self._generation and self.connected:
                if result.document is not None:
                    self._cache[key] = result
                    while len(self._cache) > 32:
                        self._cache.popitem(last=False)
                self._apply_lyrics(result)
                self.changed.emit()
            self._schedule_lyrics()
        if self.timer.isActive():
            if self._clock() - self._last_packet > 6:
                self.disconnect("QQ 音乐媒体读取超时，正在重新连接。")
                self.process.kill()
                self._last_packet = self._clock()
            if self._clock() >= self._restart_at:
                self._start_helper()

    def disconnect(self, message=None):
        had_song = bool(self.song_id)
        self._anchor = 0
        self.playing = self.connected = self.ended = False
        self.duration = 0
        self.song_id = self.title = self.artist = ""
        self._snapshot = None
        self.status = message or "等待 QQ 音乐播放歌曲 · 请开启 QQ 音乐系统媒体控制。"
        self.compatibility_status = "兼容检测：" + message if message else media_diagnostic(None)
        self.lyric_status = "等待 QQ 音乐歌词"
        self.lyrics_received = False
        if had_song:
            self._cancel_lyrics()
            self._generation += 1
            self._requested_key = None
            self._local_signature = ()
            self._local_deadline = 0
            self.document_changed.emit(None)
            self.discontinuity.emit()
        self.changed.emit()

    def stop(self):
        if self._closed:
            return
        self._closed = True
        self.timer.stop()
        self.process.kill()
        self.process.waitForFinished(1000)
        self._cancel_lyrics()
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self._cache_executor is not None and self._owns_cache_executor:
            self._cache_executor.shutdown(wait=False, cancel_futures=True)
        self.disconnect("QQ 音乐联动已停止")
