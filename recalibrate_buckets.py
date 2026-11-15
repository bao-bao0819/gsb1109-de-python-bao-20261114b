"""Recount out-of-limit frames after a recalibration, from bucketed data only.

Scenario
--------
The raw CAN frames are gone.  What remains is a histogram: temperature
intervals computed with the *old* calibration (phys = factor * raw + offset)
plus the number of frames that fell into each interval.  A *new* calibration
(new factor / new offset) and a new limit are now in force, and we want to
know how many frames exceed the limit under the new calibration.

Method
------
For every old interval we reconstruct the exact set of integer raw codes
that could have landed in it:

* the old physical interval is mapped back to raw code bounds with exact
  rational arithmetic (a negative factor flips the inequalities);
* the result is intersected with the raw domain implied by the signal
  (bit length + signedness);
* reserved raw codes (error / SNA markers) are dropped -- they are not
  measurements and can never be represented in the bucket counts.

Each usable raw code is then re-evaluated under the new calibration and
checked against the new limit.  A bucket contributes

* its full count to the provable minimum if *every* usable raw code is
  over the limit,
* its full count to the provable maximum if *any* usable raw code is over
  the limit.

Consequently a bucket whose interval straddles the limit still yields an
exact figure whenever the side beyond the limit contains no usable raw
code (e.g. only reserved codes live there).

Usage
-----
    python recalibrate_buckets.py spec.json
    python recalibrate_buckets.py --selftest

Spec format (JSON)::

    {
      "old":  {"factor": "0.1", "offset": "-40", "bits": 16, "signed": false,
               "reserved": ["0xFFFF"]},
      "new":  {"factor": "-0.05", "offset": "120", "bits": 16,
               "signed": false, "reserved": ["0xFFFF"]},
      "limit": {"value": "85", "direction": "above", "inclusive": false},
      "buckets": [{"lo": "20", "hi": "30", "count": 12},
                  {"lo": "30", "hi": "40", "count": 7, "hi_inclusive": true}]
    }

Factors/offsets/limits are parsed through ``Fraction(str(...))`` so decimal
strings stay exact.  Buckets default to [lo, hi).  ``direction`` is
``"above"`` (default) or ``"below"``; ``inclusive`` turns ``>`` into ``>=``
(resp. ``<`` into ``<=``).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import FrozenSet, List, Optional, Tuple


def _frac(value) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def _parse_reserved(entries) -> FrozenSet[int]:
    codes = set()
    for entry in entries or []:
        if isinstance(entry, int):
            codes.add(entry)
            continue
        text = str(entry).strip()
        if "-" in text[1:]:
            lo_text, hi_text = text.split("-", 1) if not text.startswith("-") else text[1:].split("-", 1)
            lo, hi = int(lo_text, 0), int(hi_text, 0)
            codes.update(range(lo, hi + 1))
        else:
            codes.add(int(text, 0))
    return frozenset(codes)


@dataclass
class Calibration:
    """One signal definition: phys = factor * raw + offset."""

    factor: Fraction
    offset: Fraction
    bits: int
    signed: bool = False
    reserved: FrozenSet[int] = field(default_factory=frozenset)

    def __post_init__(self):
        if self.factor == 0:
            raise ValueError("factor must be non-zero")

    @classmethod
    def from_dict(cls, spec) -> "Calibration":
        return cls(
            factor=_frac(spec["factor"]),
            offset=_frac(spec.get("offset", 0)),
            bits=int(spec["bits"]),
            signed=bool(spec.get("signed", False)),
            reserved=_parse_reserved(spec.get("reserved")),
        )

    @property
    def domain(self) -> Tuple[int, int]:
        if self.signed:
            return -(1 << (self.bits - 1)), (1 << (self.bits - 1)) - 1
        return 0, (1 << self.bits) - 1

    def phys(self, raw: int) -> Fraction:
        return self.factor * raw + self.offset


def _bound_on_raw(cal: Calibration, phys: Fraction, inclusive: bool, is_lower: bool) -> int:
    """Integer raw bound implied by one side of a physical interval.

    is_lower=True  -> constraint  phys(raw) >= phys  (or > if not inclusive),
                      returns the smallest admissible raw code.
    is_lower=False -> constraint  phys(raw) <= phys  (or < if not inclusive),
                      returns the largest admissible raw code.
    A negative factor flips the inequality, hence the floor/ceil choice.
    """
    edge = (phys - cal.offset) / cal.factor
    forward = cal.factor > 0
    if is_lower:
        if inclusive:
            return math.ceil(edge) if forward else math.floor(edge)
        return math.floor(edge) + 1 if forward else math.ceil(edge) - 1
    else:
        if inclusive:
            return math.floor(edge) if forward else math.ceil(edge)
        return math.ceil(edge) - 1 if forward else math.floor(edge) + 1


def raw_range_for_interval(
    cal: Calibration,
    lo: Fraction,
    hi: Fraction,
    lo_inclusive: bool = True,
    hi_inclusive: bool = False,
) -> Optional[Tuple[int, int]]:
    """Inclusive integer raw-code range mapping into [lo, hi], or None."""
    if cal.factor > 0:
        raw_lo = _bound_on_raw(cal, lo, lo_inclusive, is_lower=True)
        raw_hi = _bound_on_raw(cal, hi, hi_inclusive, is_lower=False)
    else:
        # negative factor: larger phys comes from smaller raw codes
        raw_lo = _bound_on_raw(cal, hi, hi_inclusive, is_lower=False)
        raw_hi = _bound_on_raw(cal, lo, lo_inclusive, is_lower=True)
    dom_lo, dom_hi = cal.domain
    raw_lo, raw_hi = max(raw_lo, dom_lo), min(raw_hi, dom_hi)
    if raw_lo > raw_hi:
        return None
    return raw_lo, raw_hi


def limit_raw_region(cal: Calibration, limit: Fraction, direction: str, inclusive: bool) -> Tuple[int, int]:
    """Inclusive raw-code region that violates the limit under ``cal``."""
    dom_lo, dom_hi = cal.domain
    if direction == "above":
        bound = _bound_on_raw(cal, limit, inclusive, is_lower=True)
        if cal.factor > 0:
            return max(bound, dom_lo), dom_hi
        return dom_lo, min(bound, dom_hi)
    if direction == "below":
        bound = _bound_on_raw(cal, limit, inclusive, is_lower=False)
        if cal.factor > 0:
            return dom_lo, min(bound, dom_hi)
        return max(bound, dom_lo), dom_hi
    raise ValueError("direction must be 'above' or 'below'")


def _has_usable(raw_lo: int, raw_hi: int, reserved: FrozenSet[int]) -> bool:
    """True if [raw_lo, raw_hi] holds at least one non-reserved code."""
    if raw_lo > raw_hi:
        return False
    if raw_hi - raw_lo + 1 > len(reserved):
        return True
    return any(code not in reserved for code in range(raw_lo, raw_hi + 1))


@dataclass
class BucketResult:
    lo: Fraction
    hi: Fraction
    count: int
    raw_range: Optional[Tuple[int, int]]
    usable_codes: bool
    usable_over: bool
    usable_not_over: bool
    min_over: int
    max_over: int

    @property
    def exact(self) -> bool:
        return self.min_over == self.max_over


def analyse_bucket(
    old: Calibration,
    new: Calibration,
    limit: Fraction,
    direction: str,
    inclusive: bool,
    lo: Fraction,
    hi: Fraction,
    count: int,
    lo_inclusive: bool = True,
    hi_inclusive: bool = False,
) -> BucketResult:
    raw_range = raw_range_for_interval(old, lo, hi, lo_inclusive, hi_inclusive)
    over_lo, over_hi = limit_raw_region(new, limit, direction, inclusive)

    if raw_range is None:
        return BucketResult(lo, hi, count, None, False, False, False, 0, 0)

    raw_lo, raw_hi = raw_range
    reserved = old.reserved | new.reserved

    usable = _has_usable(raw_lo, raw_hi, reserved)
    inter_lo, inter_hi = max(raw_lo, over_lo), min(raw_hi, over_hi)
    usable_over = _has_usable(inter_lo, inter_hi, reserved)
    usable_not_over = (
        _has_usable(raw_lo, min(raw_hi, over_lo - 1), reserved)
        or _has_usable(max(raw_lo, over_hi + 1), raw_hi, reserved)
    )

    min_over = count if usable and not usable_not_over else 0
    max_over = count if usable_over else 0
    return BucketResult(lo, hi, count, raw_range, usable, usable_over, usable_not_over,
                        min_over, max_over)


def analyse(spec) -> dict:
    old = Calibration.from_dict(spec["old"])
    new = Calibration.from_dict(spec["new"])
    limit_spec = spec["limit"]
    limit = _frac(limit_spec["value"])
    direction = limit_spec.get("direction", "above")
    inclusive = bool(limit_spec.get("inclusive", False))

    buckets: List[BucketResult] = []
    for entry in spec["buckets"]:
        buckets.append(analyse_bucket(
            old, new, limit, direction, inclusive,
            lo=_frac(entry["lo"]),
            hi=_frac(entry["hi"]),
            count=int(entry["count"]),
            lo_inclusive=bool(entry.get("lo_inclusive", True)),
            hi_inclusive=bool(entry.get("hi_inclusive", False)),
        ))

    return {
        "min_over_limit": sum(b.min_over for b in buckets),
        "max_over_limit": sum(b.max_over for b in buckets),
        "buckets": [
            {
                "interval": [str(b.lo), str(b.hi)],
                "count": b.count,
                "raw_codes": list(b.raw_range) if b.raw_range else None,
                "usable_codes": b.usable_codes,
                "usable_over_limit": b.usable_over,
                "usable_not_over_limit": b.usable_not_over,
                "min_over": b.min_over,
                "max_over": b.max_over,
                "exact": b.exact,
            }
            for b in buckets
        ],
    }


def _selftest() -> None:
    cal = Calibration(factor=Fraction(1, 10), offset=Fraction(-40), bits=16)
    # phys 20.0 <= t < 30.0  <->  raw 600..699
    assert raw_range_for_interval(cal, Fraction(20), Fraction(30)) == (600, 699)
    # negative factor flips the order: phys = -0.5 * raw + 100
    # 20 <= phys < 30  <->  140 < raw <= 160
    neg = Calibration(factor=Fraction(-1, 2), offset=Fraction(100), bits=8)
    assert raw_range_for_interval(neg, Fraction(20), Fraction(30)) == (141, 160)
    # signed domain clips the range
    sig = Calibration(factor=Fraction(1), offset=Fraction(0), bits=8, signed=True)
    assert raw_range_for_interval(sig, Fraction(-200), Fraction(200)) == (-128, 127)
    # limit region under a negative factor: phys > 85 means raw < 700
    neg_new = Calibration(factor=Fraction(-1, 20), offset=Fraction(120), bits=16)
    assert limit_raw_region(neg_new, Fraction(85), "above", False) == (0, 699)
    assert limit_raw_region(neg_new, Fraction(85), "below", False) == (701, 65535)
    # reserved codes can make a straddling bucket exact
    spec = {
        "old": {"factor": "1", "offset": "0", "bits": 8, "signed": False,
                "reserved": ["254", "255"]},
        "new": {"factor": "1", "offset": "0", "bits": 8, "signed": False,
                "reserved": ["254", "255"]},
        "limit": {"value": "250", "direction": "above"},
        "buckets": [{"lo": "248", "hi": "256", "count": 5, "hi_inclusive": True}],
    }
    result = analyse(spec)
    # raw 251..255 would exceed, but 254/255 are reserved and 251..253 are
    # usable -> mixed bucket, min 0 / max 5
    assert (result["min_over_limit"], result["max_over_limit"]) == (0, 5)
    spec["buckets"] = [{"lo": "254", "hi": "256", "count": 5, "hi_inclusive": True}]
    result = analyse(spec)
    # every code past the limit is reserved -> exact zero despite straddling
    assert (result["min_over_limit"], result["max_over_limit"]) == (0, 0)
    spec["buckets"] = [{"lo": "240", "hi": "244", "count": 3}]
    result = analyse(spec)
    # fully below the limit
    assert (result["min_over_limit"], result["max_over_limit"]) == (0, 0)
    spec["buckets"] = [{"lo": "251", "hi": "253", "count": 4, "hi_inclusive": True}]
    result = analyse(spec)
    # fully above the limit
    assert (result["min_over_limit"], result["max_over_limit"]) == (4, 4)
    print("selftest ok")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", nargs="?", help="JSON spec file")
    parser.add_argument("--selftest", action="store_true", help="run built-in checks")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if not args.spec:
        parser.error("a spec file is required (or use --selftest)")
    with open(args.spec, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    print(json.dumps(analyse(spec), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
