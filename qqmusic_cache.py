"""Strict metadata matching and bounded reads of QQ's existing lyric-only cache."""
from collections import OrderedDict
from concurrent.futures import CancelledError
from dataclasses import replace
import os
from pathlib import Path
import re
import threading
import time
import unicodedata

from lrc import LyricDocument, chinese_translation
from qqmusic_cache_paths import discover_cache_directories
from qqmusic_lyrics import LyricResult
from qqmusic_qrc import MAX_ENCRYPTED, decode_qrc, parse_qrc


def sanitized(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',value).strip(' .')


def normalized_filename(value):
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())


def expected_filenames(snapshot):
    if not snapshot.artist or not snapshot.album or snapshot.duration_ms<=0:
        return set()
    seconds = int(snapshot.duration_ms//1000)
    return {normalized_filename(f'{sanitized(snapshot.artist)} - {sanitized(snapshot.title)} - {duration} - {sanitized(snapshot.album)}_qm.qrc')
            for duration in (seconds-1,seconds,seconds+1) if duration>=0}


class QQCacheReader:
    def __init__(self,directory='',*,finder=discover_cache_directories,clock=time.monotonic):
        self.directory,self.finder,self.clock = directory,finder,clock
        self.status = '歌词缓存：自动检测中…' if not directory else f'歌词缓存：{directory}'
        self._roots,self._index,self._decoded = [],{},OrderedDict()
        self._discover_at = self._scan_at = float('-inf')
        self._lock = threading.Lock()

    def _refresh(self,cancelled):
        now = self.clock()
        if now>=self._discover_at:
            roots = [Path(self.directory)] if self.directory else self.finder()
            self._roots = []
            for path in roots:
                if (path/'QQMusicLyricNew').is_dir():
                    path = path/'QQMusicLyricNew'
                if path.is_dir() and path.resolve() not in self._roots:
                    self._roots.append(path.resolve())
            self._discover_at = now+30
            self._scan_at = float('-inf')
            self.status = ('歌词缓存：'+str(self._roots[0]) if self._roots else
                           '歌词缓存：未找到可用目录，使用联网回退；可手动选择 QQMusicLyricNew。')
        if now<self._scan_at:
            return
        index,total = {},0
        for root in self._roots:
            for folder in (root,root/'specialCharacter'):
                if not folder.is_dir():
                    continue
                with os.scandir(folder) as entries:
                    for entry in entries:
                        total += 1
                        if total>20000:
                            self._index = {}
                            raise ValueError('歌词缓存文件过多，使用联网回退')
                        if total%100==0 and cancelled():
                            raise CancelledError()
                        if entry.name.endswith('_qm.qrc') and entry.is_file(follow_symlinks=False):
                            index.setdefault(normalized_filename(entry.name),[]).append(Path(entry.path))
        self._index,self._scan_at = index,now+.5

    @staticmethod
    def _stamp(path):
        try:
            stat = path.stat()
            if path.is_symlink() or not path.is_file() or not 0<stat.st_size<=MAX_ENCRYPTED:
                return None
            return (str(path),stat.st_mtime_ns,stat.st_size)
        except OSError:
            return None

    def _read(self,path,stamp,cancelled):
        if cancelled():
            raise CancelledError()
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (str(path),opened.st_mtime_ns,opened.st_size)!=stamp:
                raise ValueError('歌词缓存正在更新')
            data = stream.read(MAX_ENCRYPTED+1)
        if self._stamp(path)!=stamp or len(data)!=stamp[2]:
            raise ValueError('歌词缓存正在更新')
        return parse_qrc(decode_qrc(data,cancelled))

    def load(self,snapshot,*,cancelled=lambda:False,known_signature=()):
        with self._lock:
            if cancelled():
                raise CancelledError()
            try:
                self._refresh(cancelled)
                candidates = {path for name in expected_filenames(snapshot) for path in self._index.get(name,())}
                if len(candidates)!=1:
                    if len(candidates)>1:
                        self.status = '歌词缓存：歌曲匹配存在歧义，使用联网回退。'
                    return None
                path = candidates.pop()
                stamp = self._stamp(path)
                if stamp is None:
                    return None
                translation_path = path.with_name(path.name[:-len('_qm.qrc')]+'_qmts.qrc')
                translation_stamp = self._stamp(translation_path)
                signature = (stamp,translation_stamp)
                if signature==known_signature:
                    return None
                if signature in self._decoded:
                    result = self._decoded.pop(signature)
                    self._decoded[signature] = result
                    return result
                document = self._read(path,stamp,cancelled)
                if document is not None and translation_stamp:
                    try:
                        translated = self._read(translation_path,translation_stamp,cancelled)
                        if translated:
                            by_time = {line.start_ms-translated.offset_ms:line.text for line in translated.lines if chinese_translation(line.text)}
                            lines = []
                            for line in document.lines:
                                texts = [text for at,text in by_time.items() if abs(at-line.start_ms+document.offset_ms)<=10]
                                lines.append(replace(line,translation=texts[0]) if len(texts)==1 and line.text else line)
                            document = LyricDocument(lines,document.warnings,document.offset_ms)
                    except (OSError,ValueError,UnicodeError):
                        pass
                if self._stamp(path)!=stamp:
                    return None
                message = (f'QQ 音乐歌词已同步 · {len(document.lines)} 句 · 本地缓存' if document else
                           '本曲暂无歌词或为纯音乐，桌面保持空白。 · 本地缓存')
                result = LyricResult(document,message,source='cache',cache_signature=signature)
                self._decoded[signature] = result
                while len(self._decoded)>32:
                    self._decoded.popitem(last=False)
                self.status = '歌词缓存：'+str(path.parent)
                return result
            except (OSError,ValueError,UnicodeError) as error:
                self.status = '歌词缓存：'+str(error)+'；使用联网回退。'
                return None
