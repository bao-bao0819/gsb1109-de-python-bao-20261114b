# This file is part of the musicbrainzngs library
# Copyright (C) Alastair Porter, Adrian Sampson, and others
# This file is distributed under a BSD-2-Clause type license.
# See the COPYING file for more information.

"""Incremental synchronisation of MusicBrainz entity relations.

This module keeps a local archive of entity lookups so that relations
can be synchronised incrementally instead of refetching everything.

It implements the following semantics:

* When an entity (e.g. a release group) has been merged, the web
  service answers a lookup of the old MBID with the entity's new MBID.
  The archive records this redirect, so history can be queried with the
  new MBID while every record still remembers which MBID was actually
  requested at the time.

* A single lookup only expands the relations named in its ``includes``.
  Ingesting a response only replaces the relation scopes that were
  actually requested; scopes that were not requested are left untouched.
  Only an explicitly fetched empty relation list clears a scope.

* If two redirects would form a cycle, the affected entities are
  flagged for review and their current views are left alone.  All other
  entities keep updating as usual.

* Replaying the archived responses up to a point in time reproduces
  the relation view as it was known at that time.
"""

import copy
import time

from musicbrainzngs import compat
from musicbrainzngs import mbxml
from musicbrainzngs.musicbrainz import RELATABLE_TYPES

_RELATION_LIST_SUFFIX = "-relation-list"
_RELS_INCLUDE_SUFFIX = "-rels"


class RedirectCycle(Exception):
    """Following MBID redirects led back to an already seen MBID.

    :attr:`chain` holds the MBIDs involved, in the order they were
    followed.
    """

    def __init__(self, entity_type, chain):
        self.entity_type = entity_type
        self.chain = list(chain)
        message = "redirect cycle for %s: %s" % (
            entity_type, " -> ".join(self.chain))
        super(RedirectCycle, self).__init__(message)


def _scopes_from_includes(includes):
    """Map relation includes like ``artist-rels`` to the relation list
    keys used in parsed responses (``artist-relation-list``).

    Includes that do not expand to a top level relation list of the
    entity (e.g. ``work-level-rels``) are ignored.
    """
    scopes = set()
    for include in includes or ():
        if include.endswith(_RELS_INCLUDE_SUFFIX):
            target = include[:-len(_RELS_INCLUDE_SUFFIX)]
            if target in RELATABLE_TYPES:
                scopes.add(target + _RELATION_LIST_SUFFIX)
    return scopes


def _scopes_from_payload(payload):
    return set(key for key in payload
               if key.endswith(_RELATION_LIST_SUFFIX))


class ArchiveRecord(object):
    """One archived lookup response.

    Attributes:

    * ``entity_type``: e.g. ``"release-group"``
    * ``requested_id``: the MBID that was requested
    * ``resolved_id``: the MBID of the entity that came back; differs
      from ``requested_id`` when the entity was merged
    * ``includes``: the includes the lookup was made with
    * ``scopes``: the relation scopes this record authoritatively
      covers
    * ``relations``: mapping of scope to the relation list of that
      scope (empty if the lookup explicitly returned no relations)
    * ``fetched_at``: when the response was fetched
    * ``seq``: ingestion order, used as a tiebreaker for equal
      timestamps
    * ``payload``: the parsed entity as returned by the web service
    """

    def __init__(self, entity_type, requested_id, resolved_id, includes,
                 scopes, relations, fetched_at, seq, payload):
        self.entity_type = entity_type
        self.requested_id = requested_id
        self.resolved_id = resolved_id
        self.includes = list(includes)
        self.scopes = sorted(scopes)
        self.relations = relations
        self.fetched_at = fetched_at
        self.seq = seq
        self.payload = payload

    def __repr__(self):
        return ("<ArchiveRecord %s %s -> %s at %s>"
                % (self.entity_type, self.requested_id,
                   self.resolved_id, self.fetched_at))


class SyncArchive(object):
    """An append-only archive of entity lookups with incremental,
    per-scope merging of relations.
    """

    def __init__(self, time_source=None):
        self._time = time_source or time.time
        self._records = []
        self._seq = 0
        # entity_type -> {old mbid: new mbid}
        self._redirects = {}
        # (entity_type, canonical mbid) -> {scope: [relation, ...]}
        self._views = {}
        # (entity_type, mbid) -> reason
        self._review = {}

    # -- ingestion ---------------------------------------------------

    def ingest(self, entity_type, requested_id, response, includes=(),
               fetched_at=None):
        """Archive one lookup response and merge its relations into the
        current view.

        ``response`` is either a parsed response dict as returned by
        the ``get_*_by_id`` functions or a raw XML response (string,
        bytes or file-like).  ``includes`` must be the includes the
        lookup was made with; they decide which relation scopes this
        response may replace.  Returns the :class:`ArchiveRecord`.
        """
        message = self._parse_response(response)
        if entity_type not in message:
            raise ValueError("response does not contain a %s" % entity_type)
        payload = message[entity_type]
        resolved_id = payload.get("id")
        if not resolved_id:
            raise ValueError("response %s has no id" % entity_type)

        scopes = (_scopes_from_includes(includes)
                  | _scopes_from_payload(payload))
        relations = dict((scope, copy.deepcopy(payload.get(scope, [])))
                         for scope in scopes)

        if fetched_at is None:
            fetched_at = self._time()
        self._seq += 1
        record = ArchiveRecord(entity_type, requested_id, resolved_id,
                               includes, scopes, relations, fetched_at,
                               self._seq, copy.deepcopy(payload))
        self._records.append(record)

        if resolved_id != requested_id:
            self._add_redirect(entity_type, requested_id, resolved_id)

        canonical = self._canonical(entity_type, resolved_id)
        if not self._is_under_review(entity_type, requested_id, canonical):
            view = self._views.setdefault((entity_type, canonical), {})
            view.update(copy.deepcopy(relations))
        return record

    def _parse_response(self, response):
        if isinstance(response, dict):
            return response
        if isinstance(response, compat.basestring) or hasattr(response, "read"):
            return mbxml.parse_message(response)
        raise TypeError("response must be a parsed dict or an XML response")

    # -- redirects ---------------------------------------------------

    def _redirect_table(self, entity_type):
        return self._redirects.setdefault(entity_type, {})

    def _canonical(self, entity_type, mbid):
        """Follow the redirect chain for ``mbid`` to its current MBID.

        Flags the involved entities for review and raises
        :class:`RedirectCycle` if the chain loops.
        """
        table = self._redirect_table(entity_type)
        seen = []
        current = mbid
        while current in table:
            if current in seen:
                cycle = seen[seen.index(current):]
                for member in cycle:
                    self._flag_review(entity_type, member, "redirect cycle")
                raise RedirectCycle(entity_type, cycle)
            seen.append(current)
            current = table[current]
        return current

    def _add_redirect(self, entity_type, old_id, new_id):
        """Record a redirect, unless it would close a cycle.  In that
        case flag every MBID in the cycle for review instead.
        """
        table = self._redirect_table(entity_type)
        chain = [old_id, new_id]
        current = new_id
        while current in table:
            current = table[current]
            chain.append(current)
            if current == old_id:
                for member in chain[:-1]:
                    self._flag_review(entity_type, member, "redirect cycle")
                return
        old_canonical = self._canonical(entity_type, old_id)
        table[old_id] = new_id
        new_canonical = self._canonical(entity_type, new_id)
        if old_canonical == old_id and old_id != new_canonical:
            self._migrate_view(entity_type, old_id, new_canonical)

    def _migrate_view(self, entity_type, old_canonical, new_canonical):
        old_view = self._views.pop((entity_type, old_canonical), None)
        if not old_view:
            return
        view = self._views.setdefault((entity_type, new_canonical), {})
        for scope, relations in old_view.items():
            view.setdefault(scope, relations)

    def aliases(self, entity_type, mbid):
        """All MBIDs known to refer to the same entity as ``mbid``,
        including ``mbid`` itself.
        """
        canonical = self._canonical(entity_type, mbid)
        result = set([canonical])
        for old_id in self._redirect_table(entity_type):
            if self._canonical(entity_type, old_id) == canonical:
                result.add(old_id)
        return result

    # -- review queue ------------------------------------------------

    def _flag_review(self, entity_type, mbid, reason):
        self._review[(entity_type, mbid)] = reason

    def _is_under_review(self, entity_type, *mbids):
        return any((entity_type, mbid) in self._review for mbid in mbids)

    def review_queue(self):
        """Entities left for review, as a sorted list of
        ``(entity_type, mbid, reason)`` tuples.
        """
        return sorted((entity_type, mbid, reason)
                      for (entity_type, mbid), reason in self._review.items())

    def clear_review(self, entity_type, mbid):
        """Remove ``mbid`` from the review queue and rebuild its current
        view from the archived history.  Returns the rebuilt view.
        """
        self._review.pop((entity_type, mbid), None)
        canonical = self._canonical(entity_type, mbid)
        view = self.replay(entity_type, canonical)
        self._views[(entity_type, canonical)] = copy.deepcopy(view)
        return view

    # -- querying ----------------------------------------------------

    def resolve(self, entity_type, mbid):
        """The current MBID for ``mbid`` after following redirects."""
        return self._canonical(entity_type, mbid)

    def get_current(self, entity_type, mbid):
        """The current relation view for the entity, looked up by any
        of its MBIDs.  Only scopes covered by an archived response are
        present.
        """
        canonical = self._canonical(entity_type, mbid)
        return copy.deepcopy(self._views.get((entity_type, canonical), {}))

    def history(self, entity_type, mbid):
        """All archived records for the entity, whichever of its MBIDs
        was requested, oldest first.  Each record keeps the MBID that
        was requested at the time.
        """
        known = self.aliases(entity_type, mbid)
        records = [record for record in self._records
                   if record.entity_type == entity_type
                   and (record.requested_id in known
                        or record.resolved_id in known)]
        return sorted(records, key=lambda record: (record.fetched_at,
                                                   record.seq))

    def replay(self, entity_type, mbid, at=None):
        """The relation view as it was known at time ``at`` (default:
        the latest archived state), rebuilt from the archived records.
        """
        view = {}
        for record in self.history(entity_type, mbid):
            if at is not None and record.fetched_at > at:
                continue
            view.update(copy.deepcopy(record.relations))
        return view

    def replay_record(self, record):
        """Replay a single archived record, giving the relation view as
        it was known when that response was fetched.
        """
        return self.replay(record.entity_type, record.resolved_id,
                           at=record.fetched_at)
