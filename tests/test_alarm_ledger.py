import pytest

from alarm_ledger import (
    Band,
    Reading,
    _classify,
    reading_from_signal,
    reconstruct_ledger,
)

UPPER = 90.0
RESET = 85.0
DWELL = 3.0


def high(t, hold=1.0, value=91.0, precision=1.0):
    return Reading(t, value, precision=precision, hold=hold)


def low(t, hold=1.0, value=80.0, precision=1.0):
    return Reading(t, value, precision=precision, hold=hold)


def band(t, hold=1.0, value=87.0, precision=1.0):
    return Reading(t, value, precision=precision, hold=hold)


def test_classify_respects_precision():
    assert _classify(Reading(0, 91.0, precision=1.0), UPPER, RESET) is Band.HIGH
    # 89.6 +/- 0.5 reaches 90.1 but cannot be proven over the threshold
    assert _classify(Reading(0, 89.6, precision=1.0), UPPER, RESET) is Band.BAND
    assert _classify(Reading(0, 84.0, precision=1.0), UPPER, RESET) is Band.LOW
    # 85.4 +/- 0.5 = 85.9 is not provably below the reset threshold
    assert _classify(Reading(0, 85.4, precision=1.0), UPPER, RESET) is Band.BAND


def test_definite_open_after_full_dwell():
    ledger = reconstruct_ledger(
        [low(0), high(1), high(2), high(3)], UPPER, RESET, DWELL
    )
    assert len(ledger.episodes) == 1
    assert ledger.episodes[0].definite_open == pytest.approx(4.0)


def test_short_hold_cannot_be_stretched_to_dwell():
    # A single high reading with 0.5 s hold must not be extended to 3 s.
    ledger = reconstruct_ledger([low(0), high(1, hold=0.5)], UPPER, RESET, DWELL)
    assert ledger.episodes == []


def test_gap_breaks_dwell_accumulation():
    # Coverage [1, 2.5] and [4, 6]: the gap in between may hide anything,
    # so neither chain reaches 3 s on its own.
    readings = [low(0), high(1, hold=1.5), high(4, hold=1.0), high(5, hold=1.0)]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    assert ledger.episodes == []


def test_band_reading_does_not_accumulate_towards_open():
    # High coverage [1, 3.5] (2.5 s), then an in-band reading, then more
    # high coverage: the in-band value must not complete the 3 s.
    readings = [
        low(0),
        high(1), high(2), high(3, hold=0.5),
        band(3.5, hold=0.4, value=88.0),
        high(4), high(5), high(6),
    ]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    assert len(ledger.episodes) == 1
    episode = ledger.episodes[0]
    assert episode.definite_open == pytest.approx(7.0)
    # Optimistically the accumulation restarts at 3.9 (end of the proven
    # not-high band coverage), so the alarm may have opened at 6.9 s.
    assert episode.possible_start == pytest.approx(6.9)


def test_reset_evidence_and_window():
    readings = [low(0), high(1), high(2), high(3), band(9), low(12, value=84.0)]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    (episode,) = ledger.episodes
    assert episode.settled
    assert episode.end_evidence == pytest.approx(12.0)
    # Last coverage that may still be at/above reset ends at 10 (band hold).
    assert episode.reset_window == pytest.approx((10.0, 12.0))


def test_band_maintains_only_after_actual_trigger():
    readings = [low(0), high(1), high(2), high(3), band(9), low(12, value=84.0)]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    roles = {item.reading.time: item.role for item in ledger.readings}
    # The in-band reading counts towards the open entry only because the
    # alarm actually triggered at 4.0 s before it.
    assert roles[9.0] == "maintain"
    assert ledger.readings[4].episode == 0


def test_band_before_trigger_is_neutral():
    readings = [
        low(0),
        band(1, value=88.0),
        high(2), high(3), high(4),
        low(8, value=84.0),
    ]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    roles = {item.reading.time: item.role for item in ledger.readings}
    # The very same in-band value means nothing before a proven trigger.
    assert roles[1.0] == "neutral"
    assert ledger.episodes[0].definite_open == pytest.approx(5.0)


def test_new_entry_only_after_evidenced_reset():
    # No reset evidence between the two high chains: one single entry.
    readings = [low(0), high(1), high(2), high(3), high(10), high(11), high(12)]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    assert len(ledger.episodes) == 1
    # With a proven reset in between, the second chain opens a new entry.
    readings = [
        low(0), high(1), high(2), high(3),
        low(6, value=80.0),
        high(10), high(11), high(12),
    ]
    ledger = reconstruct_ledger(readings, UPPER, RESET, DWELL)
    assert len(ledger.episodes) == 2
    assert ledger.episodes[0].end_evidence == pytest.approx(6.0)
    assert ledger.episodes[1].definite_open == pytest.approx(13.0)


def test_possible_start_unbounded_without_prior_evidence():
    ledger = reconstruct_ledger([high(1), high(2), high(3)], UPPER, RESET, DWELL)
    (episode,) = ledger.episodes
    assert episode.possible_start is None


def test_open_entry_without_reset_evidence_stays_on_the_books():
    ledger = reconstruct_ledger(
        [low(0), high(1), high(2), high(3), band(9)], UPPER, RESET, DWELL
    )
    (episode,) = ledger.episodes
    assert not episode.settled
    assert episode.end_evidence is None


def test_reading_from_canmatrix_signal():
    import canmatrix

    signal = canmatrix.Signal("OvenTemp", factor=0.5, offset=-40.0)
    signal.cycle_time = 100  # ms
    reading = reading_from_signal(signal, raw_value=260, time=1.0)
    assert reading.value == pytest.approx(90.0)
    assert reading.precision == pytest.approx(0.5)
    assert reading.hold == pytest.approx(0.1)


def test_reset_must_be_below_upper():
    with pytest.raises(ValueError):
        reconstruct_ledger([], upper=85.0, reset=90.0)
