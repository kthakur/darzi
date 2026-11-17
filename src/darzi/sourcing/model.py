"""The result types of one fetch of a job post (`sourcing/fetch.py`).

A fetch gives a `FetchOk` or a `FetchFailure`. It never raises. The reason
values are the same as in the source.
"""

from dataclasses import dataclass
from enum import StrEnum


class FetchReason(StrEnum):
    """Why a fetch was refused or failed. The value is the reason code."""

    INVALID_URL = "invalid_url"
    DISALLOWED_SCHEME = "disallowed_scheme"
    BLOCKED_ADDRESS = "blocked_address"
    UNRESOLVABLE = "dns_resolution_failed"
    TIMEOUT = "timeout"
    CONNECTION_FAILED = "connection_failed"
    HTTP_ERROR = "http_error"
    NON_HTML = "non_html_content"
    TOO_LARGE = "response_too_large"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    BAD_REDIRECT = "bad_redirect"
    EMPTY_CONTENT = "empty_content"


# The fetch refuses these inputs before it tries (a policy refusal). Each other
# reason is an operational failure: the fetch tried, and it failed.
REFUSAL_REASONS = frozenset(
    {
        FetchReason.INVALID_URL,
        FetchReason.DISALLOWED_SCHEME,
        FetchReason.BLOCKED_ADDRESS,
    }
)


@dataclass(frozen=True)
class FetchOk:
    """A fetched HTML page as Markdown text.

    `text` is untrusted data from the page. `final_url` is the URL after the
    redirects. `byte_count` is the size of the body that was read.
    """

    text: str
    title: str | None
    final_url: str
    status_code: int
    content_type: str
    byte_count: int


@dataclass(frozen=True)
class FetchFailure:
    """A fetch that was refused or that failed.

    `url` is the URL of the hop that failed. `status_code` is set only when the
    server sent a response.
    """

    reason: FetchReason
    detail: str
    url: str
    status_code: int | None = None

    @property
    def refused(self) -> bool:
        """Return True for a policy refusal, False for an operational failure."""
        return self.reason in REFUSAL_REASONS


FetchResult = FetchOk | FetchFailure
