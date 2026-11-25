"""Tests of the compression rules of the fetch (`unpack.py`, work item F-7).

No test makes a network call: each response comes from httpx.MockTransport or
from the fake backend of `net_fakes.py`. Each compressed input is made here
with `gzip` or `zlib`; no test depends on the exact bytes that zlib makes.
"""

from __future__ import annotations

import functools
import gzip
import random
import time
import tracemalloc
import zlib
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from darzi.sourcing import unpack as unpack_module
from darzi.sourcing.fetch import MAX_BODY_BYTES, fetch
from darzi.sourcing.model import FetchFailure, FetchOk, FetchReason
from darzi.sourcing.unpack import (
    STEP,
    TruncatedError,
    UnsupportedEncodingError,
    coding,
    counted,
    unpack,
)

from .net_fakes import FakeBackend, http_response
from .test_fetch import JOB_HTML, PUBLIC_IP, URL

UNSUPPORTED = "The server sent the page in a compression that darzi does not read."
TRUNCATED = "The compressed page ended early."
DAMAGED = "The connection to the server failed (DecodingError)."
TOO_LARGE = f"The page is larger than {MAX_BODY_BYTES} bytes."

JOB = JOB_HTML.encode("utf-8")
PAGES: dict[str, bytes] = {
    "ascii": (
        b"<html><head><title>Test page</title></head>"
        b"<body><p>Acme looks for a test writer.</p></body></html>"
    ),
    "utf8": JOB,
    "cp1252": (
        "<html><head><title>Café</title></head><body><p>Acme: “test” café, "
        "naïve, 5 €.</p></body></html>"
    ).encode("windows-1252"),
    "large": b"".join(b"<p>Acme test line %d.</p>\n" % i for i in range(42000)),
    "empty": b"",
}
SIZES = [None, 7, 64 * 1024]


def _raw_deflate(data: bytes) -> bytes:
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


def _gzip(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


def _same(data: bytes) -> bytes:
    return data


# name: (the Content-Encoding value or None, the packer)
ENCODINGS: dict[str, tuple[str | None, Callable[[bytes], bytes]]] = {
    "none": (None, _same),
    "identity": ("identity", _same),
    "gzip": ("gzip", _gzip),
    "x-gzip": ("x-gzip", _gzip),
    "deflate": ("deflate", zlib.compress),
    "raw-deflate": ("deflate", _raw_deflate),
}


class Chunks(httpx.SyncByteStream):
    """A response body that arrives in chunks of `size` bytes, not read yet."""

    def __init__(self, raw: bytes, size: int | None = None) -> None:
        self.raw = raw
        self.size = size or max(len(raw), 1)
        self.started = False

    def __iter__(self) -> Iterator[bytes]:
        self.started = True
        for start in range(0, len(self.raw), self.size):
            yield self.raw[start : start + self.size]


def _response(
    raw: bytes,
    encoding: str | None = None,
    *,
    size: int | None = None,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    stream: Chunks | None = None,
    extra: tuple[tuple[str, str], ...] = (),
) -> httpx.Response:
    headers = [("content-type", content_type)]
    if encoding is not None:
        headers.append(("content-encoding", encoding))
    headers += extra
    body = stream if stream is not None else Chunks(raw, size)
    return httpx.Response(status, headers=headers, stream=body)


def _get(
    responder: Callable[[httpx.Request], httpx.Response],
    max_bytes: int = MAX_BODY_BYTES,
) -> FetchOk | FetchFailure:
    return fetch(
        URL,
        transport=httpx.MockTransport(responder),
        resolver=lambda host: [PUBLIC_IP],
        max_bytes=max_bytes,
    )


def _page(raw: bytes, encoding: str | None, **kwargs: Any) -> FetchOk | FetchFailure:
    max_bytes = kwargs.pop("max_bytes", MAX_BODY_BYTES)
    return _get(lambda request: _response(raw, encoding, **kwargs), max_bytes)


def _failed(result: FetchOk | FetchFailure, reason: FetchReason, detail: str) -> None:
    assert isinstance(result, FetchFailure)
    assert result.reason == reason
    assert result.detail == detail


def _darzi(response: httpx.Response, limit: int = MAX_BODY_BYTES) -> bytes:
    kind = coding(response.headers)
    return b"".join(unpack(counted(response.iter_raw(), limit), kind, limit))


# 1. The same bytes as httpx for a normal page


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("name", list(ENCODINGS))
@pytest.mark.parametrize("page", list(PAGES))
def test_the_unpack_gives_the_same_bytes_as_httpx(
    page: str, name: str, size: int | None
) -> None:
    header, packer = ENCODINGS[name]
    raw = packer(PAGES[page])
    # httpx does not read x-gzip; it reads the same bytes as gzip.
    httpx_header = "gzip" if header == "x-gzip" else header
    expected = b"".join(_response(raw, httpx_header, size=size).iter_bytes())
    assert expected == PAGES[page]
    assert _darzi(_response(raw, header, size=size)) == expected


@pytest.mark.parametrize("name", ["gzip", "x-gzip", "deflate", "raw-deflate"])
def test_a_compressed_page_gives_the_same_text_and_title(name: str) -> None:
    header, packer = ENCODINGS[name]
    plain = _page(JOB, None)
    packed = _page(packer(JOB), header, size=7)
    assert isinstance(plain, FetchOk) and isinstance(packed, FetchOk)
    assert packed.text == plain.text
    assert packed.title == plain.title == "Product Designer"
    assert packed.byte_count == plain.byte_count == len(JOB)


# 2. The one-layer bomb


@functools.cache
def _bomb(kind: str) -> bytes:
    """Return 64 MiB of zero bytes, packed in steps (the test holds 1 MiB)."""
    wbits = zlib.MAX_WBITS | 16 if kind == "gzip" else zlib.MAX_WBITS
    packer = zlib.compressobj(9, zlib.DEFLATED, wbits)
    zeros = bytes(1024 * 1024)
    parts = [packer.compress(zeros) for _ in range(64)]
    return b"".join([*parts, packer.flush()])


def _chunks(raw: bytes, size: int) -> Iterator[bytes]:
    for start in range(0, len(raw), size):
        yield raw[start : start + size]


@pytest.mark.parametrize("kind", ["gzip", "deflate"])
def test_the_unpack_step_stops_a_bomb_at_the_limit(kind: str) -> None:
    raw = _bomb(kind)
    assert len(raw) < 128 * 1024
    start = time.monotonic()
    kind_read = coding(httpx.Headers({"content-encoding": kind}))
    pieces = unpack(_chunks(raw, 64 * 1024), kind_read, MAX_BODY_BYTES)
    sizes = [len(piece) for piece in pieces]
    assert STEP == 64 * 1024
    assert max(sizes) <= STEP
    assert sum(sizes) == MAX_BODY_BYTES + 1
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("kind", ["gzip", "deflate"])
def test_a_bomb_is_too_large_and_uses_little_memory(kind: str) -> None:
    raw = _bomb(kind)
    headers = ((b"content-type", b"text/html"), (b"content-encoding", kind.encode()))
    backend = FakeBackend(answers={PUBLIC_IP: http_response(200, raw, headers)})
    start = time.monotonic()
    tracemalloc.start()
    try:
        result = fetch(URL, resolver=lambda host: [PUBLIC_IP], network_backend=backend)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed = time.monotonic() - start
    print(f"tracemalloc peak ({kind}): {peak} bytes")
    _failed(result, FetchReason.TOO_LARGE, TOO_LARGE)
    assert peak < 8 * 1024 * 1024
    assert elapsed < 5


# 3. The stacked bomb


@pytest.mark.parametrize(
    "headers",
    [
        (("content-encoding", "gzip, gzip"),),
        (("content-encoding", "gzip"), ("content-encoding", "gzip")),
        (("content-encoding", "gzip, deflate"),),
    ],
)
def test_a_stacked_bomb_is_refused_before_any_unpack(
    headers: tuple[tuple[str, str], ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _gzip(_bomb("gzip"))
    assert len(raw) < 4096
    unpacks: list[str] = []
    decompressors: list[str] = []
    real_unpack = unpack_module.unpack
    real_decompressobj = zlib.decompressobj

    def spy_unpack(*args: Any, **kwargs: Any) -> Iterator[bytes]:
        unpacks.append("unpack")
        return real_unpack(*args, **kwargs)

    def spy_decompressobj(*args: Any, **kwargs: Any) -> Any:
        decompressors.append("decompressobj")
        return real_decompressobj(*args, **kwargs)

    monkeypatch.setattr(unpack_module, "unpack", spy_unpack)
    monkeypatch.setattr(unpack_module.zlib, "decompressobj", spy_decompressobj)
    stream = Chunks(raw)
    result = _get(lambda request: _response(raw, stream=stream, extra=headers))
    _failed(result, FetchReason.CONNECTION_FAILED, UNSUPPORTED)
    assert unpacks == [] and decompressors == []
    assert not stream.started


# 4. The rules


@pytest.mark.parametrize("value", ["br", "zstd", "compress", "x-test", "gzip2"])
def test_an_unknown_compression_is_refused_before_the_body_is_read(
    value: str,
) -> None:
    stream = Chunks(JOB)
    result = _get(lambda request: _response(JOB, value, stream=stream))
    _failed(result, FetchReason.CONNECTION_FAILED, UNSUPPORTED)
    assert not stream.started


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], "identity"),
        ([""], "identity"),
        (["identity"], "identity"),
        (["Identity, ,identity"], "identity"),
        (["gzip"], "gzip"),
        (["GZIP"], "gzip"),
        ([" gzip "], "gzip"),
        (["identity, gzip"], "gzip"),
        (["gzip,"], "gzip"),
        (["identity", "gzip"], "gzip"),
        (["x-gzip"], "gzip"),
        (["X-Gzip"], "gzip"),
        (["deflate"], "deflate"),
        (["DEFLATE"], "deflate"),
    ],
)
def test_the_encoding_list_is_read_as_httpx_reads_it(
    values: list[str], expected: str
) -> None:
    headers = httpx.Headers([("content-encoding", value) for value in values])
    assert coding(headers) == expected


@pytest.mark.parametrize(
    "values", [["gzip, gzip"], ["gzip", "deflate"], ["br"], ["gzip, br"], ["zstd"]]
)
def test_the_encoding_list_refuses_layers_and_unknown_values(
    values: list[str],
) -> None:
    headers = httpx.Headers([("content-encoding", value) for value in values])
    with pytest.raises(UnsupportedEncodingError):
        coding(headers)


@pytest.mark.parametrize("value", ["GZIP", " gzip ", "identity, gzip", "gzip,"])
def test_a_gzip_value_in_another_form_gives_the_page(value: str) -> None:
    plain = _page(JOB, None)
    packed = _page(_gzip(JOB), value)
    assert isinstance(plain, FetchOk) and isinstance(packed, FetchOk)
    assert packed.text == plain.text


@pytest.mark.parametrize(
    ("value", "raw"),
    [
        ("gzip", _gzip(b"")[:10] + b"\xff" * 20),
        ("deflate", b"\x78\x9c" + b"\xff" * 20),
        ("deflate", b"\xff" * 20),
    ],
)
def test_damaged_data_is_a_connection_failure(value: str, raw: bytes) -> None:
    _failed(_page(raw, value), FetchReason.CONNECTION_FAILED, DAMAGED)


def _cuts(packed: bytes) -> list[bytes]:
    """Return copies of `packed` that end early, at several places."""
    boundary = (len(packed) // 2) // 7 * 7
    return [packed[: len(packed) // 2], packed[:boundary], packed[:-1]]


@pytest.mark.parametrize("name", ["gzip", "deflate", "raw-deflate"])
def test_compressed_data_that_ends_early_is_refused(name: str) -> None:
    header, packer = ENCODINGS[name]
    for raw in _cuts(packer(PAGES["large"])):
        for size in (None, 7):
            result = _page(raw, header, size=size)
            _failed(result, FetchReason.CONNECTION_FAILED, TRUNCATED)


def test_a_gzip_header_with_no_data_is_refused() -> None:
    header_only = _gzip(JOB)[:10]
    _failed(_page(header_only, "gzip"), FetchReason.CONNECTION_FAILED, TRUNCATED)
    with pytest.raises(TruncatedError):
        list(unpack(iter([header_only]), "gzip", MAX_BODY_BYTES))


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "tail", [b"trailing test data", _gzip(b"<p>A second member.</p>")]
)
def test_data_after_the_gzip_end_is_ignored_as_httpx_does(
    tail: bytes, size: int | None
) -> None:
    raw = _gzip(JOB) + tail
    expected = b"".join(_response(raw, "gzip", size=size).iter_bytes())
    assert expected == JOB
    assert _darzi(_response(raw, "gzip", size=size)) == JOB
    result = _page(raw, "gzip", size=size)
    assert isinstance(result, FetchOk) and result.byte_count == len(JOB)


def test_data_after_the_gzip_end_counts_against_the_raw_limit() -> None:
    raw = _gzip(JOB) + bytes(len(JOB))
    result = _page(raw, "gzip", max_bytes=len(JOB) + 10)
    _failed(
        result, FetchReason.TOO_LARGE, f"The page is larger than {len(JOB) + 10} bytes."
    )


@pytest.mark.parametrize("value", ["gzip", "deflate"])
def test_an_empty_compressed_body_is_an_empty_page(value: str) -> None:
    result = _page(b"", value)
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchReason.EMPTY_CONTENT


def test_the_encoding_of_a_redirect_is_not_read() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return _response(b"x", "br", status=302, extra=(("location", URL),))
        return _response(JOB, None)

    result = fetch(
        "https://jobs.example.com/start",
        transport=httpx.MockTransport(respond),
        resolver=lambda host: [PUBLIC_IP],
    )
    assert isinstance(result, FetchOk) and result.final_url == URL


def test_the_encoding_of_an_http_error_is_not_read() -> None:
    stream = Chunks(b"x")
    result = _get(lambda request: _response(b"x", "br", status=404, stream=stream))
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchReason.HTTP_ERROR
    assert not stream.started


def test_the_encoding_of_a_page_that_is_not_html_is_not_read() -> None:
    stream = Chunks(b"x")
    result = _get(
        lambda request: _response(
            b"x", "br", content_type="application/json", stream=stream
        )
    )
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchReason.NON_HTML
    assert not stream.started


# 6. The raw size


def test_a_plain_page_of_the_limit_passes_and_one_byte_more_is_too_large() -> None:
    assert isinstance(_page(JOB, None, max_bytes=len(JOB)), FetchOk)
    result = _page(JOB, None, max_bytes=len(JOB) - 1)
    _failed(
        result, FetchReason.TOO_LARGE, f"The page is larger than {len(JOB) - 1} bytes."
    )


@pytest.mark.parametrize("name", ["gzip", "deflate", "raw-deflate"])
def test_a_compressed_page_of_the_limit_passes(name: str) -> None:
    header, packer = ENCODINGS[name]
    result = _page(packer(JOB), header, max_bytes=len(JOB))
    assert isinstance(result, FetchOk) and result.byte_count == len(JOB)
    result = _page(packer(JOB), header, max_bytes=len(JOB) - 1)
    assert isinstance(result, FetchFailure) and result.reason == FetchReason.TOO_LARGE


def test_a_compressed_page_with_raw_bytes_past_the_limit_is_too_large() -> None:
    # Random bytes do not compress: the gzip data is larger than the page.
    page = random.Random(0).randbytes(4000)
    raw = _gzip(page)
    assert len(raw) > len(page)
    limit = len(page) + (len(raw) - len(page)) // 2
    result = _page(raw, "gzip", content_type="text/html", max_bytes=limit)
    _failed(result, FetchReason.TOO_LARGE, f"The page is larger than {limit} bytes.")


def test_the_raw_count_stops_at_the_limit() -> None:
    with pytest.raises(unpack_module.TooLargeError):
        list(counted(iter([b"abc", b"def"]), 5))
    assert list(counted(iter([b"abc", b"de"]), 5)) == [b"abc", b"de"]
