# -*- coding: utf-8 -*-
"""Tests for the shared prefetch scheduler in h11._prefetch."""

import asyncio
from typing import Any, Dict, List

import pytest

from .._prefetch import (
    BillingSnapshot,
    DownloadError,
    DownloadResult,
    PrefetchScheduler,
    PrefetchState,
    canonicalize_url,
    normalize_resource,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeDownloader:
    """Downloader whose fetches stay in flight until a gate is opened."""

    def __init__(self) -> None:
        self.calls: List[Any] = []
        self.bodies: Dict[Any, bytes] = {}
        self.fail: Any = None
        self.gates: Dict[Any, asyncio.Event] = {}
        self.started: Dict[Any, asyncio.Event] = {}

    def set_body(self, identity: Any, body: bytes) -> None:
        self.bodies[identity] = body

    def gate_for(self, identity: Any) -> asyncio.Event:
        if identity not in self.gates:
            self.gates[identity] = asyncio.Event()
            self.started[identity] = asyncio.Event()
        return self.gates[identity]

    def release(self, identity: Any) -> None:
        self.gate_for(identity).set()

    async def wait_started(self, identity: Any) -> None:
        self.gate_for(identity)
        await self.started[identity].wait()

    async def __call__(self, identity: Any) -> Any:
        self.calls.append(identity)
        gate = self.gate_for(identity)
        self.started[identity].set()
        await gate.wait()
        if self.fail is not None:
            failing = self.fail
            self.fail = None
            if isinstance(failing, BaseException):
                raise failing
            raise DownloadError("boom", partial_bytes=failing)
        if identity in self.bodies:
            return self.bodies[identity]
        body = b"body-of-" + identity.url.encode()
        self.bodies[identity] = body
        return body


# -- identity normalization ---------------------------------------------


def test_canonicalize_url() -> None:
    assert canonicalize_url("HTTPS://Example.com:443/a?b=2&a=1#frag") == (
        "https://example.com/a?a=1&b=2"
    )
    assert canonicalize_url("http://example.com") == "http://example.com/"
    assert canonicalize_url("http://example.com:8080/x") == "http://example.com:8080/x"
    with pytest.raises(ValueError):
        canonicalize_url("not-an-absolute-url")


def test_identity_merges_equivalent_variants_but_distinguishes_headers() -> None:
    base = normalize_resource("https://e.com/a?q=1", headers={"Accept": "text/html"})
    equivalent = normalize_resource(
        "HTTPS://e.com:443/a?q=1", headers=[("accept", "text/html")]
    )
    other_header = normalize_resource("https://e.com/a?q=1", headers={"Accept": "*/*"})
    other_method = normalize_resource("https://e.com/a?q=1", method="HEAD")
    assert base == equivalent
    assert base != other_header
    assert base != other_method


# -- merged downloads ----------------------------------------------------


def test_two_hints_share_one_download() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            handle_a = scheduler.hint("page-a", identity.url)
            handle_b = scheduler.hint("page-b", identity.url)
            await downloader.wait_started(identity)
            assert len(downloader.calls) == 1

            record = scheduler.get_record(identity)
            assert record is not None
            assert record.state is PrefetchState.IN_FLIGHT
            assert record.hinted_by == {"page-a", "page-b"}
            assert record.open_handles == 2

            downloader.release(identity)
            body_a, body_b = await asyncio.gather(handle_a.wait(), handle_b.wait())
            assert body_a == body_b == downloader.bodies[identity]
            assert len(downloader.calls) == 1
            assert record.state is PrefetchState.COMPLETED

    run(scenario())


def test_concurrent_hints_start_a_single_download() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")

            async def hint(page_id: str) -> bytes:
                handle = scheduler.hint(page_id, identity.url)
                return await handle.wait()

            downloader.release(identity)
            results = await asyncio.gather(*(hint(f"p{i}") for i in range(5)))
            assert len(downloader.calls) == 1
            assert all(result == results[0] for result in results)
            scheduler.verify_billing()

    run(scenario())


def test_distinct_variants_do_not_merge() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            id_gzip = normalize_resource(
                "https://e.com/x", headers={"Accept-Encoding": "gzip"}
            )
            id_plain = normalize_resource(
                "https://e.com/x", headers={"Accept-Encoding": "identity"}
            )
            downloader.release(id_gzip)
            downloader.release(id_plain)
            h1 = scheduler.hint("p1", id_gzip.url, headers=id_gzip.headers)
            h2 = scheduler.hint("p2", id_plain.url, headers=id_plain.headers)
            await asyncio.gather(h1.wait(), h2.wait())
            assert len(downloader.calls) == 2
            assert scheduler.billing().fetch_count == 2

    run(scenario())


# -- cancellation isolation ----------------------------------------------


def test_releasing_one_handle_does_not_cancel_shared_download() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            handle_a = scheduler.hint("page-a", identity.url)
            handle_b = scheduler.hint("page-b", identity.url)
            await downloader.wait_started(identity)

            handle_a.cancel()
            assert handle_a.detached
            record = scheduler.get_record(identity)
            assert record is not None
            assert record.state is PrefetchState.IN_FLIGHT
            assert record.open_handles == 1
            with pytest.raises(RuntimeError):
                await handle_a.wait()

            downloader.release(identity)
            assert await handle_b.wait() == downloader.bodies[identity]
            assert len(downloader.calls) == 1

    run(scenario())


def test_cancelling_waiter_task_keeps_shared_download_alive() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            handle_a = scheduler.hint("page-a", identity.url)
            handle_b = scheduler.hint("page-b", identity.url)
            await downloader.wait_started(identity)

            record = scheduler.get_record(identity)
            assert record is not None
            shared_task = record.task
            assert shared_task is not None

            waiter = asyncio.ensure_future(handle_a.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

            assert not shared_task.cancelled()
            assert record.state is PrefetchState.IN_FLIGHT

            downloader.release(identity)
            assert await handle_b.wait() == downloader.bodies[identity]
            assert len(downloader.calls) == 1

    run(scenario())


def test_release_page_keeps_download_another_page_depends_on() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            handle_a = scheduler.hint("page-a", identity.url)
            handle_b = scheduler.hint("page-b", identity.url)
            await downloader.wait_started(identity)

            scheduler.release_page("page-a")
            assert handle_a.detached
            record = scheduler.get_record(identity)
            assert record is not None
            assert record.state is PrefetchState.IN_FLIGHT

            downloader.release(identity)
            assert await handle_b.wait() == downloader.bodies[identity]

    run(scenario())


# -- late confirmation ---------------------------------------------------


def test_late_confirmation_claims_buffered_content_without_refetch() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            hinted = scheduler.hint("page-a", identity.url)
            downloader.release(identity)
            await hinted.wait()
            assert len(downloader.calls) == 1

            handle = scheduler.confirm("page-late", identity.url)
            record = scheduler.get_record(identity)
            assert record is not None
            assert record.state is PrefetchState.COMPLETED
            assert record.confirmed_by == {"page-late"}
            assert len(downloader.calls) == 1
            assert await handle.wait() == downloader.bodies[identity]

    run(scenario())


def test_confirmation_while_in_flight_claims_on_completion() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            scheduler.hint("page-a", identity.url)
            await downloader.wait_started(identity)

            late = scheduler.confirm("page-b", identity.url)
            downloader.release(identity)
            assert await late.wait() == downloader.bodies[identity]
            assert len(downloader.calls) == 1
            record = scheduler.get_record(identity)
            assert record is not None
            assert record.hinted_by == {"page-a"}
            assert record.confirmed_by == {"page-b"}

    run(scenario())


# -- failures ------------------------------------------------------------


def test_failed_download_releases_all_waiters_and_keeps_partial_bytes() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        downloader.fail = 37
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/broken.js")
            handle_a = scheduler.hint("page-a", identity.url)
            handle_b = scheduler.hint("page-b", identity.url)
            downloader.release(identity)

            for handle in (handle_a, handle_b):
                with pytest.raises(DownloadError) as excinfo:
                    await handle.wait()
                assert excinfo.value.partial_bytes == 37

            assert len(downloader.calls) == 1
            record = scheduler.get_record(identity)
            assert record is not None
            assert record.state is PrefetchState.FAILED
            assert record.bytes_downloaded == 37
            snapshot = scheduler.verify_billing()
            assert snapshot.total_bytes == 37

    run(scenario())


def test_other_pages_are_unaffected_when_one_page_fails_its_own_fetch() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            broken = normalize_resource("https://e.com/broken.js")
            shared = normalize_resource("https://e.com/app.js")
            downloader.set_body(broken, b"unused")

            async def failing_page() -> None:
                handle = scheduler.hint("page-a", broken.url)
                downloader.fail = DownloadError("nope", partial_bytes=10)
                downloader.release(broken)
                with pytest.raises(DownloadError):
                    await handle.wait()

            shared_handle = scheduler.hint("page-b", shared.url)
            await downloader.wait_started(shared)
            await failing_page()

            record = scheduler.get_record(shared)
            assert record is not None
            assert record.state is PrefetchState.IN_FLIGHT
            downloader.release(shared)
            assert await shared_handle.wait() == downloader.bodies[shared]

    run(scenario())


def test_confirmation_after_failure_does_not_retry_and_keeps_bill_balanced() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        downloader.fail = 5
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/broken.js")
            early = scheduler.hint("page-a", identity.url)
            downloader.release(identity)
            with pytest.raises(DownloadError):
                await early.wait()
            assert len(downloader.calls) == 1

            late = scheduler.confirm("page-b", identity.url)
            assert len(downloader.calls) == 1
            record = scheduler.get_record(identity)
            assert record is not None
            assert record.state is PrefetchState.FAILED
            assert record.hinted_by == {"page-a"}
            assert record.confirmed_by == {"page-b"}
            with pytest.raises(DownloadError):
                await late.wait()

            snapshot = scheduler.verify_billing()
            assert snapshot.total_bytes == 5
            assert scheduler.wire_bytes == 5

    run(scenario())


# -- billing -------------------------------------------------------------
def test_orphan_overhead_is_bytes_nobody_confirmed() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            orphan = normalize_resource("https://e.com/hint-only.js")
            used = normalize_resource("https://e.com/used.js")
            downloader.set_body(orphan, b"x" * 100)
            downloader.set_body(used, b"y" * 40)
            downloader.release(orphan)
            downloader.release(used)

            orphan_handle = scheduler.hint("page-a", orphan.url)
            used_handle = scheduler.hint("page-a", used.url)
            scheduler.confirm("page-a", used.url)
            await asyncio.gather(orphan_handle.wait(), used_handle.wait())

            orphan_record = scheduler.get_record(orphan)
            used_record = scheduler.get_record(used)
            assert orphan_record is not None and used_record is not None
            assert orphan_record.orphan_bytes == 100
            assert used_record.orphan_bytes == 0

            snapshot = scheduler.verify_billing()
            assert isinstance(snapshot, BillingSnapshot)
            assert snapshot.total_bytes == 140
            assert snapshot.claimed_bytes == 40
            assert snapshot.orphan_bytes == 100
            assert snapshot.total_bytes == scheduler.wire_bytes

    run(scenario())


def test_shared_bytes_are_billed_once_no_matter_how_many_claimants() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        async with PrefetchScheduler(downloader) as scheduler:
            identity = normalize_resource("https://e.com/app.js")
            downloader.set_body(identity, b"z" * 250)
            handles = [scheduler.hint(f"p{i}", identity.url) for i in range(4)]
            for i in range(4):
                scheduler.confirm(f"p{i}", identity.url)
            downloader.release(identity)
            await asyncio.gather(*(handle.wait() for handle in handles))

            snapshot = scheduler.verify_billing()
            assert snapshot.total_bytes == 250
            assert snapshot.claimed_bytes == 250
            assert snapshot.orphan_bytes == 0
            assert scheduler.wire_bytes == 250
            assert len(downloader.calls) == 1

    run(scenario())


def test_explicit_wire_bytes_are_used_for_billing() -> None:
    async def scenario() -> None:
        identity = normalize_resource("https://e.com/gzip.js")

        async def downloader(rid: Any) -> DownloadResult:
            return DownloadResult(body=b"decoded-body", bytes_received=500)

        async with PrefetchScheduler(downloader) as scheduler:
            handle = scheduler.hint("p1", identity.url)
            assert await handle.wait() == b"decoded-body"
            snapshot = scheduler.verify_billing()
            assert snapshot.total_bytes == 500
            assert snapshot.orphan_bytes == 500

    run(scenario())


def test_billing_snapshot_always_balances() -> None:
    snapshot = BillingSnapshot(
        total_bytes=10, claimed_bytes=4, orphan_bytes=6, fetch_count=2
    )
    assert snapshot.total_bytes == snapshot.claimed_bytes + snapshot.orphan_bytes
    with pytest.raises(AssertionError):
        BillingSnapshot(total_bytes=10, claimed_bytes=4, orphan_bytes=7, fetch_count=2)


# -- shutdown ------------------------------------------------------------


def test_aclose_cancels_in_flight_downloads() -> None:
    async def scenario() -> None:
        downloader = FakeDownloader()
        scheduler = PrefetchScheduler(downloader)
        identity = normalize_resource("https://e.com/never.js")
        handle = scheduler.hint("page-a", identity.url)
        await downloader.wait_started(identity)
        record = scheduler.get_record(identity)
        assert record is not None
        assert record.state is PrefetchState.IN_FLIGHT

        await scheduler.aclose()
        with pytest.raises(BaseException):
            await handle.wait()
        assert record.state is PrefetchState.FAILED
        # Idempotent.
        await scheduler.aclose()
        with pytest.raises(RuntimeError):
            scheduler.hint("page-b", identity.url)

    run(scenario())
