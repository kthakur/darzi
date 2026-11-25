"""Connect only to an address that the guard of the fetch tested (F-6).

The guard of `fetch.py` looks up the host name of each hop one time and tests
each address. This module makes sure that the connection goes to one of those
addresses, and not to an address of a second name lookup:

- `PinnedBackend` is an httpcore network backend. It never looks up a name.
  For each hop, the fetch pins one host name and the addresses that the guard
  accepted. A connect for the pinned name goes to the first accepted address
  that answers, in the order of the lookup. A connect for any other name, or
  with no pin, is refused.
- `PinnedTransport` is an httpx transport that owns one httpcore connection
  pool, and that pool uses the pinned backend. darzi builds the pool itself, so
  the pool never makes a default backend. The pool keeps no idle connection, so
  no hop uses a connection of an earlier hop.

The URL of each request keeps the host name. So the TLS check (the server name
and the certificate) and the `Host` header use the host name, not the address.
Certificate verification is always on.

Only the public interfaces of httpx and httpcore are used. The socket is opened
by `httpcore.SyncBackend.connect_tcp`, which gets an address literal, never a
host name.
"""

from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import httpcore
import httpx

# Each httpcore error and the httpx error that the fetch catches for it. The
# order matters: a subclass comes before its base class.
_ERRORS: tuple[tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.ProxyError, httpx.ProxyError),
)


class PinnedBackend(httpcore.NetworkBackend):
    """A network backend that connects only to the pinned addresses of one host.

    `inner` opens the connection to an address literal. The fetch gives no
    `inner`, so the backend uses `httpcore.SyncBackend`. The tests give a fake
    that records each connect. The pin applies to each `inner`.
    """

    def __init__(self, inner: httpcore.NetworkBackend | None = None) -> None:
        """Wrap `inner`, or `httpcore.SyncBackend`. The backend has no pin."""
        self._inner = inner if inner is not None else httpcore.SyncBackend()
        self._pin: tuple[str, tuple[str, ...]] | None = None

    def pin(self, host: str, addresses: Sequence[str]) -> None:
        """Permit a connect to `host`, only to `addresses`, until `clear()`."""
        if not host or not addresses:
            raise ValueError("A pin needs a host name and at least one address.")
        self._pin = (host, tuple(addresses))

    def clear(self) -> None:
        """Remove the pin. Each connect is then refused."""
        self._pin = None

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        """Connect to the first pinned address of `host` that answers."""
        pin = self._pin
        if pin is None or host != pin[0]:
            raise httpcore.ConnectError(
                f"The host {host!r} has no address that the guard tested."
            )
        options = list(socket_options) if socket_options is not None else None
        # pin() refuses an empty list, so this error is always replaced.
        error: Exception = httpcore.ConnectError("The pin has no address.")
        for address in pin[1]:
            try:
                return self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                # As the system connect function does: try the next address.
                error = exc
        raise error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        """Refuse: the fetch never connects to a Unix socket."""
        raise httpcore.ConnectError("The fetch does not connect to a Unix socket.")

    def sleep(self, seconds: float) -> None:
        """Wait, as the inner backend does."""
        self._inner.sleep(seconds)


class PinnedTransport(httpx.BaseTransport):
    """An httpx transport whose connection pool uses a `PinnedBackend`."""

    def __init__(self, backend: PinnedBackend) -> None:
        """Make the connection pool. It uses only `backend` to connect."""
        self._pool = httpcore.ConnectionPool(
            # The same TLS settings as the default transport of an httpx client
            # with trust_env=False: certificate and host name checks are on.
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send `request` through the pool, and map the httpcore errors."""
        stream = request.stream
        if not isinstance(stream, httpx.SyncByteStream):
            raise TypeError("The pinned transport sends only a synchronous request.")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=stream,
            extensions=request.extensions,
        )
        with _mapped_errors(request):
            core_response = self._pool.handle_request(core_request)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_ResponseStream(core_response, request),
            extensions=core_response.extensions,
        )

    def close(self) -> None:
        """Close each connection of the pool."""
        self._pool.close()


class _ResponseStream(httpx.SyncByteStream):
    """The body of an httpcore response, with the httpcore errors mapped."""

    def __init__(self, response: httpcore.Response, request: httpx.Request) -> None:
        self._response = response
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        with _mapped_errors(self._request):
            yield from self._response.iter_stream()

    def close(self) -> None:
        self._response.close()


@contextmanager
def _mapped_errors(request: httpx.Request) -> Generator[None]:
    """Raise the httpx error for each httpcore error, as the transport of httpx does."""
    try:
        yield
    except Exception as exc:
        for core_error, error in _ERRORS:
            if isinstance(exc, core_error):
                raise error(str(exc), request=request) from exc
        raise
