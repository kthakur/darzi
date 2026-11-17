"""Fetch one job post (`sourcing/fetch.py`) and `facts new-run --url` (W-10).

No test uses the network. Each test gives the fetch a fake transport
(httpx.MockTransport) and a fake resolver. The autouse fixture `no_network` in
conftest.py fails a test that opens a socket or looks up a name.
"""

from __future__ import annotations

import datetime as dt
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from darzi import __version__, cli
from darzi.cli import main
from darzi.paths import exists
from darzi.runs.runs import APPLICATIONS, JOB, RUN_YAML
from darzi.sourcing.fetch import (
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    USER_AGENT,
    check_url,
    fetch,
    is_blocked,
)
from darzi.sourcing.model import FetchFailure, FetchOk, FetchReason

PUBLIC_IP = "1.1.1.1"
PRIVATE_IP = "127.0.0.1"
URL = "https://jobs.example.com/acme/designer"

JOB_HTML = (
    "<!DOCTYPE html><html><head>"
    "<title>Product Designer — Acme</title></head><body>"
    "<header><nav><a href='/'>Home</a><a href='/jobs'>Jobs</a></nav></header>"
    "<aside><h3>Related</h3><ul><li>Test Writer</li><li>Test Tester</li></ul>"
    "</aside><main><article><h1>Product Designer</h1>"
    "<p>Acme looks for a product designer to design the sign-up forms of its "
    "test products, with a small team of designers and engineers.</p>"
    "<p>You make prototypes, test them with users, and write the notes for the "
    "team. The role is remote, in the Springfield time zone.</p>"
    "<h2>Requirements</h2><ul><li>5 years of product design</li>"
    "<li>Figma</li><li>User tests</li></ul>"
    "<p>Apply on <a href='https://jobs.example.com/acme/apply'>the form</a>.</p>"
    "</article></main><footer>Acme test footer</footer></body></html>"
)

Responder = Callable[[httpx.Request], httpx.Response]
Resolver = Callable[[str], list[str]]


def _resolver(mapping: dict[str, list[str]] | None = None) -> Resolver:
    """Return a fake resolver: PUBLIC_IP for each host, except those in `mapping`."""

    def resolve(host: str) -> list[str]:
        if mapping is not None and host in mapping:
            return mapping[host]
        return [PUBLIC_IP]

    return resolve


def _fetch(
    url: str,
    responder: Responder,
    *,
    resolver: Resolver | None = None,
    max_bytes: int = MAX_BODY_BYTES,
) -> FetchOk | FetchFailure:
    return fetch(
        url,
        transport=httpx.MockTransport(responder),
        resolver=resolver if resolver is not None else _resolver(),
        max_bytes=max_bytes,
    )


def _html(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"}, content=JOB_HTML
    )


def _recording(calls: list[str], responder: Responder = _html) -> Responder:
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return responder(request)

    return respond


def _failed(result: FetchOk | FetchFailure, reason: FetchReason) -> FetchFailure:
    assert isinstance(result, FetchFailure)
    assert result.reason == reason
    assert result.detail
    return result


# A page that can be read


def test_page_becomes_markdown_with_a_title() -> None:
    result = _fetch(URL, _html)
    assert isinstance(result, FetchOk)
    assert "Product Designer" in result.text
    assert "5 years of product design" in result.text
    # The navigation and the side bar are not part of the text.
    assert "Related" not in result.text
    assert result.status_code == 200
    assert result.content_type == "text/html; charset=utf-8"
    assert result.title == "Product Designer"
    assert result.final_url == URL
    assert result.byte_count == len(JOB_HTML.encode())


def test_links_in_the_page_are_not_fetched() -> None:
    calls: list[str] = []
    assert isinstance(_fetch(URL, _recording(calls)), FetchOk)
    assert calls == [URL]


def test_a_request_sends_only_the_default_headers_and_the_user_agent() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _html(request)

    assert isinstance(_fetch(URL, respond), FetchOk)
    [request] = seen
    assert request.method == "GET"
    assert request.content == b""
    assert USER_AGENT == f"darzi/{__version__}"
    assert request.headers["user-agent"] == USER_AGENT
    assert sorted(request.headers.keys()) == [
        "accept",
        "accept-encoding",
        "connection",
        "host",
        "user-agent",
    ]


def test_an_unknown_charset_is_read_as_utf8() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=no-such-charset"},
            content=JOB_HTML.encode(),
        )

    result = _fetch(URL, respond)
    assert isinstance(result, FetchOk)
    assert "Product Designer" in result.text


# Failures of the server or of the page


def test_http_403_is_an_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"content-type": "text/html"}, content=b"no")

    result = _failed(_fetch(URL, respond), FetchReason.HTTP_ERROR)
    assert result.status_code == 403
    assert not result.refused


def test_a_page_that_is_not_html_is_an_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{}"
        )

    _failed(_fetch(URL, respond), FetchReason.NON_HTML)


def test_a_page_with_no_text_is_an_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html></html>"
        )

    _failed(_fetch(URL, respond), FetchReason.EMPTY_CONTENT)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_a_time_out_is_an_error(error: type[httpx.TimeoutException]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error("slow", request=request)

    _failed(_fetch(URL, respond), FetchReason.TIMEOUT)


def test_a_connection_failure_is_an_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _failed(_fetch(URL, respond), FetchReason.CONNECTION_FAILED)


def test_a_body_larger_than_the_limit_stops_the_read() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        big = b"<html><body>" + b"x" * 5000 + b"</body></html>"
        return httpx.Response(200, headers={"content-type": "text/html"}, content=big)

    _failed(_fetch(URL, respond, max_bytes=500), FetchReason.TOO_LARGE)


def test_a_declared_length_larger_than_the_limit_is_refused_before_the_read() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "10000000"},
            content=b"<html><body>small</body></html>",
        )

    _failed(_fetch(URL, respond, max_bytes=500), FetchReason.TOO_LARGE)


# Redirects


def test_a_redirect_chain_is_followed() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short.example.com":
            return httpx.Response(302, headers={"location": URL})
        return _html(request)

    result = _fetch("https://short.example.com/1", respond)
    assert isinstance(result, FetchOk)
    assert result.final_url == URL


def test_a_relative_redirect_is_joined_to_the_url() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/acme/designer"})
        return _html(request)

    result = _fetch("https://jobs.example.com/old", respond)
    assert isinstance(result, FetchOk)
    assert result.final_url == URL


def test_a_redirect_with_no_target_is_an_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": " "})

    _failed(_fetch(URL, respond), FetchReason.BAD_REDIRECT)


def test_a_redirect_loop_stops_at_the_limit() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": URL})

    _failed(_fetch(URL, respond), FetchReason.TOO_MANY_REDIRECTS)
    assert len(calls) == MAX_REDIRECTS + 1


def test_a_redirect_to_a_private_address_is_refused() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://inside.example.com/"})

    resolver = _resolver({"inside.example.com": [PRIVATE_IP]})
    result = _failed(
        _fetch(URL, respond, resolver=resolver), FetchReason.BLOCKED_ADDRESS
    )
    assert result.refused
    assert result.url == "http://inside.example.com/"
    assert calls == [URL]


def test_a_redirect_to_a_file_url_is_refused() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "file:///etc/hosts"})

    _failed(_fetch(URL, respond), FetchReason.DISALLOWED_SCHEME)
    assert calls == [URL]


# The guard


@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://jobs.example.com/x"])
def test_a_scheme_that_is_not_http_is_refused_with_no_request(url: str) -> None:
    calls: list[str] = []
    result = _failed(_fetch(url, _recording(calls)), FetchReason.DISALLOWED_SCHEME)
    assert result.refused
    assert calls == []


def test_a_private_address_is_refused_with_no_request() -> None:
    calls: list[str] = []
    resolver = _resolver({"inside.example.com": [PRIVATE_IP]})
    result = _fetch("http://inside.example.com/", _recording(calls), resolver=resolver)
    assert _failed(result, FetchReason.BLOCKED_ADDRESS).refused
    assert calls == []


@pytest.mark.parametrize(
    "address",
    [
        # The four kinds of the source test.
        "10.0.0.5",
        "192.168.1.1",
        "169.254.0.1",
        "::ffff:a9fe:a9fe",  # the cloud metadata address, as IPv6
        "::1",
        # More kinds that the source also refuses.
        "127.0.0.1",
        "172.16.0.1",
        "0.0.0.0",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
        "ff02::1",
        "240.0.0.1",
        "not-an-address",
        # Not public, and new in the port.
        "100.64.0.1",
        "fec0::1",
        "198.18.0.1",
        "192.0.2.1",
    ],
)
def test_an_address_that_is_not_public_is_refused(address: str) -> None:
    calls: list[str] = []
    resolver = _resolver({"inside.example.com": [address]})
    result = _fetch("https://inside.example.com/", _recording(calls), resolver=resolver)
    assert _failed(result, FetchReason.BLOCKED_ADDRESS).refused
    assert calls == []


@pytest.mark.parametrize("address", [PUBLIC_IP, "2606:4700::1", "::ffff:8.8.8.8"])
def test_a_public_address_is_permitted(address: str) -> None:
    assert not is_blocked(address)


def test_one_private_address_among_public_addresses_is_refused() -> None:
    resolver = _resolver({"jobs.example.com": [PUBLIC_IP, "10.0.0.5"]})
    _failed(_fetch(URL, _html, resolver=resolver), FetchReason.BLOCKED_ADDRESS)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://[::1]/"])
def test_an_address_in_the_url_is_tested(url: str) -> None:
    # The system name lookup gives back an address literal as it is.
    assert check_url(url, lambda host: [host]) == FetchReason.BLOCKED_ADDRESS


@pytest.mark.parametrize("url", ["", "   ", "https:///no-host"])
def test_an_empty_url_or_host_is_refused(url: str) -> None:
    calls: list[str] = []
    result = _failed(_fetch(url, _recording(calls)), FetchReason.INVALID_URL)
    assert result.refused
    assert calls == []


def test_a_name_that_does_not_resolve_is_an_error() -> None:
    def resolver(host: str) -> list[str]:
        raise OSError("no such name")

    result = _failed(_fetch(URL, _html, resolver=resolver), FetchReason.UNRESOLVABLE)
    assert not result.refused


def test_a_name_with_no_address_is_an_error() -> None:
    resolver = _resolver({"jobs.example.com": []})
    _failed(_fetch(URL, _html, resolver=resolver), FetchReason.UNRESOLVABLE)


def test_a_bad_international_host_name_is_refused() -> None:
    def resolver(host: str) -> list[str]:
        # The system name lookup encodes the name first, as here.
        host.encode("idna")
        return [PUBLIC_IP]

    url = "https://" + "x" * 64 + ".example.com/"
    calls: list[str] = []
    result = _fetch(url, _recording(calls), resolver=resolver)
    assert _failed(result, FetchReason.INVALID_URL).refused
    assert calls == []


def test_a_url_that_httpx_refuses_is_refused() -> None:
    calls: list[str] = []
    result = _fetch("https://jobs.example.com/\x00", _recording(calls))
    assert _failed(result, FetchReason.INVALID_URL).refused
    assert calls == []


def test_the_system_resolver_is_the_default(no_network: list[str]) -> None:
    # With no resolver, the guard uses the system name lookup. The fixture
    # stops it, so the fetch makes no network call.
    with pytest.raises(AssertionError):
        fetch(URL, transport=httpx.MockTransport(_html))
    assert no_network == ["getaddrinfo"]


def test_the_no_network_fixture_stops_a_connection(no_network: list[str]) -> None:
    with pytest.raises(AssertionError):
        socket.create_connection(("jobs.example.com", 443))
    assert no_network == ["create_connection"]


# facts new-run --url

DAY = dt.date(2026, 11, 11)
PASTE = "Save the job post to a file and pass --job FILE, or paste it with --job -."


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data root with only master.yaml, and a fixed date for a new run."""
    folder = tmp_path / "root"
    folder.mkdir()
    (folder / "master.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_today", lambda: DAY)
    return folder


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch,
    responder: Responder = _html,
    resolver: Resolver | None = None,
) -> list[str]:
    """Make the CLI fetch use a fake transport; return the list of fetched URLs."""
    calls: list[str] = []

    def fake(url: str) -> FetchOk | FetchFailure:
        return _fetch(url, _recording(calls, responder), resolver=resolver)

    monkeypatch.setattr(cli, "fetch", fake)
    return calls


def _new_run(root: Path, url: str, *extra: str, company: str = "Acme") -> int:
    args = ["new-run", "--company", company, "--role", "Designer", "--url", url]
    return main([*args, *extra, "--repo", str(root)])


def test_cli_url_writes_job_md_first_and_apply_url(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_fetch(monkeypatch)
    written: list[str] = []
    original = Path.write_text

    def record(self: Path, *args: Any, **kwargs: Any) -> int:
        written.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record)
    assert _new_run(root, URL) == 0
    run = Path(capsys.readouterr().out.strip())
    assert run == root / APPLICATIONS / "2026-11-11-acme-designer"
    assert written == [JOB, RUN_YAML]
    assert calls == [URL]
    text = (run / JOB).read_text(encoding="utf-8")
    assert "5 years of product design" in text and text.endswith("\n")
    meta = yaml.safe_load((run / RUN_YAML).read_text(encoding="utf-8"))
    assert meta["apply_url"] == URL
    assert list(meta)[-1] == "apply_url"
    assert meta["status"] == "new"


def test_cli_url_json(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_fetch(monkeypatch)
    assert _new_run(root, URL, "--json") == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert data == {
        "schema_version": 1,
        "command": "new-run",
        "run": str(root / APPLICATIONS / "2026-11-11-acme-designer"),
        "url": URL,
    }


def _failed_cli(
    root: Path, url: str, code: int, capsys: pytest.CaptureFixture[str]
) -> str:
    assert _new_run(root, url) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1
    assert not exists(root / APPLICATIONS)
    return captured.err


def test_cli_blocked_address_exit_2(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_fetch(
        monkeypatch, resolver=_resolver({"jobs.example.com": [PRIVATE_IP]})
    )
    err = _failed_cli(root, URL, 2, capsys)
    assert err.startswith("facts new-run: cannot fetch the job post (blocked_address)")
    assert err.endswith(PASTE + "\n")
    assert calls == []


def test_cli_time_out_exit_1(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _patch_fetch(monkeypatch, slow)
    err = _failed_cli(root, URL, 1, capsys)
    assert "(timeout)" in err and err.endswith(PASTE + "\n")


@pytest.mark.parametrize("url", ["", "file:///etc/hosts"])
def test_cli_refused_url_makes_no_network_call(
    root: Path, url: str, no_network: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The real fetch: the guard refuses the URL before a name lookup.
    err = _failed_cli(root, url, 2, capsys)
    assert err.endswith(PASTE + "\n")
    assert no_network == []


def test_cli_bad_company_name_makes_no_fetch(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_fetch(monkeypatch)
    assert _new_run(root, URL, company="???") == 2
    assert "gives an empty name" in capsys.readouterr().err
    assert calls == []
    assert not exists(root / APPLICATIONS)


def test_cli_existing_run_folder_makes_no_fetch(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_fetch(monkeypatch)
    run = root / APPLICATIONS / "2026-11-11-acme-designer"
    run.mkdir(parents=True)
    assert _new_run(root, URL) == 2
    assert "already exists" in capsys.readouterr().err
    assert calls == []
    assert list(run.iterdir()) == []


def test_cli_url_with_no_data_root_makes_no_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_fetch(monkeypatch)
    assert _new_run(tmp_path, URL) == 2
    assert "No master.yaml found" in capsys.readouterr().err
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_cli_job_run_yaml_has_no_apply_url(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text("Acme needs a designer.", encoding="utf-8")
    args = ["new-run", "--company", "Acme", "--role", "Designer", "--job", str(job)]
    assert main([*args, "--repo", str(root)]) == 0
    run = Path(capsys.readouterr().out.strip())
    assert "apply_url" not in yaml.safe_load((run / RUN_YAML).read_text("utf-8"))
