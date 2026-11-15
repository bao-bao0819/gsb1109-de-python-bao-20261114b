"""Failure-alert triage across two batches of JUnit archives.

When the failure taxonomy (the "failure dictionary") is revised, an old
failure category may be renamed, split into several new categories, or
merged with another one.  This module decides for every subscribed alert
whether it should keep firing (``continue``), be split (``split``), or be
ended (``end``).

Continuity is established exclusively by *shared failure samples*: the same
failing test case (identified by its ``classname`` and ``name``) must be
observed under a category in both archives.  Identical error text without a
shared sample never counts as continuity.

An *explanation* is a maximum-weight assignment in which every new category
is paired with at most one old category (an old category may pair with
several new categories, which is how a split is represented).  Several
equally good explanations may exist; an alert is only renewed when its
link(s) appear in **every** optimal explanation (unanimous agreement).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from glob import iglob
from itertools import chain
from pathlib import Path
from typing import Iterable

from . import Error, Failure, JUnitXml, Properties, TestCase

#: Decision emitted when an alert has exactly one unanimously approved link.
CONTINUE = "continue"
#: Decision emitted when an alert has several unanimously approved links.
SPLIT = "split"
#: Decision emitted when no link is unanimously approved.
END = "end"

#: The alert has positive links but the optimal explanations disagree.
CONTESTED = "contested"
#: The alert's old category shares no sample with any new category.
NO_SHARED_SAMPLE = "no_shared_sample"
#: The subscribed old category does not appear in the old archive at all.
CATEGORY_ABSENT = "category_absent"
#: Every optimal explanation contains the alert's link(s).
UNANIMOUS = "unanimous"

#: Structural relations between an old category and its renewed target(s).
RELATION_NONE = "none"
RELATION_ONE_TO_ONE = "one_to_one"
RELATION_SPLIT = "split"
RELATION_MERGE_CONTESTED = "merge_contested"

DEFAULT_CATEGORY_PROPERTY = "failure_category"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class ResultEntry:
    """One ``<failure>``/``<error>`` entry of a test case (a case may have several)."""

    kind: str
    type: str | None
    message: str | None
    text: str | None

    def signature(self) -> str:
        """Stable fingerprint of the error text; used for text-only overlap checks."""
        return "\x1f".join(
            (
                self.kind,
                self.type or "",
                (self.message or "").strip(),
                (self.text or "").strip(),
            )
        )

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "type": self.type,
            "message": self.message,
            "text": self.text,
        }


@dataclass
class Occurrence:
    """A failure sample observed in one archive file."""

    archive: str
    source_file: str
    suite: str | None
    category: str | None
    results: list[ResultEntry]

    def as_dict(self) -> dict:
        return {
            "archive": self.archive,
            "source_file": self.source_file,
            "suite": self.suite,
            "category": self.category,
            "results": [entry.as_dict() for entry in self.results],
        }


@dataclass
class FailureSample:
    """A concrete failing test case that can be confirmed across batches."""

    sample_id: str
    classname: str | None
    name: str
    file: str | None
    line: str | None
    occurrences: list[Occurrence] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "classname": self.classname,
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "occurrences": [occurrence.as_dict() for occurrence in self.occurrences],
        }


@dataclass
class CategoryObservation:
    """Everything observed about one category in one archive."""

    category: str
    archive: str
    samples: set[str] = field(default_factory=set)
    signatures: set[str] = field(default_factory=set)


@dataclass
class Edge:
    """A weighted old->new category link together with its supporting samples."""

    old_category: str
    new_category: str
    shared_sample_count: int
    sample_ids: list[str]

    def as_dict(self) -> dict:
        return {
            "old_category": self.old_category,
            "new_category": self.new_category,
            "shared_sample_count": self.shared_sample_count,
            "sample_ids": list(self.sample_ids),
        }


@dataclass
class AlertDecision:
    """Triage outcome for one subscribed alert."""

    alert_id: str
    old_category: str
    decision: str
    confidence: str
    target_categories: list[str]
    supporting_sample_ids: list[str]
    candidate_edges: list[dict]
    explanation_count: int
    relation: str
    rivals: list[str]

    def as_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "old_category": self.old_category,
            "decision": self.decision,
            "confidence": self.confidence,
            "relation": self.relation,
            "rivals": list(self.rivals),
            "target_categories": list(self.target_categories),
            "supporting_sample_ids": list(self.supporting_sample_ids),
            "candidate_edges": list(self.candidate_edges),
            "explanation_count": self.explanation_count,
        }


@dataclass
class TriageReport:
    """Full triage result, including every piece of supporting evidence."""

    meta: dict
    old_categories: list[str]
    new_categories: list[str]
    weight_matrix: dict[str, dict[str, int]]
    edge_samples: dict[tuple[str, str], list[str]]
    text_only_overlaps: list[dict]
    optimal_weight: int
    explanations: list[list[Edge]]
    decisions: list[AlertDecision]
    samples: dict[str, FailureSample]
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "meta": self.meta,
            "old_categories": list(self.old_categories),
            "new_categories": list(self.new_categories),
            "weight_matrix": {
                old: dict(sorted(columns.items()))
                for old, columns in sorted(self.weight_matrix.items())
            },
            "text_only_overlaps": list(self.text_only_overlaps),
            "optimal_weight": self.optimal_weight,
            "explanation_count": len(self.explanations),
            "explanations": [
                [edge.as_dict() for edge in explanation]
                for explanation in self.explanations
            ],
            "decisions": [decision.as_dict() for decision in self.decisions],
            "samples": {
                sample_id: sample.as_dict()
                for sample_id, sample in sorted(self.samples.items())
            },
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------


def sample_id_of(classname: str | None, name: str | None) -> str:
    """Stable cross-batch identity of a failing test case."""
    return f"{classname or ''}::{name or ''}"


def _case_property(case: TestCase, property_name: str) -> str | None:
    properties = case.child(Properties)
    if properties is None:
        return None
    for prop in properties:
        if prop.name == property_name and prop.value is not None:
            return prop.value
    return None


def extract_failure_samples(
    path: str, category_property: str = DEFAULT_CATEGORY_PROPERTY
) -> list[FailureSample]:
    """Extract every failing/erroring test case from one JUnit XML file."""
    xml = JUnitXml.fromfile(path)
    samples: list[FailureSample] = []
    for suite in xml:
        for case in suite:
            if case.is_passed or case.is_skipped:
                continue
            entries = [
                ResultEntry(
                    kind=type(result).__name__.lower(),
                    type=result.type,
                    message=result.message,
                    text=result.text,
                )
                for result in case.result
                if isinstance(result, (Failure, Error))
            ]
            if not entries:
                continue
            sample = FailureSample(
                sample_id=sample_id_of(case.classname, case.name),
                classname=case.classname,
                name=case.name or "",
                file=case._elem.attrib.get("file"),
                line=case._elem.attrib.get("line"),
            )
            sample.occurrences.append(
                Occurrence(
                    archive="",
                    source_file=path,
                    suite=suite.name,
                    category=_case_property(case, category_property),
                    results=entries,
                )
            )
            samples.append(sample)
    return samples


def _expand_paths(paths: Iterable[str], paths_are_globs: bool) -> list[str]:
    if paths_are_globs:
        return sorted(chain.from_iterable(iglob(path) for path in paths))
    return list(paths)


def read_archive(
    paths: Iterable[str],
    archive: str,
    paths_are_globs: bool = False,
    category_property: str = DEFAULT_CATEGORY_PROPERTY,
) -> dict[str, FailureSample]:
    """Read an archive (one batch, possibly sharded over several XML files).

    Returns a mapping ``sample_id -> FailureSample`` with repeated
    observations (e.g. the same case in multiple shards) merged.
    """
    registry: dict[str, FailureSample] = {}
    for path in _expand_paths(paths, paths_are_globs):
        for sample in extract_failure_samples(path, category_property):
            for occurrence in sample.occurrences:
                occurrence.archive = archive
            existing = registry.get(sample.sample_id)
            if existing is None:
                registry[sample.sample_id] = sample
            else:
                existing.occurrences.extend(sample.occurrences)
                if existing.file is None and sample.file is not None:
                    existing.file = sample.file
                if existing.line is None and sample.line is not None:
                    existing.line = sample.line
    return registry


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def load_classification_file(path: str) -> dict[str, list[dict]]:
    """Load a JSON classification file.

    Expected shape::

        {
          "old": [{"category": "A", "classname": "pkg.T", "name": "test_x"}],
          "new": [{"category": "X", "classname": "pkg.T", "name": "test_x"}]
        }

    ``file``/``line`` are optional and narrow the match when present.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    records: dict[str, list[dict]] = {"old": [], "new": []}
    for archive in records:
        entries = data.get(archive, [])
        if not isinstance(entries, list):
            raise ValueError(f"Classification entry for {archive!r} must be a list")
        records[archive] = entries
    return records


def _record_matches_sample(record: dict, sample: FailureSample) -> bool:
    if record.get("classname") != sample.classname or record.get("name") != sample.name:
        return False
    if record.get("file") is not None and record.get("file") != sample.file:
        return False
    if record.get("line") is not None and str(record.get("line")) != str(sample.line):
        return False
    return True


def classify_archive(
    registry: dict[str, FailureSample],
    archive: str,
    records: list[dict] | None,
    warnings: list[str],
) -> dict[str, CategoryObservation]:
    """Attach a category to every sample using records or inline properties."""
    observations: dict[str, CategoryObservation] = {}

    def assign(sample: FailureSample, category: str) -> None:
        observation = observations.setdefault(
            category, CategoryObservation(category=category, archive=archive)
        )
        if sample.sample_id in observation.samples:
            return
        observation.samples.add(sample.sample_id)
        for occurrence in sample.occurrences:
            if occurrence.archive == archive:
                occurrence.category = category
                for entry in occurrence.results:
                    observation.signatures.add(entry.signature())

    for sample in registry.values():
        inline = [
            occurrence.category
            for occurrence in sample.occurrences
            if occurrence.archive == archive and occurrence.category
        ]
        inline_categories = sorted(set(inline))
        matched: list[str] = []
        if records:
            matched = sorted(
                {
                    str(record["category"])
                    for record in records
                    if _record_matches_sample(record, sample)
                }
            )
        categories = sorted(set(inline_categories + matched))
        if not categories:
            warnings.append(
                f"{archive}: unclassified failure sample ignored: {sample.sample_id}"
            )
            continue
        if len(categories) > 1:
            warnings.append(
                f"{archive}: sample {sample.sample_id} maps to several categories "
                f"{categories}; keeping all memberships"
            )
        for category in categories:
            assign(sample, category)
    return observations


# ---------------------------------------------------------------------------
# Weight matrix and optimal explanations
# ---------------------------------------------------------------------------


def build_weight_matrix(
    old_observations: dict[str, CategoryObservation],
    new_observations: dict[str, CategoryObservation],
) -> tuple[dict[str, dict[str, int]], dict[tuple[str, str], list[str]]]:
    """Pair categories by shared sample count (zero-weight edges are dropped)."""
    matrix: dict[str, dict[str, int]] = {}
    edge_samples: dict[tuple[str, str], list[str]] = {}
    for old_category in sorted(old_observations):
        old_samples = old_observations[old_category].samples
        for new_category in sorted(new_observations):
            shared = old_samples & new_observations[new_category].samples
            if not shared:
                continue
            sample_ids = sorted(shared)
            matrix.setdefault(old_category, {})[new_category] = len(sample_ids)
            edge_samples[(old_category, new_category)] = sample_ids
    return matrix, edge_samples


def find_text_only_overlaps(
    old_observations: dict[str, CategoryObservation],
    new_observations: dict[str, CategoryObservation],
    edge_samples: dict[tuple[str, str], list[str]],
) -> list[dict]:
    """Category pairs with identical error text but no shared sample.

    These are explicitly *not* treated as continuity.
    """
    overlaps: list[dict] = []
    for old_category in sorted(old_observations):
        old_observation = old_observations[old_category]
        for new_category in sorted(new_observations):
            if edge_samples.get((old_category, new_category)):
                continue
            shared_text = sorted(
                old_observation.signatures & new_observations[new_category].signatures
            )
            if shared_text:
                overlaps.append(
                    {
                        "old_category": old_category,
                        "new_category": new_category,
                        "shared_error_signatures": shared_text,
                        "note": "identical error text without a shared sample; "
                        "not treated as continuity",
                    }
                )
    return overlaps


def enumerate_optimal_explanations(
    matrix: dict[str, dict[str, int]],
) -> tuple[int, list[list[tuple[str, str, int]]]]:
    """Enumerate every maximum-weight explanation.

    Each new category is assigned to at most one old category; an old
    category may receive several new categories (a split).  Enumeration is
    deterministic: nodes and edges are visited in sorted order.
    """
    new_nodes = sorted({new for columns in matrix.values() for new in columns})
    edges_by_new: dict[str, list[tuple[str, int]]] = {}
    for new_category in new_nodes:
        edges = [
            (old_category, matrix[old_category][new_category])
            for old_category in sorted(matrix)
            if new_category in matrix[old_category]
        ]
        edges_by_new[new_category] = sorted(edges, key=lambda item: (-item[1], item[0]))

    # Admissible upper bound: best edge weight of each remaining new node.
    upper_bounds = [0] * (len(new_nodes) + 1)
    running = 0
    for index in range(len(new_nodes) - 1, -1, -1):
        edges = edges_by_new[new_nodes[index]]
        running += edges[0][1] if edges else 0
        upper_bounds[index] = running

    best_weight = -1
    best: list[list[tuple[str, str, int]]] = []
    chosen: list[tuple[str, str, int]] = []

    def search(index: int, total: int) -> None:
        nonlocal best_weight
        if index == len(new_nodes):
            if total > best_weight:
                best_weight = total
                best.clear()
                best.append(list(chosen))
            elif total == best_weight:
                best.append(list(chosen))
            return
        if total + upper_bounds[index] < max(best_weight, 0):
            return
        new_category = new_nodes[index]
        # Option 1: the new category has no old counterpart (an ended lineage).
        search(index + 1, total)
        # Option 2: assign it to one of the old categories it overlaps with.
        for old_category, weight in edges_by_new[new_category]:
            chosen.append((old_category, new_category, weight))
            search(index + 1, total + weight)
            chosen.pop()

    search(0, 0)
    return max(best_weight, 0), best


def _unanimous_edges(
    explanations: list[list[tuple[str, str, int]]],
) -> set[tuple[str, str]]:
    if not explanations:
        return set()
    edge_sets = [
        {(old_category, new_category) for old_category, new_category, _ in explanation}
        for explanation in explanations
    ]
    return set.intersection(*edge_sets)


# ---------------------------------------------------------------------------
# Subscriptions and decisions
# ---------------------------------------------------------------------------


def load_subscriptions(path: str | None) -> list[dict]:
    """Load subscribed alerts as ``[{"alert_id": ..., "old_category": ...}]``."""
    if path is None:
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return [
            {"alert_id": alert_id, "old_category": old_category}
            for alert_id, old_category in sorted(data.items())
        ]
    subscriptions = []
    for entry in data:
        subscriptions.append(
            {
                "alert_id": str(entry["alert_id"]),
                "old_category": str(entry["old_category"]),
            }
        )
    return subscriptions


def decide_alerts(
    subscriptions: list[dict],
    old_observations: dict[str, CategoryObservation],
    matrix: dict[str, dict[str, int]],
    edge_samples: dict[tuple[str, str], list[str]],
    explanations: list[list[tuple[str, str, int]]],
) -> list[AlertDecision]:
    """Turn the optimal explanations into per-alert decisions."""
    unanimous = _unanimous_edges(explanations)
    decisions: list[AlertDecision] = []
    if not subscriptions:
        subscriptions = [
            {"alert_id": category, "old_category": category}
            for category in sorted(old_observations)
        ]

    for subscription in sorted(subscriptions, key=lambda item: item["alert_id"]):
        alert_id = str(subscription["alert_id"])
        old_category = str(subscription["old_category"])
        candidates = sorted(
            matrix.get(old_category, {}).items(), key=lambda item: (-item[1], item[0])
        )
        candidate_edges = [
            {
                "new_category": new_category,
                "shared_sample_count": weight,
                "unanimous": (old_category, new_category) in unanimous,
            }
            for new_category, weight in candidates
        ]

        if old_category not in old_observations:
            confidence = CATEGORY_ABSENT
        elif not candidates:
            confidence = NO_SHARED_SAMPLE
        else:
            confidence = CONTESTED

        approved = [
            (new_category, weight)
            for new_category, weight in candidates
            if (old_category, new_category) in unanimous
        ]
        target_categories = [new_category for new_category, _ in approved]
        supporting_ids = sorted(
            set(
                chain.from_iterable(
                    edge_samples[(old_category, new_category)]
                    for new_category in target_categories
                )
            )
        )

        if len(approved) == 1:
            decision = CONTINUE
            confidence = UNANIMOUS
        elif len(approved) > 1:
            decision = SPLIT
            confidence = UNANIMOUS
        else:
            decision = END

        if decision == END:
            relation = RELATION_NONE
            rivals: list[str] = []
        elif decision == SPLIT:
            relation = RELATION_SPLIT
            rivals = []
        else:
            target = target_categories[0]
            rivals = sorted(
                other
                for other in matrix
                if other != old_category and target in matrix[other]
            )
            relation = RELATION_MERGE_CONTESTED if rivals else RELATION_ONE_TO_ONE

        decisions.append(
            AlertDecision(
                alert_id=alert_id,
                old_category=old_category,
                decision=decision,
                confidence=confidence,
                target_categories=target_categories,
                supporting_sample_ids=supporting_ids,
                candidate_edges=candidate_edges,
                explanation_count=len(explanations),
                relation=relation,
                rivals=rivals,
            )
        )
    return decisions


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def triage(
    old_paths: Iterable[str],
    new_paths: Iterable[str],
    *,
    paths_are_globs: bool = False,
    classification_file: str | None = None,
    category_property: str = DEFAULT_CATEGORY_PROPERTY,
    subscriptions_file: str | None = None,
) -> TriageReport:
    """Run the full continue/split/end analysis over two JUnit archives."""
    old_paths = list(old_paths)
    new_paths = list(new_paths)
    warnings: list[str] = []

    old_registry = read_archive(old_paths, "old", paths_are_globs, category_property)
    new_registry = read_archive(new_paths, "new", paths_are_globs, category_property)

    classification = (
        load_classification_file(classification_file)
        if classification_file
        else {"old": [], "new": []}
    )
    old_observations = classify_archive(
        old_registry, "old", classification["old"], warnings
    )
    new_observations = classify_archive(
        new_registry, "new", classification["new"], warnings
    )

    matrix, edge_samples = build_weight_matrix(old_observations, new_observations)
    text_only = find_text_only_overlaps(
        old_observations, new_observations, edge_samples
    )
    optimal_weight, raw_explanations = enumerate_optimal_explanations(matrix)
    explanations = [
        [
            Edge(
                old_category=old_category,
                new_category=new_category,
                shared_sample_count=weight,
                sample_ids=edge_samples[(old_category, new_category)],
            )
            for old_category, new_category, weight in explanation
        ]
        for explanation in raw_explanations
    ]

    subscriptions = load_subscriptions(subscriptions_file)
    decisions = decide_alerts(
        subscriptions, old_observations, matrix, edge_samples, raw_explanations
    )

    samples = dict(old_registry)
    for sample_id, sample in new_registry.items():
        existing = samples.get(sample_id)
        if existing is None:
            samples[sample_id] = sample
        else:
            existing.occurrences.extend(sample.occurrences)

    meta = {
        "old_archive": _expand_paths(old_paths, paths_are_globs),
        "new_archive": _expand_paths(new_paths, paths_are_globs),
        "category_source": (
            classification_file
            if classification_file
            else f"testcase property '{category_property}'"
        ),
        "subscriptions": subscriptions_file,
        "sample_identity": "testcase classname + name",
        "min_shared_samples": 1,
    }
    return TriageReport(
        meta=meta,
        old_categories=sorted(old_observations),
        new_categories=sorted(new_observations),
        weight_matrix=matrix,
        edge_samples=edge_samples,
        text_only_overlaps=text_only,
        optimal_weight=optimal_weight,
        explanations=explanations,
        decisions=decisions,
        samples=samples,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def to_markdown(report: TriageReport) -> str:
    """Render a compact human-readable summary of a triage report."""
    lines = ["# Failure alert triage", ""]
    lines.append("## Decisions")
    lines.append("")
    lines.append(
        "| Alert | Old category | Decision | Confidence | Targets | Shared samples |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for decision in report.decisions:
        lines.append(
            "| {alert} | {old} | {decision} | {confidence} | "
            "{targets} | {count} |".format(
                alert=decision.alert_id,
                old=decision.old_category,
                decision=decision.decision,
                confidence=decision.confidence,
                targets=", ".join(decision.target_categories) or "-",
                count=len(decision.supporting_sample_ids),
            )
        )
    lines.append("")
    lines.append(
        f"Optimal weight: {report.optimal_weight} across "
        f"{len(report.explanations)} optimal explanation(s)."
    )
    lines.append("")
    lines.append("## Optimal explanations")
    for index, explanation in enumerate(report.explanations, start=1):
        rendered = ", ".join(
            f"{edge.old_category}->{edge.new_category} ({edge.shared_sample_count})"
            for edge in explanation
        )
        lines.append(f"{index}. {rendered or '(empty)'}")
    if report.text_only_overlaps:
        lines.append("")
        lines.append("## Text-only overlaps (not continuity)")
        for overlap in report.text_only_overlaps:
            lines.append(
                f"- {overlap['old_category']} vs {overlap['new_category']}: "
                f"{len(overlap['shared_error_signatures'])} identical error "
                "signature(s), no shared sample"
            )
    if report.warnings:
        lines.append("")
        lines.append("## Warnings")
        for warning in report.warnings:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "alert"


def write_evidence_files(report: TriageReport, evidence_dir: str | Path) -> list[str]:
    """Write one JSON file per renewed alert with its supporting samples."""
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for decision in report.decisions:
        if decision.decision == END:
            continue
        payload = {
            "decision": decision.as_dict(),
            "supporting_samples": [
                report.samples[sample_id].as_dict()
                for sample_id in decision.supporting_sample_ids
            ],
        }
        target = directory / f"{_safe_filename(decision.alert_id)}.json"
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written.append(str(target))
    return written
