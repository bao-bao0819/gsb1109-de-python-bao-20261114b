# -*- coding: utf-8 -*-
"""
Type-preserving JSON encoding for front matter metadata.

JSON has a small set of scalar types, while YAML and TOML can express dates,
datetimes, times, arbitrary-size integers, decimals, byte strings and more.
Round-tripping those values through JSON requires saying, for a handful of
values, what the original Python type was.

This module keeps the common case plain: strings, booleans, ordinary integers,
floats and nulls stay untouched. Only values whose type would otherwise be
lost are listed in a separate ``types`` mapping, keyed by JSON Pointer. The
mapping is kept outside the metadata object, so no metadata key can collide
with an annotation key.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import math

from collections.abc import Mapping

__all__ = [
    "SerializationError",
    "UnsupportedTypeError",
    "MAX_SAFE_INTEGER",
    "encode_metadata",
    "decode_metadata",
]


# JavaScript (and therefore most JSON consumers) can represent integers up to
# this value exactly. Larger integers are stored as strings.
MAX_SAFE_INTEGER = 2**53 - 1


class SerializationError(ValueError):
    """Raised when a typed metadata document is malformed."""


class UnsupportedTypeError(SerializationError, TypeError):
    """Raised when metadata contains a value JSON cannot represent."""


def _pointer(path: str, token: str | int) -> str:
    "Append a token to a JSON Pointer path."
    text = str(token).replace("~", "~0").replace("/", "~1")
    return f"{path}/{text}"


def _encode_value(value: object, path: str, types: dict[str, str]) -> object:
    # bool must be checked before int, because bool is a subclass of int.
    if value is None or isinstance(value, (str, bool)):
        return value

    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            types[path] = "bigint"
            return str(value)
        return value

    if isinstance(value, float):
        # JSON has one number type. Finite floats keep their JSON spelling but
        # are tagged so a reader can tell 1.0 (a decimal) from 1 (an integer).
        # NaN and infinities are not valid JSON and become strings.
        types[path] = "float"
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value

    if isinstance(value, decimal.Decimal):
        types[path] = "decimal"
        return str(value)

    # datetime is a subclass of date, so test it first.
    if isinstance(value, datetime.datetime):
        types[path] = "datetime"
        return value.isoformat()

    if isinstance(value, datetime.date):
        types[path] = "date"
        return value.isoformat()

    if isinstance(value, datetime.time):
        types[path] = "time"
        return value.isoformat()

    if isinstance(value, (bytes, bytearray, memoryview)):
        types[path] = "bytes"
        return base64.b64encode(bytes(value)).decode("ascii")

    if isinstance(value, Mapping):
        encoded: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsupportedTypeError(
                    f"metadata key {key!r} at {path or '/'} is not a string"
                )
            encoded[key] = _encode_value(item, _pointer(path, key), types)
        return encoded

    if isinstance(value, (list, tuple)):
        if isinstance(value, tuple):
            types[path] = "tuple"
        return [
            _encode_value(item, _pointer(path, index), types)
            for index, item in enumerate(value)
        ]

    if isinstance(value, (set, frozenset)):
        types[path] = "frozenset" if isinstance(value, frozenset) else "set"
        # Sets are unordered. Sorting by repr makes the artifact deterministic
        # without pretending that the original iteration order was meaningful.
        ordered = sorted(value, key=repr)
        return [
            _encode_value(item, _pointer(path, index), types)
            for index, item in enumerate(ordered)
        ]

    raise UnsupportedTypeError(
        f"metadata value of type {type(value).__name__} at {path or '/'} "
        "cannot be represented in JSON"
    )


def encode_metadata(
    metadata: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """
    Encode metadata as plain JSON values plus optional type annotations.

    Returns ``(metadata, types)``. ``metadata`` contains only JSON-compatible
    values. ``types`` maps JSON Pointer paths to tags such as ``date``,
    ``datetime``, ``float``, ``bigint`` or ``decimal``. It is empty when every
    value already has an unambiguous JSON representation.

    ``None`` is kept as an explicit JSON null. Keys whose value is ``None`` are
    not removed, which is what keeps an explicit null distinguishable from a
    missing field.
    """
    types: dict[str, str] = {}
    encoded = _encode_value(metadata, "", types)
    assert isinstance(encoded, dict)
    return encoded, types


def _decode_tag(tag: str, value: object, path: str) -> object:
    location = path or "/"

    if tag == "bigint":
        if not isinstance(value, str):
            raise SerializationError(f"bigint at {location} must be a string")
        try:
            return int(value)
        except ValueError as exc:
            raise SerializationError(
                f"invalid bigint at {location}: {value!r}"
            ) from exc

    if tag == "float":
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError as exc:
                raise SerializationError(
                    f"invalid float at {location}: {value!r}"
                ) from exc
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise SerializationError(f"invalid float at {location}: {value!r}")

    if tag == "decimal":
        if not isinstance(value, str):
            raise SerializationError(f"decimal at {location} must be a string")
        try:
            return decimal.Decimal(value)
        except decimal.InvalidOperation as exc:
            raise SerializationError(
                f"invalid decimal at {location}: {value!r}"
            ) from exc

    if tag == "date":
        if not isinstance(value, str):
            raise SerializationError(f"date at {location} must be a string")
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise SerializationError(f"invalid date at {location}: {value!r}") from exc

    if tag == "datetime":
        if not isinstance(value, str):
            raise SerializationError(f"datetime at {location} must be a string")
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.datetime.fromisoformat(text)
        except ValueError as exc:
            raise SerializationError(
                f"invalid datetime at {location}: {value!r}"
            ) from exc

    if tag == "time":
        if not isinstance(value, str):
            raise SerializationError(f"time at {location} must be a string")
        try:
            return datetime.time.fromisoformat(value)
        except ValueError as exc:
            raise SerializationError(f"invalid time at {location}: {value!r}") from exc

    if tag == "bytes":
        if not isinstance(value, str):
            raise SerializationError(f"bytes at {location} must be a string")
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise SerializationError(f"invalid base64 at {location}") from exc

    if tag in ("tuple", "set", "frozenset"):
        if not isinstance(value, list):
            raise SerializationError(f"{tag} at {location} must be a list")
        if tag == "tuple":
            return tuple(value)
        if tag == "set":
            return set(value)
        return frozenset(value)

    raise SerializationError(f"unknown type tag {tag!r} at {location}")


def _decode_value(
    value: object, path: str, types: Mapping[str, str], used: set[str]
) -> object:
    tag = types.get(path)
    if tag is not None:
        used.add(path)
        if tag in ("tuple", "set", "frozenset"):
            if not isinstance(value, list):
                raise SerializationError(
                    f"{tag} at {path or '/'} must contain a JSON array"
                )
            items = [
                _decode_value(item, _pointer(path, index), types, used)
                for index, item in enumerate(value)
            ]
            return _decode_tag(tag, items, path)
        return _decode_tag(tag, value, path)

    if isinstance(value, dict):
        return {
            key: _decode_value(item, _pointer(path, key), types, used)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _decode_value(item, _pointer(path, index), types, used)
            for index, item in enumerate(value)
        ]

    return value


def decode_metadata(
    metadata: Mapping[str, object], types: Mapping[str, str] | None = None
) -> dict[str, object]:
    """
    Restore Python types described by ``types`` into ``metadata``.

    Paths are JSON Pointers into ``metadata``. A path that does not exist, an
    unknown tag, or a value of the wrong shape raises
    :class:`SerializationError` rather than silently returning a wrong type.
    """
    types = types or {}
    if not isinstance(types, Mapping):
        raise SerializationError("types must be a mapping of JSON Pointer to tag")

    used: set[str] = set()
    decoded = _decode_value(metadata, "", types, used)
    assert isinstance(decoded, dict)

    missing = sorted(set(types) - used)
    if missing:
        raise SerializationError(
            "type annotations refer to missing paths: " + ", ".join(missing)
        )

    return decoded
