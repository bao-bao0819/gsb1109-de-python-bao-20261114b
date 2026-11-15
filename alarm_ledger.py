"""Rebuild an over-temperature alarm ledger from quantized canmatrix readings.

Ledger rules (furnace temperature supervision):

* An alarm entry opens only after the temperature stays at or above the
  upper threshold for ``dwell`` consecutive seconds.
* An open entry is cleared only when the temperature drops below the lower
  reset threshold (hysteresis band between the two thresholds).
* Every reading is one sample, valid for its agreed hold time: it covers
  ``[time, time + hold]``.  When coverage is lost between two readings the
  temperature may drift arbitrarily, so a stale high reading must NOT be
  stretched to complete the dwell time.
* Each source states a precision (the quantization step, e.g. the canmatrix
  signal factor): the true value lies within ``value +/- precision / 2``.
* A reading that falls between the two thresholds neither starts an alarm
  nor clears one.  It only *maintains* an alarm that was actually triggered
  before; the very same in-band value means nothing if no alarm has
  definitely opened yet.

For every alarm the ledger reports the proven open time, the earliest
start time consistent with the evidence, and the end times that are backed
by evidence (a reading that proves the temperature is below the reset
threshold).
"""

from __future__ import annotations

import bisect
import enum
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "Reading",
    "Band",
    "AlarmEpisode",
    "Ledger",
    "reading_from_signal",
    "reconstruct_ledger",
]


@dataclass(frozen=True)
class Reading:
    """One quantized observation.

    ``time``       sample timestamp in seconds.
    ``value``      quantized physical value (factor/offset already applied).
    ``precision``  quantization step / stated accuracy; the true value lies
                   within ``value +/- precision / 2``.
    ``hold``       maximum valid hold time in seconds; the reading covers
                   ``[time, time + hold]``.
    """

    time: float
    value: float
    precision: float = 0.0
    hold: float = 0.0

    @property
    def low(self) -> float:
        """Lower bound of the true value."""
        return self.value - self.precision / 2.0

    @property
    def high(self) -> float:
        """Upper bound of the true value."""
        return self.value + self.precision / 2.0

    @property
    def coverage(self) -> Tuple[float, float]:
        """Interval during which this sample is valid."""
        return (self.time, self.time + self.hold)


class Band(enum.Enum):
    """Classification of a reading against the two thresholds."""

    HIGH = "high"  # provably at/above the upper threshold
    LOW = "low"  # provably below the reset threshold
    BAND = "band"  # in between, or too imprecise to tell


@dataclass
class AlarmEpisode:
    """One alarm entry in the ledger."""

    index: int
    definite_open: float
    """Time by which the alarm had certainly opened (proven 3 s dwell)."""
    possible_start: Optional[float]
    """Earliest open time consistent with the evidence (None if the alarm
    may already have been open before the first observation)."""
    end_evidence: Optional[float] = None
    """Time of the first reading that proves the temperature dropped below
    the reset threshold, i.e. the alarm was cleared by then."""
    reset_window: Optional[Tuple[float, float]] = None
    """Interval in which the actual reset must have happened."""
    settled: bool = False
    """True once the entry has been cleared with evidence."""


@dataclass
class AnnotatedReading:
    """A reading together with its role in the ledger."""

    reading: Reading
    band: Band
    role: str
    episode: Optional[int] = None


@dataclass
class Ledger:
    """Result of the reconstruction."""

    upper: float
    reset: float
    dwell: float
    episodes: List[AlarmEpisode] = field(default_factory=list)
    readings: List[AnnotatedReading] = field(default_factory=list)

    @property
    def definite_alarms(self) -> List[AlarmEpisode]:
        """Alarms that certainly opened."""
        return list(self.episodes)

    @property
    def evidenced_ends(self) -> List[Tuple[int, float]]:
        """(episode index, end time) pairs backed by reset evidence."""
        return [
            (ep.index, ep.end_evidence)
            for ep in self.episodes
            if ep.end_evidence is not None
        ]


def reading_from_signal(signal, raw_value, time, hold=None):
    # type: (...) -> Reading
    """Build a :class:`Reading` from a canmatrix signal and a raw value.

    The physical value follows ``raw * factor + offset``; the precision is
    the quantization step ``abs(factor)``; the hold time defaults to the
    signal cycle time (milliseconds) when none is given.
    """
    value = float(signal.factor) * raw_value + float(signal.offset)
    precision = abs(float(signal.factor))
    if hold is None:
        cycle_time = getattr(signal, "cycle_time", 0) or 0
        hold = float(cycle_time) / 1000.0
    return Reading(time=float(time), value=value, precision=precision, hold=hold)


def _classify(reading: Reading, upper: float, reset: float) -> Band:
    if reading.low >= upper:
        return Band.HIGH
    if reading.high < reset:
        return Band.LOW
    return Band.BAND


def _merge(intervals: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Merge overlapping or touching intervals."""
    merged: List[List[float]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _coverage_end_before(intervals: Sequence[Tuple[float, float]], limit: float) -> Optional[float]:
    """Latest interval end not beyond ``limit`` (None if there is none)."""
    ends = [end for _, end in intervals if end <= limit]
    return max(ends) if ends else None


def reconstruct_ledger(
    readings: Sequence[Reading],
    upper: float,
    reset: float,
    dwell: float = 3.0,
) -> Ledger:
    """Rebuild the alarm ledger from quantized observations.

    ``upper``  upper threshold; the alarm opens after ``dwell`` continuous
               seconds at/above it.
    ``reset``  lower reset threshold; the alarm clears only below it.
    ``dwell``  required continuous over-limit time in seconds.
    """
    if reset >= upper:
        raise ValueError("reset threshold must be below the upper threshold")
    ordered = sorted(readings, key=lambda r: r.time)
    bands = [_classify(r, upper, reset) for r in ordered]

    # Coverage that is provably over the upper threshold: only a definitely
    # high reading vouches for its own hold window.  Gaps between windows
    # are NOT bridged -- a stale high must not be stretched to fill dwell.
    high_coverage = _merge(
        [r.coverage for r, band in zip(ordered, bands) if band is Band.HIGH]
    )
    # Coverage that is provably NOT over the upper threshold (blocks any
    # dwell accumulation, even the optimistic one).
    not_high_coverage = _merge(
        [r.coverage for r in ordered if r.high < upper]
    )
    # Coverage that may still be at/above the reset threshold.
    not_low_coverage = _merge(
        [r.coverage for r in ordered if r.high >= reset]
    )
    low_times = sorted(r.time for r, band in zip(ordered, bands) if band is Band.LOW)

    ledger = Ledger(upper=upper, reset=reset, dwell=dwell)

    # Definite opens: a continuous provably-high coverage of `dwell` seconds.
    open_times = [
        start + dwell
        for start, end in high_coverage
        if end - start >= dwell
    ]

    for open_time in open_times:
        current = ledger.episodes[-1] if ledger.episodes else None
        if current is not None and not current.settled:
            # A new entry only starts if the previous one was cleared with
            # evidence before this open; otherwise it is the same episode.
            idx = bisect.bisect_left(low_times, current.definite_open)
            clears = [t for t in low_times[idx:] if t < open_time]
            if not clears:
                continue
            _close(current, clears[0], not_low_coverage)
        episode = AlarmEpisode(
            index=len(ledger.episodes),
            definite_open=open_time,
            possible_start=None,
        )
        ledger.episodes.append(episode)

    for episode in ledger.episodes:
        if not episode.settled:
            idx = bisect.bisect_left(low_times, episode.definite_open)
            if idx < len(low_times):
                _close(episode, low_times[idx], not_low_coverage)
        episode.possible_start = _possible_start(
            episode.definite_open, not_high_coverage, dwell
        )

    ledger.readings = _annotate(ordered, bands, ledger.episodes)
    return ledger


def _close(
    episode: AlarmEpisode,
    evidence_time: float,
    not_low_coverage: Sequence[Tuple[float, float]],
) -> None:
    """Settle an episode with the first reading proving the reset."""
    episode.end_evidence = evidence_time
    episode.settled = True
    anchor = _coverage_end_before(not_low_coverage, evidence_time)
    if anchor is None or anchor < episode.definite_open:
        anchor = episode.definite_open
    episode.reset_window = (anchor, evidence_time)


def _possible_start(
    definite_open: float,
    not_high_coverage: Sequence[Tuple[float, float]],
    dwell: float,
) -> Optional[float]:
    """Earliest open time consistent with the evidence.

    Optimistically the temperature is at/above the upper threshold whenever
    no reading proves otherwise (gaps included), so the dwell accumulation
    can start right after the last proven-not-high coverage before the
    definite open, and completes ``dwell`` seconds later.  Returns None when
    nothing before the definite open rules out an even earlier start (the
    alarm may predate the data).
    """
    blockers = [end for start, end in not_high_coverage if start < definite_open]
    if not blockers:
        return None
    return min(max(blockers) + dwell, definite_open)


def _annotate(
    ordered: Sequence[Reading],
    bands: Sequence[Band],
    episodes: Sequence[AlarmEpisode],
) -> List[AnnotatedReading]:
    """Tag each reading with its role in the ledger."""
    annotated: List[AnnotatedReading] = []
    for reading, band in zip(ordered, bands):
        role = "neutral"
        episode_index: Optional[int] = None
        for episode in episodes:
            end = episode.end_evidence
            if episode.definite_open <= reading.time and (
                end is None or reading.time <= end
            ):
                episode_index = episode.index
                if band is Band.HIGH:
                    role = "trigger"
                elif band is Band.LOW:
                    role = "reset-evidence"
                else:
                    # In-band value: it counts towards this open entry only
                    # because the alarm actually triggered before.
                    role = "maintain"
                break
        else:
            if band is Band.HIGH:
                role = "over-limit-unproven"
            elif band is Band.LOW:
                role = "reset-evidence"
        annotated.append(
            AnnotatedReading(
                reading=reading, band=band, role=role, episode=episode_index
            )
        )
    return annotated


def _demo() -> None:
    """Worked furnace example: upper 90 degC, reset 85 degC, dwell 3 s."""
    readings = [
        Reading(0.0, 80.0, precision=1.0, hold=1.0),   # provably below reset
        Reading(1.0, 91.0, precision=1.0, hold=1.0),   # high, covers [1, 2]
        Reading(2.0, 92.0, precision=1.0, hold=1.0),   # high, covers [2, 3]
        Reading(3.0, 91.5, precision=1.0, hold=0.5),   # high, covers [3, 3.5]
        Reading(3.5, 88.0, precision=1.0, hold=0.4),   # in-band: no accumulation
        Reading(4.0, 91.0, precision=1.0, hold=1.0),
        Reading(5.0, 92.0, precision=1.0, hold=1.0),
        Reading(6.0, 91.0, precision=1.0, hold=1.0),   # chain [4, 7] -> open
        Reading(9.0, 87.0, precision=1.0, hold=1.0),   # in-band: maintains
        Reading(12.0, 84.0, precision=1.0, hold=1.0),  # reset evidence
        Reading(13.0, 91.0, precision=1.0, hold=0.5),  # 0.5 s only, then a gap
        Reading(14.0, 92.0, precision=1.0, hold=1.0),
        Reading(15.0, 92.0, precision=1.0, hold=1.0),
        Reading(16.0, 91.0, precision=1.0, hold=1.0),  # chain [14, 17] -> open
        Reading(20.0, 86.0, precision=1.0, hold=1.0),  # in-band: maintains
        Reading(25.0, 83.0, precision=1.0, hold=1.0),  # reset evidence
        Reading(30.0, 95.0, precision=1.0, hold=0.5),  # lone spike, no alarm
    ]
    ledger = reconstruct_ledger(readings, upper=90.0, reset=85.0, dwell=3.0)
    print("Alarm ledger (upper=90, reset=85, dwell=3s)")
    for episode in ledger.episodes:
        possible = (
            f"{episode.possible_start:.1f}s"
            if episode.possible_start is not None
            else "before first reading"
        )
        print(
            f"  entry {episode.index}: definitely open by "
            f"{episode.definite_open:.1f}s, possibly since {possible}"
        )
        if episode.end_evidence is not None:
            start, end = episode.reset_window
            print(
                f"    cleared with evidence at {episode.end_evidence:.1f}s "
                f"(reset happened in ({start:.1f}, {end:.1f}])"
            )
        else:
            print("    still on the books (no reset evidence)")
    print("Reading roles:")
    for item in ledger.readings:
        r = item.reading
        print(
            f"  t={r.time:5.1f}s value={r.value:5.1f} "
            f"[{item.band.value:4s}] -> {item.role}"
            + (f" (entry {item.episode})" if item.episode is not None else "")
        )


if __name__ == "__main__":
    _demo()
