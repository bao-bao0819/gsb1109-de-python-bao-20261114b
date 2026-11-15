# -*- coding: utf-8 -*-
# Copyright (c) 2013, Eduard Broecker
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that
# the following conditions are met:
#
#    Redistributions of source code must retain the above copyright notice, this list of conditions and the
#    following disclaimer.
#    Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the
#    following disclaimer in the documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
# DAMAGE.

"""Weekend recompute planning for warehouse partitions.

A plain matrix comparison (see :mod:`canmatrix.compare`) reports what changed
between two dictionaries.  The warehouse instead needs to know *which stored
partitions have to be recomputed* over the weekend.  Every partition keeps
statistics about the raw data it was built from:

* the message (frame) ids seen in the partition,
* the multiplexer selector values observed per frame,
* the source signal names feeding every derived column.

Some partitions do not carry complete statistics, in which case their fate can
only be decided by replaying the original raw frames.

The planner splits every partition into one of three buckets:

* :data:`RecomputePlan.RECOMPUTE` -- affected columns are known and can be
  recomputed directly,
* :data:`RecomputePlan.UNAFFECTED` -- the retained statistics prove that the
  dictionary changes cannot touch the partition,
* :data:`RecomputePlan.RECHECK_RAW` -- the statistics are insufficient; the
  original raw frames have to be inspected first.

Display name changes (frame/signal renames, comments, units, receivers,
attributes) do not alter decoded values stored under stable identifiers, so
they never force a recompute.  Moving the multiplexer selector to different
bits, however, invalidates every derived column built from multiplexed
signals even when those signals themselves did not change.
"""

import typing

import attr

from canmatrix._canmatrix import CanMatrix
from canmatrix.Frame import Frame
from canmatrix.Signal import Signal

# Partition statuses
RECOMPUTE = "recompute"
UNAFFECTED = "unaffected"
RECHECK_RAW = "recheck_raw"

# Machine-readable reason codes
REASON_FRAME_ADDED = "frame-added"
REASON_FRAME_REMOVED = "frame-removed"
REASON_FRAME_RENAMED = "frame-renamed"
REASON_FRAME_SIZE_CHANGED = "frame-size-changed"
REASON_FRAME_LAYOUT_CHANGED = "frame-layout-changed"
REASON_SIGNAL_ADDED = "signal-added"
REASON_SIGNAL_REMOVED = "signal-removed"
REASON_SIGNAL_RENAMED = "signal-renamed"
REASON_SIGNAL_DECODING_CHANGED = "signal-decoding-changed"
REASON_SIGNAL_BRANCH_CHANGED = "signal-multiplex-branch-changed"
REASON_SIGNAL_DISPLAY_CHANGED = "signal-display-changed"
REASON_MUX_SELECTOR_CHANGED = "multiplexer-selector-changed"
REASON_MUX_STRUCTURE_CHANGED = "multiplexing-changed"
REASON_INCOMPLETE_STATS = "incomplete-partition-statistics"
REASON_UNKNOWN_FRAME_MEMBERSHIP = "frame-membership-unknown"
REASON_UNKNOWN_DERIVED_FIELDS = "derived-field-lists-unknown"

# Signal attributes that change the decoded value.  Everything else (name,
# comment, unit, receivers, custom attributes, min/max, ...) is display or
# documentation metadata and does not invalidate stored values.
_DECODING_ATTRIBUTES = (
    "start_bit",
    "size",
    "is_little_endian",
    "is_signed",
    "factor",
    "offset",
    "is_float",
    "is_ascii",
    "scale_ranges",
    "multiplex",
    "mux_val_grp",
    "muxer_for_signal",
)


def _value_table_keys(values):
    # type: (typing.Mapping[int, str]) -> typing.Tuple[int, ...]
    return tuple(sorted(int(key) for key in values))


def _signal_decoding_identity(signal):
    # type: (Signal) -> typing.Tuple[typing.Any, ...]
    """Hashable description of everything that affects raw2phys decoding.

    Value table labels are display names and deliberately excluded: only the
    set of enumerated raw values can change decoded output.
    """
    return (
        signal.start_bit,
        signal.size,
        signal.is_little_endian,
        signal.is_signed,
        signal.factor,
        signal.offset,
        signal.is_float,
        signal.is_ascii,
        tuple(tuple(item) for item in signal.scale_ranges),
        signal.multiplex,
        tuple(tuple(group) for group in signal.mux_val_grp),
        signal.muxer_for_signal,
        _value_table_keys(signal.values),
    )


def _frame_structure_identity(frame):
    # type: (Frame) -> typing.Tuple[typing.Any, ...]
    """Frame-level properties that change how the payload is split apart."""
    return (
        frame.size,
        frame.is_complex_multiplexed,
        frame.is_fd,
        tuple(
            (pdu.id, pdu.name, tuple(signal.name for signal in pdu.signals))
            for pdu in frame.pdus
        ),
    )


def _signal_branch_values(signal, observed_values):
    # type: (Signal, typing.Optional[typing.Set[int]]) -> typing.Optional[typing.Set[int]]
    """Multiplexer branches carrying *signal*, restricted to observed values.

    Returns ``None`` for static (non-multiplexed) signals, an empty set when
    none of the observed selector values reaches the signal, or the set of
    relevant selector values.
    """
    if signal.mux_val is not None:
        if observed_values is None:
            return {signal.mux_val}
        return {signal.mux_val} if signal.mux_val in observed_values else set()
    if signal.mux_val_grp:
        if observed_values is None:
            expanded = set()  # type: typing.Set[int]
            for mux_min, mux_max in signal.mux_val_grp:
                expanded.update(range(mux_min, mux_max + 1))
            return expanded
        return {
            value
            for value in observed_values
            if signal.multiplexer_value_in_range(value)
        }
    return None


@attr.s
class PartitionStats(object):
    """Statistics retained for one warehouse partition.

    :param partition_id: stable partition identifier.
    :param message_ids: arbitration ids observed in the partition, or ``None``
        when the partition did not retain the list of read messages.
    :param multiplexer_values: observed selector values keyed by frame id.
        A missing key means no selector statistics were kept for that frame.
    :param derived_signal_fields: source signal names feeding each derived
        column, keyed by column name, or ``None`` when the field lists are
        unknown.
    :param complete: ``False`` for partitions without full statistics. Such
        partitions are never declared unaffected on partial evidence; their
        raw frames have to be replayed instead.
    """

    partition_id = attr.ib()  # type: str
    message_ids = attr.ib(default=None)  # type: typing.Optional[typing.Set[int]]
    multiplexer_values = attr.ib(factory=dict)  # type: typing.Mapping[int, typing.Set[int]]
    derived_signal_fields = attr.ib(default=None)  # type: typing.Optional[typing.Mapping[str, typing.Sequence[str]]]
    complete = attr.ib(default=True)  # type: bool

    def __attrs_post_init__(self):
        # type: () -> None
        if self.message_ids is not None:
            self.message_ids = set(self.message_ids)
        normalized = {}
        for frame_id, values in self.multiplexer_values.items():
            normalized[int(frame_id)] = set(values)
        self.multiplexer_values = normalized
        if self.derived_signal_fields is not None:
            self.derived_signal_fields = {
                column: list(fields)
                for column, fields in self.derived_signal_fields.items()
            }


@attr.s
class SignalChange(object):
    """Decoding-relevant change of one matched signal."""

    signal_name = attr.ib()  # type: str
    changed_attributes = attr.ib(factory=list)  # type: typing.List[str]
    branch_changed = attr.ib(default=False)  # type: bool
    display_attributes = attr.ib(factory=list)  # type: typing.List[str]


@attr.s
class FrameImpact(object):
    """Dictionary change of one frame identified by its arbitration id."""

    frame_id = attr.ib()  # type: int
    extended = attr.ib(default=False)  # type: bool
    old_name = attr.ib(default=None)  # type: typing.Optional[str]
    new_name = attr.ib(default=None)  # type: typing.Optional[str]
    present_old = attr.ib(default=True)  # type: bool
    present_new = attr.ib(default=True)  # type: bool
    display_only = attr.ib(default=False)  # type: bool
    selector_layout_changed = attr.ib(default=False)  # type: bool
    multiplexing_changed = attr.ib(default=False)  # type: bool
    is_multiplexed = attr.ib(default=False)  # type: bool
    affected_signals = attr.ib(factory=set)  # type: typing.Set[str]
    signal_changes = attr.ib(factory=list)  # type: typing.List[SignalChange]
    renames = attr.ib(factory=dict)  # type: typing.Mapping[str, str]
    reasons = attr.ib(factory=list)  # type: typing.List[str]

    @property
    def frame_name(self):  # type: () -> typing.Optional[str]
        return self.new_name if self.new_name is not None else self.old_name

    @property
    def decoding_changed(self):  # type: () -> bool
        return bool(self.affected_signals) or self.selector_layout_changed or self.multiplexing_changed


@attr.s
class RecomputeItem(object):
    """Concrete recompute scope for one frame inside a partition."""

    frame_id = attr.ib()  # type: int
    frame_name = attr.ib(default=None)  # type: typing.Optional[str]
    reasons = attr.ib(factory=list)  # type: typing.List[str]
    recompute_columns = attr.ib(factory=list)  # type: typing.List[str]
    multiplexer_values = attr.ib(default=None)  # type: typing.Optional[typing.List[int]]


@attr.s
class RecomputeDecision(object):
    """Plan entry for one partition."""

    partition_id = attr.ib()  # type: str
    status = attr.ib()  # type: str
    reasons = attr.ib(factory=list)  # type: typing.List[str]
    items = attr.ib(factory=list)  # type: typing.List[RecomputeItem]
    # Concrete frame ids to filter the raw replay; an empty list means the
    # whole partition raw data has to be scanned (frame membership unknown).
    inspect_message_ids = attr.ib(factory=list)  # type: typing.List[int]


@attr.s
class RecomputePlan(object):
    """Recompute plan covering every partition handed to the planner."""

    RECOMPUTE = RECOMPUTE
    UNAFFECTED = UNAFFECTED
    RECHECK_RAW = RECHECK_RAW

    decisions = attr.ib(factory=list)  # type: typing.List[RecomputeDecision]
    impacts = attr.ib(factory=list)  # type: typing.List[FrameImpact]

    def decisions_by_status(self, status):
        # type: (str) -> typing.List[RecomputeDecision]
        return [decision for decision in self.decisions if decision.status == status]

    @property
    def partitions_to_recompute(self):  # type: () -> typing.List[RecomputeDecision]
        return self.decisions_by_status(RECOMPUTE)

    @property
    def partitions_proven_unaffected(self):  # type: () -> typing.List[RecomputeDecision]
        return self.decisions_by_status(UNAFFECTED)

    @property
    def partitions_needing_raw_frames(self):  # type: () -> typing.List[RecomputeDecision]
        return self.decisions_by_status(RECHECK_RAW)


def analyze_frame_change(old_frame, new_frame):
    # type: (typing.Optional[Frame], typing.Optional[Frame]) -> typing.Optional[FrameImpact]
    """Compare two versions of the same frame (matched by arbitration id).

    ``None`` is returned when the frames are fully identical, including their
    display names.  At least one of *old_frame* / *new_frame* must be given.
    """
    if old_frame is None and new_frame is None:
        return None

    if old_frame is None:
        frame = new_frame  # type: Frame
        impact = FrameImpact(
            frame_id=frame.arbitration_id.id,
            extended=frame.arbitration_id.extended,
            old_name=None,
            new_name=frame.name,
            present_old=False,
            reasons=[REASON_FRAME_ADDED],
        )
        impact.affected_signals.update(signal.name for signal in frame.signals)
        impact.is_multiplexed = frame.is_multiplexed
        return impact

    if new_frame is None:
        impact = FrameImpact(
            frame_id=old_frame.arbitration_id.id,
            extended=old_frame.arbitration_id.extended,
            old_name=old_frame.name,
            new_name=None,
            present_new=False,
            reasons=[REASON_FRAME_REMOVED],
        )
        impact.affected_signals.update(signal.name for signal in old_frame.signals)
        impact.is_multiplexed = old_frame.is_multiplexed
        return impact

    impact = FrameImpact(
        frame_id=old_frame.arbitration_id.id,
        extended=old_frame.arbitration_id.extended,
        old_name=old_frame.name,
        new_name=new_frame.name,
        is_multiplexed=old_frame.is_multiplexed or new_frame.is_multiplexed,
    )
    if old_frame.name != new_frame.name:
        impact.reasons.append(REASON_FRAME_RENAMED)

    if _frame_structure_identity(old_frame) != _frame_structure_identity(new_frame):
        if old_frame.size != new_frame.size:
            impact.reasons.append(REASON_FRAME_SIZE_CHANGED)
        else:
            impact.reasons.append(REASON_FRAME_LAYOUT_CHANGED)
        # DLC/PDU/container changes can touch every decoded signal.
        impact.affected_signals.update(signal.name for signal in old_frame.signals)
        impact.affected_signals.update(signal.name for signal in new_frame.signals)

    _analyze_multiplexer(old_frame, new_frame, impact)
    _analyze_signals(old_frame, new_frame, impact)

    if not impact.reasons and not impact.signal_changes and not impact.renames:
        return None
    impact.reasons = list(dict.fromkeys(impact.reasons))
    if not impact.decoding_changed:
        impact.display_only = True
    return impact


def _analyze_multiplexer(old_frame, new_frame, impact):
    # type: (Frame, Frame, FrameImpact) -> None
    old_muxer = old_frame.get_multiplexer
    new_muxer = new_frame.get_multiplexer
    if old_muxer is None and new_muxer is None:
        return
    if old_muxer is None or new_muxer is None:
        impact.multiplexing_changed = True
        impact.reasons.append(REASON_MUX_STRUCTURE_CHANGED)
        impact.affected_signals.update(signal.name for signal in old_frame.signals)
        impact.affected_signals.update(signal.name for signal in new_frame.signals)
        return
    selector_fields = ("start_bit", "size", "is_little_endian", "is_signed")
    if any(getattr(old_muxer, field) != getattr(new_muxer, field) for field in selector_fields):
        impact.selector_layout_changed = True
        impact.reasons.append(REASON_MUX_SELECTOR_CHANGED)
        # The selector itself and every signal living in a mux branch become
        # untrustworthy, even if their own definitions did not change.
        impact.affected_signals.add(old_muxer.name)
        for frame in (old_frame, new_frame):
            for signal in frame.signals:
                if signal.mux_val is not None or signal.mux_val_grp:
                    impact.affected_signals.add(signal.name)


def _analyze_signals(old_frame, new_frame, impact):
    # type: (Frame, Frame, FrameImpact) -> None
    old_by_name = {signal.name: signal for signal in old_frame.signals}
    new_by_name = {signal.name: signal for signal in new_frame.signals}

    for name in sorted(set(old_by_name).union(new_by_name)):
        old_signal = old_by_name.get(name)
        new_signal = new_by_name.get(name)
        if old_signal is None:
            impact.reasons.append(REASON_SIGNAL_ADDED)
            impact.affected_signals.add(name)
            continue
        if new_signal is None:
            impact.reasons.append(REASON_SIGNAL_REMOVED)
            impact.affected_signals.add(name)
            continue
        _compare_matched_signal(old_signal, new_signal, impact)

    renames = _detect_signal_renames(old_frame, new_frame, impact, old_by_name, new_by_name)
    impact.renames = renames
    _reconcile_add_remove_reasons(impact, old_by_name, new_by_name, renames)


def _reconcile_add_remove_reasons(impact, old_by_name, new_by_name, renames):
    # type: (FrameImpact, typing.Mapping[str, Signal], typing.Mapping[str, Signal], typing.Mapping[str, str]) -> None
    """Rebuild add/remove reasons from the signals not explained by renames."""
    impact.reasons = [
        reason for reason in impact.reasons
        if reason not in (REASON_SIGNAL_ADDED, REASON_SIGNAL_REMOVED)
    ]
    for name in old_by_name:
        if name not in new_by_name and name not in renames:
            impact.reasons.append(REASON_SIGNAL_REMOVED)
    for name in new_by_name:
        if name not in old_by_name and name not in renames.values():
            impact.reasons.append(REASON_SIGNAL_ADDED)


def _compare_matched_signal(old_signal, new_signal, impact):
    # type: (Signal, Signal, FrameImpact) -> None
    changed = [
        attribute
        for attribute in _DECODING_ATTRIBUTES
        if getattr(old_signal, attribute) != getattr(new_signal, attribute)
    ]
    display_changed = []  # type: typing.List[str]
    if _value_table_keys(old_signal.values) != _value_table_keys(new_signal.values):
        changed.append("values")
    elif old_signal.values != new_signal.values:
        # Same enumerated raw values, only display labels differ.
        display_changed.append("values")
    if not changed and not display_changed:
        return
    branch_changed = any(
        attribute in ("multiplex", "mux_val_grp", "muxer_for_signal")
        for attribute in changed
    )
    impact.signal_changes.append(
        SignalChange(signal_name=old_signal.name, changed_attributes=changed,
                     branch_changed=branch_changed,
                     display_attributes=display_changed)
    )
    if changed:
        impact.affected_signals.add(old_signal.name)
        impact.reasons.append(
            REASON_SIGNAL_BRANCH_CHANGED if branch_changed
            else REASON_SIGNAL_DECODING_CHANGED
        )
    else:
        impact.reasons.append(REASON_SIGNAL_DISPLAY_CHANGED)


def _detect_signal_renames(old_frame, new_frame, impact, old_by_name, new_by_name):
    # type: (Frame, Frame, FrameImpact, typing.Mapping[str, Signal], typing.Mapping[str, Signal]) -> typing.Mapping[str, str]
    """Treat layout-identical unmatched signals as pure display renames."""
    unmatched_old = [
        signal for name, signal in old_by_name.items() if name not in new_by_name
    ]
    unmatched_new = [
        signal for name, signal in new_by_name.items() if name not in old_by_name
    ]
    frame_level_decoding_change = (
        impact.selector_layout_changed
        or impact.multiplexing_changed
        or REASON_FRAME_SIZE_CHANGED in impact.reasons
        or REASON_FRAME_LAYOUT_CHANGED in impact.reasons
    )
    renames = {}  # type: typing.Dict[str, str]
    for old_signal in unmatched_old:
        for new_signal in unmatched_new:
            if new_signal.name in renames.values():
                continue
            if _signal_decoding_identity(old_signal) == _signal_decoding_identity(new_signal):
                # The earlier add/remove bookkeeping was a rename.
                renames[old_signal.name] = new_signal.name
                impact.reasons.append(REASON_SIGNAL_RENAMED)
                if frame_level_decoding_change:
                    # Existing columns reference the old name; the frame-level
                    # change still invalidates them, so keep it affected.
                    impact.affected_signals.discard(new_signal.name)
                else:
                    # Pure display rename: decoded values stay identical.
                    impact.affected_signals.discard(old_signal.name)
                    impact.affected_signals.discard(new_signal.name)
                break
    return renames


def analyze_matrix_changes(db_old, db_new):
    # type: (CanMatrix, CanMatrix) -> typing.List[FrameImpact]
    """Collect the frame impacts between two matrices, keyed by arbitration id."""
    impacts = []  # type: typing.List[FrameImpact]
    seen_ids = set()  # type: typing.Set[int]
    for old_frame in db_old.frames:
        new_frame = db_new.frame_by_id(old_frame.arbitration_id)
        impact = analyze_frame_change(old_frame, new_frame)
        if impact is not None:
            impacts.append(impact)
        seen_ids.add(old_frame.arbitration_id.id)
    for new_frame in db_new.frames:
        if new_frame.arbitration_id.id in seen_ids:
            continue
        impact = analyze_frame_change(None, new_frame)
        if impact is not None:
            impacts.append(impact)
    return impacts


def _affected_columns(impact, stats):
    # type: (FrameImpact, PartitionStats) -> typing.List[str]
    assert stats.derived_signal_fields is not None
    return sorted(
        column
        for column, fields in stats.derived_signal_fields.items()
        if set(fields).intersection(impact.affected_signals)
    )


def _reachable_columns(impact, frame, stats, columns):
    # type: (FrameImpact, typing.Optional[Frame], PartitionStats, typing.Sequence[str]) -> typing.Tuple[typing.List[str], typing.Optional[typing.List[int]]]
    """Drop columns the observed selector values can never reach.

    Returns the columns that really have to be recomputed and the selector
    values worth replaying.  ``None`` for the selector values means that no
    narrowing is possible, so every multiplexer branch has to be considered.
    Columns are only dropped for ordinary signal changes: when the selector
    itself moved or multiplexing was added/removed, old frames may decode into
    different branches under the new dictionary, so every affected column is
    kept.
    """
    observed = stats.multiplexer_values.get(impact.frame_id)
    if not impact.is_multiplexed:
        return list(columns), None
    if not observed or frame is None:
        # No selector statistics (or frame vanished): branch exclusion is
        # impossible, so every affected column stays in scope.
        return list(columns), None
    observed_set = set(observed)
    if impact.selector_layout_changed or impact.multiplexing_changed:
        return list(columns), sorted(observed_set)

    assert stats.derived_signal_fields is not None
    kept = []  # type: typing.List[str]
    values_union = set()  # type: typing.Set[int]
    for column in columns:
        affected_fields = (
            set(stats.derived_signal_fields[column]) & impact.affected_signals
        )
        column_values = set()  # type: typing.Set[int]
        static_field = False
        for name in affected_fields:
            signal = frame.signal_by_name(name)
            if signal is None:
                # The signal was removed: its branch cannot be proven out of
                # scope from the new dictionary, so keep the column.
                static_field = True
                continue
            branch_values = _signal_branch_values(signal, observed_set)
            if branch_values is None:
                static_field = True
            else:
                column_values.update(branch_values)
        if static_field:
            kept.append(column)
            values_union.update(observed_set)
        elif column_values:
            kept.append(column)
            values_union.update(column_values)
    return kept, (sorted(values_union) if values_union else None)


def _plan_for_partition(stats, impacts, frames_by_id):
    # type: (PartitionStats, typing.List[FrameImpact], typing.Mapping[int, Frame]) -> RecomputeDecision
    decision = RecomputeDecision(partition_id=stats.partition_id, status=UNAFFECTED)
    needs_raw = False

    for impact in impacts:
        if impact.display_only:
            # Renames / metadata-only changes never invalidate stored values.
            continue

        if stats.message_ids is not None and impact.frame_id not in stats.message_ids:
            # Concrete proof that this frame never fed the partition.
            continue

        if not stats.complete:
            needs_raw = True
            decision.reasons.append(REASON_INCOMPLETE_STATS)
            if stats.message_ids is None:
                decision.reasons.append(REASON_UNKNOWN_FRAME_MEMBERSHIP)
            else:
                decision.inspect_message_ids.append(impact.frame_id)
            continue

        if stats.message_ids is None:
            needs_raw = True
            decision.reasons.append(REASON_UNKNOWN_FRAME_MEMBERSHIP)
            continue

        if stats.derived_signal_fields is None:
            needs_raw = True
            decision.reasons.append(REASON_UNKNOWN_DERIVED_FIELDS)
            decision.inspect_message_ids.append(impact.frame_id)
            continue

        columns = _affected_columns(impact, stats)
        if not columns:
            # The frame changed, but no derived column of the partition uses
            # any of the affected signals: the partition stays untouched.
            continue

        columns, mux_values = _reachable_columns(
            impact, frames_by_id.get(impact.frame_id), stats, columns)
        observed = stats.multiplexer_values.get(impact.frame_id)
        if impact.is_multiplexed and observed and not columns:
            # The affected branches were never observed in this partition.
            continue

        decision.items.append(RecomputeItem(
            frame_id=impact.frame_id,
            frame_name=impact.frame_name,
            reasons=list(impact.reasons),
            recompute_columns=columns,
            multiplexer_values=mux_values,
        ))

    if decision.items:
        decision.status = RECOMPUTE
    elif needs_raw:
        decision.status = RECHECK_RAW
        decision.inspect_message_ids = sorted(set(decision.inspect_message_ids))
    else:
        decision.status = UNAFFECTED
    decision.reasons = sorted(set(decision.reasons))
    return decision


def build_recompute_plan(db_old, db_new, partitions):
    # type: (CanMatrix, CanMatrix, typing.Sequence[PartitionStats]) -> RecomputePlan
    """Build the weekend recompute plan for all retained partitions.

    :param db_old: dictionary version the partition data was decoded with.
    :param db_new: new dictionary version to roll out.
    :param partitions: retained per-partition statistics.
    """
    impacts = analyze_matrix_changes(db_old, db_new)
    frames_by_id = {frame.arbitration_id.id: frame for frame in db_new.frames}
    decisions = [
        _plan_for_partition(stats, impacts, frames_by_id)
        for stats in partitions
    ]
    return RecomputePlan(decisions=decisions, impacts=impacts)


def format_plan(plan):  # type: (RecomputePlan) -> str
    """Render a human-readable summary of a recompute plan."""
    lines = []  # type: typing.List[str]
    titles = (
        (RECOMPUTE, "Partitions to recompute"),
        (UNAFFECTED, "Partitions proven unaffected"),
        (RECHECK_RAW, "Partitions needing raw frame replay"),
    )
    for status, title in titles:
        scoped = plan.decisions_by_status(status)
        lines.append("{} ({}):".format(title, len(scoped)))
        for decision in scoped:
            lines.append("  - {}".format(decision.partition_id))
            if decision.reasons:
                lines.append("      reasons: {}".format(", ".join(sorted(set(decision.reasons)))))
            if decision.inspect_message_ids:
                ids_text = ", ".join("0x{:X}".format(item) for item in decision.inspect_message_ids)
                lines.append("      inspect frames: {}".format(ids_text))
            elif status == RECHECK_RAW:
                lines.append("      inspect frames: ALL (frame membership unknown)")
            for item in decision.items:
                lines.append("      frame 0x{:X} ({}):".format(item.frame_id, item.frame_name))
                lines.append("        columns: {}".format(", ".join(item.recompute_columns)))
                if item.multiplexer_values is not None:
                    values_text = ", ".join(str(value) for value in item.multiplexer_values)
                    lines.append("        multiplexer values: {}".format(values_text))
        lines.append("")
    return "\n".join(lines).rstrip()
