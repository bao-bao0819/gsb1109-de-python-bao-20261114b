# -*- coding: utf-8 -*-
import datetime
import json

import pytest
from click.testing import CliRunner

import canmatrix.j1939_ledger as j1939_ledger
from canmatrix.cli.j1939_ledger import cli_j1939_ledger

SA = 0x21
DEVICE_A = 0x0000000000000200
DEVICE_B = 0x0000000000000100  # lower NAME wins J1939 address arbitration

DAY = datetime.date(2026, 11, 14)


def ts(hour, minute=0, day=14):
    return datetime.datetime(2026, 11, day, hour, minute)


def claim_frame(device, source_address=SA, timestamp=None):
    can_id = (6 << 26) | (0xEE << 16) | (0xFF << 8) | source_address
    return j1939_ledger.J1939Frame(
        timestamp=timestamp,
        can_id=can_id,
        data=device.to_bytes(8, "little"),
    )


def release_frame(device, timestamp=None):
    can_id = (6 << 26) | (0xEE << 16) | (0xFF << 8) | j1939_ledger.NULL_ADDRESS
    return j1939_ledger.J1939Frame(
        timestamp=timestamp,
        can_id=can_id,
        data=device.to_bytes(8, "little"),
    )


def measurement_frame(source_address=SA, timestamp=None, pgn=0xFEE5):
    can_id = (3 << 26) | (pgn << 8) | source_address
    return j1939_ledger.J1939Frame(
        timestamp=timestamp, can_id=can_id, data=b"\x01" * 8
    )


def make_batch(recorder, frames, start, end):
    return j1939_ledger.RecorderBatch(
        recorder=recorder,
        frames=frames,
        window_start=start,
        window_end=end,
    )


def handover_batches():
    """Machine A works the morning shift, machine B takes over the same
    source address in the afternoon; two recorders with disjoint windows."""
    morning = make_batch(
        "rec-morning",
        [
            claim_frame(DEVICE_A, timestamp=ts(8)),
            measurement_frame(timestamp=ts(9)),
            measurement_frame(timestamp=ts(11)),
        ],
        ts(7),
        ts(12),
    )
    afternoon = make_batch(
        "rec-afternoon",
        [
            claim_frame(DEVICE_B, timestamp=ts(14)),
            measurement_frame(timestamp=ts(15)),
            measurement_frame(timestamp=ts(17)),
        ],
        ts(13),
        ts(19),
    )
    return morning, afternoon


def test_handover_same_address_two_machines_not_merged():
    morning, afternoon = handover_batches()
    ledger = j1939_ledger.reconcile([morning, afternoon])

    assert ledger.ambiguous == []
    assert ledger.unattributed == []
    assert ledger.operating_days(DEVICE_A) == [DAY]
    assert ledger.operating_days(DEVICE_B) == [DAY]

    by_time = {a.measurement.timestamp: a for a in ledger.attributed}
    assert by_time[ts(9)].device == DEVICE_A
    assert by_time[ts(11)].device == DEVICE_A
    assert by_time[ts(15)].device == DEVICE_B
    assert by_time[ts(17)].device == DEVICE_B
    assert all(a.resolved_by == j1939_ledger.RESOLVED_SINGLE_CLAIM
               for a in ledger.attributed)


def test_late_batches_follow_event_order_not_file_order():
    morning, afternoon = handover_batches()
    expected = j1939_ledger.reconcile([morning, afternoon])

    # The morning batch arrives late (after the afternoon batch) and the
    # frames inside a batch are not stored in timestamp order either.
    shuffled_morning = make_batch(
        "rec-morning", list(reversed(morning.frames)), ts(7), ts(12)
    )
    ledger = j1939_ledger.reconcile([afternoon, shuffled_morning])

    assert [a.to_dict() for a in ledger.attributions] == [
        a.to_dict() for a in expected.attributions
    ]
    assert ledger.days_by_device == expected.days_by_device


def contended_batches():
    """Both recorders listen in the afternoon; A's claim window overlaps
    B's claim window for the same source address."""
    rec_1 = make_batch(
        "rec-1",
        [claim_frame(DEVICE_A, timestamp=ts(8))],
        ts(7),
        ts(16),
    )
    rec_2 = make_batch(
        "rec-2",
        [
            claim_frame(DEVICE_B, timestamp=ts(14)),
            measurement_frame(timestamp=ts(15)),
            measurement_frame(timestamp=ts(16, 30)),
        ],
        ts(13),
        ts(19),
    )
    return rec_1, rec_2


def test_contention_resolved_by_default_j1939_priority_rule():
    ledger = j1939_ledger.reconcile(contended_batches())

    by_time = {a.measurement.timestamp: a for a in ledger.attributions}
    # 15:00 is covered by both claims; the lower NAME (device B) wins.
    contended = by_time[ts(15)]
    assert contended.status == j1939_ledger.ATTRIBUTED
    assert contended.device == DEVICE_B
    assert contended.resolved_by == j1939_ledger.RESOLVED_PRIORITY_RULE
    assert contended.candidates == sorted([DEVICE_A, DEVICE_B])
    assert len(contended.evidence) == 2


def abstain(claims):
    return None


def test_overlap_without_decisive_rule_keeps_candidates_and_evidence():
    ledger = j1939_ledger.reconcile(contended_batches(), priority_rule=abstain)

    ambiguous = {a.measurement.timestamp: a for a in ledger.ambiguous}
    assert set(ambiguous) == {ts(15)}
    entry = ambiguous[ts(15)]
    assert entry.device is None
    assert entry.candidates == sorted([DEVICE_A, DEVICE_B])
    assert {claim.device for claim in entry.evidence} == {DEVICE_A, DEVICE_B}
    # The ambiguous measurement must not leak into either device's tally.
    assert ledger.operating_days(DEVICE_A) == []
    assert ledger.operating_days(DEVICE_B) == [DAY]


def test_measurements_after_handover_still_attributed():
    ledger = j1939_ledger.reconcile(contended_batches(), priority_rule=abstain)

    by_time = {a.measurement.timestamp: a for a in ledger.attributed}
    # 16:30 is past the end of rec-1's receive window: only B's claim
    # still covers it, so the measurement is attributed even though the
    # earlier overlap was undecidable.
    assert by_time[ts(16, 30)].device == DEVICE_B
    assert by_time[ts(16, 30)].resolved_by == j1939_ledger.RESOLVED_SINGLE_CLAIM


def test_unattributed_when_no_claim_covers():
    batch = make_batch(
        "rec-1",
        [
            measurement_frame(timestamp=ts(6)),  # before the claim
            claim_frame(DEVICE_A, timestamp=ts(8)),
            measurement_frame(timestamp=ts(9)),
        ],
        ts(5),
        ts(10),
    )
    late_batch = make_batch(
        "rec-2",
        [measurement_frame(timestamp=ts(12))],  # past rec-1's window
        ts(11),
        ts(13),
    )
    ledger = j1939_ledger.reconcile([batch, late_batch])

    assert [a.measurement.timestamp for a in ledger.unattributed] == [ts(6), ts(12)]
    assert ledger.operating_days(DEVICE_A) == [DAY]


def test_cannot_claim_release_ends_window():
    batch = make_batch(
        "rec-1",
        [
            claim_frame(DEVICE_A, timestamp=ts(8)),
            measurement_frame(timestamp=ts(11)),
            release_frame(DEVICE_A, timestamp=ts(12)),
            measurement_frame(timestamp=ts(13)),
        ],
        ts(7),
        ts(18),
    )
    ledger = j1939_ledger.reconcile([batch])

    assert [a.measurement.timestamp for a in ledger.attributed] == [ts(11)]
    assert [a.measurement.timestamp for a in ledger.unattributed] == [ts(13)]


def test_same_device_reclaim_keeps_single_candidate():
    batch = make_batch(
        "rec-1",
        [
            claim_frame(DEVICE_A, timestamp=ts(8)),
            claim_frame(DEVICE_A, timestamp=ts(10)),
            measurement_frame(timestamp=ts(11)),
        ],
        ts(7),
        ts(18),
    )
    ledger = j1939_ledger.reconcile([batch])

    assert ledger.ambiguous == []
    assert ledger.attributed[0].device == DEVICE_A
    assert ledger.attributed[0].resolved_by == j1939_ledger.RESOLVED_SINGLE_CLAIM


def test_operating_days_span_multiple_calendar_days():
    batch = make_batch(
        "rec-1",
        [
            claim_frame(DEVICE_A, timestamp=ts(8)),
            measurement_frame(timestamp=ts(9)),
            measurement_frame(timestamp=ts(9, day=15)),
            measurement_frame(timestamp=ts(10, day=15)),
        ],
        ts(7),
        ts(18, day=15),
    )
    ledger = j1939_ledger.reconcile([batch])

    assert ledger.operating_days(DEVICE_A) == [
        datetime.date(2026, 11, 14),
        datetime.date(2026, 11, 15),
    ]
    report = ledger.to_dict()
    name = j1939_ledger.format_name(DEVICE_A)
    assert report["devices"][name]["operating_day_count"] == 2


def test_window_inferred_from_frames_when_not_given():
    batch = j1939_ledger.RecorderBatch(
        recorder="rec-1",
        frames=[
            claim_frame(DEVICE_A, timestamp=ts(8)),
            measurement_frame(timestamp=ts(9)),
        ],
    )
    assert batch.window_start == ts(8)
    assert batch.window_end == ts(9)
    ledger = j1939_ledger.reconcile([batch])
    assert ledger.operating_days(DEVICE_A) == [DAY]


def test_load_batch_from_json_document():
    document = {
        "recorder": "rec-1",
        "window": {"start": "2026-11-14T07:00:00", "end": "2026-11-14T12:00:00"},
        "frames": [
            {
                "timestamp": "2026-11-14T08:00:00",
                "can_id": "0x18EEFF21",
                "data": DEVICE_A.to_bytes(8, "little").hex(),
            },
            {
                "timestamp": "2026-11-14T09:00:00",
                "can_id": 0x0CFEE521,
                "data": "0102030405060708",
            },
        ],
    }
    batch = j1939_ledger.load_batch(document)
    assert batch.recorder == "rec-1"
    assert batch.window_end == ts(12)
    ledger = j1939_ledger.reconcile([batch])
    assert ledger.operating_days(DEVICE_A) == [DAY]


def test_cli_reconciles_batch_files(tmp_path):
    morning, afternoon = handover_batches()
    paths = []
    for index, batch in enumerate([afternoon, morning]):  # late file first
        document = {
            "recorder": batch.recorder,
            "window": {
                "start": batch.window_start.isoformat(),
                "end": batch.window_end.isoformat(),
            },
            "frames": [
                {
                    "timestamp": frame.timestamp.isoformat(),
                    "can_id": hex(frame.can_id),
                    "data": frame.data.hex(),
                }
                for frame in batch.frames
            ],
        }
        path = tmp_path / "batch{0}.json".format(index)
        path.write_text(json.dumps(document), encoding="utf-8")
        paths.append(str(path))

    result = CliRunner().invoke(cli_j1939_ledger, paths)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ambiguous"] == []
    assert report["unattributed"] == []
    assert set(report["devices"]) == {
        j1939_ledger.format_name(DEVICE_A),
        j1939_ledger.format_name(DEVICE_B),
    }
    for device in report["devices"].values():
        assert device["operating_days"] == [DAY.isoformat()]
        assert device["operating_day_count"] == 1
