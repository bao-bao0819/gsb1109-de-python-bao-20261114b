"""Event-journal reduction and the requeue planner.

Two guarantees drive the implementation:

* replaying an event with the same ``event_id`` cannot create a second
  supplement job (the reducer is set-based, not counter-based);
* no byte-level guess is made about whether the origin acted.  A flushed
  request line or header block is already exposure; a partially flushed body
  is exposure too, regardless of how much of it arrived.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .model import (
    Action,
    Bucket,
    Decision,
    Event,
    IdempotencyRule,
    RequeuePlan,
    RequestRecord,
    SupplementJob,
)

IDEMPOTENT_METHODS = frozenset(
    {"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"}
)

# A complete response is direct evidence about what the origin did, unlike a
# disconnect.  Statuses that refuse the request before any side effect are
# safe to retry; 409 means an effect may already exist.
RETRYABLE_REJECTIONS = frozenset({400, 401, 403, 405, 413, 415, 422})
CONFLICT_STATUS = 409


class JournalError(ValueError):
    """Raised when journal input cannot be interpreted without guessing."""


def _as_int(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise JournalError(f"{field_name} must be an integer, got {value!r}") from exc
    if number < 0:
        raise JournalError(f"{field_name} must be non-negative, got {value!r}")
    return number


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    return _as_int(value, field_name)


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None


class Journal:
    """Append-only, replay-safe view of persisted send progress."""

    def __init__(self, rule: Optional[IdempotencyRule] = None) -> None:
        self.rule = rule or IdempotencyRule()
        self.requests: Dict[str, RequestRecord] = {}
        self._seen: Dict[str, Tuple[str, str]] = {}
        self.ignored_event_ids: List[str] = []
        self.anomalies: List[str] = []

    def _record(self, request_id: str, order: int) -> RequestRecord:
        record = self.requests.get(request_id)
        if record is None:
            record = RequestRecord(
                request_id=request_id,
                order=order,
            )
            self.requests[request_id] = record
        return record

    def _apply(self, event: Event, order: int) -> None:
        data = event.data
        kind = event.kind
        record = self._record(event.request_id, order)

        if kind in {"request_registered", "request", "enqueue"}:
            record.method = str(data.get("method", record.method)).upper()
            record.url = str(data.get("url", record.url))
            body_hash = _optional_str(data.get("body_sha256"))
            if body_hash is not None:
                record.body_sha256 = body_hash
                record.observed_body_hashes.add(body_hash)
            key = _optional_str(data.get("idempotency_key"))
            if key is not None:
                record.idempotency_key = key
                record.observed_keys.add(key)
            replay_of = _optional_str(data.get("replay_of"))
            if replay_of is not None:
                record.replay_of = replay_of
            record.order = min(record.order, order) if record.order else order
            return

        attempt_id = event.attempt_id
        if attempt_id in {"", "-", None}:
            attempt_id = str(data.get("attempt_id", "-"))
        attempt = record.attempt(attempt_id, order)

        if kind in {"attempt_created", "attempt_started"}:
            return

        if kind in {"attempt_headers_sent", "headers_sent", "request_headers_flushed"}:
            attempt.headers_flushed = True
            if data.get("body_total") is not None:
                attempt.body_bytes_total = _optional_int(
                    data.get("body_total"), "body_total"
                )
            return

        if kind in {"attempt_body_progress", "body_progress", "body_bytes_flushed"}:
            attempt.headers_flushed = True
            flushed = data.get("bytes", data.get("bytes_flushed", 0))
            if data.get("cumulative") is True:
                attempt.body_bytes_flushed = max(
                    attempt.body_bytes_flushed, _as_int(flushed, "bytes")
                )
            else:
                attempt.body_bytes_flushed += _as_int(flushed, "bytes")
            total = _optional_int(
                data.get("total", data.get("body_total")), "body_total"
            )
            if total is not None:
                attempt.body_bytes_total = total
            if bool(data.get("declared_complete")):
                attempt.body_declared_complete = True
            body_hash = _optional_str(data.get("body_sha256"))
            if body_hash is not None:
                self._observe_body(record, body_hash)
            return

        if kind in {"attempt_body_complete", "body_complete", "request_sent"}:
            attempt.headers_flushed = True
            attempt.body_declared_complete = True
            if data.get("body_total") is not None:
                attempt.body_bytes_total = _optional_int(
                    data.get("body_total"), "body_total"
                )
            if attempt.body_bytes_total is not None:
                attempt.body_bytes_flushed = max(
                    attempt.body_bytes_flushed, attempt.body_bytes_total
                )
            return

        if kind in {"attempt_response", "response_received"}:
            attempt.headers_flushed = True
            status = _optional_int(
                data.get("status", data.get("status_code")), "status"
            )
            if status is not None:
                attempt.response_status = status
            if bool(data.get("body_stored")):
                attempt.response_body_stored = True
            receipt_id = _optional_str(data.get("receipt_id"))
            if receipt_id is not None:
                attempt.receipt_id = receipt_id
                attempt.receipt_persisted = True
            return

        if kind in {"receipt_persisted", "receipt_stored"}:
            attempt.receipt_persisted = True
            receipt_id = _optional_str(data.get("receipt_id"))
            if receipt_id is not None:
                attempt.receipt_id = receipt_id
            return

        if kind in {"local_delivery", "delivered_local"}:
            attempt.local_delivery_ref = (
                _optional_str(data.get("ref"))
                or _optional_str(data.get("local_delivery_ref"))
                or "local"
            )
            return

        if kind in {"attempt_failed", "attempt_aborted", "disconnected"}:
            return

        raise JournalError(f"unknown event kind {kind!r}")

    def _observe_body(self, record: RequestRecord, body_hash: str) -> None:
        record.observed_body_hashes.add(body_hash)
        if not record.body_sha256:
            record.body_sha256 = body_hash
        elif record.body_sha256 != body_hash:
            record.anomalies.append(
                f"request {record.request_id}: body hash changed from "
                f"{record.body_sha256} to {body_hash}"
            )

    def append(self, event: Event, order: Optional[int] = None) -> bool:
        """Apply one event.  Returns False when it is a replayed duplicate."""
        fingerprint = event.fingerprint()
        prior = self._seen.get(event.event_id)
        if prior is not None:
            if prior[1] != fingerprint:
                self.anomalies.append(
                    f"event {event.event_id}: replayed id with different payload "
                    "(kept the first, ignored the variant)"
                )
            if event.event_id not in self.ignored_event_ids:
                self.ignored_event_ids.append(event.event_id)
            return False
        index = len(self._seen) if order is None else order
        self._seen[event.event_id] = (event.kind, fingerprint)
        self._apply(event, index)
        return True

    def extend(self, events: Iterable[Mapping[str, Any] | Event]) -> None:
        for raw in events:
            event = raw if isinstance(raw, Event) else Event.from_mapping(raw)
            self.append(event)

    def _key_state(self) -> Dict[str, Tuple[set, int]]:
        """Map each key to (known body hashes, request count without a hash)."""
        state: Dict[str, Tuple[set, int]] = {}
        for record in self.requests.values():
            key = record.idempotency_key
            if key is None:
                continue
            bodies, unknown = state.get(key, (set(), 0))
            record_bodies = set(record.observed_body_hashes)
            if record.body_sha256:
                record_bodies.add(record.body_sha256)
            bodies |= record_bodies
            if not record_bodies:
                unknown += 1
            state[key] = (bodies, unknown)
        return state

    def _coalesce(self) -> Tuple[List[RequestRecord], List[str]]:
        """Merge request records that provably denote the same logical job.

        Merging is allowed only on durable identity: an idempotency key with
        the same body, or an explicit ``replay_of`` alias recorded in the
        journal.  Two unrelated POSTs with the same URL must stay distinct
        even when their body hash was never persisted, otherwise a recreate
        (export) job could silently disappear.
        """
        alias_root: Dict[str, str] = {}
        for request_id, record in self.requests.items():
            if record.replay_of is not None:
                alias_root[request_id] = record.replay_of

        def resolve(request_id: str, seen: Optional[set] = None) -> str:
            seen = set() if seen is None else seen
            if request_id in seen:
                return request_id
            seen.add(request_id)
            target = alias_root.get(request_id)
            if target is None:
                return request_id
            return resolve(target, seen)

        grouped: Dict[Tuple[str, ...], List[RequestRecord]] = {}
        for request_id in sorted(self.requests):
            record = self.requests[request_id]
            method = (record.method or "UNKNOWN").upper()
            if record.idempotency_key is not None:
                if record.body_sha256:
                    identity = (
                        "key",
                        method,
                        record.url,
                        record.idempotency_key,
                        record.body_sha256,
                    )
                else:
                    # Without a persisted body hash two requests on one key
                    # cannot be proven to be the same job; keep them separate.
                    identity = ("request", resolve(request_id))
            else:
                identity = ("request", resolve(request_id))
            grouped.setdefault(identity, []).append(record)

        canonical: List[RequestRecord] = []
        warnings: List[str] = []
        for identity, records in grouped.items():
            if len(records) > 1 and identity[0] == "request":
                primary = next(
                    (
                        record
                        for record in records
                        if record.request_id == identity[1]
                    ),
                    records[0],
                )
            else:
                primary = records[0]
            for extra in records:
                if extra is primary:
                    continue
                self._merge_record(primary, extra)
                warnings.append(
                    f"request {extra.request_id}: replayed identity merged into "
                    f"{primary.request_id} (no second job will be queued)"
                )
            canonical.append(primary)
        canonical.sort(key=lambda record: (record.order, record.request_id))
        return canonical, warnings

    @staticmethod
    def _merge_record(primary: RequestRecord, extra: RequestRecord) -> None:
        primary.order = min(primary.order or extra.order, extra.order or primary.order)
        primary.url = primary.url or extra.url
        primary.method = primary.method or extra.method
        primary.body_sha256 = primary.body_sha256 or extra.body_sha256
        primary.idempotency_key = primary.idempotency_key or extra.idempotency_key
        primary.replay_of = primary.replay_of or extra.replay_of
        primary.observed_body_hashes |= extra.observed_body_hashes
        primary.observed_keys |= extra.observed_keys
        primary.anomalies.extend(extra.anomalies)
        primary.anomalies.append(
            f"merged replay alias {extra.request_id} into {primary.request_id}"
        )
        for attempt_id, attempt in extra.attempts.items():
            existing = primary.attempts.get(attempt_id)
            if existing is None:
                primary.attempts[attempt_id] = attempt
            else:
                primary.attempts[attempt_id] = existing.merged_with(attempt)

    def plan(self) -> RequeuePlan:
        plan = RequeuePlan(
            ignored_event_ids=list(self.ignored_event_ids),
            anomalies=list(self.anomalies),
        )

        key_state = self._key_state()
        ambiguous_keys: Dict[str, set] = {}
        unverifiable_keys: Dict[str, Set[str]] = {}
        for key, (bodies, unknown) in sorted(key_state.items()):
            request_ids = {
                request.request_id
                for request in self.requests.values()
                if request.idempotency_key == key
            }
            if len(bodies) > 1 or (unknown and len(request_ids) > 1):
                ambiguous_keys[key] = bodies
                plan.isolated_keys[key] = sorted(request_ids)
                plan.anomalies.append(
                    f"idempotency key {key!r} cannot be attributed to one body "
                    f"({len(bodies)} known bodies, {unknown} unknown among "
                    f"{len(request_ids)} requests); those requests are isolated"
                )
            elif unknown:
                unverifiable_keys[key] = request_ids
        requests, merge_notes = self._coalesce()
        plan.anomalies.extend(merge_notes)
        for request in requests:
            decision, isolation, extra = self._decide(
                request, ambiguous_keys, unverifiable_keys
            )
            if isolation is not None:
                decision = Decision(
                    **{
                        **decision.__dict__,
                        "isolation_group": isolation,
                    }
                )
            if decision.bucket is Bucket.RESEND:
                plan.resend.append(decision)
            elif decision.bucket is Bucket.VERIFY_REMOTE:
                plan.verify_remote.append(decision)
            else:
                plan.delivered_local.append(decision)
            if extra:
                plan.anomalies.append(extra)

        plan.resend.sort(key=lambda d: (d.request_id, d.attempt_id))
        plan.verify_remote.sort(key=lambda d: (d.request_id, d.attempt_id))
        plan.delivered_local.sort(key=lambda d: (d.request_id, d.attempt_id))
        return plan

    def _decide(
        self,
        record: RequestRecord,
        ambiguous_keys: Mapping[str, set],
        unverifiable_keys: Mapping[str, Set[str]],
    ) -> Tuple[Decision, Optional[str], Optional[str]]:
        method = (record.method or "UNKNOWN").upper()
        exposed = record.attempts_with_exposure()
        attempt = (
            max(exposed, key=lambda a: (a.order, a.attempt_id))
            if exposed
            else record.latest_attempt
        )
        body_hash = record.body_sha256
        key = record.idempotency_key
        isolation_group: Optional[str] = None
        anomaly: Optional[str] = None

        if key is not None and key in ambiguous_keys:
            isolation_group = _isolation_group(
                key, body_hash, None if body_hash else record.request_id
            )

        if record.identity_conflict:
            anomaly = (
                f"request {record.request_id}: conflicting request identity "
                "(more than one body hash or idempotency key observed)"
            )

        def make(
            bucket: Bucket,
            action: Action,
            reason: str,
            detail: str = "",
            attempt_id: Optional[str] = None,
        ) -> Decision:
            chosen_attempt = attempt if attempt_id is None else record.attempts[attempt_id]
            return Decision(
                bucket=bucket,
                request_id=record.request_id,
                method=method,
                attempt_id=chosen_attempt.attempt_id,
                action=action,
                reason=reason,
                progress=chosen_attempt.phase,
                url=record.url,
                body_sha256=body_hash,
                idempotency_key=key,
                isolation_group=isolation_group,
                detail=detail,
            )

        if record.any_delivered:
            return (
                make(
                    Bucket.DELIVERED_LOCAL,
                    Action.NONE,
                    "local_delivery_recorded",
                    "artifact or receipt is already persisted; do not resend",
                ),
                isolation_group,
                anomaly,
            )

        if (
            key is not None
            and key in ambiguous_keys
            and not record.any_exposure
        ):
            return (
                make(
                    Bucket.VERIFY_REMOTE,
                    Action.VERIFY_RECEIPT,
                    "ambiguous_idempotency_key",
                    "nothing was flushed for this request, but its key is "
                    "shared with other bodies; the queue must not decide "
                    "which job the key belongs to",
                ),
                isolation_group,
                anomaly,
            )

        if method == "UNKNOWN":
            if record.any_headers_flushed or record.any_partial_body:
                return (
                    make(
                        Bucket.VERIFY_REMOTE,
                        Action.VERIFY_RECEIPT,
                        "ambiguous_identity_with_exposure",
                        "method is unknown but bytes may have reached the origin",
                    ),
                    isolation_group,
                    anomaly
                    or f"request {record.request_id}: method was never persisted",
                )
            return (
                make(
                    Bucket.RESEND,
                    Action.RESEND,
                    "no_exposure",
                    "nothing was flushed; safe to queue again once method is known",
                ),
                isolation_group,
                anomaly or f"request {record.request_id}: method was never persisted",
            )

        if method in IDEMPOTENT_METHODS:
            return (
                make(
                    Bucket.RESEND,
                    Action.RESEND,
                    "idempotent_method",
                    "repeating this method cannot create a second effect",
                ),
                isolation_group,
                anomaly,
            )

        if not record.any_exposure:
            return (
                make(
                    Bucket.RESEND,
                    Action.RESEND,
                    "no_exposure",
                    "request line and headers were never flushed",
                ),
                isolation_group,
                anomaly,
            )

        if record.any_partial_body:
            base_detail = (
                "request headers were flushed and the body was only partially "
                "sent; the origin may already have processed the request"
            )
        elif record.any_body_complete:
            base_detail = (
                "the complete request was flushed; the origin may already have "
                "processed it"
            )
        else:
            base_detail = (
                "request headers were flushed without a complete body; the "
                "origin may already have acted on the metadata"
            )

        if attempt.response_status is not None:
            status = attempt.response_status
            if status in RETRYABLE_REJECTIONS:
                return (
                    make(
                        Bucket.RESEND,
                        Action.RESEND,
                        "origin_rejected_request",
                        f"origin returned {status}, which refuses the request "
                        "before any export job is created",
                    ),
                    isolation_group,
                    anomaly,
                )
            if status == CONFLICT_STATUS:
                return (
                    make(
                        Bucket.VERIFY_REMOTE,
                        Action.VERIFY_RECEIPT,
                        "origin_reports_conflict",
                        "origin returned 409, so a job with this identity may "
                        "already exist; reconcile the receipt first",
                    ),
                    isolation_group,
                    anomaly,
                )
            return (
                make(
                    Bucket.VERIFY_REMOTE,
                    Action.VERIFY_RECEIPT,
                    "response_seen_without_stored_receipt",
                    "the origin answered, so it acted; fetch the receipt instead "
                    "of creating another job",
                ),
                isolation_group,
                anomaly,
            )

        if key is not None and key in ambiguous_keys:
            return (
                make(
                    Bucket.VERIFY_REMOTE,
                    Action.VERIFY_RECEIPT,
                    "ambiguous_idempotency_key",
                    "the key is shared by different bodies, so its receipt "
                    "cannot be attributed; isolate and check the remote side",
                ),
                isolation_group,
                anomaly,
            )

        if key is not None and key in unverifiable_keys and not body_hash:
            return (
                make(
                    Bucket.VERIFY_REMOTE,
                    Action.VERIFY_RECEIPT,
                    "body_hash_not_persisted",
                    "this attempt carries an idempotency key but its body hash "
                    "was never persisted, so a resend cannot be proven to be "
                    "the same request",
                ),
                isolation_group,
                anomaly,
            )

        if self.rule.protects(method, key):
            return (
                make(
                    Bucket.RESEND,
                    Action.RESEND,
                    "explicit_idempotency_promise",
                    f"origin promises that {key!r} with the same body returns "
                    "the same receipt",
                ),
                isolation_group,
                anomaly,
            )

        return (
            make(
                Bucket.VERIFY_REMOTE,
                Action.VERIFY_RECEIPT,
                "exposure_without_idempotency_promise",
                base_detail,
            ),
            isolation_group,
            anomaly,
        )


def _isolation_group(
    key: str, body_hash: Optional[str], request_id: Optional[str] = None
) -> str:
    material = f"{key}\n{body_hash or ''}\n{request_id or ''}".encode("utf-8")
    return "iso:" + hashlib.sha256(material).hexdigest()[:16]


def reduce_events(
    events: Iterable[Mapping[str, Any] | Event],
    rule: Optional[IdempotencyRule] = None,
) -> RequeuePlan:
    """Reduce a journal and return the three requeue lists."""
    journal = Journal(rule=rule)
    journal.extend(events)
    return journal.plan()


def supplement_jobs(plan: RequeuePlan) -> List[SupplementJob]:
    """Jobs to actually enqueue, deduplicated by request and attempt.

    Because the plan is derived from a deduplicated journal, replaying the
    same events yields exactly the same job list.  Requeueing the plan twice
    must not enqueue a delivered or receipt-verified request again.
    """
    seen: Dict[Tuple[str, str], SupplementJob] = {}
    for decision in plan.resend:
        identity = (decision.request_id, decision.attempt_id)
        if identity in seen:
            continue
        seen[identity] = SupplementJob(
            request_id=decision.request_id,
            attempt_id=decision.attempt_id,
            method=decision.method,
            url=decision.url,
            idempotency_key=decision.idempotency_key,
        )
    return sorted(seen.values(), key=lambda job: (job.request_id, job.attempt_id))


def plan_jobs(
    events: Iterable[Mapping[str, Any] | Event],
    rule: Optional[IdempotencyRule] = None,
) -> Tuple[RequeuePlan, List[SupplementJob]]:
    plan = reduce_events(events, rule=rule)
    return plan, supplement_jobs(plan)
