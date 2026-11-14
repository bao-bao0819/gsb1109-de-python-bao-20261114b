# -*- coding: utf-8 -*-
"""Shared prefetch scheduling for crawler workers.

Large pages may announce resource links ahead of their final body via
103 Early Hints. While a worker is still waiting for the page body it can
prefetch those resources, but the final page is not obliged to actually
reference any of them. This module keeps a *shared* prefetch ledger so that
work announced via early hints is merged across pages instead of being
downloaded once per page.

Design points:

* A fetch is identified by a normalized :class:`ResourceId` (canonical URL
  plus the request variant: method and normalized headers). Two hints that
  resolve to the same identity share a single in-flight download.
* Each :class:`PrefetchRecord` remembers which pages hinted the resource
  (``hinted_by``), which pages ultimately confirmed it via their final page
  (``confirmed_by``), how many bytes were downloaded, and whether the fetch
  succeeded. Bytes downloaded for a record nobody confirms are *orphan
  overhead*.
* The shared download is owned by the scheduler, never by an individual
  waiting page. Detaching (or cancelling) one page's handle only drops that
  page's interest; it cannot cancel a download another page is still waiting
  on, and bytes already on the wire stay on the bill.
* A confirmation arriving after the download completed simply claims the
  buffered body without triggering a second fetch.

The module only contains scheduling and bookkeeping logic; actual network
I/O is injected through a downloader callable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = (
    "ResourceId",
    "DownloadResult",
    "DownloadError",
    "PrefetchState",
    "PrefetchRecord",
    "PrefetchHandle",
    "PrefetchScheduler",
    "BillingSnapshot",
    "canonicalize_url",
    "normalize_resource",
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_url(url: str) -> str:
    """Return a canonical form of *url* for resource identity matching.

    The scheme and host are lower-cased, default ports are removed, an empty
    path becomes ``/``, fragments are dropped, and query parameters are
    sorted. Percent-encoding itself is left untouched.
    """
    parts = urlsplit(url.strip())
    if not parts.scheme or parts.hostname is None:
        raise ValueError(
            "prefetch resource identity requires an absolute URL: %r" % url
        )
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    if port is not None:
        netloc = "{}:{}".format(host, port)
    else:
        netloc = host
    path = parts.path or "/"
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))


def _normalize_headers(
    headers: Optional[Union[Mapping[str, str], Iterable[Tuple[str, str]]]]
) -> Tuple[Tuple[str, str], ...]:
    if headers is None:
        return ()
    if isinstance(headers, Mapping):
        pairs = list(headers.items())
    else:
        pairs = list(headers)
    normalized = [(name.strip().lower(), value.strip()) for name, value in pairs]
    normalized.sort()
    return tuple(normalized)


def normalize_resource(
    url: str, method: str = "GET", headers: Optional[Any] = None
) -> "ResourceId":
    """Build the canonical identity used to merge reusable prefetch work."""
    return ResourceId(
        url=canonicalize_url(url),
        method=method.strip().upper(),
        headers=_normalize_headers(headers),
    )


@dataclass(frozen=True)
class ResourceId:
    """Canonical identity of a fetch: URL plus the request variant."""

    url: str
    method: str
    headers: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BillingSnapshot:
    """Reconciled byte bill for a :class:`PrefetchScheduler`."""

    total_bytes: int
    claimed_bytes: int
    orphan_bytes: int
    fetch_count: int

    def __post_init__(self) -> None:
        if self.total_bytes != self.claimed_bytes + self.orphan_bytes:
            raise AssertionError(
                "prefetch bill does not balance: {} != {} + {}".format(
                    self.total_bytes, self.claimed_bytes, self.orphan_bytes
                )
            )


@dataclass(frozen=True)
class DownloadResult:
    """A finished download.

    ``bytes_received`` defaults to the body length but may be set explicitly
    to account for wire bytes that differ from the decoded body size (for
    example compressed payloads).
    """

    body: bytes
    bytes_received: Optional[int] = None

    def __post_init__(self) -> None:
        if self.bytes_received is None:
            object.__setattr__(self, "bytes_received", len(self.body))


class DownloadError(Exception):
    """Download failure, optionally carrying bytes received before failing."""

    def __init__(self, message: str = "", *, partial_bytes: int = 0) -> None:
        super().__init__(message)
        self.partial_bytes = partial_bytes


class PrefetchState(Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PrefetchRecord:
    """Ledger entry for one canonical resource identity."""

    identity: ResourceId
    hinted_by: Set[str] = field(default_factory=set)
    confirmed_by: Set[str] = field(default_factory=set)
    state: PrefetchState = PrefetchState.IN_FLIGHT
    bytes_downloaded: int = 0
    error: Optional[BaseException] = None
    body: Optional[bytes] = None
    open_handles: int = 0
    done: asyncio.Future[None] = field(init=False)
    task: Optional["asyncio.Task[None]"] = None

    def __post_init__(self) -> None:
        self.done = asyncio.get_running_loop().create_future()

    @property
    def is_claimed(self) -> bool:
        """True when at least one final page confirmed this resource."""
        return bool(self.confirmed_by)

    @property
    def orphan_bytes(self) -> int:
        """Downloaded bytes that no final page ever claimed."""
        return 0 if self.confirmed_by else self.bytes_downloaded


# A downloader takes a canonical identity and returns the body (or a
# DownloadResult when wire byte accounting matters).
Downloader = Callable[[ResourceId], Awaitable[Any]]


class PrefetchHandle:
    """One page's attachment to a shared :class:`PrefetchRecord`.

    Several handles may point at the same record. Releasing a handle only
    detaches that page; the underlying shared download keeps running while
    any other handle remains or until it finishes on its own.
    """

    def __init__(
        self,
        scheduler: "PrefetchScheduler",
        record: PrefetchRecord,
        page_id: str,
    ) -> None:
        self._scheduler = scheduler
        self._record = record
        self._page_id = page_id
        self._detached = False
        record.open_handles += 1

    @property
    def page_id(self) -> str:
        return self._page_id

    @property
    def record(self) -> PrefetchRecord:
        return self._record

    @property
    def detached(self) -> bool:
        return self._detached

    async def wait(self) -> bytes:
        """Wait for the shared download and return its buffered body.

        The wait is shielded, so cancelling the waiting page never cancels
        the shared download. A failed download re-raises the original error
        for every waiter.
        """
        if self._detached:
            raise RuntimeError(
                "prefetch handle for page %r was released" % self._page_id
            )
        record = self._record
        if record.state is PrefetchState.IN_FLIGHT:
            await asyncio.shield(record.done)
        if record.error is not None:
            raise record.error
        return record.body if record.body is not None else b""

    def cancel(self) -> None:
        """Detach this page without touching the shared download."""
        if self._detached:
            return
        self._detached = True
        self._record.open_handles -= 1
        self._scheduler._forget_handle(self)

    async def __aenter__(self) -> "PrefetchHandle":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cancel()


class PrefetchScheduler:
    """Holds the shared prefetch ledger and schedules merged downloads."""

    def __init__(self, downloader: Downloader) -> None:
        self._downloader = downloader
        self._records: Dict[ResourceId, PrefetchRecord] = {}
        self._page_handles: Dict[str, Set[PrefetchHandle]] = {}
        # Bytes actually observed leaving for the network, accumulated once
        # per record regardless of how many pages shared it.
        self._wire_bytes = 0
        self._closed = False

    # -- intake ---------------------------------------------------------

    def hint(
        self,
        page_id: str,
        url: str,
        method: str = "GET",
        headers: Optional[Any] = None,
    ) -> PrefetchHandle:
        """Register a 103 Early Hint from *page_id* and start prefetching."""
        record = self._get_or_start(url, method, headers)
        record.hinted_by.add(page_id)
        return self._attach(record, page_id)

    def confirm(
        self,
        page_id: str,
        url: str,
        method: str = "GET",
        headers: Optional[Any] = None,
    ) -> PrefetchHandle:
        """Record that *page_id*'s final body actually references the resource.

        Works whether the download is in flight, already finished (the
        buffered body is claimed immediately) or had previously failed.
        """
        record = self._get_or_start(url, method, headers)
        record.confirmed_by.add(page_id)
        return self._attach(record, page_id)

    def _get_or_start(
        self, url: str, method: str, headers: Optional[Any]
    ) -> PrefetchRecord:
        if self._closed:
            raise RuntimeError("prefetch scheduler is closed")
        identity = normalize_resource(url, method, headers)
        record = self._records.get(identity)
        if record is None:
            record = PrefetchRecord(identity=identity)
            self._records[identity] = record
            record.task = asyncio.ensure_future(self._run_download(record))
        return record

    def _attach(self, record: PrefetchRecord, page_id: str) -> PrefetchHandle:
        handle = PrefetchHandle(self, record, page_id)
        self._page_handles.setdefault(page_id, set()).add(handle)
        return handle

    def _forget_handle(self, handle: PrefetchHandle) -> None:
        handles = self._page_handles.get(handle.page_id)
        if handles is not None:
            handles.discard(handle)
            if not handles:
                del self._page_handles[handle.page_id]

    # -- download lifecycle --------------------------------------------

    async def _run_download(self, record: PrefetchRecord) -> None:
        try:
            outcome = await self._downloader(record.identity)
        except BaseException as exc:
            # Cancellation only comes from aclose(); either way the record
            # settles and every waiter is released.
            record.state = PrefetchState.FAILED
            record.error = exc
            partial = getattr(exc, "partial_bytes", 0)
            record.bytes_downloaded = int(partial or 0)
            self._wire_bytes += record.bytes_downloaded
            record.task = None
            if not record.done.done():
                record.done.set_result(None)
            return
        if isinstance(outcome, DownloadResult):
            body = outcome.body
            if outcome.bytes_received is not None:
                received = outcome.bytes_received
            else:
                received = len(body)
        else:
            body = outcome
            received = len(body)
        record.body = body
        record.bytes_downloaded = received
        record.state = PrefetchState.COMPLETED
        self._wire_bytes += received
        record.task = None
        record.done.set_result(None)

    # -- page lifecycle -------------------------------------------------

    def release_page(self, page_id: str) -> None:
        """Detach every handle still held by *page_id*.

        Ledger memberships (hints/confirmations) are preserved so the final
        bill stays accurate; only open handle interest is dropped.
        """
        handles = list(self._page_handles.get(page_id, ()))
        for handle in handles:
            handle.cancel()

    # -- inspection / billing ------------------------------------------

    def records(self) -> List[PrefetchRecord]:
        return list(self._records.values())

    def get_record(self, identity: ResourceId) -> Optional[PrefetchRecord]:
        return self._records.get(identity)

    def billing(self) -> BillingSnapshot:
        """Return the current bill, asserting claimed + orphan == total."""
        total = sum(record.bytes_downloaded for record in self._records.values())
        orphan = sum(record.orphan_bytes for record in self._records.values())
        claimed = total - orphan
        return BillingSnapshot(
            total_bytes=total,
            claimed_bytes=claimed,
            orphan_bytes=orphan,
            fetch_count=len(self._records),
        )

    def verify_billing(self) -> BillingSnapshot:
        """Reconcile the ledger against the bytes actually downloaded.

        Raises :class:`AssertionError` if the per-record bill does not match
        the wire-level byte total, i.e. if merging or claiming ever lost or
        double-counted bytes.
        """
        snapshot = self.billing()
        ledger_total = sum(
            record.bytes_downloaded for record in self._records.values()
        )
        if ledger_total != self._wire_bytes:
            raise AssertionError(
                "prefetch ledger bytes {} != actual downloaded bytes {}".format(
                    ledger_total, self._wire_bytes
                )
            )
        if snapshot.total_bytes != self._wire_bytes:
            raise AssertionError(
                "prefetch bill total {} != actual downloaded bytes {}".format(
                    snapshot.total_bytes, self._wire_bytes
                )
            )
        return snapshot

    @property
    def wire_bytes(self) -> int:
        return self._wire_bytes

    # -- shutdown -------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel all in-flight downloads and release waiters."""
        if self._closed:
            return
        self._closed = True
        pending = [
            record.task
            for record in self._records.values()
            if record.task is not None and not record.task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def __aenter__(self) -> "PrefetchScheduler":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()
