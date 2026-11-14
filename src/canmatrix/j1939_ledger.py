# -*- coding: utf-8 -*-
"""Reconcile J1939 recorder batches into a per-device operating-day ledger.

Recorder fleets commonly summarize "device operating days" by J1939 *source
address*.  Source addresses are not stable device identities: a device that
powers down may hand its address to a completely different machine later the
same day, and a naive address-based ledger silently merges two machines into
one.

This module aligns three pieces of evidence:

* the source address found in each received frame,
* the J1939 *address claimed* declarations (PGN 0x00EE00) shipped with each
  recorder batch -- the 8 byte payload is the device NAME, the stable
  identity defined by J1939,
* the receive time window of each recorder (the span of time during which
  that recorder was actually listening).

All frames from all batches are merged into a single timeline ordered by
their timestamps -- never by file/batch order -- so late arriving batches
slot into the event order their timestamps support.

Claim semantics
---------------
A claim observed by recorder ``R`` at time ``t`` is evidence that the
claiming device held the source address from ``t`` until the earliest of

* the end of ``R``'s receive window (the recorder stopped listening),
* the next claim for the same address by a *different* device observed by
  ``R`` (a handover seen within one recorder's view),
* a *cannot claim / release* declaration (address-claimed PGN sent from the
  J1939 null address 0xFE) for that device observed by ``R``.

Because different recorders have different receive windows, claim windows of
two devices for the same address may overlap.  Such *address contention* is
adjudicated by a caller-provided priority rule (the default implements the
J1939 NAME arbitration order: the lower NAME wins the address).  When the
overlapping claims are insufficient to determine the owner of a measurement
-- i.e. the priority rule abstains -- the measurement is kept as *ambiguous*
together with the candidate devices and the claim evidence, instead of being
silently attributed.  Measurements after the handover, once only one claim
window covers them, are still attributed to the right device.
"""

import attr
import datetime
import json
import typing

from canmatrix.ArbitrationId import ArbitrationId

# PGN of the J1939 "Address Claimed" / "Cannot Claim Address" message.
ADDRESS_CLAIMED_PGN = 0x00EE00

# J1939 null source address: an address-claimed message sent from here is a
# "cannot claim address" declaration, i.e. the device releases the address.
NULL_ADDRESS = 0xFE

#: Attribution states.
ATTRIBUTED = "attributed"
AMBIGUOUS = "ambiguous"
UNATTRIBUTED = "unattributed"

#: How an attribution was decided.
RESOLVED_SINGLE_CLAIM = "single-claim"
RESOLVED_PRIORITY_RULE = "priority-rule"


def _parse_timestamp(value):
    # type: (typing.Union[str, datetime.datetime]) -> datetime.datetime
    if isinstance(value, datetime.datetime):
        return value
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(text)


def _parse_can_id(value):
    # type: (typing.Union[int, str]) -> int
    if isinstance(value, int):
        return value
    return int(value, 0)


def _parse_data(value):
    # type: (typing.Union[str, bytes, bytearray, typing.Sequence[int]]) -> bytes
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return bytes.fromhex(value.replace(" ", ""))
    return bytes(value)


def format_name(device):
    # type: (int) -> str
    """Render a 64 bit J1939 device NAME as a stable, readable string."""
    return "0x{0:016X}".format(device)


@attr.s
class J1939Frame(object):
    """A single received J1939 frame inside a recorder batch."""

    timestamp = attr.ib()  # type: datetime.datetime
    can_id = attr.ib()  # type: int
    data = attr.ib(factory=bytes)  # type: bytes

    @property
    def arbitration_id(self):
        # type: () -> ArbitrationId
        return ArbitrationId(id=self.can_id, extended=True)

    @property
    def pgn(self):
        # type: () -> int
        return self.arbitration_id.pgn

    @property
    def source_address(self):
        # type: () -> int
        return self.arbitration_id.j1939_source

    @property
    def is_address_claim(self):
        # type: () -> bool
        return self.pgn == ADDRESS_CLAIMED_PGN and len(self.data) >= 8

    @property
    def device_name(self):
        # type: () -> int
        """The 64 bit J1939 NAME carried by an address-claimed frame."""
        return int.from_bytes(self.data[:8], "little")


@attr.s
class RecorderBatch(object):
    """Frames recorded by one recorder during one receive window.

    ``window_start``/``window_end`` default to the first/last frame
    timestamp; pass them explicitly when the recorder was listening longer
    than the captured frames suggest.
    """

    recorder = attr.ib()  # type: str
    frames = attr.ib(factory=list)  # type: typing.List[J1939Frame]
    window_start = attr.ib(default=None)  # type: typing.Optional[datetime.datetime]
    window_end = attr.ib(default=None)  # type: typing.Optional[datetime.datetime]

    def __attrs_post_init__(self):
        # type: () -> None
        self.frames = sorted(self.frames, key=lambda frame: frame.timestamp)
        if self.frames:
            if self.window_start is None:
                self.window_start = self.frames[0].timestamp
            if self.window_end is None:
                self.window_end = self.frames[-1].timestamp


@attr.s
class Claim(object):
    """A device address declaration with its evidence window."""

    device = attr.ib()  # type: int
    source_address = attr.ib()  # type: int
    timestamp = attr.ib()  # type: datetime.datetime
    recorder = attr.ib()  # type: str
    batch_index = attr.ib()  # type: int
    window_end = attr.ib(default=None)  # type: typing.Optional[datetime.datetime]
    # The recorder's own receive window end is inclusive (the recorder was
    # still listening at that instant); a handover or release truncates the
    # window exclusively.
    window_end_inclusive = attr.ib(default=False)  # type: bool

    def covers(self, timestamp):
        # type: (datetime.datetime) -> bool
        if timestamp < self.timestamp:
            return False
        if self.window_end is None:
            return True
        if self.window_end_inclusive:
            return timestamp <= self.window_end
        return timestamp < self.window_end

    def to_dict(self):
        # type: () -> dict
        return {
            "device": format_name(self.device),
            "source_address": "0x{0:02X}".format(self.source_address),
            "claimed_at": self.timestamp.isoformat(),
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "recorder": self.recorder,
        }


@attr.s
class Measurement(object):
    """A non-claim frame whose producing device must be determined."""

    timestamp = attr.ib()  # type: datetime.datetime
    source_address = attr.ib()  # type: int
    pgn = attr.ib()  # type: int
    data = attr.ib()  # type: bytes
    recorder = attr.ib()  # type: str
    batch_index = attr.ib()  # type: int

    def to_dict(self):
        # type: () -> dict
        return {
            "timestamp": self.timestamp.isoformat(),
            "source_address": "0x{0:02X}".format(self.source_address),
            "pgn": "0x{0:05X}".format(self.pgn),
            "recorder": self.recorder,
            "data": self.data.hex(),
        }


@attr.s
class Attribution(object):
    """The attribution decision for one measurement."""

    measurement = attr.ib()  # type: Measurement
    status = attr.ib()  # type: str
    device = attr.ib(default=None)  # type: typing.Optional[int]
    resolved_by = attr.ib(default=None)  # type: typing.Optional[str]
    candidates = attr.ib(factory=list)  # type: typing.List[int]
    evidence = attr.ib(factory=list)  # type: typing.List[Claim]

    def to_dict(self):
        # type: () -> dict
        result = self.measurement.to_dict()
        result["status"] = self.status
        if self.device is not None:
            result["device"] = format_name(self.device)
        if self.resolved_by is not None:
            result["resolved_by"] = self.resolved_by
        if self.candidates:
            result["candidates"] = [format_name(device) for device in self.candidates]
        if self.evidence:
            result["evidence"] = [claim.to_dict() for claim in self.evidence]
        return result


@attr.s
class Ledger(object):
    """The reconciled per-device operating-day ledger."""

    attributions = attr.ib(factory=list)  # type: typing.List[Attribution]

    @property
    def attributed(self):
        # type: () -> typing.List[Attribution]
        return [a for a in self.attributions if a.status == ATTRIBUTED]

    @property
    def ambiguous(self):
        # type: () -> typing.List[Attribution]
        return [a for a in self.attributions if a.status == AMBIGUOUS]

    @property
    def unattributed(self):
        # type: () -> typing.List[Attribution]
        return [a for a in self.attributions if a.status == UNATTRIBUTED]

    @property
    def days_by_device(self):
        # type: () -> typing.Dict[int, typing.Set[datetime.date]]
        days = {}  # type: typing.Dict[int, typing.Set[datetime.date]]
        for attribution in self.attributed:
            days.setdefault(attribution.device, set()).add(
                attribution.measurement.timestamp.date()
            )
        return days

    def operating_days(self, device):
        # type: (int) -> typing.List[datetime.date]
        return sorted(self.days_by_device.get(device, set()))

    def to_dict(self):
        # type: () -> dict
        devices = {}
        for device, days in sorted(self.days_by_device.items()):
            devices[format_name(device)] = {
                "operating_days": [day.isoformat() for day in sorted(days)],
                "operating_day_count": len(days),
            }
        return {
            "devices": devices,
            "ambiguous": [a.to_dict() for a in self.ambiguous],
            "unattributed": [a.to_dict() for a in self.unattributed],
        }


def j1939_name_priority(claims):
    # type: (typing.Sequence[Claim]) -> typing.Optional[int]
    """Default contention rule: J1939 address arbitration order.

    The device with the numerically lower NAME wins the address.  Returns
    ``None`` (abstains) when the rule cannot decide, e.g. when no claims are
    given.
    """
    devices = {claim.device for claim in claims}
    if not devices:
        return None
    return min(devices)


def _classify_events(batches):
    # type: (typing.Sequence[RecorderBatch]) -> typing.Tuple[typing.List[Claim], typing.List[typing.Tuple], typing.List[Measurement]]
    """Split all frames into claims, releases and measurements.

    Returns claims (without windows yet), release events as
    ``(timestamp, seq, device, recorder)`` tuples and measurements.  Every
    item carries a global sequence number so the merge order never depends
    on the order batches were supplied in.
    """
    claims = []  # type: typing.List[Claim]
    releases = []  # type: typing.List[typing.Tuple]
    measurements = []  # type: typing.List[Measurement]
    seq = 0
    for batch_index, batch in enumerate(batches):
        for frame in batch.frames:
            if frame.is_address_claim:
                if frame.source_address == NULL_ADDRESS:
                    releases.append(
                        (frame.timestamp, seq, frame.device_name, batch.recorder)
                    )
                else:
                    claims.append(
                        Claim(
                            device=frame.device_name,
                            source_address=frame.source_address,
                            timestamp=frame.timestamp,
                            recorder=batch.recorder,
                            batch_index=batch_index,
                        )
                    )
            else:
                measurements.append(
                    Measurement(
                        timestamp=frame.timestamp,
                        source_address=frame.source_address,
                        pgn=frame.pgn,
                        data=frame.data,
                        recorder=batch.recorder,
                        batch_index=batch_index,
                    )
                )
            seq += 1
    return claims, releases, measurements


def _compute_claim_windows(claims, releases, batches):
    # type: (typing.List[Claim], typing.List[typing.Tuple], typing.Sequence[RecorderBatch]) -> None
    """Bound every claim's evidence window (in place).

    Within one recorder's view a claim holds until the recorder's receive
    window ends, a different device claims the same address, or the device
    itself releases the address -- whichever comes first.
    """
    window_end_by_recorder = {batch.recorder: batch.window_end for batch in batches}
    claims_by_recorder = {}  # type: typing.Dict[str, typing.List[Claim]]
    for claim in claims:
        claims_by_recorder.setdefault(claim.recorder, []).append(claim)
    releases_by_recorder = {}  # type: typing.Dict[str, typing.List[typing.Tuple]]
    for release in releases:
        releases_by_recorder.setdefault(release[3], []).append(release)

    for recorder, recorder_claims in claims_by_recorder.items():
        recorder_claims.sort(key=lambda claim: claim.timestamp)
        recorder_releases = sorted(
            releases_by_recorder.get(recorder, []), key=lambda release: release[0]
        )
        recorder_window_end = window_end_by_recorder.get(recorder)
        for claim in recorder_claims:
            ends = []  # type: typing.List[typing.Tuple[datetime.datetime, bool]]
            if recorder_window_end is not None:
                ends.append((recorder_window_end, True))
            for other in recorder_claims:
                if (
                    other.source_address == claim.source_address
                    and other.device != claim.device
                    and claim.timestamp <= other.timestamp
                    and other is not claim
                ):
                    ends.append((other.timestamp, False))
            for release_time, _seq, device, _recorder in recorder_releases:
                if device == claim.device and release_time >= claim.timestamp:
                    ends.append((release_time, False))
            if ends:
                # Earliest bound wins; an exclusive bound beats an inclusive
                # one at the same instant.
                claim.window_end, claim.window_end_inclusive = min(
                    ends, key=lambda end: (end[0], not end[1])
                )


def reconcile(batches, priority_rule=None):
    # type: (typing.Sequence[RecorderBatch], typing.Optional[typing.Callable[[typing.Sequence[Claim]], typing.Optional[int]]]) -> Ledger
    """Merge recorder batches and attribute every measurement to a device.

    :param batches: recorder batches in any order; the merge follows the
        frame timestamps (the supportable event order), never file order.
    :param priority_rule: callable receiving the competing claims (one per
        candidate device) and returning the winning device NAME, or ``None``
        when the overlapping claims are insufficient to decide.  Defaults to
        :func:`j1939_name_priority`.
    :rtype: Ledger
    """
    if priority_rule is None:
        priority_rule = j1939_name_priority

    claims, releases, measurements = _classify_events(batches)
    _compute_claim_windows(claims, releases, batches)

    claims_by_address = {}  # type: typing.Dict[int, typing.List[Claim]]
    for claim in claims:
        claims_by_address.setdefault(claim.source_address, []).append(claim)

    ledger = Ledger()
    for measurement in sorted(measurements, key=lambda m: m.timestamp):
        covering = [
            claim
            for claim in claims_by_address.get(measurement.source_address, [])
            if claim.covers(measurement.timestamp)
        ]
        # Latest covering claim per candidate device.
        covering_by_device = {}  # type: typing.Dict[int, Claim]
        for claim in covering:
            if (
                claim.device not in covering_by_device
                or covering_by_device[claim.device].timestamp < claim.timestamp
            ):
                covering_by_device[claim.device] = claim

        if not covering_by_device:
            ledger.attributions.append(
                Attribution(measurement=measurement, status=UNATTRIBUTED)
            )
        elif len(covering_by_device) == 1:
            device, claim = next(iter(covering_by_device.items()))
            ledger.attributions.append(
                Attribution(
                    measurement=measurement,
                    status=ATTRIBUTED,
                    device=device,
                    resolved_by=RESOLVED_SINGLE_CLAIM,
                    evidence=[claim],
                )
            )
        else:
            competing = sorted(
                covering_by_device.values(), key=lambda claim: claim.timestamp
            )
            winner = priority_rule(competing)
            if winner is not None and winner in covering_by_device:
                ledger.attributions.append(
                    Attribution(
                        measurement=measurement,
                        status=ATTRIBUTED,
                        device=winner,
                        resolved_by=RESOLVED_PRIORITY_RULE,
                        candidates=sorted(covering_by_device),
                        evidence=competing,
                    )
                )
            else:
                ledger.attributions.append(
                    Attribution(
                        measurement=measurement,
                        status=AMBIGUOUS,
                        candidates=sorted(covering_by_device),
                        evidence=competing,
                    )
                )
    return ledger


def load_batch(document):
    # type: (dict) -> RecorderBatch
    """Build a :class:`RecorderBatch` from its JSON-able mapping."""
    window = document.get("window") or {}
    frames = [
        J1939Frame(
            timestamp=_parse_timestamp(frame["timestamp"]),
            can_id=_parse_can_id(frame["can_id"]),
            data=_parse_data(frame.get("data", b"")),
        )
        for frame in document.get("frames", [])
    ]
    return RecorderBatch(
        recorder=document.get("recorder", ""),
        frames=frames,
        window_start=(
            _parse_timestamp(window["start"]) if window.get("start") else None
        ),
        window_end=_parse_timestamp(window["end"]) if window.get("end") else None,
    )


def load_batch_file(path):
    # type: (str) -> RecorderBatch
    with open(path, "r", encoding="utf-8") as handle:
        return load_batch(json.load(handle))


def reconcile_files(paths, priority_rule=None):
    # type: (typing.Sequence[str], typing.Optional[typing.Callable[[typing.Sequence[Claim]], typing.Optional[int]]]) -> Ledger
    """Reconcile JSON batch files into a ledger."""
    return reconcile([load_batch_file(path) for path in paths], priority_rule)
