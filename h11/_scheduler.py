"""Deterministic crawl scheduling over a small pool of HTTP/1.1 connections.

This module is deliberately I/O-free, in the spirit of the rest of h11.  It
models the part of a crawler that decides *what* to put on the wire *when*:

* a fixed pool of :class:`Slot` objects, each representing one shared HTTP/1.1
  connection.  HTTP/1.1 has no multiplexing, so a slot carries at most one
  request/response cycle at a time;
* several accounts, since multiple crawl identities share those few
  connections;
* server-imposed cooldowns scoped to an account, a path, or a whole site;
* requests with a priority, a deadline and an expected duration;
* a replayable timeline of local events, so that a run is reproducible.

Two rules drive most of the behaviour here:

1. **A slot is reusable only when its response body is done.**  Receiving the
   response headers proves the server is alive, but the connection is still
   busy until :class:`~h11.EndOfMessage` arrives.  :class:`Slot` tracks this
   with a real :class:`h11.Connection` state machine rather than a boolean, so
   a truncated or failed body can never silently free the connection.

2. **Cooldowns compose by "latest wins".**  If an account rule and a site rule
   both apply, the request may only be dispatched once *both* have expired.
   Overlapping rules can therefore never shorten a freeze: the effective
   expiry is the maximum of all applicable expiries, and cooldowns are always
   inclusive of their expiry instant.

Scheduling is a greedy priority pass.  At every instant where work could be
dispatched, pending requests are considered in a stable order (priority, then
deadline, then expected duration, then key) and each is placed on the first
eligible idle slot.  A request that cannot possibly finish by its deadline --
even if it started at the earliest instant its cooldowns and release allow --
is moved to a separate *infeasible* list and never consumes a slot.  Requests
that are merely blocked for now stay pending and are retried later.

None of this reads a wall clock: callers pass explicit timestamps, which makes
any run exactly reproducible from the same inputs.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ._connection import Connection, NEED_DATA, PAUSED
from ._events import (
    ConnectionClosed,
    EndOfMessage,
    Event,
    InformationalResponse,
    Request as _RequestEvent,
    Response as _ResponseEvent,
)
from ._state import CLIENT, IDLE, MUST_CLOSE
from ._util import LocalProtocolError, RemoteProtocolError

__all__ = [
    "Account",
    "Cooldown",
    "CooldownRule",
    "Infeasible",
    "ReplayResult",
    "Request",
    "Round",
    "ScheduleEntry",
    "Scheduler",
    "Slot",
    "SlotState",
    "TimelineEvent",
    "cooldown_rules_from_response",
]

# Scope sentinels used internally for cooldown keys.
_ALL = "*"


@dataclass(frozen=True)
class Account:
    """A crawl identity, e.g. one set of credentials or one proxy exit."""

    name: str


@dataclass(frozen=True)
class CooldownRule:
    """A server-supplied freeze.

    Args:
        scope: ``"account"``, ``"path"`` or ``"site"``.
        subject: the account name, path, or site to freeze.  ``None`` is only
            allowed for ``"site"`` and means "every site" (a global freeze).
        until: absolute time at which the freeze ends (inclusive).
        site: the site a ``"path"`` rule belongs to.  Required for path rules;
            ignored otherwise.
        reason: free-form text for reports.

    Rules for the same key never replace one another; the later expiry wins,
    so reapplying a shorter freeze cannot release a request early.
    """

    scope: str
    subject: Optional[str]
    until: float
    site: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.scope not in ("account", "path", "site"):
            raise ValueError(f"unknown cooldown scope {self.scope!r}")
        if self.scope == "account" and self.subject is None:
            raise ValueError("account cooldown needs an account name")
        if self.scope == "path":
            if self.subject is None:
                raise ValueError("path cooldown needs a path")
            if self.site is None:
                raise ValueError("path cooldown needs a site")


@dataclass(frozen=True)
class Cooldown:
    """An active freeze together with the rule that produced it."""

    scope: str
    subject: Optional[str]
    until: float
    site: Optional[str] = None
    reason: str = ""

    def covers(self, request: "Request") -> bool:
        """Whether this freeze applies to ``request`` at all."""
        if self.scope == "account":
            return self.subject == request.account.name
        if self.scope == "path":
            return self.site == request.site and self.subject == request.path
        # site
        return self.subject is None or self.subject == request.site


@dataclass(frozen=True)
class Request:
    """One unit of work.

    Args:
        key: unique, stable identifier used in reports and tie-breaking.
        account: the account used to fetch it.
        site: the site the path belongs to.
        path: request path, also the subject of ``"path"`` cooldowns.
        priority: lower numbers are dispatched first.
        deadline: absolute time by which the whole request should finish.
        duration: expected wall-clock time the request occupies its slot.
        release: earliest time the request may be dispatched regardless of
            priority.  Defaults to ``0``.
    """

    key: str
    account: Account
    site: str
    path: str
    priority: int
    deadline: float
    duration: float
    release: float = 0.0

    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError("duration must be non-negative")
        if self.release > self.deadline:
            raise ValueError("release is after deadline")


@dataclass(frozen=True)
class TimelineEvent:
    """Something observed locally at a known time.

    ``kind`` decides how ``detail`` is interpreted:

    ``"time"``      -- advance the clock, no side effects.
    ``"response"``  -- ``detail = (slot_name, raw_bytes)``; feed wire bytes to
                       that slot's :class:`h11.Connection`.
    ``"cooldown"``  -- ``detail = (CooldownRule,)``; record a freeze.
    ``"close"``     -- ``detail = (slot_name,)``; mark the slot closed.

    Building responses as raw bytes keeps replay honest: the same parser that
    gates real sockets also gates the fixture.
    """

    at: float
    kind: str
    detail: Tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if self.at < 0:
            raise ValueError("timeline times are non-negative")
        if self.kind not in ("time", "response", "cooldown", "close"):
            raise ValueError(f"unknown timeline event kind {self.kind!r}")


class SlotState:
    """Lifecycle of a connection slot."""

    IDLE = "idle"
    SENDING = "sending"
    HEADERS = "headers"
    CLOSED = "closed"


class Slot:
    """One shared HTTP/1.1 connection.

    ``conn`` is a real client-side :class:`h11.Connection`.  Slot state is
    derived from it instead of from ad-hoc booleans, so "headers received but
    body unfinished" cannot be mistaken for "free".
    """

    def __init__(self, name: str, conn: Optional[Connection] = None) -> None:
        self.name = name
        self.conn = conn if conn is not None else Connection(CLIENT)
        self._current: Optional[Request] = None
        self._started_at: Optional[float] = None
        self._response: Optional[_ResponseEvent] = None
        self._body_done = False
        self._closed = False
        self._dropped: Optional[Request] = None
        self.received_bytes = 0

    @property
    def current(self) -> Optional[Request]:
        return self._current

    @property
    def dropped(self) -> Optional[Request]:
        """The request lost when the connection died before its body finished."""
        return self._dropped

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    @property
    def response(self) -> Optional[_ResponseEvent]:
        return self._response

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def headers_received(self) -> bool:
        """True once response headers arrived, even if the body has not."""
        return self._response is not None

    @property
    def body_complete(self) -> bool:
        return self._current is not None and self._body_done

    @property
    def state(self) -> str:
        if self._closed or (
            self._current is None and self.conn.our_state is MUST_CLOSE
        ):
            return SlotState.CLOSED
        if self._current is None:
            return SlotState.IDLE
        if self._response is None:
            return SlotState.SENDING
        return SlotState.HEADERS

    @property
    def reusable(self) -> bool:
        """A slot may take new work only when fully idle."""
        return self._current is None and self.state == SlotState.IDLE

    def begin(self, request: Request, now: float) -> bytes:
        """Serialize ``request`` onto this slot and return the wire bytes."""
        if not self.reusable:
            raise LocalProtocolError(f"slot {self.name} is not reusable")
        wire = self.conn.send(
            _RequestEvent(
                method=b"GET",
                target=request.path.encode("ascii"),
                headers=[(b"host", request.site.encode("ascii"))],
            )
        )
        wire += self.conn.send(EndOfMessage())
        self._current = request
        self._started_at = now
        self._response = None
        self._body_done = False
        return wire

    def receive_data(self, data: bytes) -> List[Event]:
        """Feed response bytes through h11 and return the parsed events.

        A truncated body (the peer closed before ``EndOfMessage``) does not
        free the slot: :attr:`reusable` stays false and the slot is marked
        closed, because a connection whose body was cut short cannot be
        trusted for another request.
        """
        if self._current is None:
            raise LocalProtocolError(f"slot {self.name} has no request in flight")
        self.conn.receive_data(data)
        self.received_bytes += len(data)
        events: List[Event] = []
        try:
            while True:
                event = self.conn.next_event()
                if event is NEED_DATA or event is PAUSED:
                    break
                events.append(event)
                if isinstance(event, _ResponseEvent):
                    self._response = event
                elif isinstance(event, EndOfMessage):
                    self._body_done = True
                elif isinstance(event, ConnectionClosed):
                    self._closed = True
                    break
        except RemoteProtocolError:
            # The body was cut off mid-message.  The request cannot complete,
            # so the slot is dead but must not be handed to the next request.
            self._closed = True
            self._dropped = self._release()
        return events

    def _release(self) -> Optional[Request]:
        released = self._current
        self._current = None
        self._started_at = None
        self._response = None
        self._body_done = False
        return released

    def finish(self) -> Optional[Request]:
        """Retire a completed cycle, returning the request that was on it.

        Raises :class:`LocalProtocolError` while the response body is still
        outstanding; that is precisely the case where the connection must not
        be handed new work.
        """
        if self._current is None:
            return None
        if not self._body_done:
            raise LocalProtocolError(f"slot {self.name} still owes a response body")
        finished = self._release()
        if self._closed or self.conn.our_state is MUST_CLOSE:
            self._closed = True
            return finished
        try:
            self.conn.start_next_cycle()
        except LocalProtocolError:
            # The peer asked for the connection to close, or the cycle is not
            # resumable; keep the slot around but never reuse it.
            self._closed = True
        return finished

    def close(self) -> None:
        """Abandon whatever is in flight; the slot is unusable afterwards."""
        if self._current is not None:
            try:
                self.conn.send(ConnectionClosed())
            except LocalProtocolError:
                pass
            if not self._body_done:
                self._dropped = self._current
        self._release()
        self._closed = True


@dataclass(frozen=True)
class ScheduleEntry:
    """One dispatch decision."""

    at: float
    slot: str
    request: Request
    expected_finish: float


@dataclass(frozen=True)
class Infeasible:
    """A request that cannot finish by its deadline and was set aside."""

    at: float
    request: Request
    reason: str
    earliest_start: float
    cooldown_until: float


@dataclass
class Round:
    """The outcome of one :meth:`Scheduler.next_round` call."""

    at: float
    entries: List[ScheduleEntry]
    newly_infeasible: List[Infeasible]
    pending: List[Request]
    slots: Dict[str, str]

    @property
    def order(self) -> List[str]:
        return [entry.request.key for entry in self.entries]


@dataclass
class ReplayResult:
    """Everything a replay produced, in timeline order."""

    dispatched: List[ScheduleEntry]
    infeasible: List[Infeasible]
    unfinished: List[Request]
    cooldowns: List[Cooldown]
    dropped: List[Request] = field(default_factory=list)

    @property
    def order(self) -> List[str]:
        """Request keys in dispatch order, convenience for assertions."""
        return [entry.request.key for entry in self.dispatched]


class _CooldownRegistry:
    """Stores freezes and combines overlapping ones conservatively."""

    def __init__(self) -> None:
        self._rules: Dict[Tuple[str, Optional[str], Optional[str]], Cooldown] = {}

    @staticmethod
    def _key(rule: CooldownRule) -> Tuple[str, Optional[str], Optional[str]]:
        if rule.scope == "account":
            return (rule.scope, _ALL, rule.subject)
        if rule.scope == "path":
            return (rule.scope, rule.site, rule.subject)
        return (rule.scope, rule.subject or _ALL, _ALL)

    def add(self, rule: CooldownRule) -> Cooldown:
        key = self._key(rule)
        existing = self._rules.get(key)
        until = rule.until if existing is None else max(existing.until, rule.until)
        reason = rule.reason or (existing.reason if existing is not None else "")
        cooldown = Cooldown(
            scope=rule.scope,
            subject=rule.subject,
            until=until,
            site=rule.site,
            reason=reason,
        )
        self._rules[key] = cooldown
        return cooldown

    def _lookup(
        self, scope: str, site: Optional[str], subject: Optional[str]
    ) -> Optional[Cooldown]:
        return self._rules.get((scope, site, subject))

    def applicable(self, request: "Request") -> List[Cooldown]:
        """Every freeze covering ``request``, with duplicates removed."""
        candidates = [
            self._lookup("account", _ALL, request.account.name),
            self._lookup("path", request.site, request.path),
            self._lookup("site", request.site, _ALL),
            self._lookup("site", _ALL, _ALL),
        ]
        seen: List[Cooldown] = []
        for cooldown in candidates:
            if cooldown is not None and cooldown not in seen:
                seen.append(cooldown)
        return seen

    def effective_until(self, request: "Request") -> float:
        """The latest expiry among all freezes covering ``request``.

        Taking the maximum is what prevents stacked cooldowns from releasing
        work early.  ``0`` means "not frozen".
        """
        return max((c.until for c in self.applicable(request)), default=0.0)

    def active(self, now: float) -> List[Cooldown]:
        return sorted(
            (c for c in self._rules.values() if c.until > now),
            key=lambda c: (c.until, c.scope, c.subject or "", c.site or ""),
        )

    def all(self) -> List[Cooldown]:
        return sorted(
            self._rules.values(),
            key=lambda c: (c.until, c.scope, c.subject or "", c.site or ""),
        )


def cooldown_rules_from_response(
    headers: Iterable[Tuple[bytes, bytes]],
    *,
    now: float,
    account: Optional[Account] = None,
    site: Optional[str] = None,
    path: Optional[str] = None,
) -> List[CooldownRule]:
    """Translate freeze headers on a response into :class:`CooldownRule` s.

    Recognised headers (case-insensitive, integer seconds):

    ``Retry-After``            -- freezes the whole site (or globally when no
                                  site is given).
    ``X-Crawl-Freeze-Site``    -- same as ``Retry-After``.
    ``X-Crawl-Freeze-Path``    -- freezes ``path`` on ``site``.
    ``X-Crawl-Freeze-Account`` -- freezes ``account``.

    Unknown headers are ignored.  The function is a convenience: callers that
    already know their rule format can build :class:`CooldownRule` directly.
    """
    rules: List[CooldownRule] = []
    for raw_name, raw_value in headers:
        name = raw_name.lower()
        if name not in (
            b"retry-after",
            b"x-crawl-freeze-site",
            b"x-crawl-freeze-path",
            b"x-crawl-freeze-account",
        ):
            continue
        try:
            seconds = float(raw_value.strip())
        except ValueError:
            # HTTP-dates and other Retry-After forms are out of scope here.
            continue
        until = now + seconds
        if name in (b"retry-after", b"x-crawl-freeze-site"):
            rules.append(CooldownRule("site", site, until, reason="response"))
        elif name == b"x-crawl-freeze-path":
            if site is None or path is None:
                raise ValueError("path freeze needs both site and path")
            rules.append(
                CooldownRule("path", path, until, site=site, reason="response")
            )
        else:
            if account is None:
                raise ValueError("account freeze needs an account")
            rules.append(
                CooldownRule("account", account.name, until, reason="response")
            )
    return rules


class Scheduler:
    """Plans work onto a fixed pool of HTTP/1.1 slots.

    Args:
        slots: slot names, :class:`Slot` objects, or a mix.  Strings are turned
            into fresh slots in the given order; order is also the slot
            preference for ties.
        cooldowns: optional initial :class:`CooldownRule` s.
        requests: optional initial :class:`Request` s.
    """

    def __init__(
        self,
        slots: Sequence[Union[str, "Slot"]],
        cooldowns: Iterable[CooldownRule] = (),
        requests: Iterable[Request] = (),
    ) -> None:
        self.slots: List[Slot] = [
            slot if isinstance(slot, Slot) else Slot(slot) for slot in slots
        ]
        names = [slot.name for slot in self.slots]
        if len(set(names)) != len(names):
            raise ValueError("slot names must be unique")
        self._registry = _CooldownRegistry()
        for rule in cooldowns:
            self._registry.add(rule)
        self._pending: List[Request] = list(requests)
        keys = [request.key for request in self._pending]
        if len(set(keys)) != len(keys):
            raise ValueError("request keys must be unique")
        self._dispatched: List[ScheduleEntry] = []
        self._infeasible: List[Infeasible] = []
        self._seen_keys = set(keys)

    # -- inspection -------------------------------------------------------

    @property
    def pending(self) -> List[Request]:
        return list(self._pending)

    @property
    def dispatched(self) -> List[ScheduleEntry]:
        return list(self._dispatched)

    @property
    def infeasible(self) -> List[Infeasible]:
        return list(self._infeasible)

    @property
    def cooldowns(self) -> List[Cooldown]:
        return self._registry.all()

    def slot(self, name: str) -> Slot:
        for slot in self.slots:
            if slot.name == name:
                return slot
        raise KeyError(name)

    def applicable_cooldowns(self, request: Request) -> List[Cooldown]:
        return self._registry.applicable(request)

    def effective_cooldown_until(self, request: Request) -> float:
        return self._registry.effective_until(request)

    # -- input ------------------------------------------------------------

    def add_request(self, request: Request) -> None:
        if request.key in self._seen_keys:
            raise ValueError(f"duplicate request key {request.key!r}")
        self._seen_keys.add(request.key)
        self._pending.append(request)

    def add_requests(self, requests: Iterable[Request]) -> None:
        for request in requests:
            self.add_request(request)

    def add_cooldown(self, rule: CooldownRule) -> Cooldown:
        return self._registry.add(rule)

    # -- planning ---------------------------------------------------------

    def _earliest_start(self, request: Request, now: float) -> Tuple[float, float]:
        cooldown_until = self._registry.effective_until(request)
        return max(now, request.release, cooldown_until), cooldown_until

    def _earliest_provable_start(
        self, request: Request, now: float
    ) -> Tuple[float, str]:
        """Earliest start we can *prove* is possible, with the binding reason.

        Only two constraints are proof rather than estimate: the request's own
        release time and its cooldowns.  A busy slot might free up sooner than
        its expected finish (or later), so it is never used to declare a
        request impossible -- being optimistic there would wrongly drop work.
        """
        earliest = now
        reason = ""
        if request.release > earliest:
            earliest = request.release
            reason = "release"
        cooldown_until = self._registry.effective_until(request)
        if cooldown_until > earliest:
            earliest = cooldown_until
            reason = "cooldown"
        return earliest, reason

    def _set_aside_infeasible(self, now: float) -> None:
        no_capacity = bool(self.slots) and all(
            slot.state == SlotState.CLOSED for slot in self.slots
        )
        still_pending: List[Request] = []
        for request in self._pending:
            earliest, reason = self._earliest_provable_start(request, now)
            if no_capacity:
                self._infeasible.append(
                    Infeasible(
                        at=now,
                        request=request,
                        reason="no connection can carry the request",
                        earliest_start=earliest,
                        cooldown_until=self._registry.effective_until(request),
                    )
                )
                continue
            if earliest + request.duration > request.deadline:
                if reason == "cooldown":
                    label = "deadline unreachable after cooldown"
                elif reason == "release":
                    label = "deadline unreachable after release"
                else:
                    label = "deadline unreachable now"
                self._infeasible.append(
                    Infeasible(
                        at=now,
                        request=request,
                        reason=label,
                        earliest_start=earliest,
                        cooldown_until=self._registry.effective_until(request),
                    )
                )
            else:
                still_pending.append(request)
        self._pending = still_pending

    @staticmethod
    def _order_key(request: Request) -> Tuple[int, float, float, str]:
        # Priority first, then the tightest deadline, then the shortest job
        # (which frees a slot sooner), then the key for full determinism.
        return (request.priority, request.deadline, request.duration, request.key)

    def next_round(self, now: float) -> "Round":
        """Plan the next round of work at ``now`` and describe it.

        This is the one-shot entry point: it refreshes the set of infeasible
        requests, dispatches everything that currently fits, and returns both
        the new dispatches and the work that was set aside, so a caller can act
        on the round without inspecting scheduler internals.
        """
        before = len(self._infeasible)
        entries = self.plan(now)
        return Round(
            at=now,
            entries=entries,
            newly_infeasible=list(self._infeasible[before:]),
            pending=self.pending,
            slots={slot.name: slot.state for slot in self.slots},
        )

    def plan(self, now: float) -> List[ScheduleEntry]:
        """Dispatch as much pending work as possible at ``now``.

        Returns the entries created by this call; previously returned entries
        are retained by :meth:`replay` and :attr:`dispatched`.  A slot that has
        received response headers but not its body is busy and is skipped.
        """
        self._set_aside_infeasible(now)
        ready = [
            request
            for request in self._pending
            if request.release <= now
            and self._registry.effective_until(request) <= now
        ]
        ready.sort(key=self._order_key)
        free = [slot for slot in self.slots if slot.reusable]
        entries: List[ScheduleEntry] = []
        for request in ready:
            if not free:
                break
            slot = free.pop(0)
            slot.begin(request, now)
            self._pending.remove(request)
            entry = ScheduleEntry(
                at=now,
                slot=slot.name,
                request=request,
                expected_finish=now + request.duration,
            )
            entries.append(entry)
        self._dispatched.extend(entries)
        return entries

    # -- responses --------------------------------------------------------

    def observe(
        self, slot_name: str, data: bytes, at: Optional[float] = None
    ) -> List[Event]:
        """Feed response bytes into a slot, auto-retiring finished bodies.

        When the body reaches ``EndOfMessage`` the cycle is closed and the slot
        becomes reusable for the next dispatch; until then it stays busy no
        matter what the headers said.

        If ``at`` is given, freeze headers on the response are turned into
        cooldowns automatically, attached to the account/site/path of the
        request currently on that slot.
        """
        slot = self.slot(slot_name)
        request = slot.current
        events = slot.receive_data(data)
        if at is not None and request is not None:
            for event in events:
                if isinstance(event, _ResponseEvent) and not isinstance(
                    event, InformationalResponse
                ):
                    rules = cooldown_rules_from_response(
                        event.headers,
                        now=at,
                        account=request.account,
                        site=request.site,
                        path=request.path,
                    )
                    for rule in rules:
                        self._registry.add(rule)
        if slot.body_complete:
            slot.finish()
        return events

    def close_slot(self, slot_name: str) -> None:
        self.slot(slot_name).close()

    # -- replay -----------------------------------------------------------

    def _next_internal_wake(self, now: float) -> Optional[float]:
        # Only worth waking for a pending request held back by its own release
        # or cooldown window.  While every slot is busy the caller must supply
        # the next real event (a response, EOF, or close), because only that
        # can tell us when a slot actually frees up.
        if not any(slot.reusable for slot in self.slots):
            return None
        wakes: List[float] = []
        for request in self._pending:
            earliest, _ = self._earliest_start(request, now)
            if earliest > now:
                wakes.append(earliest)
        return min(wakes) if wakes else None

    def _apply(self, event: TimelineEvent, now: float) -> None:
        if event.kind == "time":
            return
        if event.kind == "response":
            slot_name, data = event.detail
            self.observe(slot_name, data, at=now)
            return
        if event.kind == "cooldown":
            (rule,) = event.detail
            self.add_cooldown(rule)
            return
        if event.kind == "close":
            (slot_name,) = event.detail
            self.close_slot(slot_name)
            return
        raise AssertionError(f"unhandled timeline event {event.kind!r}")

    def replay(
        self, timeline: Iterable[TimelineEvent], start: float = 0.0
    ) -> ReplayResult:
        """Apply a local event timeline and schedule around it.

        Events are processed in time order, starting at ``start``.  Between
        events the scheduler wakes itself for the next release or cooldown
        expiry, so a request that becomes eligible while the timeline is quiet
        is not stranded until the following external event.
        """
        events = sorted(timeline, key=lambda event: event.at)
        now = start
        index = 0
        while True:
            # Events stamped at the current instant are applied before the
            # scheduler plans, so a freeze or a body completion observed "now"
            # affects the decisions made "now".  Response events therefore
            # require the request to have been dispatched at an earlier step.
            while index < len(events) and events[index].at <= now:
                self._apply(events[index], now)
                index += 1
            self.plan(now)
            next_external = events[index].at if index < len(events) else None
            next_internal = self._next_internal_wake(now)
            candidates = [
                time
                for time in (next_external, next_internal)
                if time is not None and time > now
            ]
            if not candidates:
                break
            now = min(candidates)
        return ReplayResult(
            dispatched=list(self._dispatched),
            infeasible=list(self._infeasible),
            unfinished=self.pending,
            cooldowns=self.cooldowns,
            dropped=[slot.dropped for slot in self.slots if slot.dropped is not None],
        )
