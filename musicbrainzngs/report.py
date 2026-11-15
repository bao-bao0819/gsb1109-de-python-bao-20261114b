"""Auditable summaries of recording appearances.

MusicBrainz keeps recordings separate from the releases and track positions
where they appear.  A report therefore needs three different counts:

``track_position_count``
    Every distinct release/medium/track position.  The same recording on two
    media of one release counts twice, so a double-disc release cannot
    silently lose a position.

``independent_recording_count``
    The distinct recording MBIDs among those positions.  Reusing one
    recording on a single, the original album and a later compilation does
    not inflate this count.

``evidenced_remaster_count``
    The recordings for which a formal ``remaster`` relationship identifies
    them as a remaster version.

Missing relationships only mean that the evidence is insufficient.  A title
or disambiguation containing "Remastered" is reported as a title hint but
never used as evidence for a remaster version or for a separate audio source.
When formal relationships are added later, :func:`compare_reports` shows
exactly which counts changed and which did not.

The complete report keeps every counted position, every normalised
relationship, and per-recording details so that each total can be traced
back to the retrieved MusicBrainz data.
"""

from __future__ import print_function

import collections
import json

from musicbrainzngs import compat
from musicbrainzngs import mbxml

__all__ = [
    "RecordingReport",
    "RecordingSummary",
    "ReportComparison",
    "CountChange",
    "build_recording_report",
    "build_report",
    "summarize_recordings",
    "compare_reports",
    "format_changes",
    "format_report",
]

REMASTER_RELATION_TYPE = "remaster"

TRACK_POSITION_COUNT = "track_position_count"
INDEPENDENT_RECORDING_COUNT = "independent_recording_count"
EVIDENCED_REMASTER_COUNT = "evidenced_remaster_count"
KNOWN_RECORDING_COUNT = "known_recording_count"
TITLE_HINT_ONLY_COUNT = "title_hint_only_count"
POSITIONS_WITHOUT_RECORDING_COUNT = "positions_without_recording_count"

PUBLISHED_COUNTS = (
    TRACK_POSITION_COUNT,
    INDEPENDENT_RECORDING_COUNT,
    EVIDENCED_REMASTER_COUNT,
)

DIAGNOSTIC_COUNTS = (
    KNOWN_RECORDING_COUNT,
    TITLE_HINT_ONLY_COUNT,
    POSITIONS_WITHOUT_RECORDING_COUNT,
)

_COUNT_ALIASES = {
    TRACK_POSITION_COUNT: TRACK_POSITION_COUNT,
    "track_positions": TRACK_POSITION_COUNT,
    "position_count": TRACK_POSITION_COUNT,
    "track_appearance_count": TRACK_POSITION_COUNT,
    "appearance_count": TRACK_POSITION_COUNT,
    "appearances_count": TRACK_POSITION_COUNT,
    "occurrence_count": TRACK_POSITION_COUNT,
    "total_track_positions": TRACK_POSITION_COUNT,
    INDEPENDENT_RECORDING_COUNT: INDEPENDENT_RECORDING_COUNT,
    "independent_recordings": INDEPENDENT_RECORDING_COUNT,
    "independent_recording_number": INDEPENDENT_RECORDING_COUNT,
    "distinct_recording_count": INDEPENDENT_RECORDING_COUNT,
    "distinct_recordings": INDEPENDENT_RECORDING_COUNT,
    "unique_recording_count": INDEPENDENT_RECORDING_COUNT,
    "recording_count": INDEPENDENT_RECORDING_COUNT,
    EVIDENCED_REMASTER_COUNT: EVIDENCED_REMASTER_COUNT,
    "evidenced_remaster_version_count": EVIDENCED_REMASTER_COUNT,
    "remastered_version_count": EVIDENCED_REMASTER_COUNT,
    "evidenced_remastered_count": EVIDENCED_REMASTER_COUNT,
    "remaster_version_count": EVIDENCED_REMASTER_COUNT,
    "remaster_versions": EVIDENCED_REMASTER_COUNT,
    "remaster_count": EVIDENCED_REMASTER_COUNT,
    KNOWN_RECORDING_COUNT: KNOWN_RECORDING_COUNT,
    "distinct_known_recordings": KNOWN_RECORDING_COUNT,
    TITLE_HINT_ONLY_COUNT: TITLE_HINT_ONLY_COUNT,
    "title_only_remaster_count": TITLE_HINT_ONLY_COUNT,
    POSITIONS_WITHOUT_RECORDING_COUNT: POSITIONS_WITHOUT_RECORDING_COUNT,
}

_DETAIL_ALIASES = {
    "tracks": "positions",
    "track_positions": "positions",
    "appearances": "positions",
    "occurrences": "positions",
    "row": "recordings",
    "rows": "recordings",
    "recording_rows": "recordings",
    "recording_details": "recordings",
    "remaster_details": "remasters",
    "relationship_list": "relations",
    "releases": "release_ids",
    "diagnostics": "coverage",
}

_SOURCE_LABELS = {
    "pregap": "pregap",
    "track-list": "audio",
    "data-track-list": "data",
}

_SOURCE_ORDER = {"pregap": 0, "audio": 1, "data": 2}


def _string(value):
    if value is None:
        return ""
    if isinstance(value, compat.bytes):
        return value.decode("utf-8", "replace")
    return compat.unicode(value)


def _is_text(value):
    return isinstance(value, compat.basestring)


def _looks_like_xml(value):
    if isinstance(value, compat.bytes):
        head = value.lstrip()[:1]
        return head == b"<"
    if isinstance(value, compat.unicode):
        return value.lstrip().startswith("<")
    return False


def _id_of(value):
    if isinstance(value, dict):
        for key in ("id", "recording-id", "recording_id"):
            identifier = value.get(key)
            if identifier:
                return _string(identifier)
        return None
    if _is_text(value):
        return _string(value)
    return None


def _numeric(value, fallback=0):
    try:
        return int(str(value).strip().split(".")[0])
    except (TypeError, ValueError):
        return fallback


def _parse_xml(value):
    try:
        return mbxml.parse_message(value)
    except Exception as error:
        raise ValueError("could not parse MusicBrainz XML: %s" % error)


def _iter_release_contexts(value):
    """Yield ``(release, inherited recording)`` pairs.

    A recording response retrieved with the ``releases`` include nests
    ``release-list`` entries whose tracks do not repeat the recording MBID.
    Yielding the enclosing recording lets those positions stay traceable.
    """
    if value is None:
        return
    if isinstance(value, dict):
        if "release" in value and isinstance(value["release"], dict):
            for context in _iter_release_contexts(value["release"]):
                yield context
            return
        if "release-list" in value:
            for release in value.get("release-list") or []:
                for context in _iter_release_contexts(release):
                    yield context
            return
        if "medium-list" in value:
            yield value, None
            return
        if "disc" in value and isinstance(value["disc"], dict):
            for context in _iter_release_contexts(value["disc"]):
                yield context
            return
        if "cdstub" in value and isinstance(value["cdstub"], dict):
            yield _cdstub_as_release(value["cdstub"]), None
            return
        if "recording" in value and isinstance(value["recording"], dict):
            recording = value["recording"]
            for release in recording.get("release-list") or []:
                yield release, recording
            return
        if "recording-list" in value:
            for recording in value.get("recording-list") or []:
                for release in recording.get("release-list") or []:
                    yield release, recording
            return
    elif isinstance(value, (list, tuple)):
        for item in value:
            for context in _iter_release_contexts(item):
                yield context
    elif hasattr(value, "read"):
        for context in _iter_release_contexts(_parse_xml(value)):
            yield context
    elif _looks_like_xml(value):
        for context in _iter_release_contexts(_parse_xml(value)):
            yield context


def _iter_relations(value, warnings):
    """Yield ``(source_recording_id, relation)`` pairs from supported input.

    Supported forms are a recording entity or response containing a
    ``recording-relation-list``, a mapping of recording MBID to relation
    lists, single relation dictionaries, ``(recording MBID, relation)``
    pairs and lists of those.  XML bytes, text or file objects are parsed
    first.
    """
    if value is None:
        return
    if (isinstance(value, tuple) and len(value) == 2
            and _is_text(value[0])):
        if isinstance(value[1], dict):
            yield _string(value[0]), value[1]
            return
        if isinstance(value[1], (list, tuple)):
            for relation in value[1]:
                if isinstance(relation, dict):
                    yield _string(value[0]), relation
                else:
                    warnings.append(
                        "ignored relation input for recording %s"
                        % _string(value[0])
                    )
            return
    if isinstance(value, dict):
        relation_keys = [
            key for key in value if key.endswith("-relation-list")
        ]
        if relation_keys:
            source = _id_of(value)
            for key in relation_keys:
                for relation in value[key] or []:
                    yield source, relation
            return
        if _looks_like_relation(value):
            yield _source_id(value), value
            return
        if "recording" in value and isinstance(value["recording"], dict):
            for pair in _iter_relations(value["recording"], warnings):
                yield pair
            return
        if "recording-list" in value:
            for recording in value.get("recording-list") or []:
                for pair in _iter_relations(recording, warnings):
                    yield pair
            return
        for source, relations in value.items():
            if not _is_text(source) or not isinstance(relations, (list, tuple)):
                continue
            relation_items = [
                relation for relation in relations if isinstance(relation, dict)
            ]
            if not any(_looks_like_relation(relation)
                       for relation in relation_items):
                continue
            for relation in relations:
                if isinstance(relation, dict):
                    yield _string(source), relation
                else:
                    warnings.append(
                        "ignored relation input for recording %s" % source
                    )
    elif isinstance(value, (list, tuple)):
        for item in value:
            for pair in _iter_relations(item, warnings):
                yield pair
    elif hasattr(value, "read"):
        for pair in _iter_relations(_parse_xml(value), warnings):
            yield pair
    elif _looks_like_xml(value):
        for pair in _iter_relations(_parse_xml(value), warnings):
            yield pair


def _looks_like_relation(value):
    if not isinstance(value, dict) or "type" not in value:
        return False
    return any(
        key in value
        for key in (
            "target",
            "target-id",
            "target_id",
            "target_recording_id",
            "target-recording-id",
            "target_recording",
            "recording",
            "remaster_recording_id",
        )
    )


def _relation_type(relation):
    return _string(
        relation.get("type")
        or relation.get("relation-type")
        or relation.get("relation_type")
    ).strip().lower()


def _target_id(relation):
    for key in (
        "target_recording_id",
        "target-recording-id",
        "target_recording",
        "target-recording",
        "target_id",
        "target-id",
        "target",
        "recording",
    ):
        identifier = _id_of(relation.get(key))
        if identifier:
            return identifier
    return None


def _source_id(relation):
    for key in (
        "source_recording_id",
        "source-recording-id",
        "source_recording",
        "source-recording",
        "source_id",
        "source-id",
        "source",
    ):
        identifier = _id_of(relation.get(key))
        if identifier:
            return identifier
    return None


def _normalise_relations(relation_inputs, remaster_types, warnings):
    relations = []
    seen = set()
    for value in relation_inputs:
        for source, relation in _iter_relations(value, warnings):
            if not isinstance(relation, dict):
                warnings.append("ignored a relation that is not a dictionary")
                continue
            relation_type = _relation_type(relation)
            if relation_type not in remaster_types:
                continue
            target_type = _string(relation.get("target-type") or "").lower()
            if target_type and target_type != "recording":
                warnings.append(
                    "ignored %s relation whose target type is %s"
                    % (relation_type, target_type)
                )
                continue

            explicit_remaster = _id_of(
                relation.get("remaster_recording_id")
            )
            explicit_original = _id_of(
                relation.get("original_recording_id")
            )
            target = _target_id(relation)
            if source is None:
                source = _source_id(relation)
            direction = _string(relation.get("direction") or "").strip().lower()

            if explicit_remaster and explicit_original:
                remaster_id, original_id = explicit_remaster, explicit_original
                if source is None:
                    source = explicit_remaster
                if target is None:
                    target = explicit_original
            else:
                remaster_id = original_id = None
                if source and target:
                    if direction == "forward":
                        remaster_id, original_id = source, target
                    elif direction == "backward":
                        remaster_id, original_id = target, source
                    else:
                        remaster_id, original_id = source, target
                        warnings.append(
                            "remaster relation %s -> %s has no direction; "
                            "the source recording is treated as the remaster"
                            % (source, target)
                        )
            if not source or not target:
                warnings.append(
                    "ignored %s relation without both recording ids"
                    % relation_type
                )
                continue
            if source == target:
                warnings.append(
                    "ignored %s relation on recording %s pointing to itself"
                    % (relation_type, source)
                )
                continue
            if not remaster_id or not original_id:
                warnings.append(
                    "ignored %s relation %s -> %s without evidence of which "
                    "recording is the remaster" % (relation_type, source, target)
                )
                continue
            if remaster_id == original_id:
                warnings.append(
                    "ignored %s relation %s -> %s that identifies one recording"
                    % (relation_type, source, target)
                )
                continue
            key = (remaster_id, original_id)
            if key in seen:
                continue
            seen.add(key)
            relations.append({
                "type": relation_type,
                "type-id": relation.get("type-id"),
                "source_recording_id": source,
                "target_recording_id": target,
                "direction": direction or None,
                "remaster_recording_id": remaster_id,
                "original_recording_id": original_id,
            })
    return relations


def _medium_tracks(medium):
    pregap = medium.get("pregap")
    if isinstance(pregap, dict):
        yield "pregap", pregap
    for key in ("track-list", "data-track-list"):
        for track in medium.get(key) or []:
            if isinstance(track, dict):
                yield key, track


def _release_identity(release):
    release_id = release.get("id")
    if release_id:
        return ("mbid", _string(release_id))
    return ("object", id(release))


def _has_title_hint(*values):
    for value in values:
        text = _string(value).lower()
        if ("remaster" in text or "re-master" in text
                or "重制" in text or "重製" in text):
            return True
    return False


class _ReleaseContexts(list):
    """Explicit ``(release, inherited recording)`` pairs.

    Passing the contexts back in avoids re-interpreting a plain tuple of
    releases as one release plus an inherited recording.
    """


def _normalise_contexts(releases):
    """Return ``(release, inherited recording)`` pairs."""
    if isinstance(releases, _ReleaseContexts):
        return list(releases)
    return list(_iter_release_contexts(releases))


def _collect_positions(release_contexts, warnings):
    positions = []
    position_keys = set()
    release_ids = []
    release_mbids = []
    seen_release_ids = set()
    embedded_relations = []

    for release_index, (release, inherited_recording) in enumerate(
            release_contexts):
        if not isinstance(release, dict):
            warnings.append("ignored a release that is not a dictionary")
            continue
        identity = _release_identity(release)
        release_mbid = _string(release.get("id"))
        release_key = release_mbid
        if not release_key:
            release_key = "anonymous-release-%d" % (release_index + 1)
            warnings.append(
                "release at input position %d has no MBID; it is counted "
                "but cannot be traced to a release MBID" % (release_index + 1)
            )
        if release_key not in seen_release_ids:
            seen_release_ids.add(release_key)
            release_ids.append(release_key)
        if release_mbid and release_mbid not in release_mbids:
            release_mbids.append(release_mbid)
        release_group = release.get("release-group") or {}
        release_title_hint = _has_title_hint(release.get("title"))

        for medium_index, medium in enumerate(release.get("medium-list") or []):
            if not isinstance(medium, dict):
                warnings.append("ignored a medium that is not a dictionary")
                continue
            medium_position = medium.get("position")
            if medium_position in (None, ""):
                medium_position = str(medium_index + 1)
            medium_position = _string(medium_position)
            source_counts = collections.Counter()
            for track_index, (source_key, track) in enumerate(
                    _medium_tracks(medium)):
                source = _SOURCE_LABELS.get(source_key, source_key)
                source_counts[source] += 1
                track_position = track.get("position")
                if track_position in (None, ""):
                    track_position = (
                        "0" if source == "pregap"
                        else track.get("number")
                    )
                if track_position in (None, ""):
                    track_position = str(source_counts[source])
                track_position = _string(track_position)
                key = (identity, medium_position, source, track_position)
                if key in position_keys:
                    warnings.append(
                        "ignored duplicate position release=%s medium=%s "
                        "position=%s (source=%s)"
                        % (release_key, medium_position, track_position, source)
                    )
                    continue
                position_keys.add(key)

                recording = track.get("recording") or {}
                if not isinstance(recording, dict):
                    recording = {}
                inherited_id = _id_of(inherited_recording)
                inherited_relations = (
                    inherited_recording.get("recording-relation-list")
                    if isinstance(inherited_recording, dict) else None
                )
                recording_id = _id_of(recording) or inherited_id
                if not recording_id:
                    warnings.append(
                        "track at release=%s medium=%s position=%s has no "
                        "recording MBID"
                        % (release_key, medium_position, track_position)
                    )
                relation_list = (
                    recording.get("recording-relation-list")
                    or inherited_relations
                )
                if relation_list:
                    embedded_relations.append((recording_id, relation_list))

                positions.append({
                    "release_id": release_key,
                    "release_mbid": release_mbid or None,
                    "release_index": release_index,
                    "release_title": release.get("title"),
                    "release_date": release.get("date"),
                    "release_status": release.get("status"),
                    "release_group_id": _id_of(release_group),
                    "release_title_hint": release_title_hint,
                    "medium_position": medium_position,
                    "medium_format": medium.get("format"),
                    "medium_title": medium.get("title"),
                    "source": source,
                    "track_position": track_position,
                    "track_number": track.get("number"),
                    "track_id": _id_of(track),
                    "recording_id": recording_id,
                    "recording_title": (
                        recording.get("title")
                        or (inherited_recording or {}).get("title")
                    ),
                    "recording_disambiguation": (
                        recording.get("disambiguation")
                        or (inherited_recording or {}).get("disambiguation")
                    ),
                })

    def sort_key(position):
        return (
            position["release_index"],
            _numeric(position["medium_position"]),
            _SOURCE_ORDER.get(position["source"], 99),
            _numeric(position["track_position"]),
            _string(position["track_id"]),
        )

    positions.sort(key=sort_key)
    return positions, release_ids, release_mbids, embedded_relations


def _unique(values):
    result = []
    for value in values:
        if value is None:
            continue
        if value not in result:
            result.append(value)
    return result


class RecordingReport(dict):
    """A dict-like report retaining the evidence behind every count."""

    def __missing__(self, key):
        if key in _COUNT_ALIASES:
            return dict.__getitem__(self, _COUNT_ALIASES[key])
        if key in _DETAIL_ALIASES:
            return dict.__getitem__(self, _DETAIL_ALIASES[key])
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        if dict.__contains__(self, key):
            return True
        if key in _COUNT_ALIASES or key in _DETAIL_ALIASES:
            return True
        return False

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            pass
        if name in _COUNT_ALIASES or name in _DETAIL_ALIASES:
            raise AttributeError(name)
        raise AttributeError(
            "%r is not a report count or detail" % name
        )

    def metric(self, name):
        """Return one count by canonical name or a common alias."""
        return self[name]

    def counts(self):
        """Return the three published counts."""
        return dict(
            (name, dict.__getitem__(self, name)) for name in PUBLISHED_COUNTS
        )

    def diagnostics(self):
        """Return supporting counts that never use title text as evidence."""
        return dict(
            (name, dict.__getitem__(self, name)) for name in DIAGNOSTIC_COUNTS
        )

    def with_relations(self, relations=None, recordings=None):
        """Return a new report built with additional formal relationships."""
        inputs = list(self._relation_inputs)
        if recordings is not None:
            inputs.append(recordings)
        if relations is not None:
            inputs.append(relations)
        return build_recording_report(
            self._source_releases,
            relations=inputs,
            include_embedded_relations=self._include_embedded_relations,
            remaster_relation_types=self._remaster_relation_types,
        )

    def changes_from(self, before):
        """Return count changes between ``before`` and this report."""
        return compare_reports(before, self)

    def format(self, include_unchanged=False):
        """Format the published counts as human-readable lines."""
        return format_report(self, include_unchanged=include_unchanged)

    def to_json(self, indent=2):
        """Serialise the complete report, including its evidence."""
        return json.dumps(self, ensure_ascii=False, indent=indent, default=str)

    def summary(self):
        """Return the published counts plus a short change-friendly view."""
        result = self.counts()
        result["release_count"] = self["coverage"]["release_count"]
        result["title_hint_only_count"] = self[TITLE_HINT_ONLY_COUNT]
        result["positions_without_recording_count"] = (
            self[POSITIONS_WITHOUT_RECORDING_COUNT]
        )
        return result

    def rows(self):
        """Return one traceable row per counted track position."""
        return list(self["positions"])

    def evidence(self):
        """Return the formal relationships used as evidence."""
        return list(self["relations"])


class CountChange(int):
    """A signed delta that also carries before/after values.

    It behaves like an integer, so ``comparison[name] == 0`` works, while
    ``comparison[name]["before"]`` and ``.changed`` remain available.
    """

    def __new__(cls, name, before, after):
        value = after - before
        result = int.__new__(cls, value)
        result.name = name
        result.before = before
        result.after = after
        result.delta = value
        result.changed = before != after
        return result

    def __getitem__(self, key):
        if key in ("before", "after", "delta", "changed", "name"):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self):
        return {
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
            "changed": self.changed,
        }

    def __repr__(self):
        return "CountChange(%r, before=%d, after=%d, delta=%+d)" % (
            self.name, self.before, self.after, self.delta
        )


class ReportComparison(dict):
    """Count differences between two reports.

    Each compared count is a :class:`CountChange` (an integer delta with
    ``before``, ``after`` and ``changed`` attributes).  ``changed_counts``
    and ``unchanged_counts`` list the names, so callers can show which totals
    the new evidence affected and which stayed the same.
    """

    def __missing__(self, key):
        names = [name for name in self if isinstance(
            dict.__getitem__(self, name), CountChange)]
        if key in ("changed", "changed_counts"):
            return [
                name for name in names
                if dict.__getitem__(self, name).changed
            ]
        if key in ("unchanged", "unchanged_counts"):
            return [
                name for name in names
                if not dict.__getitem__(self, name).changed
            ]
        if key in ("deltas", "changes"):
            return dict(
                (name, dict.__getitem__(self, name)) for name in names
            )
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def changed_counts(self):
        return list(self["changed_counts"])

    def unchanged_counts(self):
        return list(self["unchanged_counts"])

    def detailed(self):
        """Return plain dictionaries for JSON or form output."""
        result = {
            name: value.as_dict() for name, value in self.items()
        }
        result["changed_counts"] = self.changed_counts()
        result["unchanged_counts"] = self.unchanged_counts()
        return result

    def to_json(self, indent=2):
        return json.dumps(self.detailed(), ensure_ascii=False, indent=indent)


def build_recording_report(
    releases=None,
    relations=None,
    recordings=None,
    include_embedded_relations=True,
    remaster_relation_types=None,
):
    """Build an auditable report from retrieved MusicBrainz data.

    :param releases: A release dictionary, a ``release-list`` response, a
        list of either, or MusicBrainz XML as bytes, text or a file object.
        Every medium in ``medium-list`` is read, including ``pregap``,
        ``track-list`` and ``data-track-list``.  Positions are identified by
        release MBID, medium position, source and track position, so two
        positions on a double-disc release remain two positions.
    :param relations: Formal recording relationships.  Accepts a recording
        entity or response with a ``recording-relation-list``, a mapping of
        recording MBID to relation lists, relation dictionaries,
        ``(recording MBID, relation)`` pairs, lists of those, or XML.
        Only formal remaster relationships are used as evidence.
    :param recordings: Recording entities whose relation lists should also
        be read, for example a ``get_recording_by_id`` response.
    :param include_embedded_relations: Read relation lists already embedded
        in recordings inside the supplied releases.
    :param remaster_relation_types: Optional iterable of extra relation type
        names to accept.  The standard MusicBrainz type is ``remaster``.
    :returns: A :class:`RecordingReport`.

    The report always contains ``track_position_count``,
    ``independent_recording_count`` and ``evidenced_remaster_count``.  It
    also keeps ``positions``, ``recordings``, ``remasters``, ``relations``,
    ``release_ids``, ``warnings`` and diagnostic counts for audit.
    """
    warnings = []
    release_contexts = _normalise_contexts(releases)
    release_list = [context[0] for context in release_contexts]
    positions, release_ids, release_mbids, embedded_relations = (
        _collect_positions(release_contexts, warnings)
    )

    accepted_types = set([REMASTER_RELATION_TYPE])
    if remaster_relation_types:
        accepted_types.update(
            _string(value).strip().lower()
            for value in remaster_relation_types
            if _string(value).strip()
        )
    accepted_types = frozenset(accepted_types)

    relation_inputs = []
    if include_embedded_relations and embedded_relations:
        relation_inputs.append(embedded_relations)
    if recordings is not None:
        relation_inputs.append(recordings)
    if relations is not None:
        relation_inputs.append(relations)
    normalised_relations = _normalise_relations(
        relation_inputs, accepted_types, warnings
    )

    known_by_recording = collections.OrderedDict()
    unknown_positions = []
    for position in positions:
        recording_id = position.get("recording_id")
        if recording_id:
            known_by_recording.setdefault(recording_id, []).append(position)
        else:
            unknown_positions.append(position)

    recording_id_set = set(known_by_recording)
    versions = set()
    originals = set()
    for relation in normalised_relations:
        remaster_id = relation["remaster_recording_id"]
        original_id = relation["original_recording_id"]
        if remaster_id in recording_id_set:
            versions.add(remaster_id)
        if original_id in recording_id_set:
            originals.add(original_id)
        if (remaster_id not in recording_id_set
                and original_id not in recording_id_set):
            warnings.append(
                "remaster relation %s -> %s was not counted because neither "
                "recording appears in the supplied releases"
                % (remaster_id, original_id)
            )
        elif (original_id in recording_id_set
                and remaster_id not in recording_id_set):
            warnings.append(
                "remaster evidence names recording %s as the remaster of %s, "
                "but that version has no position in the supplied releases"
                % (remaster_id, original_id)
            )

    recordings_detail = []
    title_hint_only_count = 0
    for recording_id, recording_positions in known_by_recording.items():
        if recording_id in versions and recording_id in originals:
            status = "evidenced_remaster_and_original"
        elif recording_id in versions:
            status = "evidenced_remaster"
        elif recording_id in originals:
            status = "evidenced_original"
        else:
            status = "insufficient_evidence"
        titles = _unique(
            position.get("recording_title")
            for position in recording_positions
        )
        disambiguations = _unique(
            position.get("recording_disambiguation")
            for position in recording_positions
        )
        title_hint = _has_title_hint(*(titles + disambiguations))
        if title_hint and recording_id not in versions:
            title_hint_only_count += 1
        recordings_detail.append({
            "recording_id": recording_id,
            "titles": titles,
            "disambiguations": disambiguations,
            "track_position_count": len(recording_positions),
            "release_ids": _unique(
                position.get("release_id")
                for position in recording_positions
            ),
            "positions": recording_positions,
            "evidence_status": status,
            "title_hint": title_hint,
            "remaster_of": sorted(set([
                relation["original_recording_id"]
                for relation in normalised_relations
                if relation["remaster_recording_id"] == recording_id
            ])),
            "remastered_by": sorted(set([
                relation["remaster_recording_id"]
                for relation in normalised_relations
                if relation["original_recording_id"] == recording_id
            ])),
        })

    for position in unknown_positions:
        recordings_detail.append({
            "recording_id": None,
            "titles": _unique([position.get("recording_title")]),
            "disambiguations": _unique([
                position.get("recording_disambiguation")
            ]),
            "track_position_count": 1,
            "release_ids": [position.get("release_id")],
            "positions": [position],
            "evidence_status": "missing_recording_id",
            "title_hint": _has_title_hint(
                position.get("recording_title"),
                position.get("recording_disambiguation"),
            ),
            "remaster_of": [],
            "remastered_by": [],
        })

    remasters = []
    for recording_id in sorted(versions):
        evidence = [
            relation for relation in normalised_relations
            if relation["remaster_recording_id"] == recording_id
        ]
        remasters.append({
            "recording_id": recording_id,
            "titles": _unique(
                position.get("recording_title")
                for position in known_by_recording.get(recording_id, [])
            ),
            "original_recording_ids": sorted(set([
                relation["original_recording_id"] for relation in evidence
            ])),
            "evidence": evidence,
            "positions": known_by_recording.get(recording_id, []),
        })

    positions_without_recording = len(unknown_positions)
    report = RecordingReport()
    report[TRACK_POSITION_COUNT] = len(positions)
    report[INDEPENDENT_RECORDING_COUNT] = len(known_by_recording)
    report[EVIDENCED_REMASTER_COUNT] = len(versions)
    report[KNOWN_RECORDING_COUNT] = len(known_by_recording)
    report[TITLE_HINT_ONLY_COUNT] = title_hint_only_count
    report[POSITIONS_WITHOUT_RECORDING_COUNT] = positions_without_recording
    report["positions"] = positions
    report["recordings"] = recordings_detail
    report["remasters"] = remasters
    report["relations"] = normalised_relations
    report["release_ids"] = release_ids
    report["release_mbids"] = release_mbids
    report["warnings"] = _unique(warnings)
    report["coverage"] = {
        "release_count": len(release_ids),
        "release_mbid_count": len(release_mbids),
        "position_count": len(positions),
        "distinct_known_recordings": len(known_by_recording),
        "positions_without_recording": positions_without_recording,
        "relation_count": len(normalised_relations),
        "remaster_relation_types": sorted(accepted_types),
    }
    report._source_releases = _ReleaseContexts(release_contexts)
    report._relation_inputs = tuple(
        value for value in (recordings, relations) if value is not None
    )
    report._include_embedded_relations = include_embedded_relations
    report._remaster_relation_types = accepted_types
    return report


def build_report(*args, **kwargs):
    """Alias for :func:`build_recording_report`."""
    return build_recording_report(*args, **kwargs)


def summarize_recordings(*args, **kwargs):
    """Alias for :func:`build_recording_report`."""
    return build_recording_report(*args, **kwargs)


RecordingSummary = RecordingReport


def _count(report, name):
    if isinstance(report, dict):
        return report[name]
    return getattr(report, name)


def compare_reports(before, after, counts=None):
    """Compare counts between two reports.

    :param before: The report built without the new evidence.
    :param after: The report built with the new evidence.
    :param counts: Optional iterable of count names to compare.  Defaults to
        the three published counts.
    :returns: A :class:`ReportComparison`.  Every compared count is an
        integer-like :class:`CountChange` with ``before``, ``after``,
        ``delta`` and ``changed``.  ``changed_counts`` and
        ``unchanged_counts`` name the totals, which makes it explicit which
        counts the evidence did and did not alter.
    """
    names = tuple(counts) if counts is not None else PUBLISHED_COUNTS
    comparison = ReportComparison()
    for name in names:
        comparison[name] = CountChange(
            name, _count(before, name), _count(after, name)
        )
    return comparison


def format_changes(comparison, include_unchanged=True):
    """Format a :class:`ReportComparison` as human-readable lines."""
    if not isinstance(comparison, ReportComparison):
        if not isinstance(comparison, dict):
            raise TypeError("comparison must be a ReportComparison")
        comparison = ReportComparison(comparison)
    lines = []
    for name in PUBLISHED_COUNTS:
        if name not in comparison:
            continue
        change = comparison[name]
        if change.changed:
            lines.append(
                "%s: %d -> %d (%+d)"
                % (name, change.before, change.after, change.delta)
            )
        elif include_unchanged:
            lines.append(
                "%s: %d (unchanged)" % (name, change.before)
            )
    if not lines:
        lines.append("no published count changed")
    return lines


def format_report(report, include_unchanged=False):
    """Format the published counts of a report as readable lines."""
    lines = [
        "track positions: %d" % report[TRACK_POSITION_COUNT],
        "independent recordings: %d" % report[INDEPENDENT_RECORDING_COUNT],
        "evidenced remaster versions: %d" % report[EVIDENCED_REMASTER_COUNT],
    ]
    if include_unchanged:
        lines.extend([
            "known recording MBIDs: %d" % report[KNOWN_RECORDING_COUNT],
            "title hints without formal evidence: %d"
            % report[TITLE_HINT_ONLY_COUNT],
            "positions without a recording MBID: %d"
            % report[POSITIONS_WITHOUT_RECORDING_COUNT],
        ])
    return lines


def main(argv=None):
    """Print a report for retrieved MusicBrainz XML files."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Summarise recording appearances from MusicBrainz XML."
    )
    parser.add_argument(
        "files", nargs="+",
        help="release or recording response XML files",
    )
    parser.add_argument(
        "--relations", nargs="+", default=[],
        help="additional recording response XML files with relations",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print the complete report as JSON",
    )
    args = parser.parse_args(argv)

    release_data = []
    explicit_relations = []
    for filename in args.files:
        with open(filename, "rb") as handle:
            parsed = _parse_xml(handle)
        if "release" in parsed or "release-list" in parsed:
            release_data.append(parsed)
        if "recording" in parsed or "recording-list" in parsed:
            release_data.append(parsed)
    for filename in args.relations:
        with open(filename, "rb") as handle:
            explicit_relations.append(_parse_xml(handle))
    report = build_recording_report(
        release_data, relations=explicit_relations or None
    )
    if args.json:
        print(report.to_json())
    else:
        for line in format_report(report, include_unchanged=True):
            print(line)
        for warning in report["warnings"]:
            print("warning: %s" % warning)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
