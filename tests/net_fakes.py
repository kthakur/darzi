"""A fake httpcore network backend for the fetch tests: no socket, no network.

The fake records each connect (the address and the port), each TLS start (the
server name and the TLS context) and the bytes of each request. It answers with
a canned HTTP response for each address.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpcore


def http_response(
    status: int = 200,
    body: bytes = b"",
    headers: Iterable[tuple[bytes, bytes]] = ((b"content-type", b"text/html"),),
) -> bytes:
    """Return the bytes of an HTTP/1.1 response with a Content-Length."""
    lines = [f"HTTP/1.1 {status} X".encode()]
    lines += [name + b": " + value for name, value in headers]
    lines.append(b"content-length: " + str(len(body)).encode())
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


@dataclass
class TlsStart:
    """One call of start_tls: the server name and the TLS context."""

    server_hostname: str | None
    ssl_context: ssl.SSLContext


@dataclass
class FakeStream(httpcore.NetworkStream):
    """A network stream that records what is written and reads a canned answer."""

    answer: bytes
    written: bytearray = field(default_factory=bytearray)
    tls: list[TlsStart] = field(default_factory=lambda: [])
    closed: bool = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self.answer = self.answer[:max_bytes], self.answer[max_bytes:]
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer

    def close(self) -> None:
        self.closed = True

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.tls.append(TlsStart(server_hostname, ssl_context))
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


@dataclass
class FakeBackend(httpcore.NetworkBackend):
    """A backend that records each connect and answers from `answers`.

    `answers` maps an address to the response bytes. An address in `fail`
    raises the error that is given for it.
    """

    answers: dict[str, bytes] = field(default_factory=lambda: {})
    fail: dict[str, Exception] = field(default_factory=lambda: {})
    connects: list[tuple[str, int]] = field(default_factory=lambda: [])
    streams: list[FakeStream] = field(default_factory=lambda: [])

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        self.connects.append((host, port))
        if host in self.fail:
            raise self.fail[host]
        stream = FakeStream(self.answers.get(host, http_response(404)))
        self.streams.append(stream)
        return stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise AssertionError("the fetch must not connect to a Unix socket")

    def sleep(self, seconds: float) -> None:
        pass
