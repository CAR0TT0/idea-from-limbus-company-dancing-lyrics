"""Latency regressions: connection reuse and obsolete track cancellation."""
from concurrent.futures import CancelledError
import io
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from lrc import LyricDocument, LyricLine
from qqmusic import QQMusicPlayer, QQSnapshot
import qqmusic_http as transport
import qqmusic_lyrics as lyrics

APP = QApplication.instance() or QApplication([])
URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
SNAPSHOT = QQSnapshot("Hello", "Artist", "Album", 60000, 1000, False)


def response(status=200):
    value = io.BytesIO(b'{"code":0}')
    value.status, value.headers, value.msg = status, {}, "test"
    return value


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        proxy = patch.object(transport.urllib.request, "getproxies", return_value={})
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_search_lyric_chain_reuses_verified_https_and_closes_at_scope_end(self):
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = [response(), response()]
            with transport.lyric_connection():
                self.assertEqual(lyrics.request_json(URL, {"search": 1}), {"code": 0})
                self.assertEqual(lyrics.request_json(URL, {"lyric": 1}), {"code": 0})
                factory.assert_called_once_with("u.y.qq.com", timeout=5, context=None)
                connection.close.assert_not_called()
                headers = connection.request.call_args.args[3]
                self.assertEqual(headers["Connection"], "keep-alive")
                self.assertNotIn("Cookie", headers)
            connection.close.assert_called_once()

    def test_host_change_closes_previous_connection(self):
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            factory.return_value.getresponse.side_effect = [response(), response()]
            with transport.lyric_connection():
                lyrics.request_json(URL)
                lyrics.request_json("https://c.y.qq.com/lyric/")
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(factory.return_value.close.call_count, 2)

    def test_proxy_tunnel_credentials_stay_on_proxy(self):
        handler = transport._KeepAliveHTTPS()
        request = urllib.request.Request(URL, headers={"Proxy-Authorization": "Basic test-proxy"})
        request.set_proxy("proxy.invalid:8080", "https")
        request.timeout = 5
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            factory.return_value.getresponse.return_value = response()
            handler.https_open(request).close()
            factory.assert_called_once_with("proxy.invalid:8080", timeout=5, context=None)
            factory.return_value.set_tunnel.assert_called_once_with("u.y.qq.com", headers={
                "Proxy-Authorization": "Basic test-proxy"})
            self.assertNotIn("Proxy-Authorization", factory.return_value.request.call_args.args[3])
            handler.close()

    def test_stale_reused_connection_retries_once(self):
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = [response(), transport.http.client.RemoteDisconnected(), response()]
            with transport.lyric_connection():
                lyrics.request_json(URL)
                lyrics.request_json(URL)
            self.assertEqual(connection.request.call_count, 3)
            self.assertEqual(factory.call_count, 1)

    def test_timeout_and_tls_error_do_not_retry_or_skip_verification(self):
        import ssl
        for error in (TimeoutError("timeout"), ssl.SSLCertVerificationError("untrusted")):
            with self.subTest(error=type(error).__name__), patch.object(transport.http.client, "HTTPSConnection") as factory:
                factory.return_value.request.side_effect = error
                with transport.lyric_connection(), self.assertRaises(urllib.error.URLError):
                    lyrics.request_json(URL)
                self.assertEqual(factory.return_value.request.call_count, 1)
                factory.return_value.close.assert_called()

    def test_http_errors_close_socket_and_keep_urllib_error_behavior(self):
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            factory.return_value.getresponse.return_value = response(503)
            with transport.lyric_connection(), self.assertRaises(urllib.error.HTTPError) as caught:
                lyrics.request_json(URL)
            self.assertEqual(caught.exception.code, 503)
            factory.return_value.close.assert_called_once()

    def test_exception_and_nested_scope_restore_previous_opener(self):
        with transport.lyric_connection():
            outer = transport._local.opener
            with self.assertRaises(ValueError), transport.lyric_connection():
                self.assertIsNot(transport._local.opener, outer)
                raise ValueError("bad JSON")
            self.assertIs(transport._local.opener, outer)
        self.assertIsNone(transport._local.opener)

    def test_rejected_query_discards_connection_before_next_fallback(self):
        with patch.object(transport.http.client, "HTTPSConnection") as factory:
            replies = []
            for _ in range(3):
                rejected = io.BytesIO(json.dumps({lyrics.SEARCH_MODULE: {"code": 2001}}).encode())
                rejected.status, rejected.headers, rejected.msg = 200, {}, "OK"
                replies.append(rejected)
            factory.return_value.getresponse.side_effect = replies
            with self.assertRaises(ValueError):
                lyrics.load_song_lyrics(SNAPSHOT)
            self.assertEqual(factory.call_count, 3)
            self.assertEqual(factory.return_value.close.call_count, 3)

    def test_parallel_tracks_have_separate_connections(self):
        barrier = threading.Barrier(2)
        openers, errors = [], []
        def run():
            try:
                with transport.lyric_connection():
                    openers.append(transport._local.opener)
                    barrier.wait(timeout=2)
                    self.assertEqual(lyrics.request_json(URL), {"code": 0})
            except Exception as error:
                errors.append(error)
        def connection(*args, **kwargs):
            value = MagicMock()
            value.getresponse.return_value = response()
            return value
        with patch.object(transport.http.client, "HTTPSConnection", side_effect=connection) as factory:
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertFalse(errors, errors)
            self.assertEqual(factory.call_count, 2)
        self.assertIsNot(*openers)


class CancellationTests(unittest.TestCase):
    def test_cancelled_search_does_not_try_remaining_queries(self):
        cancelled = threading.Event()
        def request(*args):
            cancelled.set()
            return {lyrics.SEARCH_MODULE: {"code": 2001}}
        with patch.object(lyrics, "request_json", side_effect=request) as requesting:
            with self.assertRaises(CancelledError):
                lyrics.load_song_lyrics(SNAPSHOT, cancelled=cancelled.is_set)
            self.assertEqual(requesting.call_count, 1)

    def test_cancel_after_matching_prevents_lyric_request(self):
        cancelled = threading.Event()
        candidate = dict(title="Hello", singer=[{"name": "Artist"}], interval=60,
                         mid="004D7rAM2SPnrR", album={"title": "Album"})
        def request(*args):
            cancelled.set()
            return {lyrics.SEARCH_MODULE: {"code": 0, "data": {"body": {"song": {"list": [candidate]}}}}}
        with patch.object(lyrics, "request_json", side_effect=request) as requesting:
            with self.assertRaises(CancelledError):
                lyrics.load_song_lyrics(SNAPSHOT, cancelled=cancelled.is_set)
            self.assertEqual(requesting.call_count, 1)

    def test_cancel_during_lyric_error_skips_legacy_fallback(self):
        cancelled = threading.Event()
        with patch.object(lyrics, "_search_song", return_value={"mid": "004D7rAM2SPnrR"}):
            def fail(*args):
                cancelled.set()
                raise OSError("offline")
            with patch.object(lyrics, "request_json", side_effect=fail) as requesting:
                with self.assertRaises(CancelledError):
                    lyrics.load_song_lyrics(SNAPSHOT, cancelled=cancelled.is_set)
                self.assertEqual(requesting.call_count, 1)

    def test_next_song_finishes_while_old_network_request_is_still_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def loader(snapshot):
            if snapshot.title == "Hello":
                entered.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("test old request")
            return lyrics.LyricResult(LyricDocument([LyricLine(1000, snapshot.title)], []), "已同步")
        player = QQMusicPlayer(loader=loader, autostart=False)
        documents = []
        player.document_changed.connect(documents.append)
        try:
            player.apply_snapshot(SNAPSHOT)
            self.assertTrue(entered.wait(timeout=1))
            old = player._future
            player.apply_snapshot(QQSnapshot("New", "Artist", "Album", 60000, 1000, False))
            player._future.result(timeout=1)
            self.assertFalse(old.done(), "new lyrics must not wait for the old network timeout")
            player._poll()
            self.assertEqual(documents[-1].lines[0].text, "New")
            release.set()
            old.result(timeout=1)
            player._poll()
            self.assertEqual(documents[-1].lines[0].text, "New")
            self.assertEqual(player._retired, [])
        finally:
            release.set()
            player.stop()


if __name__ == "__main__":
    unittest.main()
