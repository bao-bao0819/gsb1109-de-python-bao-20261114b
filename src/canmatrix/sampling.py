# -*- coding: utf-8 -*-
"""Reconstruct per-round (pressure, speed, valve) triplets from raw readings.

The bench samples each signal once per round, but on disk the readings are
stored in per-signal columns ordered by message arrival.  This module puts
them back together into triplets that can be handed to the analysis table.

Rules implemented here:

* a reading can be claimed by at most one round (no double consumption);
* for a given signal inside a group, the sampling sequence number assigned to
  successive rounds must strictly increase (the sequence may never go back);
* a triplet is only emitted when all three signals of a group are available
  for the same round;
* complete rounds are maximised first, the total number of triplets is the
  tie-breaker (lexicographic optimisation);
* when several pairings share the optimum (e.g. one pressure record whose
  sampling window overlaps two rounds), every optimal pairing is enumerated
  and the report distinguishes triplets all pairings agree on from the
  candidates that have to be retained for a manual decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Dict, FrozenSet, Hashable, List, Mapping, Sequence, Tuple

PRESSURE = "pressure"
SPEED = "speed"
VALVE = "valve"
SIGNALS: Tuple[str, str, str] = (PRESSURE, SPEED, VALVE)

_OVERLAP = "overlap"
_CONTAINED = "contained"
_MATCH_POLICIES = (_OVERLAP, _CONTAINED)


@dataclass(frozen=True)
class Reading:
    """One sampled value arriving in a per-signal column.

    ``window_start``/``window_end`` describe the sampling time window of the
    reading; a point-in-time reading simply passes the same timestamp twice.
    ``seq`` is the sampling sequence number of the reading *within its own
    signal of its group*; it must never decrease across rounds.
    """

    reading_id: Hashable
    group: str
    signal: str
    seq: int
    window_start: float
    window_end: float
    value: Any = None

    def __post_init__(self) -> None:
        if self.signal not in SIGNALS:
            raise ValueError(
                "unknown signal %r, expected one of %r" % (self.signal, SIGNALS)
            )
        if self.window_end < self.window_start:
            raise ValueError(
                "reading %r has window_end before window_start" % (self.reading_id,)
            )


@dataclass(frozen=True)
class RoundWindow:
    """Time span covered by one bench round."""

    round_id: Hashable
    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(
                "round %r ends before it starts" % (self.round_id,)
            )


@dataclass(frozen=True)
class RoundTriplet:
    """The three readings of one group that belong to one round."""

    round_id: Hashable
    group: str
    pressure: Reading
    speed: Reading
    valve: Reading

    def as_tuple(self) -> Tuple[Reading, Reading, Reading]:
        return self.pressure, self.speed, self.valve

    def reading_ids(self) -> Tuple[Hashable, Hashable, Hashable]:
        return (
            self.pressure.reading_id,
            self.speed.reading_id,
            self.valve.reading_id,
        )


@dataclass(frozen=True)
class Contention:
    """A reading claimed for different rounds across the optimal pairings."""

    reading: Reading
    round_ids: Tuple[Hashable, ...]


@dataclass
class ReconstructionReport:
    """Result of :func:`reconstruct_rounds`."""

    canonical_triplets: List[RoundTriplet]
    consensus_triplets: List[RoundTriplet]
    retained_candidates: Dict[
        Tuple[Hashable, str, str], List[Reading]
    ] = field(default_factory=dict)
    contentions: List[Contention] = field(default_factory=list)
    optimal_pairings: List[List[RoundTriplet]] = field(default_factory=list)

    @property
    def complete_round_count(self) -> int:
        return self._objective[0]

    @property
    def triplet_count(self) -> int:
        return self._objective[1]

    _objective: Tuple[int, int] = (0, 0)


def _eligible(
    reading: Reading, round_window: RoundWindow, match_policy: str
) -> bool:
    if match_policy == _OVERLAP:
        return (
            reading.window_end >= round_window.start
            and reading.window_start <= round_window.end
        )
    return (
        round_window.start <= reading.window_start
        and reading.window_end <= round_window.end
    )


def _reading_key(reading: Reading) -> Tuple[int, float, Hashable]:
    return reading.seq, reading.window_start, reading.reading_id


def reconstruct_rounds(
    readings: Sequence[Reading],
    rounds: Sequence[RoundWindow],
    groups: Sequence[str],
    match_policy: str = _OVERLAP,
) -> ReconstructionReport:
    """Reconstruct the per-round triplets and report every optimum.

    ``match_policy`` selects how a reading window matches a round window:

    * ``"overlap"`` (default): closed intervals intersect;
    * ``"contained"``: the reading window must lie inside the round window.
    """

    if match_policy not in _MATCH_POLICIES:
        raise ValueError(
            "unknown match_policy %r, expected one of %r"
            % (match_policy, _MATCH_POLICIES)
        )

    by_id: Dict[Hashable, Reading] = {}
    for reading in readings:
        if reading.reading_id in by_id:
            raise ValueError("duplicate reading_id %r" % (reading.reading_id,))
        by_id[reading.reading_id] = reading

    groups = tuple(groups)
    if len(set(groups)) != len(groups):
        raise ValueError("duplicate group ids in %r" % (groups,))
    round_ids = [round_window.round_id for round_window in rounds]
    if len(set(round_ids)) != len(round_ids):
        raise ValueError("duplicate round ids in %r" % (round_ids,))

    ordered_rounds = sorted(rounds, key=lambda item: (item.start, item.end))

    # candidates[round_index][group][signal] -> eligible readings, sorted so
    # that the DFS explores pairings in a deterministic order.
    candidates: List[Dict[str, Dict[str, List[Reading]]]] = []
    for round_window in ordered_rounds:
        per_group: Dict[str, Dict[str, List[Reading]]] = {}
        for group in groups:
            per_signal: Dict[str, List[Reading]] = {signal: [] for signal in SIGNALS}
            for reading in readings:
                if reading.group != group:
                    continue
                if _eligible(reading, round_window, match_policy):
                    per_signal[reading.signal].append(reading)
            for signal in SIGNALS:
                per_signal[signal].sort(key=_reading_key)
            per_group[group] = per_signal
        candidates.append(per_group)

    # One memo entry per DFS state: best objective reachable from the state
    # plus every suffix assignment (round_id -> group -> triplet) attaining it.
    memo: Dict[
        Tuple[int, FrozenSet[Hashable], Tuple[Tuple[str, str, int], ...]],
        Tuple[Tuple[int, int], List[Dict[Hashable, Dict[str, Tuple[Reading, ...]]]]],
    ] = {}

    def solve(
        index: int,
        used: FrozenSet[Hashable],
        last_seq: Mapping[Tuple[str, str], int],
    ) -> Tuple[
        Tuple[int, int],
        List[Dict[Hashable, Dict[str, Tuple[Reading, ...]]]],
    ]:
        last_key = tuple(
            (group, signal, seq)
            for (group, signal), seq in sorted(last_seq.items())
        )
        state = (index, used, last_key)
        if state in memo:
            return memo[state]

        if index == len(ordered_rounds):
            memo[state] = ((0, 0), [{}])
            return memo[state]

        round_window = ordered_rounds[index]

        # For every group: either skip it this round or pick one triplet.
        options_per_group: List[List[Tuple[str, None] | Tuple[str, Tuple[Reading, ...]]]] = []
        for group in groups:
            options: List[
                Tuple[str, None] | Tuple[str, Tuple[Reading, ...]]
            ] = [("skip", None)]
            per_signal = candidates[index][group]
            pickable: Dict[str, List[Reading]] = {}
            for signal in SIGNALS:
                previous = last_seq.get((group, signal))
                pool = [
                    reading
                    for reading in per_signal[signal]
                    if reading.reading_id not in used
                    and (previous is None or reading.seq > previous)
                ]
                pickable[signal] = pool
            for pressure, speed, valve in product(
                pickable[PRESSURE], pickable[SPEED], pickable[VALVE]
            ):
                ids = {
                    pressure.reading_id,
                    speed.reading_id,
                    valve.reading_id,
                }
                if len(ids) != 3:
                    continue
                if ids & used:
                    continue
                options.append(("triplet", (pressure, speed, valve)))
            options_per_group.append(options)

        best_objective: Tuple[int, int] | None = None
        best_suffixes: List[
            Dict[Hashable, Dict[str, Tuple[Reading, ...]]]
        ] = []

        for combo in product(*options_per_group):
            next_used = set(used)
            next_last = dict(last_seq)
            assigned: Dict[Hashable, Dict[str, Tuple[Reading, ...]]] = {}
            complete = True
            for group, choice in zip(groups, combo):
                kind, payload = choice
                if kind == "skip":
                    complete = False
                    continue
                triplet = payload  # type: ignore[assignment]
                for reading in triplet:
                    next_used.add(reading.reading_id)
                    next_last[(group, reading.signal)] = reading.seq
                assigned.setdefault(round_window.round_id, {})[group] = triplet
            if not assigned:
                complete = False

            sub_objective, sub_suffixes = solve(
                index + 1, frozenset(next_used), next_last
            )
            produced = sum(len(per_group) for per_group in assigned.values())
            objective = (
                sub_objective[0] + (1 if complete else 0),
                sub_objective[1] + produced,
            )
            if best_objective is not None and objective < best_objective:
                continue
            merged = [
                _merge_assignments(assigned, suffix) for suffix in sub_suffixes
            ]
            if best_objective is None or objective > best_objective:
                best_objective = objective
                best_suffixes = merged
            else:
                best_suffixes.extend(merged)

        assert best_objective is not None
        memo[state] = (best_objective, best_suffixes)
        return memo[state]

    objective, raw_solutions = solve(0, frozenset(), {})
    pairings = [
        _to_triplets(raw, ordered_rounds) for raw in raw_solutions
    ]

    canonical = pairings[0]
    consensus = _consensus_triplets(pairings, ordered_rounds, groups)
    retained = _retained_candidates(pairings, by_id)
    contentions = _contention_report(pairings, by_id)

    return ReconstructionReport(
        canonical_triplets=canonical,
        consensus_triplets=consensus,
        retained_candidates=retained,
        contentions=contentions,
        optimal_pairings=pairings,
        _objective=objective,
    )


def _merge_assignments(
    assigned: Dict[Hashable, Dict[str, Tuple[Reading, ...]]],
    suffix: Dict[Hashable, Dict[str, Tuple[Reading, ...]]],
) -> Dict[Hashable, Dict[str, Tuple[Reading, ...]]]:
    merged: Dict[Hashable, Dict[str, Tuple[Reading, ...]]] = {
        round_id: dict(per_group) for round_id, per_group in suffix.items()
    }
    for round_id, per_group in assigned.items():
        merged.setdefault(round_id, {}).update(per_group)
    return merged


def _to_triplets(
    raw: Mapping[Hashable, Mapping[str, Tuple[Reading, ...]]],
    ordered_rounds: Sequence[RoundWindow],
) -> List[RoundTriplet]:
    triplets: List[RoundTriplet] = []
    round_order = {
        round_window.round_id: position
        for position, round_window in enumerate(ordered_rounds)
    }
    for round_id, per_group in raw.items():
        for group, (pressure, speed, valve) in sorted(
            per_group.items(), key=lambda item: item[0]
        ):
            triplets.append(
                RoundTriplet(
                    round_id=round_id,
                    group=group,
                    pressure=pressure,
                    speed=speed,
                    valve=valve,
                )
            )
    triplets.sort(
        key=lambda triplet: (
            round_order[triplet.round_id],
            triplet.group,
        )
    )
    return triplets


def _consensus_triplets(
    pairings: Sequence[List[RoundTriplet]],
    ordered_rounds: Sequence[RoundWindow],
    groups: Sequence[str],
) -> List[RoundTriplet]:
    slots: Dict[Tuple[Hashable, str], List[Tuple[Hashable, ...]]] = {}
    lookup: Dict[Tuple[Hashable, str], RoundTriplet] = {}
    for pairing in pairings:
        seen: Dict[Tuple[Hashable, str], Tuple[Hashable, ...]] = {}
        for triplet in pairing:
            key = (triplet.round_id, triplet.group)
            seen[key] = triplet.reading_ids()
            lookup[key] = triplet
        for round_window in ordered_rounds:
            for group in groups:
                key = (round_window.round_id, group)
                slots.setdefault(key, []).append(seen.get(key))
    consensus: List[RoundTriplet] = []
    round_order = {
        round_window.round_id: position
        for position, round_window in enumerate(ordered_rounds)
    }
    for key, variants in slots.items():
        if all(variant is not None and variant == variants[0] for variant in variants):
            consensus.append(lookup[key])
    consensus.sort(key=lambda triplet: (round_order[triplet.round_id], triplet.group))
    return consensus


def _retained_candidates(
    pairings: Sequence[List[RoundTriplet]],
    by_id: Mapping[Hashable, Reading],
) -> Dict[Tuple[Hashable, str, str], List[Reading]]:
    # Every (round, group, signal) bucket seen in any pairing together with
    # the reading id assigned to it.
    bucket_ids: Dict[Tuple[Hashable, str, str], set] = {}
    # Where each reading is used: a reading is contended when it lands in more
    # than one (round, group) slot across the optimal pairings.
    reading_slots: Dict[Hashable, set] = {}
    for pairing in pairings:
        for triplet in pairing:
            slot = (triplet.round_id, triplet.group)
            for reading in triplet.as_tuple():
                bucket_ids.setdefault(
                    (triplet.round_id, triplet.group, reading.signal), set()
                ).add(reading.reading_id)
                reading_slots.setdefault(reading.reading_id, set()).add(slot)
    contended = {
        reading_id
        for reading_id, slots in reading_slots.items()
        if len(slots) > 1
    }

    buckets: Dict[Tuple[Hashable, str, str], List[Reading]] = {}
    for key, ids in bucket_ids.items():
        if len(ids) > 1 or ids & contended:
            buckets[key] = sorted(
                (by_id[reading_id] for reading_id in ids),
                key=_reading_key,
            )
    return dict(sorted(buckets.items(), key=lambda item: item[0]))


def _contention_report(
    pairings: Sequence[List[RoundTriplet]],
    by_id: Mapping[Hashable, Reading],
) -> List[Contention]:
    usage: Dict[Hashable, set] = {}
    for pairing in pairings:
        for triplet in pairing:
            for reading in triplet.as_tuple():
                usage.setdefault(reading.reading_id, set()).add(triplet.round_id)
    contentions = [
        Contention(
            reading=by_id[reading_id],
            round_ids=tuple(sorted(round_ids, key=lambda value: str(value))),
        )
        for reading_id, round_ids in usage.items()
        if len(round_ids) > 1
    ]
    contentions.sort(
        key=lambda contention: (
            contention.reading.group,
            contention.reading.signal,
            contention.reading.reading_id,
        )
    )
    return contentions
