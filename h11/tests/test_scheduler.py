from typing import List, Sequence, Union

import pytest

from .._scheduler import (
    Account,
    CooldownRule,
    Infeasible,
    Request,
    ScheduleEntry,
    Scheduler,
    SlotState,
    TimelineEvent,
    cooldown_rules_from_response,
)
from .._util import LocalProtocolError

A = Account("acct-a")
B = Account("acct-b")
C = Account("acct-c")

EMPTY_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def request(
    key: str,
    *,
    account: Account = A,
    site: str = "site",
    path: str = "/",
    priority: int = 0,
    deadline: float = 1000.0,
    duration: float = 1.0,
    release: float = 0.0,
) -> Request:
    return Request(
        key=key,
        account=account,
        site=site,
        path=path,
        priority=priority,
        deadline=deadline,
        duration=duration,
        release=release,
    )


def complete(scheduler: Scheduler, slot: str, at: float = 0.0) -> None:
    scheduler.observe(slot, EMPTY_OK, at=at)


def keys(entries: Sequence[Union[ScheduleEntry, Infeasible]]) -> List[str]:
    return [entry.request.key for entry in entries]


# ---------------------------------------------------------------- ordering


def test_dispatch_order_priority_then_deadline_then_duration() -> None:
    scheduler = Scheduler(
        ["c1", "c2", "c3", "c4"],
        requests=[
            request("late", priority=1, deadline=90),
            request("tight", priority=1, deadline=10),
            request("slow", priority=1, deadline=10, duration=5),
            request("high", priority=0, deadline=1000),
        ],
    )
    entries = scheduler.plan(0)
    # priority 0 first, then priority 1 by deadline, then shorter job first.
    assert keys(entries) == ["high", "tight", "slow", "late"]


def test_dispatch_order_is_stable_for_full_ties() -> None:
    scheduler = Scheduler(
        ["c1", "c2", "c3"],
        requests=[request("b"), request("a"), request("c")],
    )
    assert keys(scheduler.plan(0)) == ["a", "b", "c"]


def test_only_as_many_requests_as_slots_are_dispatched() -> None:
    scheduler = Scheduler(
        ["c1", "c2"],
        requests=[request("a"), request("b"), request("c")],
    )
    entries = scheduler.plan(0)
    assert keys(entries) == ["a", "b"]
    assert {entry.slot for entry in entries} == {"c1", "c2"}
    assert [r.key for r in scheduler.pending] == ["c"]


def test_expected_finish_uses_duration() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a", duration=4)])
    (entry,) = scheduler.plan(10)
    assert entry.at == 10
    assert entry.expected_finish == 14


# --------------------------------------------------- headers vs body rule


def test_headers_alone_do_not_free_a_slot() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe("c1", b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")

    slot = scheduler.slot("c1")
    assert slot.headers_received
    assert not slot.body_complete
    assert slot.state == SlotState.HEADERS
    assert not slot.reusable

    # The second request cannot be dispatched while the body is outstanding.
    assert scheduler.plan(0) == []
    assert [r.key for r in scheduler.pending] == ["b"]


def test_partial_body_still_blocks_reuse() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe(
        "c1", b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n0123"
    )
    assert scheduler.slot("c1").state == SlotState.HEADERS
    assert scheduler.plan(0) == []


def test_completed_body_frees_the_slot() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe("c1", b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n")
    scheduler.observe("c1", b"hello")

    slot = scheduler.slot("c1")
    # The finished cycle was retired automatically: the slot is idle again.
    assert slot.current is None
    assert slot.state == SlotState.IDLE
    assert slot.reusable
    assert keys(scheduler.plan(1)) == ["b"]


def test_chunked_body_must_terminate_before_reuse() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe(
        "c1", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    )
    scheduler.observe("c1", b"5\r\nhello\r\n")
    assert not scheduler.slot("c1").reusable
    assert scheduler.plan(0) == []

    scheduler.observe("c1", b"0\r\n\r\n")
    assert scheduler.slot("c1").reusable
    assert keys(scheduler.plan(0)) == ["b"]


def test_headless_body_completes_in_one_batch() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe("c1", b"HTTP/1.1 204 No Content\r\n\r\n")
    assert scheduler.slot("c1").reusable
    assert keys(scheduler.plan(0)) == ["b"]


def test_finish_refuses_incomplete_body() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a")])
    scheduler.plan(0)
    scheduler.observe("c1", b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")
    with pytest.raises(LocalProtocolError):
        scheduler.slot("c1").finish()


def test_begin_refuses_busy_slot() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    with pytest.raises(LocalProtocolError):
        scheduler.slot("c1").begin(request("b"), 0)


# ------------------------------------------------------------- cooldowns


def test_account_cooldown_does_not_block_other_accounts() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("account", A.name, until=10)],
        requests=[
            request("a1", account=A),
            request("b1", account=B),
        ],
    )
    assert keys(scheduler.plan(0)) == ["b1"]
    assert [r.key for r in scheduler.pending] == ["a1"]


def test_path_cooldown_only_blocks_that_path() -> None:
    scheduler = Scheduler(
        ["c1", "c2"],
        cooldowns=[CooldownRule("path", "/blocked", until=10, site="site")],
        requests=[
            request("blocked", path="/blocked"),
            request("other", path="/other"),
            request("same-path-other-site", path="/blocked", site="other"),
        ],
    )
    dispatched = keys(scheduler.plan(0))
    assert "blocked" not in dispatched
    assert set(dispatched) == {"other", "same-path-other-site"}


def test_site_cooldown_blocks_every_account_and_path() -> None:
    scheduler = Scheduler(
        ["c1", "c2", "c3"],
        cooldowns=[CooldownRule("site", "site", until=5)],
        requests=[
            request("a", account=A, path="/a"),
            request("b", account=B, path="/b"),
            request("elsewhere", account=A, site="other", path="/b"),
        ],
    )
    assert keys(scheduler.plan(0)) == ["elsewhere"]


def test_global_site_cooldown_blocks_everything() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("site", None, until=5)],
        requests=[request("a", site="s1"), request("b", site="s2")],
    )
    assert scheduler.plan(0) == []


def test_stacked_cooldowns_use_the_latest_expiry() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[
            CooldownRule("account", A.name, until=10),
            CooldownRule("path", "/p", until=5, site="site"),
        ],
        requests=[request("a", path="/p")],
    )
    assert scheduler.effective_cooldown_until(scheduler.pending[0]) == 10

    # A shorter rule arriving later must not release the request early.
    scheduler.add_cooldown(CooldownRule("site", "site", until=7))
    assert scheduler.effective_cooldown_until(scheduler.pending[0]) == 10

    # ... and a longer one extends it.
    scheduler.add_cooldown(CooldownRule("site", "site", until=30))
    assert scheduler.effective_cooldown_until(scheduler.pending[0]) == 30


def test_repeated_rule_only_extends() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a")])
    scheduler.add_cooldown(CooldownRule("account", A.name, until=20))
    scheduler.add_cooldown(CooldownRule("account", A.name, until=4))
    assert scheduler.effective_cooldown_until(scheduler.pending[0]) == 20


def test_cooldown_expiry_is_inclusive() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("site", "site", until=10)],
        requests=[request("a")],
    )
    assert scheduler.plan(9.999) == []
    assert keys(scheduler.plan(10)) == ["a"]


def test_path_cooldown_requires_site() -> None:
    with pytest.raises(ValueError):
        CooldownRule("path", "/p", until=1)


def test_unknown_scope_is_rejected() -> None:
    with pytest.raises(ValueError):
        CooldownRule("galaxy", "x", until=1)


def test_cooldowns_from_response_headers() -> None:
    rules = cooldown_rules_from_response(
        [
            (b"Retry-After", b"30"),
            (b"X-Crawl-Freeze-Path", b"12"),
            (b"X-Crawl-Freeze-Account", b"4"),
            (b"X-Ignored", b"999"),
        ],
        now=100,
        account=A,
        site="site",
        path="/p",
    )
    by_scope = {rule.scope: rule for rule in rules}
    assert by_scope["site"].until == 130
    assert by_scope["path"].until == 112
    assert by_scope["path"].site == "site"
    assert by_scope["account"].until == 104
    assert by_scope["account"].subject == A.name


def test_response_headers_install_cooldowns_during_replay() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe(
        "c1",
        b"HTTP/1.1 200 OK\r\nRetry-After: 30\r\nContent-Length: 0\r\n\r\n",
        at=5,
    )
    assert scheduler.effective_cooldown_until(scheduler.pending[0]) == 35
    assert scheduler.plan(10) == []


# ----------------------------------------------------- deadline handling


def test_request_blocked_past_deadline_is_set_aside() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("site", "site", until=10)],
        requests=[request("doomed", deadline=12, duration=3)],
    )
    result = scheduler.replay([TimelineEvent(0, "time")])
    assert result.dispatched == []
    assert [item.request.key for item in result.infeasible] == ["doomed"]
    assert result.infeasible[0].reason == "deadline unreachable after cooldown"
    assert result.infeasible[0].cooldown_until == 10
    assert result.unfinished == []


def test_infeasible_requests_do_not_consume_slots() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("site", "site", until=10)],
        requests=[
            request("doomed", priority=0, deadline=12, duration=3),
            request("fine", priority=9, site="other", deadline=1000),
        ],
    )
    result = scheduler.replay([TimelineEvent(0, "time")])
    assert result.order == ["fine"]
    assert [item.request.key for item in result.infeasible] == ["doomed"]
    assert scheduler.slot("c1").current is not None


def test_infeasible_due_to_release_is_reported() -> None:
    scheduler = Scheduler(
        ["c1"],
        requests=[request("late", release=9, deadline=10, duration=2)],
    )
    result = scheduler.replay([TimelineEvent(0, "time")])
    assert result.dispatched == []
    assert [item.request.key for item in result.infeasible] == ["late"]
    assert result.infeasible[0].earliest_start == 9


def test_release_is_a_lower_bound_only() -> None:
    scheduler = Scheduler(
        ["c1"], requests=[request("a", priority=5, release=3)]
    )
    assert scheduler.plan(0) == []
    assert keys(scheduler.plan(2.999)) == []
    assert keys(scheduler.plan(3)) == ["a"]


# ---------------------------------------------------------------- replay


def test_replay_is_deterministic() -> None:
    def build() -> Scheduler:
        return Scheduler(
            ["c1", "c2"],
            cooldowns=[CooldownRule("account", B.name, until=4)],
            requests=[
                request("a", account=A, priority=1),
                request("b", account=B, priority=0),
                request("c", account=C, priority=2, site="other"),
                request("d", account=A, priority=3, deadline=60),
            ],
        )

    timeline = [
        TimelineEvent(2, "response", ("c1", EMPTY_OK)),
        TimelineEvent(3, "response", ("c2", EMPTY_OK)),
        TimelineEvent(8, "response", ("c1", EMPTY_OK)),
        TimelineEvent(9, "response", ("c2", EMPTY_OK)),
    ]
    first = build().replay(timeline)
    second = build().replay(timeline)
    assert first.order == second.order
    assert [(e.at, e.slot) for e in first.dispatched] == [
        (e.at, e.slot) for e in second.dispatched
    ]


def test_replay_wakes_at_cooldown_expiry_without_an_event() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("site", "site", until=10)],
        requests=[request("a", deadline=100)],
    )
    result = scheduler.replay([TimelineEvent(0, "time")])
    assert result.order == ["a"]
    assert result.dispatched[0].at == 10


def test_replay_applies_cooldown_before_dispatching_at_the_same_instant() -> None:
    scheduler = Scheduler(
        ["c1"], requests=[request("a", deadline=100)]
    )
    result = scheduler.replay(
        [
            TimelineEvent(
                0, "cooldown", (CooldownRule("site", "site", until=10),)
            ),
            TimelineEvent(10, "time"),
        ]
    )
    assert result.order == ["a"]
    assert result.dispatched[0].at == 10


def test_replay_keeps_busy_slot_busy_across_events() -> None:
    scheduler = Scheduler(
        ["c1"], requests=[request("a", deadline=100), request("b", deadline=100)]
    )
    result = scheduler.replay(
        [
            TimelineEvent(
                1,
                "response",
                ("c1", b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n"),
            ),
            TimelineEvent(2, "time"),
            TimelineEvent(3, "response", ("c1", b"body")),
        ]
    )
    assert result.order == ["a", "b"]
    assert result.dispatched[1].at == 3


def test_replay_reports_unfinished_work() -> None:
    scheduler = Scheduler(
        ["c1"],
        requests=[request("a"), request("b", release=100, deadline=1000)],
    )
    result = scheduler.replay([TimelineEvent(0, "time")])
    assert result.order == ["a"]
    assert [r.key for r in result.unfinished] == ["b"]


def test_close_slot_prevents_future_dispatch() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a")])
    scheduler.close_slot("c1")
    assert scheduler.slot("c1").state == SlotState.CLOSED
    assert scheduler.plan(0) == []


def test_timeline_events_can_close_a_slot() -> None:
    scheduler = Scheduler(
        ["c1", "c2"],
        requests=[request("a"), request("b", deadline=1000)],
    )
    result = scheduler.replay(
        [
            TimelineEvent(0, "close", ("c1",)),
            TimelineEvent(1, "close", ("c2",)),
        ]
    )
    # Both connections died while "a" was in flight, so nothing can carry "b".
    assert result.order == ["a"]
    assert [item.request.key for item in result.infeasible] == ["b"]
    assert result.unfinished == []


def test_truncated_body_kills_the_slot_without_reuse() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a"), request("b")])
    scheduler.plan(0)
    scheduler.observe("c1", b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n012")
    # EOF before the body was complete.
    scheduler.observe("c1", b"")
    slot = scheduler.slot("c1")
    assert slot.state == SlotState.CLOSED
    assert not slot.reusable
    assert slot.dropped is not None and slot.dropped.key == "a"
    assert scheduler.plan(1) == []
    assert [item.request.key for item in scheduler.infeasible] == ["b"]


def test_dropped_requests_are_reported_in_replay() -> None:
    scheduler = Scheduler(["c1"], requests=[request("a")])
    result = scheduler.replay(
        [
            TimelineEvent(
                1,
                "response",
                ("c1", b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nshort"),
            ),
            TimelineEvent(2, "response", ("c1", b"")),
        ]
    )
    assert [request.key for request in result.dropped] == ["a"]


# ------------------------------------------------------------- validation


def test_duplicate_request_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        Scheduler(["c1"], requests=[request("a"), request("a")])


def test_duplicate_slot_names_are_rejected() -> None:
    with pytest.raises(ValueError):
        Scheduler(["c1", "c1"])


def test_release_after_deadline_is_rejected() -> None:
    with pytest.raises(ValueError):
        request("a", release=11, deadline=10)


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValueError):
        request("a", duration=-1)


def test_unknown_slot_raises() -> None:
    scheduler = Scheduler(["c1"])
    with pytest.raises(KeyError):
        scheduler.slot("nope")


def test_busy_slots_do_not_prove_infeasibility() -> None:
    # c1 is expected to be busy for 20 units, but that is only an estimate:
    # the request must stay pending rather than be discarded, and it must go
    # out as soon as the real body actually completes.
    scheduler = Scheduler(
        ["c1"],
        requests=[
            request("occupier", duration=20, deadline=1000),
            request("waiter", priority=9, deadline=15, duration=5),
        ],
    )
    assert keys(scheduler.plan(0)) == ["occupier"]
    result = scheduler.replay(
        [
            TimelineEvent(2, "response", ("c1", EMPTY_OK)),
        ]
    )
    assert result.infeasible == []
    assert result.order == ["occupier", "waiter"]
    assert result.dispatched[1].at == 2


def test_request_still_late_after_slots_free_stays_pending() -> None:
    scheduler = Scheduler(
        ["c1"],
        requests=[
            request("occupier", duration=20, deadline=1000),
            request("waiter", priority=9, deadline=15, duration=5),
        ],
    )
    scheduler.plan(0)
    result = scheduler.replay([TimelineEvent(0, "time")])
    # No response timeline arrived, so the body really is still outstanding:
    # the waiter is blocked, not provably impossible.
    assert result.order == ["occupier"]
    assert result.infeasible == []
    assert [item.key for item in result.unfinished] == ["waiter"]


def test_next_round_reports_dispatches_and_set_asides() -> None:
    scheduler = Scheduler(
        ["c1"],
        cooldowns=[CooldownRule("site", "site", until=5)],
        requests=[
            request("ok", deadline=100),
            request("doomed", deadline=6, duration=3),
        ],
    )
    round_ = scheduler.next_round(0)
    assert round_.at == 0
    assert round_.entries == []
    assert [item.request.key for item in round_.newly_infeasible] == ["doomed"]
    assert [req.key for req in round_.pending] == ["ok"]
    assert round_.slots == {"c1": SlotState.IDLE}

    round_ = scheduler.next_round(5)
    assert round_.order == ["ok"]
    assert round_.newly_infeasible == []
    assert round_.slots == {"c1": SlotState.SENDING}


def test_history_is_retained_across_rounds() -> None:
    scheduler = Scheduler(
        ["c1"], requests=[request("a"), request("b", deadline=1000)]
    )
    scheduler.next_round(0)
    complete(scheduler, "c1")
    scheduler.next_round(1)
    assert [entry.request.key for entry in scheduler.dispatched] == ["a", "b"]
    assert scheduler.infeasible == []
