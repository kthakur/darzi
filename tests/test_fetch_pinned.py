"""The fetch connects only to an address that its guard tested (F-6).

No test uses the network. The fake inner backend records each connect and
gives a canned HTTP response. The autouse fixture `no_network` in conftest.py
fails a test that opens a socket or looks up a name.
"""

from __future__ import annotations

import ipaddress

import pytest

from darzi.sourcing.fetch import check_url
from darzi.sourcing.model import FetchReason

METADATA_IP = str(ipaddress.IPv4Address(0xA9FEA9FE))
FULL_WIDTH_HOST = "ｊｏｂｓ.example.com"

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
    ("https://user:secret@jobs.example.com/", ["1.1.1.1"], INVALID),
    ("https://user@jobs.example.com/", ["1.1.1.1"], INVALID),
    ("https://:secret@jobs.example.com/", ["1.1.1.1"], INVALID),
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


def _table_resolver(addresses: list[str] | None):  # noqa: ANN202
    def resolve(host: str) -> list[str]:
        return [host] if addresses is None else list(addresses)

    return resolve


@pytest.mark.parametrize(("url", "addresses", "expected"), GUARD_TABLE)
def test_the_guard_gives_the_recorded_result(
    url: str, addresses: list[str] | None, expected: FetchReason | None
) -> None:
    assert check_url(url, _table_resolver(addresses)) == expected
