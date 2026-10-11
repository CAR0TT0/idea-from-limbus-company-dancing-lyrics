"""Scoped HTTPS reuse with urllib's proxy, TLS and HTTP error handling intact."""
from contextlib import contextmanager
import http.client
import threading
import urllib.error
import urllib.request
import urllib.response

_local = threading.local()


class _KeepAliveHTTPS(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__()
        self.connection = None
        self.key = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = self.key = None

    def https_open(self, request):
        headers = dict(request.unredirected_hdrs)
        headers.update({key: value for key, value in request.headers.items() if key not in headers})
        headers = {key.title(): value for key, value in headers.items()}
        headers["Connection"] = "keep-alive"
        tunnel_headers = {}
        tunnel = getattr(request, "_tunnel_host", None)
        if tunnel and "Proxy-Authorization" in headers:
            tunnel_headers["Proxy-Authorization"] = headers.pop("Proxy-Authorization")
        key = (request.host, tunnel, tuple(tunnel_headers.items()))
        reused = self.connection is not None and key == self.key
        if not reused:
            self.close()
            self.connection = http.client.HTTPSConnection(request.host, timeout=request.timeout,
                                                          context=self._context)
            self.key = key
            if tunnel:
                self.connection.set_tunnel(tunnel, headers=tunnel_headers)
        # A server may close an idle socket. Retry that stale connection once;
        # timeouts and TLS verification failures retain the normal bounded failure.
        for attempt in range(2):
            try:
                self.connection.request(request.get_method(), request.selector, request.data, headers,
                                        encode_chunked=request.has_header("Transfer-encoding"))
                response = self.connection.getresponse()
                if not 200 <= response.status < 300:
                    # urllib's error/redirect handlers own this response. Do not
                    # reuse a socket whose error body has not been consumed.
                    self.close()
                return urllib.response.addinfourl(response, response.headers, request.full_url, response.status)
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as error:
                self.connection.close()
                if not reused or attempt:
                    raise urllib.error.URLError(error) from error
            except (OSError, http.client.HTTPException) as error:
                self.close()
                raise urllib.error.URLError(error) from error


@contextmanager
def lyric_connection():
    """Keep one connection for a search/lyric chain; always close it afterwards."""
    previous = getattr(_local, "opener", None)
    handler = _KeepAliveHTTPS()
    previous_handler = getattr(_local, "handler", None)
    _local.handler = handler
    _local.opener = urllib.request.build_opener(handler)
    try:
        yield
    finally:
        handler.close()
        _local.opener = previous
        _local.handler = previous_handler


def discard_connection():
    handler = getattr(_local, "handler", None)
    if handler is not None:
        handler.close()


def open_request(request, timeout):
    opener = getattr(_local, "opener", None)
    return opener.open(request, timeout=timeout) if opener else urllib.request.urlopen(request, timeout=timeout)
