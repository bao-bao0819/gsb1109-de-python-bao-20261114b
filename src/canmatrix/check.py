# -*- coding: utf-8 -*-

import itertools
import typing
from builtins import *

import attr


Range = typing.Tuple[int, int]
Ranges = typing.Tuple[Range, ...]

__all__ = [
    "SignalConflict",
    "Conflict",
    "signal_bits",
    "frame_conflicts",
    "matrix_conflicts",
    "check_frame",
    "check_matrix",
    "check_signal_conflicts",
    "find_conflicts",
    "check",
]


class BitPositions(tuple):
    """Tuple of bit positions which also compares equal to a plain list or set."""

    def __eq__(self, other):
        if isinstance(other, (list, set, frozenset)):
            return set(self) == set(other)
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        equal = self.__eq__(other)
        if equal is NotImplemented:
            return NotImplemented
        return not equal

    __hash__ = tuple.__hash__


@attr.s(frozen=True)
class SignalConflict:
    """A physical-wire collision between two signals in one frame.

    ``overlap_bits`` contains zero based physical payload bit positions.
    Bit ``0`` is the least significant bit of byte ``0``; positions then run
    upwards through each byte, matching standard CAN wire numbering.
    ``selector_values`` contains one selector assignment which activates both
    signals at the same time. Selector constraints inherited from parent
    multiplexers are included.
    """

    frame = attr.ib(default=None)  # type: typing.Any
    signal1 = attr.ib(default=None)  # type: typing.Any
    signal2 = attr.ib(default=None)  # type: typing.Any
    overlap_bits = attr.ib(factory=tuple)  # type: typing.Tuple[int, ...]
    selector_values = attr.ib(factory=dict)  # type: typing.Mapping[str, int]

    @property
    def frame_name(self):  # type: () -> str
        return getattr(self.frame, "name", str(self.frame))

    @property
    def signal1_name(self):  # type: () -> str
        return getattr(self.signal1, "name", str(self.signal1))

    @property
    def signal2_name(self):  # type: () -> str
        return getattr(self.signal2, "name", str(self.signal2))

    @property
    def signals(self):  # type: () -> typing.Tuple[typing.Any, typing.Any]
        return self.signal1, self.signal2

    @property
    def signal_names(self):  # type: () -> typing.Tuple[str, str]
        return self.signal1_name, self.signal2_name

    @property
    def first_signal(self):  # type: typing.Any
        return self.signal1

    @property
    def second_signal(self):  # type: typing.Any
        return self.signal2

    @property
    def bits(self):  # type: () -> typing.Tuple[int, ...]
        return self.overlap_bits

    @property
    def bit_positions(self):  # type: () -> typing.Tuple[int, ...]
        return self.overlap_bits

    @property
    def overlapping_bits(self):  # type: () -> typing.Tuple[int, ...]
        return self.overlap_bits

    @property
    def wire_bits(self):  # type: () -> typing.Tuple[int, ...]
        return self.overlap_bits

    @property
    def selectors(self):  # type: () -> typing.Mapping[str, int]
        return self.selector_values

    @property
    def selector(self):  # type: () -> typing.Mapping[str, int]
        return self.selector_values

    @property
    def triggerable_selectors(self):  # type: () -> typing.Mapping[str, int]
        return self.selector_values

    @property
    def trigger_values(self):  # type: () -> typing.Mapping[str, int]
        return self.selector_values

    @property
    def overlap_bit_ranges(self):  # type: () -> typing.Tuple[Range, ...]
        ranges = []
        for bit in self.overlap_bits:
            if ranges and bit == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], bit)
            else:
                ranges.append((bit, bit))
        return tuple(ranges)

    def as_dict(self):  # type: () -> dict
        return {
            "frame": self.frame_name,
            "signal1": self.signal1_name,
            "signal2": self.signal2_name,
            "signals": list(self.signal_names),
            "overlap_bits": list(self.overlap_bits),
            "selector_values": dict(self.selector_values),
        }

    def __str__(self):
        bits = ",".join(str(bit) for bit in self.overlap_bits)
        selectors = ",".join(
            "{}={}".format(name, value)
            for name, value in sorted(self.selector_values.items())
        )
        return "{}: {} <-> {} overlap bits [{}]; selectors {}".format(
            self.frame_name, self.signal1_name, self.signal2_name, bits, selectors
        )


@attr.s(frozen=True)
class _Condition:
    """A conjunction of selector ranges.

    Each selector is independently constrained to one of its declared
    ranges. Keeping ranges (rather than every possible value) avoids
    expanding wide multiplexer fields.
    """

    ranges = attr.ib(factory=dict)  # type: typing.Mapping[str, Ranges]

    def intersect(self, other):
        # type: (_Condition) -> typing.Optional[_Condition]
        ranges = dict(self.ranges)
        for name, other_ranges in other.ranges.items():
            if name in ranges:
                common = _intersect_ranges(ranges[name], other_ranges)
                if not common:
                    return None
                ranges[name] = common
            else:
                ranges[name] = other_ranges
        return _Condition(ranges)


_EMPTY_CONDITION = _Condition()


def _normalize_range(mux_value):  # type: (typing.Any) -> Range
    if isinstance(mux_value, (list, tuple)):
        if len(mux_value) == 1:
            value = int(mux_value[0])
            return value, value
        return int(mux_value[0]), int(mux_value[1])
    value = int(mux_value)
    return value, value


def _declared_ranges(signal):  # type: (typing.Any) -> typing.Optional[Ranges]
    """Return declared selector ranges, or None for an unconditioned signal."""
    if signal.mux_val_grp:
        return tuple(_normalize_range(mux_value) for mux_value in signal.mux_val_grp)
    if signal.mux_val is None:
        return None
    return ((int(signal.mux_val), int(signal.mux_val)),)


def _intersect_ranges(first, second):  # type: (Ranges, Ranges) -> Ranges
    intersections = []
    for first_min, first_max in first:
        for second_min, second_max in second:
            lower = max(first_min, second_min)
            upper = min(first_max, second_max)
            if lower <= upper:
                intersections.append((lower, upper))
    return tuple(sorted(set(intersections)))


def _valid_ranges(ranges):  # type: (typing.Optional[Ranges]) -> typing.Optional[Ranges]
    if ranges is None:
        return None
    valid = tuple(sorted(set(
        (int(lower), int(upper))
        for lower, upper in ranges
        if int(lower) <= int(upper)
    )))
    return valid if valid else tuple()


def _root_multiplexer(frame):  # type: (typing.Any) -> typing.Optional[typing.Any]
    roots = [
        candidate
        for candidate in frame.signals
        if candidate.is_multiplexer and candidate.muxer_for_signal is None
    ]
    if len(roots) == 1:
        return roots[0]
    return None


def _infer_selector(signal, frame):  # type: (typing.Any, typing.Any) -> typing.Optional[str]
    """Infer the parent multiplexer for dictionaries without explicit links."""
    if not signal.mux_val_grp and signal.mux_val is None:
        return None
    root = _root_multiplexer(frame)
    if root is not None and root is not signal:
        return root.name
    return None


def _condition_for_signal(signal, frame, selector_name):
    # type: (typing.Any, typing.Any, typing.Optional[str]) -> typing.Optional[_Condition]
    ranges = _valid_ranges(_declared_ranges(signal))
    if ranges is None:
        if selector_name is not None:
            # A selector link without declared selector values cannot be
            # proven to activate this signal together with another one.
            return None
        return _EMPTY_CONDITION
    if not ranges:
        return None
    selector_name = selector_name or _infer_selector(signal, frame)
    if selector_name is None:
        # A signal with selector values but no resolvable selector cannot be
        # proven to be simultaneously active with another signal.
        return None
    return _Condition({selector_name: ranges})


def _is_selector(signal, frame):  # type: (typing.Any, typing.Any) -> bool
    """Check whether a signal controls at least one other signal."""
    if signal is None:
        return False
    if signal.is_multiplexer:
        return True
    return any(
        candidate.muxer_for_signal == signal.name
        for candidate in frame.signals
    )


def _ancestor_condition(signal, frame):  # type: (typing.Any, typing.Any) -> typing.Optional[_Condition]
    """Return all selector constraints needed to activate a signal."""
    condition = _condition_for_signal(signal, frame, signal.muxer_for_signal)
    if condition is None:
        return None

    seen = {id(signal)}
    current = signal
    while True:
        selector_name = current.muxer_for_signal or _infer_selector(current, frame)
        if selector_name is None:
            return condition
        selector = frame.signal_by_name(selector_name)
        if selector is None or id(selector) in seen or not _is_selector(selector, frame):
            return None
        seen.add(id(selector))
        selector_condition = _condition_for_signal(selector, frame, selector.muxer_for_signal)
        if selector_condition is None:
            return None
        condition = condition.intersect(selector_condition)
        if condition is None:
            return None
        current = selector


def _conditions_compatible(first, second):  # type: (_Condition, _Condition) -> bool
    for name in set(first.ranges) & set(second.ranges):
        if not _intersect_ranges(first.ranges[name], second.ranges[name]):
            return False
    return True


def _representative_values(first, second):  # type: (_Condition, _Condition) -> dict
    """Pick one value per selector which activates both signals."""
    values = {}
    for name in set(first.ranges) | set(second.ranges):
        if name in first.ranges and name in second.ranges:
            common = _intersect_ranges(first.ranges[name], second.ranges[name])
            values[name] = min(lower for lower, _upper in common)
        elif name in first.ranges:
            values[name] = min(lower for lower, _upper in first.ranges[name])
        else:
            values[name] = min(lower for lower, _upper in second.ranges[name])
    return values


def signal_bits(signal, frame):  # type: (typing.Any, typing.Any) -> typing.Tuple[int, ...]
    """Return the payload bits a signal really occupies on the wire.

    Bit ``0`` is the least significant bit of byte ``0``; bits then run
    upwards through each byte, matching standard CAN wire numbering. Only
    bits inside the declared frame payload are reported, so malformed
    signals cannot create phantom overlaps.
    """
    size = int(signal.size)
    start = int(signal.start_bit)
    total = int(frame.size) * 8
    if size <= 0 or start < 0 or start >= total:
        return BitPositions(())
    end = min(start + size, total)
    if signal.is_little_endian:
        bits = set(range(start, end))
    else:
        bits = {
            (position // 8) * 8 + 7 - (position % 8)
            for position in range(start, end)
        }
    return BitPositions(sorted(bit for bit in bits if 0 <= bit < total))


def frame_conflicts(frame):  # type: (typing.Any) -> typing.Sequence[SignalConflict]
    """Find physical collisions which can occur within one frame.

    Signals are compared on their actual payload bits. Both signals must have
    at least one common selector assignment, including constraints inherited
    from parent multiplexers. Signals in mutually exclusive multiplex branches
    therefore do not create a conflict, even when they share a start bit.
    """
    conflicts = []
    signals = [signal for signal in frame.signals if int(signal.size) > 0]
    bits = {id(signal): signal_bits(signal, frame) for signal in signals}
    conditions = {}
    for first, second in itertools.combinations(signals, 2):
        if first is second:
            continue
        overlap = sorted(set(bits[id(first)]) & set(bits[id(second)]))
        if not overlap:
            continue
        if id(first) not in conditions:
            conditions[id(first)] = _ancestor_condition(first, frame)
        if id(second) not in conditions:
            conditions[id(second)] = _ancestor_condition(second, frame)
        first_condition = conditions[id(first)]
        second_condition = conditions[id(second)]
        if first_condition is None or second_condition is None:
            continue
        if not _conditions_compatible(first_condition, second_condition):
            continue
        conflicts.append(SignalConflict(
            frame,
            first,
            second,
            BitPositions(overlap),
            _representative_values(first_condition, second_condition),
        ))
    return conflicts


def matrix_conflicts(matrix):  # type: (typing.Any) -> typing.Sequence[SignalConflict]
    """Return all physical signal conflicts of all frames in a matrix."""
    return [conflict for frame in matrix.frames for conflict in frame_conflicts(frame)]


def check(obj):  # type: (typing.Any) -> typing.Sequence[SignalConflict]
    """Check either a :class:`CanMatrix` or a :class:`Frame` for conflicts."""
    if hasattr(obj, "frames"):
        return matrix_conflicts(obj)
    if hasattr(obj, "signals"):
        return frame_conflicts(obj)
    raise TypeError("expected a CanMatrix or Frame, got {!r}".format(type(obj)))


Conflict = SignalConflict
check_frame = frame_conflicts
check_matrix = matrix_conflicts
check_signal_conflicts = matrix_conflicts
find_conflicts = matrix_conflicts
