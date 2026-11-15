from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from ufoLib2.catalog.input import ManualPair
from ufoLib2.catalog.model import (
    BASIS_ORDER,
    Catalog,
    CatalogError,
    CandidateGroup,
    Conflict,
    ConflictClaim,
    Evidence,
    GlyphRecord,
    Match,
    SourceInfo,
    Unmatched,
    stable_key,
    unicode_labels,
)

HARD_BASES = frozenset({"manual", "stable-id", "unicode"})
SOFT_BASES = frozenset({"name", "name-unicode"})
ALL_BASES = HARD_BASES | SOFT_BASES


def format_unicodes(values: Iterable[int]) -> str:
    labels = unicode_labels(values)
    return "{" + ", ".join(labels) + "}" if labels else "{}"


def _basis_rank(basis: str) -> int:
    try:
        return BASIS_ORDER.index(basis)
    except ValueError:
        return len(BASIS_ORDER)


@dataclass(frozen=True)
class _Claim:
    basis: str
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.basis not in ALL_BASES:
            raise CatalogError(f"unknown evidence basis {self.basis!r}")
        object.__setattr__(self, "targets", tuple(sorted(set(self.targets))))


def _claim_detail(claim: _Claim, record: GlyphRecord) -> str:
    if claim.basis == "manual":
        return "human-confirmed mapping"
    if claim.basis == "stable-id":
        return f"stable id {record.stable_id!r}"
    if claim.basis == "unicode":
        return (
            f"explicit Unicode set {format_unicodes(record.unicodes)} "
            f"({record.unicode_source}-derived)"
        )
    if claim.basis == "name":
        return f"glyph name {record.glyph_name!r}"
    if claim.basis == "name-unicode":
        return (
            f"glyph name {record.glyph_name!r} parses to "
            f"{format_unicodes(record.name_unicodes)}"
        )
    return claim.basis


def _sorted_claims(claims: Iterable[_Claim]) -> tuple[_Claim, ...]:
    return tuple(sorted(claims, key=lambda c: (_basis_rank(c.basis), c.basis)))


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while value != parent:
            next_value = self.parent[value]
            self.parent[value] = parent
            value = next_value
        return parent

    def union(self, first: str, second: str) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            # Choose the lexicographically smaller root so component identity
            # never depends on input order.
            if first_root < second_root:
                self.parent[second_root] = first_root
            else:
                self.parent[first_root] = second_root

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for value in self.parent:
            result[self.find(value)].append(value)
        return {root: sorted(values) for root, values in result.items()}


class _Resolver:
    def __init__(
        self,
        sources: Sequence[SourceInfo],
        records: Sequence[GlyphRecord],
        manual_pairs: Sequence[ManualPair],
    ) -> None:
        if len(sources) != 2:
            raise CatalogError("a catalog requires exactly two sources")
        if sources[0].name == sources[1].name:
            raise CatalogError("source labels must be unique")
        self.sources = tuple(sources)
        self.records = tuple(sorted(records, key=lambda record: record.id))
        self.by_id = {record.id: record for record in self.records}
        if len(self.by_id) != len(self.records):
            raise CatalogError("catalog records contain duplicate ids")
        self.source_names = tuple(source.name for source in self.sources)
        self.left_source, self.right_source = self.source_names

        self.by_source: dict[str, list[GlyphRecord]] = {
            name: [] for name in self.source_names
        }
        for record in self.records:
            if record.source not in self.by_source:
                raise CatalogError(
                    f"record {record.id!r} belongs to unconfigured source "
                    f"{record.source!r}"
                )
            self.by_source[record.source].append(record)

        self.unicode_index: dict[
            tuple[str, tuple[int, ...]], list[str]
        ] = defaultdict(list)
        self.name_unicode_index: dict[
            tuple[str, tuple[int, ...]], list[str]
        ] = defaultdict(list)
        self.stable_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.name_index: dict[tuple[str, str], str] = {}
        for record in self.records:
            if record.unicodes:
                self.unicode_index[(record.source, record.unicodes)].append(record.id)
            if record.name_unicodes:
                self.name_unicode_index[
                    (record.source, record.name_unicodes)
                ].append(record.id)
            if record.stable_id is not None:
                self.stable_index[(record.source, record.stable_id)].append(record.id)
            self.name_index[(record.source, record.glyph_name)] = record.id

        self.manual: dict[str, str] = {}
        self.manual_details: dict[tuple[str, str], str] = {}
        for pair in manual_pairs:
            self._add_manual(pair)
        self.conflicts: dict[str, Conflict] = {}

    def other_source(self, source: str) -> str:
        return self.right_source if source == self.left_source else self.left_source

    def _add_manual(self, pair: ManualPair) -> None:
        left = self.by_id.get(pair.left)
        right = self.by_id.get(pair.right)
        if left is None or right is None:
            raise CatalogError(
                f"manual mapping references unknown record: "
                f"{pair.left!r}, {pair.right!r}"
            )
        if left.source == right.source:
            raise CatalogError(
                f"manual mapping {pair.left!r} -> {pair.right!r} is within one source"
            )
        if left.source != self.left_source:
            left, right = right, left
        if left.source != self.left_source or right.source != self.right_source:
            raise CatalogError(
                f"manual mapping {pair.left!r} -> {pair.right!r} does not cross "
                "the two configured sources"
            )
        self._set_manual(left.id, right.id, pair.detail)

    def _set_manual(self, left_id: str, right_id: str, detail: str) -> None:
        previous = self.manual.get(left_id)
        if previous is not None and previous != right_id:
            raise CatalogError(
                f"glyph {left_id!r} has conflicting human-confirmed mappings "
                f"to {previous!r} and {right_id!r}"
            )
        previous = self.manual.get(right_id)
        if previous is not None and previous != left_id:
            raise CatalogError(
                f"glyph {right_id!r} has conflicting human-confirmed mappings "
                f"to {previous!r} and {left_id!r}"
            )
        self.manual[left_id] = right_id
        self.manual[right_id] = left_id
        self.manual_details[(left_id, right_id)] = detail

    def claims_for(self, record: GlyphRecord) -> tuple[_Claim, ...]:
        opposite = self.other_source(record.source)
        claims: list[_Claim] = []

        manual_target = self.manual.get(record.id)
        if manual_target is not None:
            claims.append(_Claim("manual", (manual_target,)))

        if record.stable_id is not None:
            claims.append(
                _Claim(
                    "stable-id",
                    tuple(self.stable_index.get((opposite, record.stable_id), ())),
                )
            )

        if record.unicodes:
            claims.append(
                _Claim(
                    "unicode",
                    tuple(self.unicode_index.get((opposite, record.unicodes), ())),
                )
            )

        target = self.name_index.get((opposite, record.glyph_name))
        if target is not None:
            claims.append(_Claim("name", (target,)))

        if record.name_unicodes and record.name_unicodes != record.unicodes:
            targets = set(self.unicode_index.get((opposite, record.name_unicodes), ()))
            targets.update(
                self.name_unicode_index.get((opposite, record.name_unicodes), ())
            )
            if targets:
                claims.append(_Claim("name-unicode", tuple(targets)))

        return _sorted_claims(claims)

    @staticmethod
    def _nonzero(claims: Sequence[_Claim]) -> list[_Claim]:
        return [claim for claim in claims if claim.targets]

    @staticmethod
    def _intersection(claims: Sequence[_Claim]) -> set[str]:
        result: set[str] | None = None
        for claim in claims:
            targets = set(claim.targets)
            result = targets if result is None else result & targets
            if not result:
                break
        return result or set()

    def _unique_hard_target(self, claims: Sequence[_Claim]) -> str | None:
        """Select one counterpart only when explicit evidence is unambiguous.

        A Unicode claim may admit several candidates; a single-target stable-id
        claim or human-confirmed mapping can narrow it.  The selected target
        must still appear in every other non-empty claim.  Name-only evidence
        never selects a target.
        """

        nonempty = self._nonzero(claims)
        if not nonempty:
            return None
        manual = next(
            (claim for claim in nonempty if claim.basis == "manual"), None
        )
        if manual is not None:
            # A human-confirmed mapping is authoritative; disagreeing claims
            # are retained separately as a manual-override conflict.
            return manual.targets[0] if len(manual.targets) == 1 else None
        intersection = self._intersection(nonempty)
        if len(intersection) != 1:
            return None
        target = next(iter(intersection))
        for claim in nonempty:
            if claim.basis in HARD_BASES and claim.targets == (target,):
                return target
        return None

    def _conflict_for(
        self, record: GlyphRecord, claims: Sequence[_Claim]
    ) -> Conflict | None:
        nonempty = self._nonzero(claims)
        if len(nonempty) < 2:
            return None
        manual = next(
            (claim for claim in nonempty if claim.basis == "manual"), None
        )
        if manual is not None:
            # A human confirmation wins over automatic evidence, but the
            # disagreeing name/stable-id/Unicode claims stay visible instead of
            # being silently discarded.
            disagreeing = [
                claim
                for claim in nonempty
                if claim is not manual
                and not set(claim.targets).intersection(manual.targets)
            ]
            if not disagreeing:
                return None
            claims_json = tuple(
                ConflictClaim(
                    basis=claim.basis,
                    targets=claim.targets,
                    detail=_claim_detail(claim, record),
                )
                for claim in nonempty
            )
            disagreements = tuple(
                tuple(sorted((manual.basis, claim.basis), key=_basis_rank))
                for claim in disagreeing
            )
            material = [record.id, "manual-override"]
            for claim in nonempty:
                material.append(claim.basis)
                material.extend(claim.targets)
            return Conflict(
                id=stable_key("conflict", material),
                kind="manual-override",
                record=record.id,
                claims=claims_json,
                disagreements=tuple(sorted(set(disagreements))),
            )
        if self._intersection(nonempty):
            # Every evidence form admits at least one common counterpart; this
            # is ambiguity, not contradiction.
            return None
        claims_json = tuple(
            ConflictClaim(
                basis=claim.basis,
                targets=claim.targets,
                detail=_claim_detail(claim, record),
            )
            for claim in nonempty
        )
        disagreements: list[tuple[str, str]] = []
        for index, first in enumerate(nonempty):
            for second in nonempty[index + 1 :]:
                if not set(first.targets).intersection(second.targets):
                    pair = sorted((first.basis, second.basis), key=_basis_rank)
                    disagreements.append((pair[0], pair[1]))
        material = [record.id]
        for claim in nonempty:
            material.append(claim.basis)
            material.extend(claim.targets)
        return Conflict(
            id=stable_key("conflict", material),
            kind="evidence-disagreement",
            record=record.id,
            claims=claims_json,
            disagreements=tuple(sorted(set(disagreements))),
        )

    def _evidence(self, left: GlyphRecord, right: GlyphRecord) -> tuple[Evidence, ...]:
        evidence: list[Evidence] = []
        if self.manual.get(left.id) == right.id:
            detail = self.manual_details.get((left.id, right.id), "")
            evidence.append(
                Evidence(
                    "manual",
                    left.id,
                    right.id,
                    "human-confirmed mapping" + (f": {detail}" if detail else ""),
                )
            )
        if left.stable_id is not None and left.stable_id == right.stable_id:
            evidence.append(
                Evidence(
                    "stable-id",
                    left.id,
                    right.id,
                    f"both records carry stable id {left.stable_id!r}",
                )
            )
        if left.unicodes and left.unicodes == right.unicodes:
            evidence.append(
                Evidence(
                    "unicode",
                    left.id,
                    right.id,
                    f"identical Unicode set {format_unicodes(left.unicodes)} "
                    f"(left: {left.unicode_source}, right: {right.unicode_source})",
                )
            )
        if left.glyph_name == right.glyph_name:
            evidence.append(
                Evidence(
                    "name",
                    left.id,
                    right.id,
                    f"identical glyph name {left.glyph_name!r}",
                )
            )
        if (
            left.name_unicodes
            and left.name_unicodes == right.name_unicodes
            and left.name_unicodes != left.unicodes
        ):
            evidence.append(
                Evidence(
                    "name-unicode",
                    left.id,
                    right.id,
                    "glyph names both parse to "
                    f"{format_unicodes(left.name_unicodes)}",
                )
            )
        return tuple(evidence)

    def build(self) -> Catalog:
        claim_cache: dict[str, tuple[_Claim, ...]] = {}
        for record in self.records:
            claims = self.claims_for(record)
            claim_cache[record.id] = claims
            conflict = self._conflict_for(record, claims)
            if conflict is not None:
                self.conflicts[record.id] = conflict

        matches: list[Match] = []
        confirmed: set[str] = set()
        for left in self.by_source[self.left_source]:
            left_conflict = self.conflicts.get(left.id)
            if left_conflict is not None and left_conflict.kind != "manual-override":
                continue
            target = self._unique_hard_target(claim_cache[left.id])
            if target is None:
                continue
            right = self.by_id[target]
            right_conflict = self.conflicts.get(right.id)
            if right_conflict is not None and right_conflict.kind != "manual-override":
                continue
            if right.source != self.right_source:
                continue
            if self._unique_hard_target(claim_cache[right.id]) != left.id:
                continue
            evidence = self._evidence(left, right)
            if not any(item.basis in HARD_BASES for item in evidence):
                continue
            if left.id in confirmed or right.id in confirmed:
                # Uniqueness on both sides should already prevent this; guard
                # anyway rather than silently letting one side win a race.
                raise CatalogError(
                    "one-to-one resolution produced a duplicate pairing at "
                    f"{left.id!r} or {right.id!r}"
                )
            confirmed.add(left.id)
            confirmed.add(right.id)
            matches.append(
                Match(
                    id=stable_key("match", (left.id, right.id)),
                    left=left.id,
                    right=right.id,
                    basis=tuple(item.basis for item in evidence),
                    status="confirmed",
                    evidence=evidence,
                )
            )
        matches.sort(key=lambda match: (match.left, match.right))

        groups = self._candidate_groups(confirmed)
        grouped_ids = {record_id for group in groups for record_id in group.records}
        unmatched: list[Unmatched] = []
        for record in self.records:
            if record.id in confirmed or record.id in grouped_ids:
                continue
            if record.id in self.conflicts:
                reason = "evidence disagreement; see conflicts"
            elif record.unencoded:
                reason = (
                    "unencoded record kept as its own entry; without an explicit "
                    "stable id or Unicode set it cannot form a provable pair"
                )
            else:
                reason = (
                    "no unique one-to-one counterpart from a stable id, an "
                    "identical Unicode set, or a human-confirmed mapping"
                )
            unmatched.append(Unmatched(record.id, reason))

        return Catalog(
            sources=self.sources,
            records=self.records,
            matches=tuple(matches),
            candidate_groups=tuple(groups),
            conflicts=tuple(
                sorted(
                    self.conflicts.values(),
                    key=lambda conflict: (conflict.record, conflict.id),
                )
            ),
            unmatched=tuple(sorted(unmatched, key=lambda item: item.record)),
        )

    def _candidate_groups(self, confirmed: set[str]) -> list[CandidateGroup]:
        """Partition unresolved records into non-overlapping evidence components.

        Edges are explicit Unicode-set ties, stable-id collisions, and conflicts.
        Name-only similarity is deliberately not an edge: an unencoded alternate
        stays a separate record instead of being folded into its base glyph.
        """

        unresolved = [
            record.id for record in self.records if record.id not in confirmed
        ]
        union_find = _UnionFind(unresolved)
        edges: set[tuple[str, str, str]] = set()

        by_unicode_set: dict[tuple[int, ...], list[str]] = defaultdict(list)
        for record_id in unresolved:
            record = self.by_id[record_id]
            if record.unicodes:
                by_unicode_set[record.unicodes].append(record_id)
        for record_ids in by_unicode_set.values():
            for index, first in enumerate(record_ids):
                for second in record_ids[index + 1 :]:
                    union_find.union(first, second)
                    edges.add(("unicode", first, second))

        for source in self.source_names:
            for stable_id in sorted(
                {
                    record.stable_id
                    for record in self.by_source[source]
                    if record.stable_id is not None
                }
            ):
                local = self.stable_index.get((source, stable_id), ())
                if len(local) < 2:
                    continue
                users = sorted(
                    {
                        record_id
                        for name in self.source_names
                        for record_id in self.stable_index.get((name, stable_id), ())
                        if record_id in unresolved
                    }
                )
                for index, first in enumerate(users):
                    for second in users[index + 1 :]:
                        union_find.union(first, second)
                        edges.add(("stable-id", first, second))

        conflict_ids_by_record: dict[str, list[str]] = defaultdict(list)
        for conflict in self.conflicts.values():
            if conflict.kind == "manual-override":
                # A human-confirmed pair stays confirmed; its override conflict
                # is reported but does not drag unrelated records into a
                # candidate group.
                continue
            if conflict.record not in unresolved:
                continue
            conflict_ids_by_record[conflict.record].append(conflict.id)
            participants = [
                target for claim in conflict.claims for target in claim.targets
            ]
            for target in participants:
                if target in unresolved:
                    union_find.union(conflict.record, target)
                conflict_ids_by_record[target].append(conflict.id)

        result: list[CandidateGroup] = []
        for record_ids in union_find.groups().values():
            if len(record_ids) < 2:
                continue
            basis_names: set[str] = set()
            evidence: list[Evidence] = []
            for record_id in record_ids:
                record = self.by_id[record_id]
                if record.unicodes and len(by_unicode_set[record.unicodes]) > 1:
                    basis_names.add("unicode")
                    evidence.append(
                        Evidence(
                            "unicode",
                            record_id,
                            record_id,
                            f"record has Unicode set "
                            f"{format_unicodes(record.unicodes)} "
                            f"({record.unicode_source}-derived)",
                        )
                    )
                if (
                    record.stable_id is not None
                    and len(
                        self.stable_index.get((record.source, record.stable_id), ())
                    )
                    > 1
                ):
                    basis_names.add("stable-id")
                    evidence.append(
                        Evidence(
                            "stable-id",
                            record_id,
                            record_id,
                            f"record carries stable id {record.stable_id!r}, "
                            f"which is not unique in source {record.source!r}",
                        )
                    )
                if record_id in conflict_ids_by_record:
                    basis_names.add("conflict")
                    evidence.append(
                        Evidence(
                            "conflict",
                            record_id,
                            record_id,
                            "record is named by conflict "
                            + ", ".join(sorted(set(conflict_ids_by_record[record_id]))),
                        )
                    )
            status = "conflicted" if "conflict" in basis_names else "ambiguous"
            unicodes = tuple(
                sorted(
                    {
                        cp
                        for record_id in record_ids
                        for cp in self.by_id[record_id].unicodes
                    }
                )
            )
            basis_text = ", ".join(sorted(basis_names, key=_basis_rank))
            result.append(
                CandidateGroup(
                    id=stable_key("candidate", record_ids),
                    records=tuple(record_ids),
                    unicodes=unicodes,
                    status=status,
                    reason=(
                        f"{len(record_ids)} unresolved records are linked by "
                        f"{basis_text}; no ordering-independent one-to-one split "
                        "is possible without a human decision"
                    ),
                    evidence=tuple(evidence),
                )
            )
        result.sort(key=lambda group: group.id)
        return result


def resolve_catalog(
    sources: Sequence[SourceInfo],
    records: Sequence[GlyphRecord],
    manual_pairs: Sequence[ManualPair] = (),
) -> Catalog:
    """Resolve two suppliers' records into a deterministic catalog.

    No branch prefers the left record, the first input, or a lower glyph name
    index when evidence disagrees; disagreement is emitted as a conflict and an
    unsplittable candidate group.
    """
    return _Resolver(sources, records, manual_pairs).build()


def summarize(catalog: Catalog) -> str:
    """Render the human-facing part of a catalog as plain text."""
    lines: list[str] = [
        f"Catalog: {catalog.sources[0].name} / {catalog.sources[1].name}",
        f"Records: {len(catalog.records)}",
        "",
        "Confirmed one-to-one correspondences",
    ]
    if not catalog.matches:
        lines.append("  (none)")
    for match in catalog.matches:
        left = catalog.record(match.left)
        right = catalog.record(match.right)
        lines.append(
            f"  {left.id} {format_unicodes(left.unicodes)}"
            f"  <->  {right.id} {format_unicodes(right.unicodes)}"
            f"  [{', '.join(match.basis)}]"
        )
    lines.extend(["", "Candidate groups (kept together, not split)"])
    if not catalog.candidate_groups:
        lines.append("  (none)")
    for group in catalog.candidate_groups:
        lines.append(f"  {group.id}  [{group.status}]")
        lines.append(f"    records: {', '.join(group.records)}")
        if group.unicodes:
            lines.append(f"    Unicode: {format_unicodes(group.unicodes)}")
        lines.append(f"    reason: {group.reason}")
    lines.extend(["", "Evidence conflicts"])
    if not catalog.conflicts:
        lines.append("  (none)")
    for conflict in catalog.conflicts:
        lines.append(f"  {conflict.id}  record: {conflict.record}")
        for claim in conflict.claims:
            targets = ", ".join(claim.targets) if claim.targets else "(none)"
            lines.append(f"    {claim.basis}: {claim.detail} -> {targets}")
    lines.extend(["", "Independent / unmatched records"])
    if not catalog.unmatched:
        lines.append("  (none)")
    for item in catalog.unmatched:
        record = catalog.record(item.record)
        lines.append(
            f"  {item.record} {format_unicodes(record.unicodes)}: {item.reason}"
        )
    return "\n".join(lines) + "\n"
