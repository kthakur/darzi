"""Fetch one job post from a URL and return it as Markdown (`new-run --url`).

This is the one network call of `new-run` (docs/SPEC.md section 6). The URL
comes from the owner or from an agent, and the page is untrusted data:

- `fetch()` gives a typed result for each failure. It never raises.
- The guard refuses a URL that is not http or https, and a host that has an
  address that is not a public internet address. The guard runs before the
  first request and again before each redirect hop. The fetch follows each
  redirect itself, at most MAX_REDIRECTS times.
- The body is read in chunks and the read stops at the size limit. Each read
  and each connection has a time limit.
- No link in the page is fetched. Only the redirects of the given URL are.

A request sends the default headers of httpx and the User-Agent
`darzi/<version>`. It sends no cookie of its own and no Authorization header.
"""

import codecs
import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import trafilatura

from darzi import __version__
from darzi.sourcing.model import FetchFailure, FetchOk, FetchReason, FetchResult

USER_AGENT = f"darzi/{__version__}"
# 5 MiB: large for an HTML page, but it limits the memory.
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
# The time limit of each read, and of each connection. There is no total limit.
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_SCHEMES = frozenset({"http", "https"})
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

Resolver = Callable[[str], list[str]]

_DETAILS: dict[FetchReason, str] = {
    FetchReason.INVALID_URL: "The URL is empty or is not a valid http or https URL.",
    FetchReason.DISALLOWED_SCHEME: "Only http and https URLs can be fetched.",
    FetchReason.BLOCKED_ADDRESS: (
        "The host has an address that is not a public internet address."
    ),
    FetchReason.UNRESOLVABLE: "The host name has no address.",
    FetchReason.TIMEOUT: "The server did not answer in time.",
    FetchReason.CONNECTION_FAILED: "The connection to the server failed.",
    FetchReason.BAD_REDIRECT: "The server sent a redirect with no valid target.",
    FetchReason.TOO_MANY_REDIRECTS: (
        f"The URL gave more than {MAX_REDIRECTS} redirects."
    ),
    FetchReason.EMPTY_CONTENT: (
        "The page has no text to read. The page can need JavaScript or a login."
    ),
}


def fetch(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    max_bytes: int = MAX_BODY_BYTES,
) -> FetchResult:
    """Fetch `url` and return the page as Markdown text, or a typed failure.

    `transport` and `resolver` are for the tests: a fake transport and a fake
    name lookup. The CLI does not pass them, so the fetch uses the network and
    the system name lookup.
    """
    resolve = resolver if resolver is not None else _system_resolver
    url = url.strip()
    if not url:
        return _failure(FetchReason.INVALID_URL, url)
    with _client(transport) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            refusal = check_url(current, resolve)
            if refusal is not None:
                return _failure(refusal, current)
            hop = _get(client, current, max_bytes)
            if hop.failure is not None:
                return hop.failure
            if hop.location is not None:
                target = _join(current, hop.location)
                if target is None:
                    return _failure(FetchReason.BAD_REDIRECT, current)
                current = target
                continue
            assert hop.body is not None and hop.response is not None
            return _page(hop.body, hop.response, current)
    return _failure(FetchReason.TOO_MANY_REDIRECTS, url)


# The guard


def check_url(url: str, resolver: Resolver) -> FetchReason | None:
    """Return None when `url` can be fetched, else the reason of the refusal.

    The guard refuses a scheme that is not http or https, and a host with an
    address that is not a public internet address (see `is_blocked`). It tests
    each address that the name lookup gives.

    Known gap: the guard looks up the host name, and then httpx looks up the
    name again when it connects. A name server can give a public address to
    the guard and a private address to httpx (DNS rebinding). A connection that
    uses only the tested address closes this gap; that is a later work item.
    """
    try:
        split = urlsplit(url)
    except ValueError:
        return FetchReason.INVALID_URL
    if split.scheme.lower() not in _SCHEMES:
        return FetchReason.DISALLOWED_SCHEME
    host = split.hostname
    if not host:
        return FetchReason.INVALID_URL
    try:
        addresses = resolver(host)
    except OSError:
        return FetchReason.UNRESOLVABLE
    except ValueError:
        # A host name that the name lookup cannot encode, for example an
        # international name with a label that is too long (UnicodeError).
        return FetchReason.INVALID_URL
    if not addresses:
        return FetchReason.UNRESOLVABLE
    if any(is_blocked(address) for address in addresses):
        return FetchReason.BLOCKED_ADDRESS
    return None


def is_blocked(address: str) -> bool:
    """Return True when `address` is not a public internet address.

    The test of the source (private, loopback, link-local, reserved, multicast,
    unspecified) stays. The port also refuses each address that Python does not
    mark as global (for example 100.64.0.0/10) and each IPv6 site-local address
    (fec0::/10). An IPv4 address inside IPv6 (::ffff:a.b.c.d) is tested as IPv4.
    An address that does not parse is refused.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.is_site_local:
            return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _system_resolver(host: str) -> list[str]:
    """Return the addresses of `host` from the system name lookup."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


# One request


@dataclass(frozen=True)
class _Hop:
    """The result of one request: a redirect target, a failure, or a body."""

    location: str | None = None
    failure: FetchFailure | None = None
    body: bytes | None = None
    response: httpx.Response | None = None


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    """Return a client that does not follow redirects (fetch() does that)."""
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


def _get(client: httpx.Client, url: str, max_bytes: int) -> _Hop:
    """Make one GET request with a streamed body. Each failure becomes a _Hop."""
    try:
        with client.stream("GET", url) as response:
            if response.is_redirect:
                return _Hop(location=response.headers.get("location", ""))
            code = response.status_code
            content_type = response.headers.get("content-type", "")
            if code >= 400:
                detail = f"The server gave HTTP status {code}."
                return _Hop(failure=_failure(FetchReason.HTTP_ERROR, url, detail, code))
            if not _is_html(content_type):
                detail = f"The page is not HTML (content type {content_type!r})."
                return _Hop(failure=_failure(FetchReason.NON_HTML, url, detail, code))
            too_large = _failure(
                FetchReason.TOO_LARGE,
                url,
                f"The page is larger than {max_bytes} bytes.",
                code,
            )
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                return _Hop(failure=too_large)
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    return _Hop(failure=too_large)
            return _Hop(body=bytes(body), response=response)
    except httpx.TimeoutException:
        return _Hop(failure=_failure(FetchReason.TIMEOUT, url))
    except httpx.HTTPError as exc:
        detail = f"The connection to the server failed ({type(exc).__name__})."
        return _Hop(failure=_failure(FetchReason.CONNECTION_FAILED, url, detail))
    except httpx.InvalidURL:
        # httpx refuses some URLs that the guard accepts, for example a URL
        # with a control character. The source does not catch this error.
        return _Hop(failure=_failure(FetchReason.INVALID_URL, url))


# The page


def _page(body: bytes, response: httpx.Response, url: str) -> FetchResult:
    """Return the Markdown text of an HTML body, or EMPTY_CONTENT."""
    content_type = response.headers.get("content-type", "")
    html = body.decode(_charset(content_type), errors="replace")
    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
    )
    if not text:
        return _failure(FetchReason.EMPTY_CONTENT, url, status=response.status_code)
    return FetchOk(
        text=text,
        title=_title(html),
        final_url=url,
        status_code=response.status_code,
        content_type=content_type,
        byte_count=len(body),
    )


def _title(html: str) -> str | None:
    """Return the title from the page metadata, else from the <title> tag."""
    # trafilatura 2 always returns a Document; the title can be None.
    title = trafilatura.extract_metadata(html).title
    if title:
        return str(title)
    match = _TITLE_RE.search(html)
    return match.group(1).strip() if match else None


def _is_html(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in _HTML_TYPES


def _charset(content_type: str) -> str:
    """Return the charset of `content_type`, or UTF-8.

    A charset that Python does not know gives UTF-8. The source raises
    LookupError for it.
    """
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        name = value.strip().strip('"')
        if key.lower() == "charset" and name:
            try:
                return codecs.lookup(name).name
            except LookupError:
                return "utf-8"
    return "utf-8"


def _join(base: str, location: str) -> str | None:
    """Return the redirect target joined to `base`, or None when it is not valid."""
    target = location.strip()
    if not target:
        return None
    try:
        return str(httpx.URL(base).join(target))
    except (httpx.InvalidURL, ValueError):
        return None


def _failure(
    reason: FetchReason,
    url: str,
    detail: str | None = None,
    status: int | None = None,
) -> FetchFailure:
    return FetchFailure(
        reason=reason, detail=detail or _DETAILS[reason], url=url, status_code=status
    )
