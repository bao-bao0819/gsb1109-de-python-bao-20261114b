"""Core data model for requeue decisions.

The model is intentionally transport agnostic: callers persist an append-only
journal of attempt events and describe whatever idempotency behaviour the
origin has *explicitly documented*.  Nothing here assumes an undocumented
server guarantee.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Set


SAFE_METHODS: FrozenSet[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

DIFFERENT_BODY_POLICIES = frozenset({"isolate", "reject", "unspecified"})


class Phase(str, Enum):
    """Persisted send progress for one attempt."""

    NOT_SENT = "not_sent"
    HEADERS_SENT = "headers_sent"
    BODY_PARTIAL = "body_partial"
    REQUEST_SENT = "request_sent"
    RESPONSE_RECEIVED = "response_received"
    RECEIPT_STORED = "receipt_stored"
    LOCAL_DELIVERY = "local_delivery"


class Bucket(str, Enum):
    """The three requested output lists."""

    RESEND = "resend"
    VERIFY_REMOTE = "verify_remote"
    DELIVERED_LOCAL = "delivered_local"


class Action(str, Enum):
    RESEND = "resend"
    VERIFY_RECEIPT = "verify_receipt"
    NONE = "none"


@dataclass(frozen=True)
class Event:
    """One append-only journal record.

    ``event_id`` is the at-least-once delivery identity.  Replays carrying the
    same id are ignored by the reducer; the same id with different payload is
    treated as an anomaly instead of being silently applied.
    """

    event_id: str
    kind: str
    request_id: str
    attempt_id: str = "-"
    data: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = {
            "event_id": self.event_id,
            "kind": self.kind,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "data": dict(self.data),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Event":
        data = raw.get("data")
        if data is None:
            data = raw.get("payload")
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ValueError("event data must be a mapping")
        return cls(
            event_id=str(raw["event_id"]),
            kind=str(raw["kind"]),
            request_id=str(raw["request_id"]),
            attempt_id=str(raw.get("attempt_id", "-")),
            data=dict(data),
        )


@dataclass(frozen=True)
class IdempotencyRule:
    """The origin's *explicitly promised* idempotency-key contract.

    The defaults deliberately describe no promise at all: ``explicit=False``
    means the planner must not treat a key as replay protection.
    """

    header_name: str = "Idempotency-Key"
    methods: FrozenSet[str] = frozenset({"POST"})
    same_key_same_body_returns_same_receipt: bool = False
    different_body_same_key: str = "isolate"
    explicit: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        if self.different_body_same_key not in DIFFERENT_BODY_POLICIES:
            raise ValueError(
                "different_body_same_key must be one of "
                f"{sorted(DIFFERENT_BODY_POLICIES)}"
            )

    def applies_to(self, method: str) -> bool:
        return method.upper() in {m.upper() for m in self.methods}

    def protects(self, method: str, key: Optional[str]) -> bool:
        """True only when an explicit promise covers this method and key."""
        return bool(
            self.explicit
            and self.same_key_same_body_returns_same_receipt
            and key
            and self.applies_to(method)
        )


@dataclass
class AttemptProgress:
    """Cumulative persisted progress for a single attempt."""

    attempt_id: str
    headers_flushed: bool = False
    body_bytes_flushed: int = 0
    body_bytes_total: Optional[int] = None
    body_declared_complete: bool = False
    response_status: Optional[int] = None
    response_body_stored: bool = False
    receipt_persisted: bool = False
    receipt_id: Optional[str] = None
    local_delivery_ref: Optional[str] = None
    order: int = 0

    @property
    def body_complete(self) -> bool:
        if self.body_bytes_total is not None:
            return self.headers_flushed and self.body_bytes_flushed >= self.body_bytes_total
        return bool(self.headers_flushed and self.body_declared_complete)

    @property
    def has_exposure(self) -> bool:
        """Any byte handed to the transport means the server may have acted."""
        return bool(self.headers_flushed or self.body_bytes_flushed > 0)

    @property
    def body_partial(self) -> bool:
        if not self.headers_flushed or self.body_complete:
            return False
        if self.body_bytes_total is not None:
            return 0 < self.body_bytes_flushed < self.body_bytes_total
        return self.body_bytes_flushed > 0

    @property
    def delivered(self) -> bool:
        if self.local_delivery_ref is not None:
            return True
        if self.receipt_persisted or self.receipt_id is not None:
            return True
        return bool(
            self.response_body_stored
            and self.response_status is not None
            and 200 <= self.response_status < 400
        )

    @property
    def phase(self) -> Phase:
        if self.local_delivery_ref is not None:
            return Phase.LOCAL_DELIVERY
        if self.receipt_persisted or self.receipt_id is not None:
            return Phase.RECEIPT_STORED
        if self.response_status is not None or self.response_body_stored:
            return Phase.RESPONSE_RECEIVED
        if self.body_complete:
            return Phase.REQUEST_SENT
        if self.headers_flushed and self.body_bytes_flushed > 0:
            return Phase.BODY_PARTIAL
        if self.headers_flushed:
            return Phase.HEADERS_SENT
        return Phase.NOT_SENT

    def merged_with(self, other: "AttemptProgress") -> "AttemptProgress":
        """Combine progress observed for the same attempt id."""
        total = self.body_bytes_total
        if total is None:
            total = other.body_bytes_total
        status = self.response_status
        if status is None:
            status = other.response_status
        receipt_id = self.receipt_id or other.receipt_id
        delivery_ref = self.local_delivery_ref or other.local_delivery_ref
        return replace(
            self,
            headers_flushed=self.headers_flushed or other.headers_flushed,
            body_bytes_flushed=max(self.body_bytes_flushed, other.body_bytes_flushed),
            body_bytes_total=total,
            body_declared_complete=(
                self.body_declared_complete or other.body_declared_complete
            ),
            response_status=status,
            response_body_stored=(
                self.response_body_stored or other.response_body_stored
            ),
            receipt_persisted=self.receipt_persisted or other.receipt_persisted,
            receipt_id=receipt_id,
            local_delivery_ref=delivery_ref,
            order=max(self.order, other.order),
        )


@dataclass
class RequestRecord:
    """Logical request identity plus every attempt observed for it."""

    request_id: str
    method: str = ""
    url: str = ""
    body_sha256: str = ""
    idempotency_key: Optional[str] = None
    replay_of: Optional[str] = None
    attempts: Dict[str, AttemptProgress] = field(default_factory=dict)
    observed_body_hashes: Set[str] = field(default_factory=set)
    observed_keys: Set[str] = field(default_factory=set)
    order: int = 0
    anomalies: List[str] = field(default_factory=list)

    def attempt(self, attempt_id: str, order: int = 0) -> AttemptProgress:
        existing = self.attempts.get(attempt_id)
        if existing is None:
            existing = AttemptProgress(attempt_id=attempt_id, order=order)
            self.attempts[attempt_id] = existing
        return existing

    @property
    def latest_attempt(self) -> AttemptProgress:
        if not self.attempts:
            return AttemptProgress(attempt_id="-")
        return max(self.attempts.values(), key=lambda a: (a.order, a.attempt_id))

    def attempts_with_exposure(self) -> List[AttemptProgress]:
        return [a for a in self.attempts.values() if a.has_exposure]

    @property
    def any_delivered(self) -> bool:
        return any(a.delivered for a in self.attempts.values())

    @property
    def any_partial_body(self) -> bool:
        return any(a.body_partial for a in self.attempts.values())

    @property
    def any_body_complete(self) -> bool:
        return any(a.body_complete for a in self.attempts.values())

    @property
    def any_headers_flushed(self) -> bool:
        return any(a.headers_flushed for a in self.attempts.values())

    @property
    def any_exposure(self) -> bool:
        return any(a.has_exposure for a in self.attempts.values())

    @property
    def identity_conflict(self) -> bool:
        return len(self.observed_body_hashes) > 1 or len(self.observed_keys) > 1


@dataclass(frozen=True)
class Decision:
    """One request's queue disposition."""

    bucket: Bucket
    request_id: str
    method: str
    attempt_id: str
    action: Action
    reason: str
    progress: Phase
    url: str = ""
    body_sha256: str = ""
    idempotency_key: Optional[str] = None
    isolation_group: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bucket": self.bucket.value,
            "request_id": self.request_id,
            "method": self.method,
            "attempt_id": self.attempt_id,
            "action": self.action.value,
            "reason": self.reason,
            "progress": self.progress.value,
            "url": self.url,
            "body_sha256": self.body_sha256,
            "idempotency_key": self.idempotency_key,
            "isolation_group": self.isolation_group,
            "detail": self.detail,
        }


@dataclass
class RequeuePlan:
    """The three requested lists plus provenance needed to audit them."""

    resend: List[Decision] = field(default_factory=list)
    verify_remote: List[Decision] = field(default_factory=list)
    delivered_local: List[Decision] = field(default_factory=list)
    isolated_keys: Dict[str, List[str]] = field(default_factory=dict)
    ignored_event_ids: List[str] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)

    def all_decisions(self) -> List[Decision]:
        return list(self.resend) + list(self.verify_remote) + list(self.delivered_local)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resend": [d.to_dict() for d in self.resend],
            "verify_remote": [d.to_dict() for d in self.verify_remote],
            "delivered_local": [d.to_dict() for d in self.delivered_local],
            "isolated_keys": {
                key: list(request_ids)
                for key, request_ids in sorted(self.isolated_keys.items())
            },
            "ignored_event_ids": list(self.ignored_event_ids),
            "anomalies": list(self.anomalies),
        }


@dataclass(frozen=True)
class SupplementJob:
    """A concrete requeue job that may be handed to the queue."""

    request_id: str
    attempt_id: str
    method: str
    url: str
    idempotency_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "method": self.method,
            "url": self.url,
            "idempotency_key": self.idempotency_key,
        }
