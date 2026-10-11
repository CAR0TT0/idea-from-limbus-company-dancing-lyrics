"""Anonymous QQ Music lyric reads. No cookies, audio URLs or third-party services."""
from dataclasses import dataclass, replace
from concurrent.futures import CancelledError
import base64
import html
import json
import math
import re
import unicodedata
import urllib.parse
import urllib.request

from lrc import LyricDocument, parse_lrc, chinese_translation
from qqmusic_http import discard_connection, lyric_connection, open_request

MAX_BYTES = 2 * 1024 * 1024
SEARCH_MODULE = "music.search.SearchCgiService"


@dataclass(frozen=True)
class LyricResult:
    document: LyricDocument | None
    message: str
    mid: str = ""
    source: str = "network"
    cache_signature: tuple = ()


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", html.unescape(value)).casefold().split())


def artist_names(value):
    # QQ's SMTC uses display aliases such as Mili (ミリー), while search uses Mili.
    return {normalized(re.sub(r"\s*[（(][^()（）]*[)）]\s*$", "", name))
            for name in re.split(r"[/、,，;&]", value) if name.strip()}


def request_json(url, body=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body else None,
                                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/",
                                             "Content-Type": "application/json"})
    with open_request(request, timeout=5) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("QQ 音乐歌词响应过大")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("code", 0) != 0:
        raise ValueError("QQ 音乐歌词接口暂不可用")
    return data


def select_song(songs, title, artist, duration_ms, album=""):
    """Reject covers, versions, ambiguous matches and unknown durations/artists."""
    if not artist_names(artist) or duration_ms <= 0 or not isinstance(songs, list):
        return None
    matches = {}
    for song in songs[:60]:
        if not isinstance(song, dict):
            continue
        name = song.get("title", song.get("name", ""))
        singers = song.get("singer", [])
        duration, mid = song.get("interval"), song.get("mid")
        if not isinstance(name, str) or normalized(name) != normalized(title):
            continue
        if (not isinstance(singers, list) or any(not isinstance(s, dict) or not isinstance(s.get("name"), str)
                                               for s in singers)):
            continue
        names = set().union(*(artist_names(s["name"]) for s in singers))
        if names != artist_names(artist):
            continue
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or abs(duration * 1000 - duration_ms) > 2000):
            continue
        candidate_album = song.get("album", {})
        if not isinstance(candidate_album, dict):
            continue
        album_title = candidate_album.get("title", candidate_album.get("name", ""))
        if album and (not isinstance(album_title, str) or normalized(album_title) != normalized(album)):
            continue
        if isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9]{14}", mid):
            matches[mid] = song
    return next(iter(matches.values())) if len(matches) == 1 else None


def _decode(text, encoded):
    if not isinstance(text, str) or len(text) > MAX_BYTES:
        raise ValueError("QQ 音乐歌词内容无效")
    if encoded and text:
        text = base64.b64decode(text, validate=True).decode("utf-8-sig")
    return html.unescape(text)


def parse_lyrics(data, encoded=True):
    if not isinstance(data, dict) or data.get("crypt", 0) != 0 or data.get("qrc", 0) != 0:
        raise ValueError("QQ 音乐未返回可读取的整句歌词")
    text = _decode(data.get("lyric", ""), encoded)
    if not text.strip():
        return None
    markers = ("纯音乐，请欣赏", "纯音乐,请欣赏", "此歌曲为没有填词的纯音乐")
    if any(marker in text for marker in markers):
        return None
    document = parse_lrc(text)
    if len(document.lines) > 5000 or any(len(line.text) > 2048 or line.start_ms > 86400000
                                       for line in document.lines):
        raise ValueError("QQ 音乐歌词超出支持范围")
    # Translation is optional: damaged translations must not discard original LRC.
    try:
        translated = parse_lrc(_decode(data.get("trans", ""), encoded))
    except (ValueError, UnicodeError, TypeError):
        return document
    by_time = {line.start_ms + translated.offset_ms: line.text for line in translated.lines
               if chinese_translation(line.text)}
    lines = []
    for line in document.lines:
        candidates = [value for stamp, value in by_time.items()
                      if abs(stamp - line.start_ms - document.offset_ms) <= 250]
        lines.append(replace(line, translation=candidates[0]) if len(candidates) == 1 and line.text else line)
    return LyricDocument(lines, document.warnings, document.offset_ms)


def _checkpoint(cancelled):
    if cancelled():
        raise CancelledError()


def _search_song(snapshot, cancelled):
    # Search's query parser is case/order sensitive for some collaborations.
    # Preserve the client's display order and spelling; normalize only to compare.
    singers = [re.sub(r"\s*[（(][^()（）]*[)）]\s*$", "", name).strip()
               for name in re.split(r"[/、,，;&]", snapshot.artist) if name.strip()]
    # QQ sometimes rejects a title+artists query with code 2001 even when the
    # title-only or artist-first query works. Bound discovery to three distinct
    # queries, never broaden the identity checks or repeat a failed query.
    artist_query = ' '.join(singers)
    queries = dict.fromkeys((f"{snapshot.title} {artist_query}".strip(), snapshot.title,
                             f"{artist_query} {snapshot.title}".strip()))
    candidates, received, last_error = [], False, None
    for query in queries:
        _checkpoint(cancelled)
        try:
            data = request_json("https://u.y.qq.com/cgi-bin/musicu.fcg", {
                SEARCH_MODULE: {"module": SEARCH_MODULE, "method": "DoSearchForQQMusicDesktop",
                                "param": {"search_type": 0, "query": query, "page_num": 1, "num_per_page": 20}}})
            response = data.get(SEARCH_MODULE, {})
            if not isinstance(response, dict) or response.get("code") != 0:
                raise ValueError("QQ 音乐歌曲搜索暂不可用")
            try:
                songs = response["data"]["body"]["song"]["list"]
            except (KeyError, TypeError):
                raise ValueError("QQ 音乐歌曲搜索返回格式无效") from None
            if not isinstance(songs, list):
                raise ValueError("QQ 音乐歌曲搜索返回格式无效")
            candidates.extend(songs[:20])
            received = True
            song = select_song(candidates, snapshot.title, snapshot.artist, snapshot.duration_ms, snapshot.album)
            if song is not None:
                return song
        except (OSError, ValueError, UnicodeError) as error:
            # An official endpoint may reject a request. Start the next fallback
            # on a fresh connection rather than pinning all queries to it.
            discard_connection()
            last_error = error
    if not received and last_error is not None:
        raise last_error
    return None


def load_song_lyrics(snapshot, *, cancelled=lambda: False):
    with lyric_connection():
        return _load_song_lyrics(snapshot, cancelled)


def _load_song_lyrics(snapshot, cancelled):
    song = _search_song(snapshot, cancelled)
    _checkpoint(cancelled)
    if song is None:
        return LyricResult(None, "未找到可靠匹配的 QQ 音乐歌词，可点击“重新获取歌词”重试。")
    mid = song["mid"]
    try:
        data = request_json("https://u.y.qq.com/cgi-bin/musicu.fcg", {
            "comm": {"ct": 24, "cv": 0}, "req_0": {
                "module": "music.musichallSong.PlayLyricInfo", "method": "GetPlayLyricInfo",
                "param": {"songMID": mid, "songID": song.get("id", 0), "qrc": 0, "crypt": 0,
                          "roma": 0, "trans": 1}}})
        part = data.get("req_0", {})
        if not isinstance(part, dict) or part.get("code") != 0:
            raise ValueError("QQ 音乐歌词接口暂不可用")
        document = parse_lyrics(part.get("data"))
    except (OSError, ValueError, UnicodeError):
        _checkpoint(cancelled)
        url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?" + urllib.parse.urlencode(
            {"songmid": mid, "format": "json", "nobase64": 1})
        document = parse_lyrics(request_json(url), encoded=False)
    message = f"QQ 音乐歌词已同步 · {len(document.lines)} 句" if document else "本曲暂无歌词或为纯音乐，桌面保持空白。"
    return LyricResult(document, message, mid)
