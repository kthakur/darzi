"""The fetch connects only to an address that its guard tested (F-6).

No test uses the network. The fake inner backend records each connect and
gives a canned HTTP response. The autouse fixture `no_network` in conftest.py
fails a test that opens a socket or looks up a name.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpcore
import pytest

from darzi import cli
from darzi.cli import main
from darzi.sourcing import fetch as fetch_module
from darzi.sourcing.fetch import check_url, fetch
from darzi.sourcing.model import FetchFailure, FetchOk, FetchReason
from darzi.sourcing.pinned import PinnedBackend

from .net_fakes import FakeBackend, http_response
from .test_fetch import JOB_HTML, PASTE, URL

SRC = Path(__file__).resolve().parents[1] / "src" / "darzi"
SOURCING = SRC / "sourcing"
METADATA_IP = str(ipaddress.IPv4Address(0xA9FEA9FE))
FULL_WIDTH_HOST = (
    "".join(chr(0xFF41 + ord(c) - ord("a")) for c in "jobs") + ".example.com"
)
PUBLIC_V4 = "1.1.1.1"
PUBLIC_V6 = "2606:4700::1"
SHORT = "https://short.example.com/1"
INVALID_REDIRECT = "The server sent a redirect to a URL that is not valid."

# The results of check_url before F-6, recorded as a table. The same table must
# pass after F-6: the pin adds no permission. Each row: the URL, the addresses
# that the fake name lookup gives (None: the lookup gives the host as it is,
# as for an address literal), and the result.
BLOCKED = FetchReason.BLOCKED_ADDRESS
INVALID = FetchReason.INVALID_URL
GUARD_TABLE: list[tuple[str, list[str] | None, FetchReason | None]] = [
    # Public addresses.
    ("https://jobs.example.com/", ["1.1.1.1"], None),
    ("https://jobs.example.com/", ["2606:4700::1"], None),
    ("https://jobs.example.com/", ["::ffff:8.8.8.8"], None),
    ("https://jobs.example.com/", ["1.1.1.1", "2606:4700::1"], None),
    # Loopback.
    ("https://jobs.example.com/", ["127.0.0.1"], BLOCKED),
    ("https://jobs.example.com/", ["127.1.2.3"], BLOCKED),
    ("https://jobs.example.com/", ["::1"], BLOCKED),
    # Private IPv4 ranges.
    ("https://jobs.example.com/", ["10.0.0.5"], BLOCKED),
    ("https://jobs.example.com/", ["172.16.0.1"], BLOCKED),
    ("https://jobs.example.com/", ["192.168.1.1"], BLOCKED),
    # Link-local and the cloud metadata address.
    ("https://jobs.example.com/", ["169.254.0.1"], BLOCKED),
    ("https://jobs.example.com/", [METADATA_IP], BLOCKED),
    ("https://jobs.example.com/", ["fe80::1"], BLOCKED),
    # Shared address space (CGNAT).
    ("https://jobs.example.com/", ["100.64.0.1"], BLOCKED),
    # Multicast, reserved, unspecified, documentation, benchmark.
    ("https://jobs.example.com/", ["224.0.0.1"], BLOCKED),
    ("https://jobs.example.com/", ["ff02::1"], BLOCKED),
    ("https://jobs.example.com/", ["240.0.0.1"], BLOCKED),
    ("https://jobs.example.com/", ["0.0.0.0"], BLOCKED),
    ("https://jobs.example.com/", ["::"], BLOCKED),
    ("https://jobs.example.com/", ["192.0.2.1"], BLOCKED),
    ("https://jobs.example.com/", ["203.0.113.1"], BLOCKED),
    ("https://jobs.example.com/", ["2001:db8::1"], BLOCKED),
    ("https://jobs.example.com/", ["198.18.0.1"], BLOCKED),
    # IPv4 inside IPv6.
    ("https://jobs.example.com/", ["::ffff:127.0.0.1"], BLOCKED),
    ("https://jobs.example.com/", ["::ffff:a9fe:a9fe"], BLOCKED),
    ("https://jobs.example.com/", ["::ffff:10.0.0.5"], BLOCKED),
    # Unique-local and site-local IPv6.
    ("https://jobs.example.com/", ["fc00::1"], BLOCKED),
    ("https://jobs.example.com/", ["fd12:3456::1"], BLOCKED),
    ("https://jobs.example.com/", ["fec0::1"], BLOCKED),
    # A name with one public and one private address, in each order.
    ("https://jobs.example.com/", ["1.1.1.1", "10.0.0.5"], BLOCKED),
    ("https://jobs.example.com/", ["192.168.1.1", "1.1.1.1"], BLOCKED),
    ("https://jobs.example.com/", ["2606:4700::1", "fd12:3456::1"], BLOCKED),
    # Text that is not an address, and no address.
    ("https://jobs.example.com/", ["not-an-address"], BLOCKED),
    ("https://jobs.example.com/", [], FetchReason.UNRESOLVABLE),
    # Address literals in the URL.
    ("http://127.0.0.1/", None, BLOCKED),
    ("http://127.0.0.1:8080/", None, BLOCKED),
    ("http://[::1]/", None, BLOCKED),
    (f"http://{METADATA_IP}/latest/", None, BLOCKED),
    ("http://[::ffff:127.0.0.1]/", None, BLOCKED),
    ("http://1.1.1.1/", None, None),
    # User-info.
    ("https://user:secret@example.com/", ["1.1.1.1"], INVALID),
    ("https://user@example.com/", ["1.1.1.1"], INVALID),
    ("https://:secret@example.com/", ["1.1.1.1"], INVALID),
    # International host names.
    ("https://bücher.example.com/", ["1.1.1.1"], None),
    ("https://xn--bcher-kva.example.com/", ["1.1.1.1"], None),
    ("https://straße.example.com/", ["1.1.1.1"], INVALID),
    ("https://σοφος.example.com/", ["1.1.1.1"], INVALID),
    ("https://xn--a.example.com/", ["1.1.1.1"], INVALID),
    ("https://xn--zz.example.com/", ["1.1.1.1"], INVALID),
    (f"https://{FULL_WIDTH_HOST}/", ["1.1.1.1"], INVALID),
    # A leading hyphen: accepted today, and the addresses are tested.
    ("https://-acme.example.com/", ["1.1.1.1"], None),
    ("https://-acme.example.com/", ["10.0.0.5"], BLOCKED),
    # Ports.
    ("https://jobs.example.com:8443/", ["1.1.1.1"], None),
    ("https://jobs.example.com:8443/", ["127.0.0.1"], BLOCKED),
    # The guard does not test the port number.
    ("https://jobs.example.com:99999/", ["1.1.1.1"], None),
    # Schemes, and an empty host.
    ("file:///etc/hosts", ["1.1.1.1"], FetchReason.DISALLOWED_SCHEME),
    ("ftp://jobs.example.com/x", ["1.1.1.1"], FetchReason.DISALLOWED_SCHEME),
    ("https:///no-host", ["1.1.1.1"], INVALID),
]


def _table_resolver(addresses: list[str] | None) -> Callable[[str], list[str]]:
    def resolve(host: str) -> list[str]:
        return [host] if addresses is None else list(addresses)

    return resolve


@pytest.mark.parametrize(("url", "addresses", "expected"), GUARD_TABLE)
def test_the_guard_gives_the_recorded_result(
    url: str, addresses: list[str] | None, expected: FetchReason | None
) -> None:
    assert check_url(url, _table_resolver(addresses)) == expected


# Helpers

PAGE = http_response(
    200, JOB_HTML.encode(), [(b"content-type", b"text/html; charset=utf-8")]
)


def _redirect(location: bytes) -> bytes:
    return http_response(302, headers=[(b"location", location)])


class Rebinding:
    """A fake name lookup that gives a public address on the first call for a
    host, and a private address on each later call (DNS rebinding)."""

    def __init__(self, first: dict[str, str], later: str = "127.0.0.1") -> None:
        self.first = first
        self.later = later
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        known = host in self.calls
        self.calls.append(host)
        return [self.later] if known else [self.first[host]]


def _public(host: str) -> list[str]:
    return [PUBLIC_V4]


def _both(host: str) -> list[str]:
    return [PUBLIC_V4, PUBLIC_V6]


# 2. DNS rebinding: one lookup at each hop, and the connect uses its address


def test_rebinding_connects_only_to_the_address_that_the_guard_tested() -> None:
    resolver = Rebinding({"jobs.example.com": PUBLIC_V4})
    backend = FakeBackend(answers={PUBLIC_V4: PAGE})
    result = fetch(URL, resolver=resolver, network_backend=backend)
    assert isinstance(result, FetchOk), result
    assert resolver.calls == ["jobs.example.com"]
    assert backend.connects == [(PUBLIC_V4, 443)]


def test_rebinding_at_a_redirect_hop() -> None:
    resolver = Rebinding(
        {"short.example.com": PUBLIC_V4, "jobs.example.com": PUBLIC_V6}
    )
    backend = FakeBackend(answers={PUBLIC_V4: _redirect(URL.encode()), PUBLIC_V6: PAGE})
    result = fetch(SHORT, resolver=resolver, network_backend=backend)
    assert isinstance(result, FetchOk), result
    assert result.final_url == URL
    assert resolver.calls == ["short.example.com", "jobs.example.com"]
    assert backend.connects == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]


def test_a_second_hop_to_the_same_host_looks_up_the_name_again() -> None:
    # The second lookup gives a private address: the guard refuses it, and
    # the fetch makes no second connect.
    resolver = Rebinding({"jobs.example.com": PUBLIC_V4})
    backend = FakeBackend(answers={PUBLIC_V4: _redirect(b"/acme/designer")})
    result = fetch(
        "https://jobs.example.com/old", resolver=resolver, network_backend=backend
    )
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchReason.BLOCKED_ADDRESS
    assert resolver.calls == ["jobs.example.com", "jobs.example.com"]
    assert backend.connects == [(PUBLIC_V4, 443)]


def test_two_hops_on_one_host_use_two_connections() -> None:
    lookups: list[str] = []

    def resolve(host: str) -> list[str]:
        lookups.append(host)
        return [PUBLIC_V4] if len(lookups) == 1 else [PUBLIC_V6]

    backend = FakeBackend(
        answers={PUBLIC_V4: _redirect(b"/acme/designer"), PUBLIC_V6: PAGE}
    )
    result = fetch(
        "https://jobs.example.com/old", resolver=resolve, network_backend=backend
    )
    assert isinstance(result, FetchOk), result
    assert backend.connects == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]
    assert len(backend.streams) == 2


# 3. The backend refuses each connect that is not pinned


def test_the_backend_refuses_a_connect_with_no_pin() -> None:
    inner = FakeBackend()
    backend = PinnedBackend(inner)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("jobs.example.com", 443)
    assert inner.connects == []


@pytest.mark.parametrize(
    "host", ["other.example.com", PUBLIC_V4, "jobs.example.com.", "localhost"]
)
def test_the_backend_refuses_a_host_that_is_not_pinned(host: str) -> None:
    inner = FakeBackend(answers={PUBLIC_V4: PAGE})
    backend = PinnedBackend(inner)
    backend.pin("jobs.example.com", [PUBLIC_V4])
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp(host, 443)
    assert inner.connects == []


def test_the_backend_refuses_the_pinned_host_after_the_pin_is_cleared() -> None:
    inner = FakeBackend(answers={PUBLIC_V4: PAGE})
    backend = PinnedBackend(inner)
    backend.pin("jobs.example.com", [PUBLIC_V4])
    backend.connect_tcp("jobs.example.com", 443)
    backend.clear()
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("jobs.example.com", 443)
    assert inner.connects == [(PUBLIC_V4, 443)]


def test_the_backend_refuses_a_unix_socket_and_an_empty_pin() -> None:
    inner = FakeBackend()
    backend = PinnedBackend(inner)
    with pytest.raises(ValueError, match="at least one address"):
        backend.pin("jobs.example.com", [])
    backend.pin("jobs.example.com", [PUBLIC_V4])
    with pytest.raises(httpcore.ConnectError):
        backend.connect_unix_socket("darzi-test.sock")
    assert inner.connects == []


def test_the_pin_holds_only_during_the_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = PinnedBackend(FakeBackend(answers={PUBLIC_V4: PAGE}))
    seen: list[object] = []
    original = fetch_module._get  # pyright: ignore[reportPrivateUsage]

    def record(*args: Any) -> Any:
        seen.append(backend._pin)  # pyright: ignore[reportPrivateUsage]
        return original(*args)

    monkeypatch.setattr(fetch_module, "_get", record)
    monkeypatch.setattr(fetch_module, "PinnedBackend", lambda inner: backend)  # pyright: ignore[reportUnknownLambdaType]
    assert isinstance(fetch(URL, resolver=_public), FetchOk)
    assert seen == [("jobs.example.com", (PUBLIC_V4,))]
    assert backend._pin is None  # pyright: ignore[reportPrivateUsage]


# 4. The next accepted address, and IPv6


def test_the_next_accepted_address_is_tried_when_the_first_fails() -> None:
    backend = FakeBackend(
        answers={PUBLIC_V6: PAGE},
        fail={PUBLIC_V4: httpcore.ConnectError("refused")},
    )
    result = fetch(URL, resolver=_both, network_backend=backend)
    assert isinstance(result, FetchOk), result
    assert backend.connects == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpcore.ConnectError("refused"), FetchReason.CONNECTION_FAILED),
        (httpcore.ConnectTimeout("slow"), FetchReason.TIMEOUT),
    ],
)
def test_when_each_address_fails_the_reason_stays(
    error: Exception, reason: FetchReason
) -> None:
    backend = FakeBackend(fail={PUBLIC_V4: error, PUBLIC_V6: error})
    result = fetch(URL, resolver=_both, network_backend=backend)
    assert isinstance(result, FetchFailure)
    assert result.reason == reason
    assert backend.connects == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]


def test_an_ipv6_address_is_the_address_of_the_connect() -> None:
    backend = FakeBackend(answers={PUBLIC_V6: PAGE})
    result = fetch(URL, resolver=lambda host: [PUBLIC_V6], network_backend=backend)
    assert isinstance(result, FetchOk), result
    assert backend.connects == [(PUBLIC_V6, 443)]


def test_a_port_in_the_url_is_the_port_of_the_connect() -> None:
    backend = FakeBackend(answers={PUBLIC_V4: PAGE})
    url = "http://jobs.example.com:8080/acme/designer"
    assert isinstance(fetch(url, resolver=_public, network_backend=backend), FetchOk)
    assert backend.connects == [(PUBLIC_V4, 8080)]
    assert backend.streams[0].tls == []


# 5. TLS and the Host header use the host name


def test_tls_and_the_host_header_use_the_host_name() -> None:
    backend = FakeBackend(answers={PUBLIC_V4: PAGE})
    assert isinstance(fetch(URL, resolver=_public, network_backend=backend), FetchOk)
    [stream] = backend.streams
    [tls] = stream.tls
    assert tls.server_hostname == "jobs.example.com"
    assert tls.ssl_context.check_hostname is True
    assert tls.ssl_context.verify_mode == ssl.CERT_REQUIRED
    request = bytes(stream.written)
    assert request.startswith(b"GET /acme/designer HTTP/1.1\r\n")
    assert b"\r\nHost: jobs.example.com\r\n" in request
    assert PUBLIC_V4.encode() not in request
    assert b"cookie" not in request.lower()
    assert b"authorization" not in request.lower()


# 6. The real httpcore pool and backend connect to the address, not the name


def test_the_real_backend_gets_the_address_never_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def record(address: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(tuple(address))
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", record)
    result = fetch(URL, resolver=_both)
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchReason.CONNECTION_FAILED
    assert calls == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]


# 7. Each refused case of the table makes no connect


REFUSED = [row for row in GUARD_TABLE if row[2] is not None]


@pytest.mark.parametrize(("url", "addresses", "expected"), REFUSED)
def test_a_refused_url_makes_no_connect(
    url: str, addresses: list[str] | None, expected: FetchReason
) -> None:
    backend = FakeBackend(answers={PUBLIC_V4: PAGE, PUBLIC_V6: PAGE})
    result = fetch(url, resolver=_table_resolver(addresses), network_backend=backend)
    assert isinstance(result, FetchFailure)
    assert result.reason == expected
    assert backend.connects == []


def test_a_redirect_to_each_refused_address_makes_no_second_connect() -> None:
    one = [row[1] for row in REFUSED if row[1] and len(row[1]) == 1]
    blocked = sorted({addresses[0] for addresses in one} - {PUBLIC_V4})
    assert len(blocked) > 20
    for address in blocked:
        backend = FakeBackend(
            answers={PUBLIC_V4: _redirect(b"https://in.example.com/")}
        )

        def resolve(host: str, address: str = address) -> list[str]:
            return [PUBLIC_V4] if host == "short.example.com" else [address]

        result = fetch(SHORT, resolver=resolve, network_backend=backend)
        assert isinstance(result, FetchFailure)
        assert result.reason == BLOCKED, address
        assert backend.connects == [(PUBLIC_V4, 443)], address


# 8. One place for the name lookup, and no other way to connect


def _files_with(pattern: str) -> list[str]:
    found = re.compile(pattern)
    return [
        str(path.relative_to(SRC))
        for path in sorted(SOURCING.rglob("*.py"))
        if found.search(path.read_text("utf-8"))
    ]


def test_the_name_lookup_is_in_one_place() -> None:
    assert _files_with(r"\bgetaddrinfo\b") == ["sourcing/fetch.py"]
    text = (SOURCING / "fetch.py").read_text("utf-8")
    assert len(re.findall(r"\bgetaddrinfo\b", text)) == 1
    assert "def _system_resolver" in text


def test_no_other_way_to_open_a_connection() -> None:
    for pattern in (
        r"\bcreate_connection\b",
        r"\.connect\(",
        r"socket\.socket\b",
        r"\burlopen\b",
        r"\bHTTPTransport\b",
        r"\bproxy=",
    ):
        assert _files_with(pattern) == [], pattern
    for pattern in (r"\bSyncBackend\b", r"\bConnectionPool\b"):
        assert _files_with(pattern) == ["sourcing/pinned.py"], pattern
    pinned = (SOURCING / "pinned.py").read_text("utf-8")
    assert "network_backend=backend" in pinned
    assert "verify=True" in pinned
    # The client of the fetch always gets a transport that darzi made.
    fetch_text = (SOURCING / "fetch.py").read_text("utf-8")
    assert fetch_text.count("httpx.Client(") == 1
    assert "transport = PinnedTransport(PinnedBackend())" in fetch_text


# 9. A redirect to a URL that is not valid


BAD_TARGETS = [
    b"https://xn--zz.example.com/",
    b"https://xn--a.example.com/acme",
    b"//xn--zz.example.com/",
    ("https://" + FULL_WIDTH_HOST + "/").encode(),
]


@pytest.mark.parametrize("location", BAD_TARGETS)
def test_a_redirect_to_a_url_that_is_not_valid_is_refused(location: bytes) -> None:
    backend = FakeBackend(answers={PUBLIC_V4: _redirect(location), PUBLIC_V6: PAGE})
    resolved: list[str] = []

    def resolve(host: str) -> list[str]:
        resolved.append(host)
        return [PUBLIC_V4]

    result = fetch(SHORT, resolver=resolve, network_backend=backend)
    assert isinstance(result, FetchFailure)
    assert result.reason == FetchReason.INVALID_URL
    assert result.refused
    assert result.detail == INVALID_REDIRECT
    assert result.url == SHORT
    assert resolved == ["short.example.com"]
    assert backend.connects == [(PUBLIC_V4, 443)]


def test_a_redirect_to_a_host_with_a_leading_hyphen_is_followed() -> None:
    # No change: the guard tests the addresses of this host, as before.
    backend = FakeBackend(
        answers={PUBLIC_V4: _redirect(b"https://-acme.example.com/"), PUBLIC_V6: PAGE}
    )
    resolver = Rebinding(
        {"short.example.com": PUBLIC_V4, "-acme.example.com": PUBLIC_V6}
    )
    result = fetch(SHORT, resolver=resolver, network_backend=backend)
    assert isinstance(result, FetchOk), result
    assert backend.connects == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]


def test_the_cli_gives_exit_code_2_for_a_redirect_that_is_not_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "master.yaml").write_text("{}\n", encoding="utf-8")
    backend = FakeBackend(answers={PUBLIC_V4: _redirect(BAD_TARGETS[0])})

    def fake(url: str) -> FetchOk | FetchFailure:
        return fetch(url, resolver=_public, network_backend=backend)

    monkeypatch.setattr(cli, "fetch", fake)
    args = ["new-run", "--company", "Acme", "--role", "Designer", "--url", SHORT]
    assert main([*args, "--repo", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "(invalid_url)" in err
    assert INVALID_REDIRECT in err
    assert err.endswith(PASTE + "\n")
    assert backend.connects == [(PUBLIC_V4, 443)]
