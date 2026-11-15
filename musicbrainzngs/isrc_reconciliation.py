# This file is part of the musicbrainzngs library
# Copyright (C) Alastair Porter, Adrian Sampson, and others
# This file is distributed under a BSD-2-Clause type license.
# See the COPYING file for more information.

"""Reconcile the recordings returned for a single ISRC.

An ISRC lookup can legitimately return more than one recording because
several sources may use the same ISRC for different (or merely
differently-described) recordings.  This module keeps the query result
untouched and layers explicit confirmations on top of it:

* ``alias`` confirmations are transitive (a verified alias chain means
  all recordings in the chain are the same recording),
* ``compatible`` confirmations apply to exactly the confirmed pair and
  are never propagated to a third recording,
* ``mutually-exclusive`` confirmations say that two recordings cannot be
  the same recording.

Only recordings that are explicitly part of a mutual-exclusion
confirmation are quarantined.  Artist names, durations and other
metadata are never used to decide identity (or to decide that an
unrelated recording should be quarantined).
"""

import copy

from . import compat


# Confirmation statuses accepted by ConfirmationLog.
ALIAS = "alias"
COMPATIBLE = "compatible"
MUTUALLY_EXCLUSIVE = "mutually-exclusive"

VALID_STATUSES = (ALIAS, COMPATIBLE, MUTUALLY_EXCLUSIVE)

_STATUS_ALIASES = {
    ALIAS: ALIAS,
    COMPATIBLE: COMPATIBLE,
    MUTUALLY_EXCLUSIVE: MUTUALLY_EXCLUSIVE,
    "mutually_exclusive": MUTUALLY_EXCLUSIVE,
    "mutually exclusive": MUTUALLY_EXCLUSIVE,
    "exclusive": MUTUALLY_EXCLUSIVE,
    "mutual-exclusion": MUTUALLY_EXCLUSIVE,
}


def _normalise_status(status):
    if not isinstance(status, compat.basestring):
        raise IsrcReconciliationError(
            "unknown confirmation status: %r" % (status,))
    normalised = _STATUS_ALIASES.get(status.strip().lower())
    if normalised is None:
        raise IsrcReconciliationError(
            "unknown confirmation status: %r" % (status,))
    return normalised

# Reasons attached to entries in the ``review-items`` list.
REASON_ALIAS_EXCLUSION = "alias-chain-crosses-exclusion"
REASON_MERGE_EXCLUSION = "merge-spans-exclusion"
REASON_MERGE_QUARANTINE = "merge-spans-quarantine"
REASON_EXPLICIT_EXCLUSION = "explicit-exclusion"
REASON_COMPATIBLE_CHAIN = "compatible-not-transitive"

__all__ = [
    "ALIAS",
    "COMPATIBLE",
    "MUTUALLY_EXCLUSIVE",
    "VALID_STATUSES",
    "Confirmation",
    "ConfirmationConflict",
    "ConfirmationLog",
    "IsrcReconciliationError",
    "IsrcReconciler",
    "RecordingObservation",
    "observations_from_query_result",
    "recording_ids_from_query",
    "reconcile_isrc",
]


class IsrcReconciliationError(ValueError):
    """Base class for invalid ISRC reconciliation input."""


class ConfirmationConflict(IsrcReconciliationError):
    """Raised when the same pair is given contradictory confirmations."""


def _check_recording_id(value, label="recording id"):
    if not isinstance(value, compat.basestring) or not value:
        raise IsrcReconciliationError("%s must be a non-empty string" % label)
    return value


def _ordered_pair(left, right):
    left = _check_recording_id(left, "left recording id")
    right = _check_recording_id(right, "right recording id")
    if left == right:
        raise IsrcReconciliationError("a recording cannot be confirmed against itself")
    if right < left:
        return right, left
    return left, right


def _coerce_length(length):
    if length is None or length == "":
        return None
    try:
        return int(length)
    except (TypeError, ValueError):
        raise IsrcReconciliationError("length must be an integer number of milliseconds")


def _artist_names(recording):
    """Extract artist names from a parsed recording's artist credit."""
    names = []
    for credit in recording.get("artist-credit", []) or []:
        if not isinstance(credit, dict):
            continue
        name = credit.get("name")
        if not name:
            name = credit.get("artist", {}).get("name")
        if name:
            names.append(name)
    return tuple(names)


def _recording_list(query_result):
    """Return ``(isrc, recordings)`` from a parsed ISRC lookup result.

    ``recordings`` may contain parsed recording dicts or plain recording
    ids, so that callers can pass a hand-built result as well.
    """
    if query_result is None:
        return None, []

    if isinstance(query_result, (list, tuple)):
        return None, list(query_result)

    if not isinstance(query_result, dict):
        raise IsrcReconciliationError("query result must be a mapping")

    if "isrc" in query_result:
        node = query_result["isrc"]
    else:
        node = query_result
    if not isinstance(node, dict):
        raise IsrcReconciliationError("query result has no ISRC mapping")

    isrc = node.get("id")
    recordings = node.get("recording-list")
    if recordings is None:
        recordings = node.get("recordings", [])
    if not isinstance(recordings, (list, tuple)):
        raise IsrcReconciliationError("recording-list must be a list")
    return isrc, list(recordings)


def recording_ids_from_query(query_result):
    """Return recording ids from a parsed ISRC lookup, preserving raw order.

    The returned list keeps every occurrence in the raw many-to-many
    relationship (duplicates included); the query result itself is not
    modified.
    """
    _, recordings = _recording_list(query_result)
    result = []
    for recording in recordings:
        if isinstance(recording, dict):
            recording_id = recording.get("id")
        else:
            recording_id = recording
        result.append(_check_recording_id(recording_id))
    return result


class RecordingObservation(object):
    """Metadata observed for a recording at one source.

    Observations are evidence for a human reviewer.  They are deliberately
    not used to infer identity or quarantine status.
    """

    def __init__(self, recording_id, source, length=None, artists=None,
                 title=None):
        self.recording_id = _check_recording_id(recording_id)
        if not isinstance(source, compat.basestring) or not source:
            raise IsrcReconciliationError("source must be a non-empty string")
        self.source = source
        self.length = _coerce_length(length)
        self.artists = tuple(artists or ())
        self.title = title

    def as_dict(self):
        return {
            "recording-id": self.recording_id,
            "source": self.source,
            "length": self.length,
            "artists": list(self.artists),
            "title": self.title,
        }


def observations_from_query_result(query_result, source):
    """Build observations from the recordings in a parsed ISRC lookup."""
    _, recordings = _recording_list(query_result)
    observations = []
    for recording in recordings:
        if not isinstance(recording, dict):
            raise IsrcReconciliationError(
                "observations can only be extracted from parsed recordings")
        observations.append(RecordingObservation(
            recording["id"],
            source,
            length=recording.get("length"),
            artists=_artist_names(recording),
            title=recording.get("title"),
        ))
    return observations


class Confirmation(object):
    """A single explicit confirmation about a pair of recordings."""

    def __init__(self, left, right, status, evidence=None, note=None):
        self.left, self.right = _ordered_pair(left, right)
        self.status = _normalise_status(status)
        self.evidence = evidence
        self.note = note

    def pair(self):
        return self.left, self.right

    def as_dict(self):
        return {
            "recording-ids": [self.left, self.right],
            "status": self.status,
            "evidence": self.evidence,
            "note": self.note,
        }


class ConfirmationLog(object):
    """Append-only log of explicit confirmations for a reconciliation."""

    def __init__(self):
        self._confirmations = []
        self._status_by_pair = {}

    def add(self, left, right, status, evidence=None, note=None):
        confirmation = Confirmation(left, right, status, evidence, note)
        previous = self._status_by_pair.get(confirmation.pair())
        if previous is not None and previous != confirmation.status:
            raise ConfirmationConflict(
                "recording pair %s already confirmed as %s, cannot also "
                "confirm as %s" % (list(confirmation.pair()), previous,
                                   confirmation.status))
        self._status_by_pair[confirmation.pair()] = confirmation.status
        self._confirmations.append(confirmation)
        return confirmation

    def confirm_alias(self, left, right, evidence=None, note=None):
        return self.add(left, right, ALIAS, evidence=evidence, note=note)

    def confirm_compatible(self, left, right, evidence=None, note=None):
        return self.add(left, right, COMPATIBLE, evidence=evidence, note=note)

    def confirm_mutually_exclusive(self, left, right, evidence=None, note=None):
        return self.add(left, right, MUTUALLY_EXCLUSIVE,
                        evidence=evidence, note=note)

    def status_for(self, left, right):
        return self._status_by_pair.get(_ordered_pair(left, right))

    def confirmations(self, status=None):
        if status is None:
            return list(self._confirmations)
        return [c for c in self._confirmations if c.status == status]

    def _edges(self, status):
        result = []
        seen = set()
        for confirmation in self._confirmations:
            if confirmation.status != status:
                continue
            pair = confirmation.pair()
            if pair not in seen:
                seen.add(pair)
                result.append(pair)
        return result

    def alias_edges(self):
        return self._edges(ALIAS)

    def compatible_edges(self):
        return self._edges(COMPATIBLE)

    def mutual_exclusion_edges(self):
        return self._edges(MUTUALLY_EXCLUSIVE)


class _UnionFind(object):
    def __init__(self, items):
        self.parent = dict((item, item) for item in items)

    def find(self, item):
        parent = self.parent[item]
        while parent != self.parent[parent]:
            self.parent[parent] = self.parent[self.parent[parent]]
            parent = self.parent[parent]
        return parent

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return left_root
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        return left_root

    def groups(self):
        grouped = {}
        for item in sorted(self.parent):
            grouped.setdefault(self.find(item), []).append(item)
        return [sorted(members) for _, members in sorted(grouped.items())]


def _adjacency(items, edges):
    result = dict((item, set()) for item in items)
    for left, right in edges:
        result.setdefault(left, set()).add(right)
        result.setdefault(right, set()).add(left)
    return result


def _shortest_path(adjacency, start, goal):
    if start not in adjacency or goal not in adjacency:
        return None
    previous = {start: None}
    queue = [start]
    index = 0
    while index < len(queue):
        current = queue[index]
        index += 1
        if current == goal:
            break
        for neighbour in sorted(adjacency[current]):
            if neighbour not in previous:
                previous[neighbour] = current
                queue.append(neighbour)
    if goal not in previous:
        return None
    path = []
    current = goal
    while current is not None:
        path.append(current)
        current = previous[current]
    path.reverse()
    return path


def _chain(path):
    if not path or len(path) < 2:
        return None
    return {
        "recording-ids": list(path),
        "edges": [[path[i], path[i + 1]] for i in range(len(path) - 1)],
    }


def _chain_to_any(adjacency, start, targets):
    best = None
    for target in sorted(set(targets)):
        path = _shortest_path(adjacency, start, target)
        if path is None:
            continue
        if best is None or (len(path), path) < (len(best), best):
            best = path
    return _chain(best)


def _component_chains(adjacency, members):
    """Describe how an alias component is connected, root-first."""
    root = min(members)
    chains = []
    for member in sorted(members):
        if member == root:
            continue
        chain = _chain(_shortest_path(adjacency, root, member))
        if chain is not None:
            chains.append(chain)
    return chains


class IsrcReconciler(object):
    """Collect a raw ISRC lookup and explicit confirmations, then resolve.

    The raw lookup is copied when it is supplied and is never rewritten, so
    the original ISRC-to-recording many-to-many relationship remains
    available for review.
    """

    def __init__(self, isrc=None, query_result=None, confirmations=None):
        self._isrc = isrc
        self._query_result = None
        self._raw_recording_ids = []
        self._observations = []
        self.confirmations = ConfirmationLog()
        if query_result is not None:
            self.set_query_result(query_result, isrc=isrc)
        elif isrc is not None:
            _check_recording_id(isrc, "ISRC")
            self._isrc = isrc
        if confirmations:
            self.add_confirmations(confirmations)

    def set_query_result(self, query_result, isrc=None):
        embedded_isrc, _ = _recording_list(query_result)
        if isrc is not None:
            isrc = _check_recording_id(isrc, "ISRC")
            if embedded_isrc is not None and embedded_isrc != isrc:
                raise IsrcReconciliationError(
                    "ISRC argument %r does not match query result %r"
                    % (isrc, embedded_isrc))
        elif embedded_isrc is not None:
            isrc = _check_recording_id(embedded_isrc, "ISRC")
        self._isrc = isrc
        self._query_result = copy.deepcopy(query_result)
        self._raw_recording_ids = recording_ids_from_query(query_result)
        return self

    def add_observation(self, recording_id, source, length=None, artists=None,
                        title=None):
        observation = RecordingObservation(
            recording_id, source, length=length, artists=artists, title=title)
        self._observations.append(observation)
        return observation

    def add_observations(self, observations):
        for observation in observations:
            if isinstance(observation, RecordingObservation):
                self._observations.append(observation)
            elif isinstance(observation, dict):
                self._observations.append(RecordingObservation(
                    observation["recording-id"],
                    observation["source"],
                    length=observation.get("length"),
                    artists=observation.get("artists"),
                    title=observation.get("title"),
                ))
            else:
                raise IsrcReconciliationError(
                    "observations must be RecordingObservation objects or dicts")
        return self

    def confirm(self, left, right, status, evidence=None, note=None):
        return self.confirmations.add(left, right, status, evidence, note)

    def confirm_alias(self, left, right, evidence=None, note=None):
        return self.confirmations.confirm_alias(left, right, evidence, note)

    def confirm_compatible(self, left, right, evidence=None, note=None):
        return self.confirmations.confirm_compatible(left, right, evidence, note)

    def confirm_mutually_exclusive(self, left, right, evidence=None, note=None):
        return self.confirmations.confirm_mutually_exclusive(
            left, right, evidence, note)

    def add_confirmations(self, confirmations):
        for confirmation in confirmations:
            if isinstance(confirmation, Confirmation):
                self.confirmations.add(
                    confirmation.left, confirmation.right, confirmation.status,
                    confirmation.evidence, confirmation.note)
            elif isinstance(confirmation, dict):
                self.confirmations.add(
                    confirmation["left"], confirmation["right"],
                    confirmation["status"], confirmation.get("evidence"),
                    confirmation.get("note"))
            elif isinstance(confirmation, (list, tuple)) and len(confirmation) == 3:
                self.confirmations.add(*confirmation)
            else:
                raise IsrcReconciliationError(
                    "confirmations must be Confirmation objects, dicts or "
                    "(left, right, status) tuples")
        return self

    def resolve(self):
        alias_edges = self.confirmations.alias_edges()
        compatible_edges = self.confirmations.compatible_edges()
        exclusion_edges = self.confirmations.mutual_exclusion_edges()

        all_ids = set(self._raw_recording_ids)
        for observation in self._observations:
            all_ids.add(observation.recording_id)
        for edge in alias_edges + compatible_edges + exclusion_edges:
            all_ids.update(edge)
        all_ids = sorted(all_ids)

        alias_adjacency = _adjacency(all_ids, alias_edges)

        union_find = _UnionFind(all_ids)
        for left, right in alias_edges:
            union_find.union(left, right)
        alias_components = union_find.groups()

        component_of = {}
        for index, members in enumerate(alias_components):
            for member in members:
                component_of[member] = index

        quarantine_info = {}
        review_items = []

        # Alias components and exclusions.  An exclusion inside an alias
        # component is a contradiction in the verified alias chain.
        for left, right in exclusion_edges:
            left_component = component_of[left]
            right_component = component_of[right]
            for index in (left_component, right_component):
                info = quarantine_info.setdefault(
                    index, {"exclusions": [], "conflict-chains": []})
                if [left, right] not in info["exclusions"]:
                    info["exclusions"].append([left, right])

            if left_component == right_component:
                path = _shortest_path(alias_adjacency, left, right)
                chain = _chain(path)
                info = quarantine_info[left_component]
                if chain is not None and chain not in info["conflict-chains"]:
                    info["conflict-chains"].append(chain)
                review_items.append({
                    "reason": REASON_ALIAS_EXCLUSION,
                    "recording-ids": [left, right],
                    "exclusion": [left, right],
                    "alias-chains": [chain] if chain is not None else [],
                    "message": (
                        "The verified alias chain joins recordings that are "
                        "also explicitly mutually exclusive; check the alias "
                        "chain before merging."),
                })
            else:
                review_items.append({
                    "reason": REASON_EXPLICIT_EXCLUSION,
                    "recording-ids": [left, right],
                    "exclusion": [left, right],
                    "alias-groups": [
                        alias_components[left_component],
                        alias_components[right_component],
                    ],
                    "alias-chains": [],
                    "message": (
                        "Explicitly mutually exclusive recordings share this "
                        "ISRC; both sides are quarantined."),
                })

        quarantined_ids = set()
        for index in quarantine_info:
            quarantined_ids.update(alias_components[index])
        quarantined_ids &= set(all_ids)

        # Build the initial groups from alias components that are not
        # quarantined.  Compatibility merges are applied one pair at a time in
        # a stable order; a group can contain at most one compatibility edge,
        # which prevents compatibility from becoming transitive.
        group_of_id = {}
        for index, members in enumerate(alias_components):
            if index in quarantine_info:
                continue
            group = {
                "ids": set(members),
                "component-indices": [index],
                "compatible-edges": [],
            }
            for member in members:
                group_of_id[member] = group

        for left, right in sorted(compatible_edges):
            left_group = group_of_id.get(left)
            right_group = group_of_id.get(right)
            left_component = component_of[left]
            right_component = component_of[right]

            if left_component == right_component:
                # Alias evidence already groups these recordings; a
                # compatibility confirmation adds nothing to the merge.
                continue

            if left_group is None or right_group is None:
                # At least one side is quarantined.  If this compatible pair
                # sits on opposite sides of the same exclusion, a merge
                # would put an excluded pair in one group: report the alias
                # chains leading to the exclusion.  Otherwise the pair only
                # touches quarantine, so the other side stays out.
                cross = []
                for excluded_left, excluded_right in exclusion_edges:
                    excluded_components = (component_of[excluded_left],
                                           component_of[excluded_right])
                    if excluded_components == (left_component, right_component) or \
                            excluded_components == (right_component, left_component):
                        cross.append((excluded_left, excluded_right))

                chains = []
                if cross:
                    excluded_left, excluded_right = cross[0]
                    if component_of[excluded_left] == left_component:
                        left_target, right_target = excluded_left, excluded_right
                    else:
                        left_target, right_target = excluded_right, excluded_left
                    chain = _chain_to_any(alias_adjacency, left, [left_target])
                    if chain is not None:
                        chains.append(chain)
                    chain = _chain_to_any(alias_adjacency, right, [right_target])
                    if chain is not None:
                        chains.append(chain)
                    review_items.append({
                        "reason": REASON_MERGE_EXCLUSION,
                        "recording-ids": [left, right],
                        "compatible-edge": [left, right],
                        "exclusion": [excluded_left, excluded_right],
                        "alias-chains": chains,
                        "message": (
                            "Merging this compatible pair would place an "
                            "explicitly mutually exclusive pair in one "
                            "group; review the alias chains first."),
                    })
                else:
                    targets = {left_component: [], right_component: []}
                    for excluded_left, excluded_right in exclusion_edges:
                        if component_of[excluded_left] in targets:
                            targets[component_of[excluded_left]].append(
                                excluded_left)
                        if component_of[excluded_right] in targets:
                            targets[component_of[excluded_right]].append(
                                excluded_right)
                    for endpoint, index in ((left, left_component),
                                            (right, right_component)):
                        chain = _chain_to_any(alias_adjacency, endpoint,
                                              targets[index])
                        if chain is not None:
                            chains.append(chain)
                    both_quarantined = (left_group is None and
                                        right_group is None)
                    review_items.append({
                        "reason": REASON_MERGE_QUARANTINE,
                        "recording-ids": [left, right],
                        "compatible-edge": [left, right],
                        "alias-chains": chains,
                        "message": (
                            "Compatible evidence would merge recordings "
                            "across quarantine boundaries; no quarantine is "
                            "lifted." if both_quarantined else
                            "A compatible recording would join a quarantined "
                            "group; the other recording is not quarantined."),
                    })
                continue

            if left_group is right_group:
                continue

            if left_group["compatible-edges"] or right_group["compatible-edges"]:
                previous_edges = (left_group["compatible-edges"] +
                                  right_group["compatible-edges"])
                review_items.append({
                    "reason": REASON_COMPATIBLE_CHAIN,
                    "recording-ids": [left, right],
                    "compatible-edge": [left, right],
                    "compatible-edges": previous_edges + [[left, right]],
                    "alias-chains": [],
                    "message": (
                        "Compatible evidence only applies to the confirmed "
                        "pair; it is not transitive. Confirm an alias or an "
                        "exclusion for the remaining recordings."),
                })
                continue

            merged = {
                "ids": left_group["ids"] | right_group["ids"],
                "component-indices": (left_group["component-indices"] +
                                      right_group["component-indices"]),
                "compatible-edges": [[left, right]],
            }
            for member in merged["ids"]:
                group_of_id[member] = merged

        # Collect current groups only: old groups replaced by a merge are
        # unreachable from group_of_id.
        current_groups = []
        seen_groups = set()
        for recording_id in all_ids:
            group = group_of_id.get(recording_id)
            if group is None:
                continue
            marker = id(group)
            if marker not in seen_groups:
                seen_groups.add(marker)
                current_groups.append(group)

        merged_groups = []
        recording_states = {}
        for group in current_groups:
            members = sorted(group["ids"])
            if len(members) > 1:
                alias_groups = [alias_components[index]
                                for index in sorted(group["component-indices"])]
                alias_chains = []
                for members_in_alias in alias_groups:
                    alias_chains.extend(_component_chains(
                        alias_adjacency, members_in_alias))
                basis = []
                for members_in_alias in alias_groups:
                    if len(members_in_alias) > 1:
                        basis.append({
                            "type": "alias",
                            "recording-ids": members_in_alias,
                        })
                for pair in group["compatible-edges"]:
                    basis.append({"type": "compatible", "recording-ids": pair})
                merged_groups.append({
                    "recording-ids": members,
                    "alias-groups": alias_groups,
                    "alias-chains": alias_chains,
                    "compatible-edges": [list(pair)
                                         for pair in group["compatible-edges"]],
                    "basis": basis,
                })
                for member in members:
                    recording_states[member] = "merged"

        quarantine_groups = []
        for index in sorted(quarantine_info):
            members = alias_components[index]
            info = quarantine_info[index]
            quarantine_groups.append({
                "recording-ids": members,
                "exclusions": info["exclusions"],
                "alias-chains": _component_chains(alias_adjacency, members),
                "conflict-chains": info["conflict-chains"],
            })
            for member in members:
                recording_states[member] = "quarantined"

        for recording_id in all_ids:
            if recording_id not in recording_states:
                recording_states[recording_id] = "separate"

        observations = {}
        for observation in self._observations:
            observations.setdefault(observation.recording_id, []).append(
                observation.as_dict())

        observation_summary = {}
        observation_diffs = []
        for recording_id in sorted(observations):
            items = observations[recording_id]
            lengths = sorted(set(item["length"] for item in items
                                 if item["length"] is not None))
            artists = sorted(set(tuple(item["artists"]) for item in items))
            titles = sorted(set(item["title"] for item in items
                                if item["title"] is not None))
            observation_summary[recording_id] = {
                "sources": sorted(set(item["source"] for item in items)),
                "lengths": lengths,
                "artist-credits": [list(names) for names in artists],
                "titles": titles,
            }
            if len(lengths) > 1:
                observation_diffs.append({
                    "recording-id": recording_id,
                    "field": "length",
                    "values-by-source": [
                        {"source": item["source"], "length": item["length"]}
                        for item in items],
                })
            if len(artists) > 1:
                observation_diffs.append({
                    "recording-id": recording_id,
                    "field": "artists",
                    "values-by-source": [
                        {"source": item["source"],
                         "artists": list(item["artists"])}
                        for item in items],
                })

        raw = {
            "isrc": self._isrc,
            "recording-ids": list(self._raw_recording_ids),
            "links": [{"isrc": self._isrc, "recording-id": recording_id}
                      for recording_id in self._raw_recording_ids],
            "response": copy.deepcopy(self._query_result),
        }

        return {
            "isrc": self._isrc,
            "raw": raw,
            "observations": observations,
            "observation-summary": observation_summary,
            "observation-diffs": observation_diffs,
            "alias-groups": [
                {
                    "recording-ids": members,
                    "alias-chains": _component_chains(alias_adjacency, members),
                }
                for members in alias_components if len(members) > 1
            ],
            "merged-groups": merged_groups,
            "quarantined-recording-ids": sorted(quarantined_ids),
            "quarantine-groups": quarantine_groups,
            "review-items": review_items,
            "recording-states": recording_states,
            "confirmations": [confirmation.as_dict()
                              for confirmation in self.confirmations.confirmations()],
        }


def reconcile_isrc(isrc, query_result, observations=None, confirmations=None):
    """Convenience wrapper around :class:`IsrcReconciler`.

    ``query_result`` is the unmodified dict returned by
    :func:`musicbrainzngs.get_recordings_by_isrc`.  Confirmations can be
    :class:`Confirmation` objects, dicts or ``(left, right, status)``
    tuples.
    """
    reconciler = IsrcReconciler(isrc=isrc, query_result=query_result)
    if observations:
        reconciler.add_observations(observations)
    if confirmations:
        reconciler.add_confirmations(confirmations)
    return reconciler.resolve()
