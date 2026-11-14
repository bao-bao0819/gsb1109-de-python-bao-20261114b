"""Recursive layout expansion with cycle-safe pointer classification.

Record graphs built by :mod:`structmap.collector` only contain cycles through
pointers (C forbids inline self-containment). We nevertheless guard every
recursive walk with an active-stack, and classify pointer edges with a
Tarjan SCC pass so self references and mutual references stay distinct.
"""

from dataclasses import dataclass

from .collector import (
    Array,
    FieldInfo,
    FuncPtr,
    ModuleSchema,
    Prim,
    Ptr,
    RecordRef,
    TypeDescriptor,
    Unknown,
)


@dataclass(frozen=True)
class PtrEdge:
    field: str
    target_rid: str


def record_edges(schema: ModuleSchema) -> dict[str, set[str]]:
    """All record-to-record rids referenced anywhere in each record."""
    edges: dict[str, set[str]] = {rid: set() for rid in schema.records}
    for rid, record in schema.records.items():
        for field_info in record.fields:
            for ref in _collect_refs(field_info.descriptor):
                if ref.rid in schema.records:
                    edges[rid].add(ref.rid)
    return edges


def pointer_edges(schema: ModuleSchema) -> dict[str, list[PtrEdge]]:
    edges: dict[str, list[PtrEdge]] = {rid: [] for rid in schema.records}
    for rid, record in schema.records.items():
        for field_info in record.fields:
            for ref in _collect_ptr_refs(field_info.descriptor):
                if ref.rid in schema.records:
                    edges[rid].append(PtrEdge(field_info.name, ref.rid))
    return edges


def _collect_refs(desc: TypeDescriptor) -> list[RecordRef]:
    if isinstance(desc, RecordRef):
        return [desc]
    if isinstance(desc, Ptr):
        return _collect_refs(desc.pointee)
    if isinstance(desc, Array):
        return _collect_refs(desc.element)
    return []


def _collect_ptr_refs(desc: TypeDescriptor) -> list[RecordRef]:
    if isinstance(desc, Ptr):
        return _collect_refs(desc.pointee)
    if isinstance(desc, Array):
        return _collect_ptr_refs(desc.element)
    return []


def strongly_connected_components(
    edges: dict[str, set[str]],
) -> dict[str, int]:
    """Map each rid to the id of its SCC (iterative Tarjan)."""
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    component: dict[str, int] = {}
    component_counter = 0

    for root in edges:
        if root in indices:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, next_edge = work[-1]
            if next_edge == 0:
                indices[node] = index_counter
                lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)
            neighbors = sorted(edges[node])
            if next_edge < len(neighbors):
                neighbor = neighbors[next_edge]
                work[-1] = (node, next_edge + 1)
                if neighbor not in indices:
                    work.append((neighbor, 0))
                elif neighbor in on_stack:
                    lowlink[node] = min(lowlink[node], indices[neighbor])
            else:
                if lowlink[node] == indices[node]:
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component[member] = component_counter
                        if member == node:
                            break
                    component_counter += 1
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
    return component


class PointerClassifier:
    """Labels a pointer target as ``self``, ``mutual`` or ``external``."""

    def __init__(self, schema: ModuleSchema) -> None:
        self._components = strongly_connected_components(record_edges(schema))

    def classify(self, owner_rid: str, target_rid: str) -> str:
        if target_rid == owner_rid:
            return "self"
        if self._components.get(owner_rid) == self._components.get(target_rid):
            return "mutual"
        return "external"


def expand_record(
    schema: ModuleSchema,
    rid: str,
    *,
    follow_pointers: bool = True,
    _active: tuple[str, ...] = (),
) -> dict:
    """Expand a record into a JSON-able nested layout.

    Re-entering a rid already on the active stack emits a cycle marker
    instead of recursing, so self and mutual references terminate.
    """
    if rid in _active:
        return {"cycle": rid, "kind": "self" if rid == _active[0] else "mutual"}
    record = schema.records[rid]
    active = _active + (rid,)
    return {
        "rid": rid,
        "kind": record.kind,
        "tag": record.tag,
        "fields": [
            _expand_field(schema, field_info, active, follow_pointers)
            for field_info in record.fields
        ],
    }


def _expand_field(
    schema: ModuleSchema,
    field_info: FieldInfo,
    active: tuple[str, ...],
    follow_pointers: bool,
) -> dict:
    return {
        "name": field_info.name,
        "bitsize": field_info.bitsize,
        "layout": _expand_descriptor(
            schema, field_info.descriptor, active, follow_pointers
        ),
    }


def _expand_descriptor(
    schema: ModuleSchema,
    desc: TypeDescriptor,
    active: tuple[str, ...],
    follow_pointers: bool,
) -> dict:
    if isinstance(desc, Prim):
        return {"type": "prim", "names": list(desc.names)}
    if isinstance(desc, FuncPtr):
        return {"type": "funcptr"}
    if isinstance(desc, Unknown):
        return {"type": "unknown", "spelling": desc.spelling}
    if isinstance(desc, Array):
        return {
            "type": "array",
            "dim": desc.dim,
            "element": _expand_descriptor(
                schema, desc.element, active, follow_pointers
            ),
        }
    if isinstance(desc, Ptr):
        inner = desc.pointee
        if isinstance(inner, RecordRef) and inner.rid in active:
            target = {
                "type": "record-ref",
                "rid": inner.rid,
                "kind": inner.kind,
                "cycle": "self" if inner.rid == active[-1] else "mutual",
            }
        elif follow_pointers:
            target = _expand_descriptor(schema, inner, active, follow_pointers)
        else:
            target = _shallow_ref(inner)
        return {"type": "ptr", "to": target}
    if isinstance(desc, RecordRef):
        if desc.rid in active:
            return {
                "type": "record-ref",
                "rid": desc.rid,
                "kind": desc.kind,
                "cycle": "self" if desc.rid == active[-1] else "mutual",
            }
        return expand_record(
            schema, desc.rid, follow_pointers=follow_pointers, _active=active
        )
    raise TypeError(f"unhandled descriptor {desc!r}")


def _shallow_ref(desc: TypeDescriptor) -> dict:
    if isinstance(desc, RecordRef):
        return {"type": "record-ref", "rid": desc.rid, "kind": desc.kind}
    return desc.to_dict()


def render_schema(schema: ModuleSchema) -> dict:
    """Flat, JSON-able structural description of one preprocessed module."""
    classifier = PointerClassifier(schema)
    return {
        "source": schema.source,
        "aliases": {
            name: {
                "descriptor": alias.descriptor.to_dict(),
                "coord": alias.coord,
            }
            for name, alias in schema.aliases.items()
        },
        "records": [
            {
                "rid": rid,
                "kind": record.kind,
                "tag": record.tag,
                "coord": record.coord,
                "fields": [
                    _render_field(schema, classifier, rid, field_info)
                    for field_info in record.fields
                ],
            }
            for rid, record in schema.records.items()
        ],
    }


def _render_field(
    schema: ModuleSchema,
    classifier: PointerClassifier,
    owner_rid: str,
    field_info: FieldInfo,
) -> dict:
    return {
        "name": field_info.name,
        "bitsize": field_info.bitsize,
        "descriptor": _annotate_descriptor(
            schema, classifier, owner_rid, field_info.descriptor
        ),
    }


def _annotate_descriptor(
    schema: ModuleSchema,
    classifier: PointerClassifier,
    owner_rid: str,
    desc: TypeDescriptor,
) -> dict:
    rendered = desc.to_dict()
    if isinstance(desc, Ptr) and isinstance(desc.pointee, RecordRef):
        target = desc.pointee
        rendered["to"]["ptr_class"] = (
            classifier.classify(owner_rid, target.rid)
            if target.rid in schema.records
            else "unresolved"
        )
    elif isinstance(desc, Array):
        rendered["element"] = _annotate_descriptor(
            schema, classifier, owner_rid, desc.element
        )
    return rendered
