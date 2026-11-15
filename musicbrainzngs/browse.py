# This file is part of the musicbrainzngs library
# Copyright (C) Alastair Porter, Adrian Sampson, and others
# This file is distributed under a BSD-2-Clause type license.
# See the COPYING file for more information.

"""Interruptible, drift-tolerant browse sessions.

The MusicBrainz web service keeps no historical snapshot. Browsing with
``offset`` walks a directory that can change while the scan is in progress:
when a newly entered entity is inserted in front of an offset that has
already been scanned, every following page shifts. A naive loop that stops
at the first short page can therefore miss entities and still believe the
result is complete.

A :class:`BrowseSession` keeps the evidence needed to notice this:

* the IDs already delivered, so an entity is never written out twice;
* a witness of the ordered ID sequence at every page boundary, so a shifted
  page is detected on the next walk;
* every total count reported by the server.

After the first sweep the pages are re-walked from offset zero. A clean
re-walk in which every boundary witness and the total count stay stable
proves that the result is complete. If a boundary moves, the new entities
are merged and another re-walk starts, up to ``max_rechecks`` times. If the
directory never settles, :class:`BrowseIncomplete` is raised with the
confirmed set and the boundary that could not be proven complete.
"""

import threading
try:
    from collections import OrderedDict
except ImportError:  # pragma: no cover - Python 2.6 and older
    from ordereddict import OrderedDict

from musicbrainzngs import musicbrainz
from musicbrainzngs.musicbrainz import MusicBrainzError, UsageError

__all__ = ["BrowseSession", "BrowseIncomplete", "browse_session"]


#: Python keyword name -> web service query parameter name, per browsable
#: entity. Mirrors the parameter handling of the plain ``browse_*``
#: functions in :mod:`musicbrainzngs.musicbrainz`.
BROWSE_BY = {
    "artist": {
        "recording": "recording",
        "release": "release",
        "release_group": "release-group",
        "work": "work",
    },
    "event": {
        "area": "area",
        "artist": "artist",
        "place": "place",
    },
    "label": {
        "release": "release",
    },
    "place": {
        "area": "area",
    },
    "recording": {
        "artist": "artist",
        "release": "release",
    },
    "release": {
        "artist": "artist",
        "track_artist": "track_artist",
        "label": "label",
        "recording": "recording",
        "release_group": "release-group",
    },
    "release-group": {
        "artist": "artist",
        "release": "release",
    },
    "url": {
        "resource": "resource",
    },
    "work": {
        "artist": "artist",
    },
}


class BrowseIncomplete(MusicBrainzError):
    """Raised when the browsed directory never settles within the allowed
    number of rechecks, so completeness cannot be proven.

    The :attr:`result` attribute holds the confirmed entities together with
    the unstable boundary, in the same dict shape as
    :meth:`BrowseSession.result`.
    """
    def __init__(self, message, result):
        MusicBrainzError.__init__(self, message)
        self.result = result


def _is_cancelled(cancel_event):
    """Return whether the caller asked the scan to stop. Accepts a
    ``threading.Event``-like object or a zero-argument callable.
    """
    if cancel_event is None:
        return False
    if callable(cancel_event):
        return bool(cancel_event())
    return bool(cancel_event.is_set())


class BrowseSession(object):
    """A resumable browse over all entities linked to another entity.

    Construct a session with the same keyword arguments as the plain
    ``browse_*`` functions, then drive it with :meth:`collect` (or step
    through it page by page with :meth:`fetch_page`)::

        session = BrowseSession("release", artist=artist_id,
                                includes=["labels"], max_rechecks=3)
        result = session.collect()
        releases = result["release-list"]

    The session survives process boundaries through :meth:`to_dict` and
    :meth:`from_dict`, and network failures leave its state consistent:
    catching the error and calling :meth:`collect` again resumes the scan
    without delivering any entity twice.
    """

    #: Initial walk that discovers pages.
    PHASE_SWEEP = "sweep"
    #: Re-walk that verifies the recorded page boundary witnesses.
    PHASE_VERIFY = "verify"
    #: A clean re-walk proved the result complete.
    PHASE_COMPLETE = "complete"
    #: The directory kept changing until the recheck budget was spent.
    PHASE_INCOMPLETE = "incomplete"

    def __init__(self, entity, max_rechecks=3, limit=100, includes=None,
                 release_status=None, release_type=None, **browse_args):
        if entity not in BROWSE_BY:
            raise UsageError("'%s' is not a browsable entity" % entity)
        if not isinstance(max_rechecks, int) or max_rechecks < 0:
            raise UsageError("max_rechecks must be a non-negative integer")
        if not isinstance(limit, int) or limit < 1:
            raise UsageError("limit must be a positive integer")

        allowed = BROWSE_BY[entity]
        params = {}
        for key, value in browse_args.items():
            if value:
                if key not in allowed:
                    raise UsageError(
                        "cannot browse %s by '%s'" % (entity, key))
                params[allowed[key]] = value
        if len(params) != 1:
            raise UsageError(
                "exactly one entity to browse by is required")

        if includes is None:
            includes = []
        elif not isinstance(includes, list):
            includes = [includes]

        self._configure(
            entity, params, limit, includes,
            release_status or [], release_type or [], max_rechecks)

    def _configure(self, entity, params, limit, includes,
                   release_status, release_type, max_rechecks):
        self.entity = entity
        self.list_key = "%s-list" % entity
        self.count_key = "%s-count" % entity
        self.params = params
        self.limit = limit
        self.includes = includes
        self.release_status = release_status
        self.release_type = release_type
        self.max_rechecks = max_rechecks

        # MBID -> entity dict, kept in first-seen order so that consumers
        # receive a stable ordering and never see an entity twice.
        self.delivered = OrderedDict()
        # Offset -> tuple of MBIDs witnessed at that page boundary.
        self.witnesses = {}
        # Every total count the server has reported, in request order.
        self.observed_counts = []
        # Counts reported during the current (re-)walk only.
        self.pass_counts = []
        # Offsets at which a witness or count mismatch was seen this pass.
        self.drift_offsets = []
        # Total count witnessed at the terminal page of the latest walk.
        self.anchor_count = None
        # Offset at which the current directory ended.
        self.end_offset = None
        self.next_offset = 0
        self.phase = self.PHASE_SWEEP
        self.rechecks = 0
        self.drift = False
        self.cancelled = False
        self._cancel = threading.Event()
        self._lock = threading.RLock()

    # -- public state -----------------------------------------------------

    @property
    def entities(self):
        """The entities delivered so far, in first-seen order."""
        return list(self.delivered.values())

    @property
    def delivered_ids(self):
        """The set of MBIDs already delivered."""
        return set(self.delivered.keys())

    @property
    def boundary_witnesses(self):
        """A copy of the page boundary witnesses, keyed by offset."""
        return dict(self.witnesses)

    @property
    def complete(self):
        """Whether a stable re-walk has proven the result complete."""
        return self.phase == self.PHASE_COMPLETE

    def cancel(self):
        """Ask a running :meth:`collect` to stop between page requests.

        The state is left resumable: call :meth:`resume` (or construct a
        new cancellation event) and call :meth:`collect` again to continue
        the scan.
        """
        self._cancel.set()

    def resume(self):
        """Clear a previous :meth:`cancel` so :meth:`collect` can continue."""
        self._cancel.clear()
        self.cancelled = False

    def result(self):
        """The confirmed result in the shape of a browse response dict.

        In addition to the usual ``<entity>-list`` and ``<entity>-count``
        keys, the dict reports whether the result is proven ``complete``,
        whether it was ``cancelled`` mid-scan, how many ``rechecks`` were
        spent, all ``observed-counts`` and the ``unstable-boundary`` (the
        first offset that could not be proven stable, or ``None``).
        """
        return {
            self.list_key: list(self.delivered.values()),
            self.count_key: self.anchor_count,
            "complete": self.phase == self.PHASE_COMPLETE,
            "cancelled": self.cancelled,
            "rechecks": self.rechecks,
            "observed-counts": list(self.observed_counts),
            "unstable-boundary": self._unstable_boundary(),
        }

    # -- driving the session ---------------------------------------------

    def fetch_page(self):
        """Fetch the page at the current offset and fold it into the state.

        Returns a report dict with the parsed ``response``, the page
        ``entities``, the ``new-entities`` delivered for the first time, the
        requested ``offset``, the reported ``count``, whether the boundary
        ``drifted`` compared with an earlier witness and whether this is the
        ``end-of-directory`` (a short page).

        Network and parsing errors propagate unchanged. State is committed
        before the request returns, so retrying the call after such an error
        never delivers an entity twice.
        """
        with self._lock:
            if self.phase in (self.PHASE_COMPLETE, self.PHASE_INCOMPLETE):
                raise UsageError(
                    "this browse session has already finished (%s)"
                    % self.phase)

            offset = self.next_offset
            response = self._request_page(offset)
            page = response.get(self.list_key, [])
            count = response.get(self.count_key)
            page_ids = [entity["id"] for entity in page]

            previous_witness = self.witnesses.get(offset)
            boundary_drifted = (
                previous_witness is not None
                and tuple(page_ids) != previous_witness
            )
            count_drifted = (
                count is not None
                and bool(self.pass_counts)
                and count not in self.pass_counts
            )
            drifted = boundary_drifted or count_drifted

            if count is not None:
                self.observed_counts.append(count)
                self.pass_counts.append(count)
            self.witnesses[offset] = tuple(page_ids)

            new_entities = []
            for entity in page:
                mbid = entity["id"]
                if mbid not in self.delivered:
                    self.delivered[mbid] = entity
                    new_entities.append(entity)

            short_page = len(page) < self.limit
            if self.phase == self.PHASE_SWEEP:
                if short_page:
                    self._reach_end(offset, count)
                    self._begin_verify_pass()
                else:
                    self.next_offset = offset + self.limit
            else:
                if drifted:
                    self.drift = True
                    self.drift_offsets.append(offset)
                if short_page:
                    self._reach_end(offset, count)
                    self._finish_verify_pass()
                else:
                    self.next_offset = offset + self.limit

            return {
                "offset": offset,
                "response": response,
                "entities": page,
                "new-entities": new_entities,
                "count": count,
                "drifted": drifted,
                "end-of-directory": short_page,
            }

    def collect(self, cancel_event=None, on_new_entities=None):
        """Run the sweep and every needed re-walk.

        :param cancel_event: a ``threading.Event``-like object or a
            zero-argument callable; when it signals, the scan stops at the
            next page boundary and the partial result is returned. The
            session's own :meth:`cancel` is always honoured as well.
        :param on_new_entities: optional callback invoked with the list of
            entities delivered for the first time after each page, so a
            caller can write each entity out exactly once.
        :returns: the confirmed result dict (see :meth:`result`).
        :raises BrowseIncomplete: if the directory keeps changing until
            ``max_rechecks`` is exhausted.
        """
        while self.phase not in (self.PHASE_COMPLETE, self.PHASE_INCOMPLETE):
            if self._cancel.is_set() or _is_cancelled(cancel_event):
                self.cancelled = True
                return self.result()
            self.cancelled = False
            report = self.fetch_page()
            if on_new_entities is not None and report["new-entities"]:
                on_new_entities(report["new-entities"])

        if self.phase == self.PHASE_COMPLETE:
            return self.result()
        raise BrowseIncomplete(self._incomplete_message(), self.result())

    def retry(self, max_rechecks=None):
        """Give an incomplete session another budget of re-walks.

        :param max_rechecks: optional new value for ``max_rechecks``; the
            current value is kept when omitted.
        """
        with self._lock:
            if self.phase != self.PHASE_INCOMPLETE:
                raise UsageError("only an incomplete session can be retried")
            if max_rechecks is not None:
                if not isinstance(max_rechecks, int) or max_rechecks < 1:
                    raise UsageError(
                        "max_rechecks must be a positive integer")
                self.max_rechecks = max_rechecks
            self.phase = self.PHASE_VERIFY
            self.next_offset = 0
            self.drift = False
            self.drift_offsets = []
            self.pass_counts = []
            self.cancelled = False
            self._cancel.clear()

    # -- persistence ------------------------------------------------------

    def to_dict(self):
        """Return a JSON-serialisable snapshot of the session state.

        Restore it later with :meth:`from_dict`; every delivered entity and
        boundary witness is preserved.
        """
        return {
            "entity": self.entity,
            "params": self.params,
            "limit": self.limit,
            "includes": self.includes,
            "release_status": self.release_status,
            "release_type": self.release_type,
            "max_rechecks": self.max_rechecks,
            "delivered": [[mbid, entity]
                          for mbid, entity in self.delivered.items()],
            "witnesses": dict((str(offset), list(ids))
                              for offset, ids in self.witnesses.items()),
            "observed_counts": self.observed_counts,
            "pass_counts": self.pass_counts,
            "drift_offsets": self.drift_offsets,
            "anchor_count": self.anchor_count,
            "end_offset": self.end_offset,
            "next_offset": self.next_offset,
            "phase": self.phase,
            "rechecks": self.rechecks,
            "drift": self.drift,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, state):
        """Rebuild a session previously saved with :meth:`to_dict`."""
        session = cls.__new__(cls)
        session._configure(
            state["entity"],
            dict(state["params"]),
            state["limit"],
            list(state["includes"]),
            list(state["release_status"]),
            list(state["release_type"]),
            state["max_rechecks"])
        session.delivered = OrderedDict(
            (mbid, entity) for mbid, entity in state["delivered"])
        session.witnesses = dict(
            (int(offset), tuple(ids))
            for offset, ids in state["witnesses"].items())
        session.observed_counts = list(state["observed_counts"])
        session.pass_counts = list(state["pass_counts"])
        session.drift_offsets = list(state["drift_offsets"])
        session.anchor_count = state["anchor_count"]
        session.end_offset = state["end_offset"]
        session.next_offset = state["next_offset"]
        session.phase = state["phase"]
        session.rechecks = state["rechecks"]
        session.drift = state["drift"]
        session.cancelled = state["cancelled"]
        # The live cancellation event is deliberately not restored: a
        # deserialised session starts runnable and is paused again with
        # cancel() if desired.
        return session

    # -- internals --------------------------------------------------------

    def _request_page(self, offset):
        return musicbrainz._browse_impl(
            self.entity, self.includes, self.limit, offset,
            dict(self.params), self.release_status, self.release_type)

    def _reach_end(self, offset, count):
        self.end_offset = offset
        self.anchor_count = count

    def _begin_verify_pass(self):
        self.phase = self.PHASE_VERIFY
        self.next_offset = 0
        self.drift = False
        self.drift_offsets = []
        self.pass_counts = []

    def _finish_verify_pass(self):
        if not self.drift and self._count_invariant_holds():
            self.phase = self.PHASE_COMPLETE
            self.next_offset = 0
        elif self.rechecks >= self.max_rechecks:
            self.phase = self.PHASE_INCOMPLETE
            self.next_offset = 0
        else:
            self.rechecks += 1
            self._begin_verify_pass()

    def _count_invariant_holds(self):
        if self.anchor_count is None:
            # The server offered no totals: boundary witnesses plus the
            # short terminal page are the only evidence available.
            return True
        if any(count != self.anchor_count for count in self.pass_counts):
            return False
        # The stable final walk enumerated the whole current directory, so
        # the delivered set must cover at least that many entities. It may
        # hold more: an entity confirmed earlier and deleted before the walk
        # settled is retained on purpose and is never delivered twice.
        return len(self.delivered) >= self.anchor_count

    def _unstable_boundary(self):
        if self.drift_offsets:
            return min(self.drift_offsets)
        if self.phase == self.PHASE_INCOMPLETE:
            return self.end_offset
        return None

    def _incomplete_message(self):
        return (
            "browse of %s %s did not settle after %d recheck(s); "
            "%d entity/entities confirmed, boundary at offset %s "
            "could not be proven complete"
            % (self.entity, self.params, self.rechecks,
               len(self.delivered), self._unstable_boundary()))


def browse_session(entity, **kwargs):
    """Construct a :class:`BrowseSession`. Keyword arguments are the same
    as for the matching ``browse_*`` function plus ``max_rechecks`` and
    ``limit``.
    """
    return BrowseSession(entity, **kwargs)
