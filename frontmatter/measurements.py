# -*- coding: utf-8 -*-
"""
Normalize measurement values found in experiment-note frontmatter.

Measurements show up in headers in a few shapes: a value with an
uncertainty (``2.50±0.02 cm``), a pair of limits (``2.48–2.52 cm``),
or a bare value that inherits its unit from the sample group it
belongs to. This module parses those strings and exports numeric
intervals in a unified dimension, driven by a caller-supplied unit
table. The original string and its significant figures are preserved
on every result.

.. testsetup:: *

    from frontmatter.measurements import UnitTable, MeasurementCollector

.. doctest::

    >>> table = UnitTable({
    ...     "m":  ("length", "1"),
    ...     "cm": ("length", "0.01"),
    ...     "mm": ("length", "0.001"),
    ...     "K":  ("temperature", "1"),
    ...     "°C": ("temperature", "1", "273.15"),
    ... })
    >>> collector = MeasurementCollector(table, unit="cm")
    >>> interval = collector.collect("2.50±0.02 cm")
    >>> interval.lower, interval.upper, interval.unit
    (Decimal('0.0248'), Decimal('0.0252'), 'm')

A sample may temporarily switch units; siblings are not affected:

.. doctest::

    >>> collector.collect("25.0±0.2 mm").upper
    Decimal('0.0252')
    >>> collector.collect("2.50±0.02").unit  # still the group unit
    'm'

Units with an offset (like °C) convert absolute values with
``scale * x + offset`` but errors/widths with ``scale * x``:

.. doctest::

    >>> temps = MeasurementCollector(table, unit="°C")
    >>> interval = temps.collect("20.0±0.5 °C")
    >>> interval.lower, interval.upper
    (Decimal('292.65'), Decimal('293.65'))

When the requested result precision is lower than the input's,
endpoints are rounded outward so the interval never shrinks:

.. doctest::

    >>> collector.collect("2.547±0.001 cm", sig_figs=2).lower
    Decimal('0.025')
    >>> collector.collect("2.547±0.001 cm", sig_figs=2).upper
    Decimal('0.026')
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Iterable, Mapping, Union

__all__ = [
    "Measurement",
    "MeasurementCollector",
    "NormalizedInterval",
    "UnitTable",
    "normalize",
    "parse_measurement",
]

DecimalLike = Union[str, int, float, Decimal]
UnitSpec = Union[
    "tuple[str, DecimalLike]",
    "tuple[str, DecimalLike, DecimalLike]",
    Mapping[str, object],
]

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"

_PM_RE = re.compile(
    rf"^\s*(?P<value>{_NUMBER})\s*(?:±|\+/-)\s*(?P<error>{_NUMBER})"
    rf"(?:\s*(?P<unit>\S.*?))?\s*$"
)
_LIMITS_RE = re.compile(
    rf"^\s*(?P<lower>{_NUMBER})\s*(?:\.\.|–|—|~|to)\s*(?P<upper>{_NUMBER})"
    rf"(?:\s*(?P<unit>\S.*?))?\s*$"
)
_VALUE_RE = re.compile(rf"^\s*(?P<value>{_NUMBER})(?:\s*(?P<unit>\S.*?))?\s*$")


def _to_decimal(value: DecimalLike) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _sig_figs(token: str) -> int:
    "Count significant figures in a numeric token, e.g. '2.50' -> 3."
    text = token.lstrip("+-")
    if "." in text:
        whole, frac = text.split(".", 1)
        if whole.strip("0"):
            digits = whole.lstrip("0") + frac
        else:
            digits = frac.lstrip("0")
        return len(digits) or 1
    digits = text.lstrip("0")
    if not digits:
        return 1
    # trailing zeros are ambiguous without a decimal point
    return len(digits.rstrip("0")) or 1


def _quantize_sig_figs(value: Decimal, sig_figs: int, rounding: str) -> Decimal:
    if sig_figs < 1:
        raise ValueError("sig_figs must be >= 1")
    if value.is_zero():
        return value
    exponent = value.adjusted() - sig_figs + 1
    return value.quantize(Decimal(1).scaleb(exponent), rounding=rounding)


def _quantize_places(value: Decimal, places: int, rounding: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=rounding)


@dataclass(frozen=True)
class _Unit:
    dimension: str
    scale: Decimal
    offset: Decimal


class UnitTable:
    """
    A table of units and how to convert them into a base unit per dimension.

    ``units`` maps a unit name to ``(dimension, scale)`` or
    ``(dimension, scale, offset)`` (mappings with ``dimension``/``scale``/
    ``offset`` keys work, too). Scales and offsets are relative to the
    dimension's reference unit (the one with scale 1 and offset 0).
    Results are expressed in the dimension's base unit: the first unit
    registered for a dimension, unless ``base_units`` overrides that.
    """

    def __init__(
        self,
        units: Mapping[str, UnitSpec],
        base_units: Mapping[str, str] | None = None,
    ) -> None:
        self._units: dict[str, _Unit] = {}
        self._base_units: dict[str, str] = {}
        for name, spec in units.items():
            unit = self._build_unit(spec)
            self._units[name] = unit
            self._base_units.setdefault(unit.dimension, name)
        for dimension, name in (base_units or {}).items():
            if name not in self._units:
                raise KeyError(f"unknown base unit: {name!r}")
            if self._units[name].dimension != dimension:
                raise ValueError(f"unit {name!r} is not in dimension {dimension!r}")
            self._base_units[dimension] = name

    @staticmethod
    def _build_unit(spec: UnitSpec) -> _Unit:
        if isinstance(spec, Mapping):
            dimension = str(spec["dimension"])
            scale = _to_decimal(spec.get("scale", 1))  # type: ignore[arg-type]
            offset = _to_decimal(spec.get("offset", 0))  # type: ignore[arg-type]
        else:
            parts = list(spec)
            dimension = str(parts[0])
            scale = _to_decimal(parts[1]) if len(parts) > 1 else Decimal(1)
            offset = _to_decimal(parts[2]) if len(parts) > 2 else Decimal(0)
        return _Unit(dimension=dimension, scale=scale, offset=offset)

    def __contains__(self, unit: str) -> bool:
        return unit in self._units

    def dimension(self, unit: str) -> str:
        "Dimension (e.g. 'length') a unit belongs to."
        return self._units[unit].dimension

    def base_unit(self, dimension: str) -> str:
        "Base unit name for a dimension."
        return self._base_units[dimension]

    def base_unit_for(self, unit: str) -> str:
        "Base unit name for the dimension a unit belongs to."
        return self._base_units[self._units[unit].dimension]

    def _base(self, dimension: str) -> _Unit:
        return self._units[self._base_units[dimension]]

    def convert_value(self, value: DecimalLike, unit: str) -> Decimal:
        """
        Convert an absolute value into the dimension's base unit.
        Offset units apply ``scale * value + offset``.
        """
        spec = self._units[unit]
        base = self._base(spec.dimension)
        canonical = spec.scale * _to_decimal(value) + spec.offset
        return (canonical - base.offset) / base.scale

    def convert_error(self, error: DecimalLike, unit: str) -> Decimal:
        """
        Convert an uncertainty or interval width into the base unit.
        Offset units apply ``scale * error`` only — never the offset.
        """
        spec = self._units[unit]
        base = self._base(spec.dimension)
        return spec.scale * _to_decimal(error) / base.scale


@dataclass
class Measurement:
    """
    A parsed measurement string.

    ``lower``/``upper`` are in ``unit`` (which may be inherited from the
    sample group). ``value``/``error`` are only set for the ``±`` form.
    ``raw`` is the original string, ``sig_figs`` its significant figures.
    """

    raw: str
    lower: Decimal
    upper: Decimal
    unit: str | None
    value: Decimal | None = None
    error: Decimal | None = None
    sig_figs: int = 1


@dataclass
class NormalizedInterval:
    """
    A measurement exported as a numeric interval in the base unit of its
    dimension. ``raw`` keeps the original string and ``sig_figs`` the
    significant figures it carried (or the override used for rounding).
    """

    raw: str
    lower: Decimal
    upper: Decimal
    unit: str
    sig_figs: int

    def to_dict(self) -> dict[str, object]:
        "JSON-friendly export of the interval."
        return {
            "raw": self.raw,
            "lower": float(self.lower),
            "upper": float(self.upper),
            "unit": self.unit,
            "sig_figs": self.sig_figs,
        }


def parse_measurement(text: str, unit: str | None = None) -> Measurement:
    """
    Parse a measurement string: ``2.50±0.02 cm``, ``2.48–2.52 cm``
    (also ``..``, ``~`` or ``to`` between limits) or a bare ``2.50 cm``.
    A unit written in the string wins; otherwise ``unit`` (typically the
    sample group's unit) is used.
    """
    match = _PM_RE.match(text)
    if match:
        value = Decimal(match["value"])
        error = Decimal(match["error"])
        return Measurement(
            raw=text,
            lower=value - error,
            upper=value + error,
            unit=match["unit"] or unit,
            value=value,
            error=error,
            sig_figs=_sig_figs(match["value"]),
        )

    match = _LIMITS_RE.match(text)
    if match:
        lower = Decimal(match["lower"])
        upper = Decimal(match["upper"])
        if lower > upper:
            raise ValueError(f"lower limit exceeds upper limit: {text!r}")
        return Measurement(
            raw=text,
            lower=lower,
            upper=upper,
            unit=match["unit"] or unit,
            sig_figs=min(_sig_figs(match["lower"]), _sig_figs(match["upper"])),
        )

    match = _VALUE_RE.match(text)
    if match:
        value = Decimal(match["value"])
        return Measurement(
            raw=text,
            lower=value,
            upper=value,
            unit=match["unit"] or unit,
            value=value,
            sig_figs=_sig_figs(match["value"]),
        )

    raise ValueError(f"cannot parse measurement: {text!r}")


def normalize(
    measurement: Measurement,
    table: UnitTable,
    sig_figs: int | None = None,
    places: int | None = None,
) -> NormalizedInterval:
    """
    Convert a parsed measurement into the base unit of its dimension.

    Endpoints are exact unless ``sig_figs`` or ``places`` asks for a
    lower precision, in which case the lower endpoint rounds down and
    the upper endpoint rounds up — the interval never silently shrinks.
    """
    if sig_figs is not None and places is not None:
        raise ValueError("pass sig_figs or places, not both")
    if measurement.unit is None:
        raise ValueError(
            f"measurement has no unit and none was inherited: {measurement.raw!r}"
        )

    unit = measurement.unit
    if measurement.value is not None and measurement.error is not None:
        center = table.convert_value(measurement.value, unit)
        half = table.convert_error(measurement.error, unit)
        lower, upper = center - half, center + half
    else:
        lower = table.convert_value(measurement.lower, unit)
        upper = table.convert_value(measurement.upper, unit)

    figures = sig_figs if sig_figs is not None else measurement.sig_figs
    if places is not None:
        lower = _quantize_places(lower, places, ROUND_FLOOR)
        upper = _quantize_places(upper, places, ROUND_CEILING)
    elif sig_figs is not None:
        lower = _quantize_sig_figs(lower, figures, ROUND_FLOOR)
        upper = _quantize_sig_figs(upper, figures, ROUND_CEILING)

    return NormalizedInterval(
        raw=measurement.raw,
        lower=lower,
        upper=upper,
        unit=table.base_unit_for(unit),
        sig_figs=figures,
    )


class MeasurementCollector:
    """
    Collection export for a sample group sharing a unit table.

    ``unit`` is the group's default unit, applied to samples that don't
    name one. A unit named inside a sample string — or passed to
    :py:meth:`collect` — applies to that sample only and never leaks
    into sibling samples.
    """

    def __init__(
        self,
        table: UnitTable,
        unit: str | None = None,
        sig_figs: int | None = None,
        places: int | None = None,
    ) -> None:
        if sig_figs is not None and places is not None:
            raise ValueError("pass sig_figs or places, not both")
        self.table = table
        self.unit = unit
        self.sig_figs = sig_figs
        self.places = places

    def collect(
        self,
        text: str,
        unit: str | None = None,
        sig_figs: int | None = None,
        places: int | None = None,
    ) -> NormalizedInterval:
        "Parse one sample and export its interval in the base unit."
        measurement = parse_measurement(
            text, unit=unit if unit is not None else self.unit
        )
        return normalize(
            measurement,
            self.table,
            sig_figs=sig_figs if sig_figs is not None else self.sig_figs,
            places=places if places is not None else self.places,
        )

    def collect_all(self, texts: Iterable[str]) -> list[NormalizedInterval]:
        "Export a whole group of samples, in order."
        return [self.collect(text) for text in texts]
