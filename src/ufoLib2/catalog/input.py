from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple

from fontTools.agl import toUnicode

from ufoLib2.catalog.model import CatalogError, GlyphRecord
from ufoLib2.objects import Font
from ufoLib2.typing import PathLike

_UNICODE_GLYPH_NAME_RE = re.compile(r"^(?:uni[0-9A-F]{4}|u[0-9A-F]{4,6})$")


@dataclass(frozen=True)
class SourceSpec:
    """Description of one supplier UFO.

    Args:
        label: Short, stable name used in catalog record ids.  Must not contain
            a colon.
        path: Path to the UFO.
        naming: ``"auto"``, ``"unicode"`` or ``"internal"``.  ``"unicode"``
            allows a glyph without an explicit glif ``<unicode>`` element to
            take its Unicode set from a canonical glyph name.  ``"internal"``
            never does.  ``"auto"`` enables that fallback when at least half
            of the names look like ``uniXXXX``/``uXXXXX`` names.
        stable_id_key: Optional glyph lib key containing an explicit stable id.
    """

    label: str
    path: PathLike
    naming: str = "auto"
    stable_id_key: str | None = None

    def __post_init__(self) -> None:
        if not self.label:
            raise CatalogError("source label must not be empty")
        if ":" in self.label:
            raise CatalogError(
                f"source label {self.label!r} must not contain a colon"
            )
        if self.naming not in {"auto", "unicode", "internal"}:
            raise CatalogError(
                f"invalid naming strategy {self.naming!r}; expected 'auto', "
                "'unicode' or 'internal'"
            )


@dataclass(frozen=True)
class ManualPair:
    left: str
    right: str
    detail: str = ""


def load_json_file(path: PathLike) -> Any:
    """Read JSON while rejecting duplicate object keys.

    Python's default decoder keeps the last duplicate key, which would make a
    supplier mapping depend on file order.  A duplicate key is an input error
    here instead.
    """

    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CatalogError(f"duplicate JSON key {key!r} in {path!s}")
            result[key] = value
        return result

    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text, object_pairs_hook=no_duplicate_keys)
    except CatalogError:
        raise
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read JSON file {path!s}: {exc}") from exc


def _agl_name_unicodes(name: str) -> Tuple[int, ...]:
    """Parse an AGL glyph name into a sorted tuple of code points.

    A trailing ``.suffix`` makes the name a variant.  ``fontTools.agl.toUnicode``
    strips that suffix, which would make ``A.alt`` look identical to ``A``; a
    variant's suffix is not identity evidence, so such names yield no
    name-derived Unicode set.
    """
    if "." in name:
        return ()
    try:
        text = toUnicode(name)
    except Exception:
        return ()
    values = {ord(char) for char in text}
    return tuple(sorted(values))


def _identity_name_unicodes(name: str, naming: str) -> Tuple[int, ...]:
    if naming != "unicode":
        return ()
    if not _UNICODE_GLYPH_NAME_RE.match(name):
        return ()
    return _agl_name_unicodes(name)


def _detect_naming(names: Sequence[str], requested: str) -> str:
    if requested != "auto":
        return requested
    if not names:
        return "internal"
    encoded = sum(1 for name in names if _UNICODE_GLYPH_NAME_RE.match(name))
    return "unicode" if encoded * 2 >= len(names) else "internal"


def _glyph_stable_id(glyph: Any, key: str | None, record_id: str) -> str | None:
    if key is None:
        return None
    value = glyph.lib.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CatalogError(
            f"glyph {record_id!r} has a non-string or empty stable id under "
            f"{key!r}"
        )
    return value


def load_source(
    spec: SourceSpec,
    stable_ids: Mapping[str, str] | None = None,
) -> tuple[Any, list[GlyphRecord]]:
    """Read one UFO and turn every glyph into an independent record.

    The returned :class:`~ufoLib2.catalog.model.SourceInfo` is imported
    lazily to avoid a model/input cycle.
    """

    from ufoLib2.catalog.model import SourceInfo

    path = os.fspath(spec.path)
    if not os.path.exists(path):
        raise CatalogError(f"UFO not found: {path!r}")

    external_ids = stable_ids or {}
    try:
        font = Font.open(path, lazy=False, validate=False)
    except Exception as exc:
        raise CatalogError(f"cannot open UFO {path!r}: {exc}") from exc

    try:
        names = sorted(font.keys())
        naming = _detect_naming(names, spec.naming)
        records: list[GlyphRecord] = []
        for name in names:
            glyph = font[name]
            record_id = f"{spec.label}:{name}"
            explicit = tuple(glyph.unicodes)
            if explicit:
                unicodes = explicit
                unicode_source = "glif"
            else:
                from_name = _identity_name_unicodes(name, naming)
                if from_name:
                    unicodes = from_name
                    unicode_source = "name"
                else:
                    unicodes = ()
                    unicode_source = "none"

            external = external_ids.get(record_id)
            if external is not None:
                if not isinstance(external, str) or not external:
                    raise CatalogError(
                        f"stable id for {record_id!r} must be a non-empty string"
                    )
            from_lib = _glyph_stable_id(glyph, spec.stable_id_key, record_id)
            if external is not None and from_lib is not None and external != from_lib:
                raise CatalogError(
                    f"conflicting explicit stable ids for {record_id!r}: "
                    f"{external!r} from the stable-id file and {from_lib!r} from "
                    f"glyph lib key {spec.stable_id_key!r}"
                )
            stable_id = external if external is not None else from_lib

            records.append(
                GlyphRecord(
                    id=record_id,
                    source=spec.label,
                    glyph_name=name,
                    stable_id=stable_id,
                    unicodes=unicodes,
                    unicode_source=unicode_source,
                    name_unicodes=_agl_name_unicodes(name),
                )
            )
    finally:
        font.close()

    info = SourceInfo(
        name=spec.label,
        path=str(path),
        naming=naming,
        glyph_count=len(records),
        stable_id_key=spec.stable_id_key,
    )
    return info, records


def _source_for_label(label: str, specs: Sequence[SourceSpec]) -> SourceSpec:
    for spec in specs:
        if spec.label == label:
            return spec
    raise CatalogError(f"unknown source {label!r}")


def normalize_stable_ids(
    data: Any,
    specs: Sequence[SourceSpec],
    records: Sequence[GlyphRecord],
) -> dict[str, str]:
    """Normalize accepted stable-id file shapes into ``{record_id: id}``.

    Accepted shapes::

        {"left": {"uni0041": "id-1"}, "right": {"A": "id-1"}}
        {"left:uni0041": "id-1"}
        {"entries": [{"source": "left", "glyph": "uni0041", "stableId": "id-1"}]}
        [{"source": "left", "glyph": "uni0041", "stableId": "id-1"}]
    """

    labels = {spec.label for spec in specs}
    by_record = {record.id: record for record in records}
    by_source_glyph = {(record.source, record.glyph_name): record for record in records}
    result: dict[str, str] = {}

    def assign(record_id: str, stable_id: Any, where: str) -> None:
        if record_id not in by_record:
            raise CatalogError(f"stable-id entry {where!r} references unknown glyph")
        if not isinstance(stable_id, str) or not stable_id:
            raise CatalogError(
                f"stable id for {record_id!r} must be a non-empty string"
            )
        previous = result.get(record_id)
        if previous is not None and previous != stable_id:
            raise CatalogError(
                f"glyph {record_id!r} is assigned two stable ids: "
                f"{previous!r} and {stable_id!r}"
            )
        result[record_id] = stable_id

    def resolve_key(key: str, where: str) -> str:
        if key in by_record:
            return key
        if ":" in key:
            source, glyph_name = key.split(":", 1)
            if source in labels:
                record = by_source_glyph.get((source, glyph_name))
                if record is not None:
                    return record.id
        raise CatalogError(f"stable-id entry {where!r} references unknown glyph")

    def consume(value: Any, where: str) -> None:
        if isinstance(value, Mapping):
            if {"source", "glyph"} <= set(value):
                source = value["source"]
                glyph_name = value.get("glyph", value.get("glyphName"))
                if not isinstance(source, str) or not isinstance(glyph_name, str):
                    raise CatalogError(f"invalid stable-id entry {where!r}")
                record = by_source_glyph.get((source, glyph_name))
                if record is None:
                    raise CatalogError(
                        f"stable-id entry {where!r} references unknown glyph "
                        f"{source}:{glyph_name}"
                    )
                assign(record.id, value.get("stableId"), where)
                return
            entries = value.get("entries")
            if entries is not None:
                if not isinstance(entries, Sequence) or isinstance(
                    entries, (str, bytes)
                ):
                    raise CatalogError("stable-id 'entries' must be a list")
                for index, item in enumerate(entries):
                    consume(item, f"entries[{index}]")
                return
            for key, nested in value.items():
                if isinstance(nested, str):
                    assign(resolve_key(key, key), nested, key)
                elif isinstance(nested, Mapping):
                    if key in labels:
                        for glyph_name, stable_id in nested.items():
                            record = by_source_glyph.get((key, glyph_name))
                            if record is None:
                                raise CatalogError(
                                    f"stable-id entry references unknown glyph "
                                    f"{key}:{glyph_name}"
                                )
                            assign(record.id, stable_id, f"{key}:{glyph_name}")
                    else:
                        consume(nested, key)
                else:
                    raise CatalogError(f"invalid stable-id entry {key!r}")
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value):
                consume(item, f"item[{index}]")
            return
        raise CatalogError(f"invalid stable-id data at {where!r}")

    consume(data, "root")

    # Reject a stable-id file entry that points at a glyph in a different
    # source than the one it is nested under.
    for key in result:
        if key not in by_record:
            raise CatalogError(f"stable-id file references unknown glyph {key!r}")

    return result


def _resolve_record_ref(
    ref: Any,
    side: str,
    specs: Sequence[SourceSpec],
    by_record: Mapping[str, GlyphRecord],
    by_source_glyph: Mapping[tuple[str, str], GlyphRecord],
) -> str:
    default_source = specs[0].label if side == "left" else specs[1].label
    if isinstance(ref, Mapping):
        record_id = ref.get("record", ref.get("id"))
        if record_id is not None:
            if not isinstance(record_id, str) or record_id not in by_record:
                raise CatalogError(
                    f"manual mapping references unknown record {record_id!r}"
                )
            return record_id
        source = ref.get("source", default_source)
        glyph_name = ref.get("glyph", ref.get("glyphName"))
        if not isinstance(source, str) or not isinstance(glyph_name, str):
            raise CatalogError(f"invalid manual mapping reference {ref!r}")
        record = by_source_glyph.get((source, glyph_name))
        if record is None:
            raise CatalogError(
                f"manual mapping references unknown glyph {source}:{glyph_name}"
            )
        return record.id
    if not isinstance(ref, str) or not ref:
        raise CatalogError(f"invalid manual mapping reference {ref!r}")
    if ref in by_record:
        return ref
    if ":" in ref:
        source, glyph_name = ref.split(":", 1)
        record = by_source_glyph.get((source, glyph_name))
        if record is not None:
            return record.id
        raise CatalogError(f"manual mapping references unknown glyph {ref!r}")
    record = by_source_glyph.get((default_source, ref))
    if record is None:
        raise CatalogError(
            f"manual mapping references unknown glyph {default_source}:{ref}"
        )
    return record.id


def normalize_manual_mapping(
    data: Any,
    specs: Sequence[SourceSpec],
    records: Sequence[GlyphRecord],
) -> list[ManualPair]:
    """Normalize a human-confirmed mapping file into record-id pairs.

    Accepted shapes::

        {"pairs": [{"left": "uni0041", "right": "A", "note": "..."}]}
        [{"left": "uni0041", "right": "A"}]
        [["uni0041", "A"]]
        {"uni0041": "A"}

    A reference is either a catalog record id, a plain glyph name belonging to
    the left/right source, or ``{"source": "...", "glyph": "..."}``.
    """

    if len(specs) != 2:
        raise CatalogError("manual mapping requires exactly two sources")
    by_record = {record.id: record for record in records}
    by_source_glyph = {(record.source, record.glyph_name): record for record in records}
    pairs: list[ManualPair] = []

    def add(left_ref: Any, right_ref: Any, detail: Any = "") -> None:
        left_id = _resolve_record_ref(
            left_ref, "left", specs, by_record, by_source_glyph
        )
        right_id = _resolve_record_ref(
            right_ref, "right", specs, by_record, by_source_glyph
        )
        left_record = by_record[left_id]
        right_record = by_record[right_id]
        if left_record.source == right_record.source:
            raise CatalogError(
                f"manual pair {left_id!r} -> {right_id!r} is within one source; "
                "a confirmed pair must cross the two suppliers"
            )
        if {left_record.source, right_record.source} != {
            specs[0].label,
            specs[1].label,
        }:
            raise CatalogError(
                f"manual pair {left_id!r} -> {right_id!r} does not connect the "
                "two configured sources"
            )
        note = "" if detail is None else str(detail)
        if any(
            (existing.left == left_id and existing.right != right_id)
            or (existing.right == right_id and existing.left != left_id)
            for existing in pairs
        ):
            raise CatalogError(
                f"conflicting human-confirmed mappings for {left_id!r} or "
                f"{right_id!r}; a glyph may have only one confirmed counterpart"
            )
        pairs.append(ManualPair(left=left_id, right=right_id, detail=note))

    def consume(value: Any, where: str) -> None:
        if isinstance(value, Mapping):
            if "pairs" in value:
                items = value["pairs"]
                if not isinstance(items, Sequence) or isinstance(
                    items, (str, bytes)
                ):
                    raise CatalogError("manual 'pairs' must be a list")
                for index, item in enumerate(items):
                    consume(item, f"pairs[{index}]")
                return
            if "left" in value or "right" in value:
                if "left" not in value or "right" not in value:
                    raise CatalogError(
                        f"manual pair {where!r} must contain both 'left' and 'right'"
                    )
                detail = value.get("note", value.get("detail", ""))
                add(value["left"], value["right"], detail)
                return
            if len(value) == 1 and "pair" in value:
                item = value["pair"]
                if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
                    raise CatalogError("manual 'pair' must be a two-item list")
                consume(item, where)
                return
            for left_ref, right_ref in value.items():
                add(left_ref, right_ref, "")
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) == 2 and all(isinstance(item, str) for item in value):
                add(value[0], value[1], "")
                return
            for index, item in enumerate(value):
                consume(item, f"item[{index}]")
            return
        raise CatalogError(f"invalid manual mapping data at {where!r}")

    consume(data, "root")
    return pairs


def load_manual_file(
    path: PathLike,
    specs: Sequence[SourceSpec],
    records: Sequence[GlyphRecord],
) -> list[ManualPair]:
    return normalize_manual_mapping(load_json_file(path), specs, records)
