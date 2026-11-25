"""Read the compression of a page and unpack it in small steps (`new-run --url`).

A server can send a page compressed, with a `Content-Encoding` header. The fetch
reads the raw bytes and unpacks them here, with `zlib`. Each unpack step gives
at most STEP bytes, and the unpack stops when the page is larger than the size
limit. So a small compressed page cannot fill the memory.

The rules (docs/SPEC.md section 6):

- The encoding list is read as httpx reads it: all header lines, split at
  commas, stripped, in lower case. Empty values and `identity` are removed.
- No value: the raw bytes are the page. One `gzip` (or `x-gzip`) or one
  `deflate`: one layer is unpacked. `deflate` is first read with the zlib
  wrapper, then (if the first read fails) as raw deflate, as httpx does.
- Two or more values, or a value that darzi does not know, are refused
  before the body is read (UnsupportedEncodingError).
- Damaged data raises httpx.DecodingError. Data that ends before the end of
  the compressed stream raises TruncatedError. An empty body is an empty page.
- Data after the end of the compressed stream (for example a second gzip
  member) is not unpacked.
"""

import zlib
from collections.abc import Iterable, Iterator
from typing import Literal

import httpx

# The largest piece that one unpack step gives.
STEP = 64 * 1024
# The request header: the compressions that unpack() reads.
ACCEPT_ENCODING = "gzip, deflate"

Coding = Literal["identity", "gzip", "deflate"]

_CODINGS: dict[str, Coding] = {"gzip": "gzip", "x-gzip": "gzip", "deflate": "deflate"}


class UnsupportedEncodingError(Exception):
    """The page has a compression that darzi does not read, or more than one."""


class TruncatedError(Exception):
    """The compressed page ended before the end of the compressed stream."""


class TooLargeError(Exception):
    """The raw bytes of the page are more than the size limit."""


def coding(headers: httpx.Headers) -> Coding:
    """Return the one compression of a response, or "identity" for none.

    Raise UnsupportedEncodingError for an unknown value or for two or more.
    """
    values = [
        value.strip().lower()
        for value in headers.get_list("content-encoding", split_commas=True)
    ]
    layers = [value for value in values if value and value != "identity"]
    if not layers:
        return "identity"
    if len(layers) > 1 or layers[0] not in _CODINGS:
        raise UnsupportedEncodingError
    return _CODINGS[layers[0]]


def counted(chunks: Iterable[bytes], limit: int) -> Iterator[bytes]:
    """Give each chunk of `chunks`. Raise TooLargeError past `limit` bytes."""
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise TooLargeError
        yield chunk


def unpack(chunks: Iterable[bytes], kind: Coding, limit: int) -> Iterator[bytes]:
    """Give the unpacked bytes of `chunks`, in pieces of at most STEP bytes.

    For a compressed page, the total of the pieces stops at `limit` + 1 bytes:
    the caller then knows that the page is too large. For "identity", the
    chunks are given as they are, and the caller counts them.
    """
    if kind == "identity":
        yield from chunks
        return
    if kind == "gzip":
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    else:
        decompressor = zlib.decompressobj()
    first = kind == "deflate"
    fed = False
    total = 0
    for chunk in chunks:
        if not chunk or decompressor.eof:
            continue
        fed = True
        data = chunk
        while True:
            room = min(STEP, limit + 1 - total)
            try:
                piece = decompressor.decompress(data, room)
            except zlib.error as exc:
                if not first:
                    raise httpx.DecodingError(str(exc)) from exc
                # The first read of `deflate` failed with the zlib wrapper:
                # read the same bytes as raw deflate, as httpx does.
                first = False
                decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                continue
            first = False
            if piece:
                total += len(piece)
                yield piece
                if total > limit:
                    return
            data = decompressor.unconsumed_tail
            if decompressor.eof or (not data and len(piece) < room):
                break
    if fed and not decompressor.eof:
        raise TruncatedError
