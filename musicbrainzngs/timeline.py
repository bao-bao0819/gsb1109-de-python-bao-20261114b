"""Band lineup timelines built from MusicBrainz artist relationships.

MusicBrainz represents band membership as individual ``member of band``
relationships.  The same artist may have several relationships with the same
band (leaving and rejoining are two separate relationships), and each
relationship carries its own ``begin``/``end`` dates.  Those dates are often
only precise to a year or a month, and guest performers sometimes have the
same display name as a formal member.

The tricky parts this module handles explicitly:

* Partial dates are stored as *possible intervals*.  ``2011`` means the join
  happened somewhere between 2011-01-01 and 2011-12-31, not on January 1st.
  A query inside such a boundary is reported as a candidate, not a confirmed
  member.
* Every relationship remains its own :class:`Tenure`.  Relationships are
  never merged, and no membership is inferred between two records simply
  because their date ranges touch or overlap.  A gap stays a gap.
* People are keyed by MusicBrainz artist id, never by name alone, so a guest
  performer cannot be mistaken for a formal member with the same name.
* :meth:`Lineup.apply_evidence` records exactly which tenure each observation
  narrowed, with before/after bounds, so an application can explain the
  effect of new evidence.

The main entry points are :class:`Lineup`, :func:`build_lineup` and
:func:`query_lineup`.
"""

from __future__ import unicode_literals

import calendar
import datetime
import re

try:
    string_types = (basestring,)  # noqa: F821  -- Python 2
    text_type = unicode  # noqa: F821  -- Python 2
except NameError:  # pragma: no cover - Python 3
    string_types = (str,)
    text_type = str


__all__ = [
    "Lineup",
    "Tenure",
    "Membership",
    "Evidence",
    "EvidenceResult",
    "DatePrecisionError",
    "PRECISION",
    "parse_date",
    "date_precision",
    "build_lineup",
    "query_lineup",
]

PRECISION = ("year", "month", "day")
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")

# Relationship types understood without an explicit role map.  Other relation
# types (subgroups, URLs, ...) are ignored.  A caller-supplied role map can
# add types or override the classification.
_DEFAULT_RELATION_ROLES = {
    "member of band": ("member", "member"),
    "member": ("member", "member"),
    "performer": ("performer", "guest"),
    "guest performer": ("guest", "guest"),
    "guest musician": ("guest", "guest"),
    "guest": ("guest", "guest"),
    "session musician": ("session", "guest"),
    "session": ("session", "guest"),
    "supporting musician": ("supporting", "guest"),
    "additional musician": ("additional", "guest"),
    "additional": ("additional", "guest"),
    "collaboration": ("collaboration", "guest"),
}

_GUEST_WORDS = (
    "guest",
    "session",
    "additional",
    "supporting",
    "客席",
    "客串",
    "嘉宾",
)


class DatePrecisionError(ValueError):
    """Raised when a MusicBrainz date string cannot be interpreted."""


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, string_types):
        text = value.strip()
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
        if match:
            try:
                return datetime.date(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))
                )
            except ValueError:
                pass
        raise ValueError(
            "expected a full ISO date (YYYY-MM-DD), got %r" % (value,)
        )
    raise TypeError("expected a date, got %r" % (value,))


def parse_date(value):
    """Parse a MusicBrainz partial date.

    Returns ``(first_day, precision)`` where ``first_day`` is the first day
    of the represented period and precision is ``"year"``, ``"month"`` or
    ``"day"``.  A missing date returns ``(None, None)``.
    """
    if value is None:
        return None, None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value, "day"
    if not isinstance(value, string_types):
        raise DatePrecisionError("not a date string: %r" % (value,))
    text = value.strip()
    if not text:
        return None, None
    match = _DATE_RE.match(text)
    if not match:
        raise DatePrecisionError("invalid MusicBrainz date: %r" % (value,))
    year = int(match.group(1))
    month = match.group(2)
    day = match.group(3)
    if month is None:
        return datetime.date(year, 1, 1), "year"
    month = int(month)
    if not 1 <= month <= 12:
        raise DatePrecisionError("invalid month in %r" % (value,))
    if day is None:
        return datetime.date(year, month, 1), "month"
    try:
        return datetime.date(year, month, int(day)), "day"
    except ValueError:
        raise DatePrecisionError("invalid day in %r" % (value,))


def _period_bounds(value):
    """Return ``(first, last, precision)`` for a partial date."""
    first, precision = parse_date(value)
    if first is None:
        return None, None, None
    if precision == "year":
        last = datetime.date(first.year, 12, 31)
    elif precision == "month":
        last = datetime.date(
            first.year, first.month, calendar.monthrange(first.year, first.month)[1]
        )
    else:
        last = first
    return first, last, precision


def date_precision(value):
    """Return the precision of a MusicBrainz date (``None`` when absent)."""
    return parse_date(value)[1]


def _iso(value):
    return None if value is None else value.isoformat()


def _ranges(first, last):
    return [_iso(first), _iso(last)]


def _as_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


def _range_label(earliest, latest, original_precision):
    """Give the effective precision of a possible interval.

    This is display metadata: a one-day interval is ``day``, while a
    multi-day interval keeps ``year``/``month`` when it still fits the
    original period and otherwise becomes ``range``.
    """
    if earliest is None or latest is None:
        return "unknown"
    if earliest == latest:
        return "day"
    if original_precision == "year" and earliest.year == latest.year:
        return "year"
    if (
        original_precision in ("year", "month")
        and earliest.year == latest.year
        and earliest.month == latest.month
    ):
        return "month"
    return "range"


def _role_spec(value):
    """Normalize a role-map value to ``(label, kind)``.

    Values may be plain strings such as ``"member"``/``"guest"`` or dicts::

        {"role": "客席吉他", "kind": "guest"}
    """
    if isinstance(value, dict):
        label = value.get("role") or value.get("label") or value.get("name")
        kind = value.get("kind") or value.get("category")
        if kind is None and "guest" in value:
            kind = "guest" if value["guest"] else "member"
        if label is None:
            label = kind
        return (text_type(label) if label is not None else None), kind
    if value is None:
        return None, None
    label = text_type(value).strip()
    return (label or None), None


def _label_kind(label):
    if label is None:
        return None
    lowered = label.lower()
    if any(word in lowered for word in _GUEST_WORDS):
        return "guest"
    return None


def _default_role(rel_type):
    if rel_type in _DEFAULT_RELATION_ROLES:
        return _DEFAULT_RELATION_ROLES[rel_type]
    lowered = text_type(rel_type or "").strip().lower()
    if not lowered:
        return "member", "member"
    if "member" in lowered:
        return lowered, "member"
    if any(word in lowered for word in _GUEST_WORDS):
        return lowered, "guest"
    return lowered, "member"


def _is_membership(rel_type, attributes, role_map):
    if role_map:
        if rel_type in role_map:
            return True
        if any(attribute in role_map for attribute in attributes):
            return True
    return rel_type in _DEFAULT_RELATION_ROLES


def _resolve_role(rel_type, attributes, role_map):
    """Resolve the display label and ``member``/``guest`` kind.

    The relation type gets first say about the kind.  A mapped attribute may
    supply a more specific display label (for example ``"lead vocals"`` ->
    ``"主唱"``).  Unknown types fall back to the built-in table.
    """
    type_label = type_kind = None
    if rel_type in role_map:
        type_label, type_kind = _role_spec(role_map[rel_type])

    attr_label = attr_kind = None
    attr_raw = None
    for attribute in attributes:
        if attribute in role_map:
            label, kind = _role_spec(role_map[attribute])
            if label or kind:
                attr_raw = attribute
                attr_label = label
                attr_kind = kind
                break

    default_label, default_kind = _default_role(rel_type)
    # An attribute is more specific than the relation type: a band member
    # whose mapped attribute says "guest" is a guest for that tenure.
    kind = attr_kind or type_kind or _label_kind(attr_label) or _label_kind(type_label)
    if kind is None:
        kind = default_kind
    label = attr_label or type_label or default_label
    return label, kind


class Tenure(object):
    """One membership relationship, independent of every other record.

    The raw strings and original precision are retained for display.  The
    effective bounds live in ``begin_earliest``/``begin_latest`` and
    ``end_earliest``/``end_latest``.  A ``None`` bound means unbounded:

    * an absent begin is ``(None, None)``: the join date is unknown;
    * an absent end with ``ended`` false/absent is an ongoing membership;
    * an absent end with ``ended`` true is an unknown departure, so dates
      after the start remain candidates.

    ``begin_exact``/``end_exact`` indicate that a boundary is a single
    proven day.  Partial dates remain ranges even when a range happens to
    contain only one day.
    """

    def __init__(
        self,
        artist_id,
        artist_name=None,
        begin=None,
        end=None,
        role="member",
        role_kind="member",
        raw_role=None,
        ended=None,
        target_credit=None,
        attributes=None,
        source=None,
        index=0,
    ):
        self.artist_id = artist_id
        self.artist_name = artist_name or target_credit or artist_id
        self.raw_begin = begin
        self.raw_end = end
        self.role = role
        self.role_kind = role_kind
        self.raw_role = raw_role or role
        self.ended = ended
        self.target_credit = target_credit
        self.attributes = list(attributes or [])
        self.source = source
        self.index = index

        be, bl, bp = _period_bounds(begin)
        ee, el, ep = _period_bounds(end)
        self.begin_earliest = be
        self.begin_latest = bl
        self.end_earliest = ee
        self.end_latest = el
        self.begin_precision = bp
        self.end_precision = ep
        self.begin_exact = bp == "day"
        self.end_exact = ep == "day"
        # No end date but ended=true means the departure happened, we just
        # do not know when.  It must not be treated as an ongoing membership.
        self.end_unknown = ep is None and self.ended is True

        # Exact days on which presence was directly observed.  Boundary
        # evidence narrows the possible interval; the witness confirms that
        # one day even when both outer bounds are still ranges.
        self.witnesses = set()
        self._initial = self._snapshot()
        self._evidence = []

    @property
    def begin(self):
        return self.raw_begin

    @property
    def end(self):
        return self.raw_end

    @property
    def is_open(self):
        """Whether the relationship is an ongoing, still-open membership.

        A record with no end date but ``ended=true`` is not open: MusicBrainz
        states the person left, but the departure date is missing.
        """
        return (
            (self.raw_end is None or str(self.raw_end).strip() == "")
            and not self.end_unknown
        )

    @property
    def is_guest(self):
        return self.role_kind == "guest"

    @property
    def key(self):
        return self.artist_id, self.raw_begin, self.raw_end, self.role_kind, self.index

    @property
    def begin_range(self):
        return _ranges(self.begin_earliest, self.begin_latest)

    @property
    def end_range(self):
        return _ranges(self.end_earliest, self.end_latest)

    def covers(self, when):
        """Whether the relationship can possibly include ``when``."""
        when = _as_date(when)
        if self.begin_earliest is not None and when < self.begin_earliest:
            return False
        if self.end_latest is not None and when > self.end_latest:
            return False
        return True

    def certainty(self, when):
        """Classify ``when`` as ``absent``, ``candidate`` or ``confirmed``.

        ``confirmed`` means the stored precision proves membership that day.
        ``candidate`` means the day is inside the possible bounds but a
        join/departure boundary is only known to a year or month, or is
        unknown.
        """
        when = _as_date(when)
        if not self.covers(when):
            return "absent"
        if when in self.witnesses:
            return "confirmed"
        return "confirmed" if self._start_proven(when) and self._end_proven(when) else "candidate"

    def _start_proven(self, when):
        if self.begin_latest is None:
            return False
        if self.begin_exact:
            return when >= self.begin_latest
        # A year/month start proves membership only after the whole join
        # window has passed.
        return when > self.begin_latest

    def _end_proven(self, when):
        if self.end_latest is None:
            return not self.end_unknown
        if self.end_exact:
            return when <= self.end_latest
        if self.end_earliest is None:
            return when <= self.end_latest
        # A year/month end proves membership only before the departure
        # window starts.
        return when < self.end_earliest

    def _snapshot(self):
        return {
            "begin-range": self.begin_range,
            "end-range": self.end_range,
            "begin-exact": self.begin_exact,
            "end-exact": self.end_exact,
            "begin-precision": self.begin_precision,
            "end-precision": self.end_precision,
            "end-unknown": self.end_unknown,
            "confirmed-days": sorted(_iso(day) for day in self.witnesses),
        }

    def _clone(self):
        result = Tenure(
            self.artist_id,
            artist_name=self.artist_name,
            begin=self.raw_begin,
            end=self.raw_end,
            role=self.role,
            role_kind=self.role_kind,
            raw_role=self.raw_role,
            ended=self.ended,
            target_credit=self.target_credit,
            attributes=self.attributes,
            source=self.source,
            index=self.index,
        )
        for name in (
            "begin_earliest",
            "begin_latest",
            "end_earliest",
            "end_latest",
            "begin_precision",
            "end_precision",
            "begin_exact",
            "end_exact",
            "end_unknown",
        ):
            setattr(result, name, getattr(self, name))
        result._initial = dict(self._initial)
        result._evidence = list(self._evidence)
        result.witnesses = set(self.witnesses)
        return result

    def changed_since_recorded(self):
        initial = dict(self._initial)
        current = dict(self._snapshot())
        initial.pop("confirmed-days", None)
        current.pop("confirmed-days", None)
        return current != initial

    def narrow(self, evidence):
        """Apply one :class:`Evidence` to this tenure.

        Returns ``(new_tenure, conflict)``.  ``new_tenure`` is ``None`` when
        the evidence does not narrow anything, and ``conflict`` is a string
        when the observation contradicts the relationship.

        Only date *bounds* are changed here.  A ``present`` observation that
        falls inside an already-certain core does not alter the stored
        membership span and therefore returns ``None``; the caller records
        the witnessed day separately.
        """
        when = evidence.when
        kind = evidence.kind

        if kind == "present":
            if not self.covers(when):
                return None, "evidence date %s is outside this tenure" % _iso(when)
            result = self._clone()
            if result.begin_latest is None or when < result.begin_latest:
                result.begin_latest = when
                result.begin_precision = _range_label(
                    result.begin_earliest, result.begin_latest, result.begin_precision
                )
                result.begin_exact = (
                    result.begin_earliest is not None
                    and result.begin_earliest == result.begin_latest
                )
            if result.end_earliest is None or when > result.end_earliest:
                result.end_earliest = when
                result.end_precision = _range_label(
                    result.end_earliest, result.end_latest, result.end_precision
                )
                result.end_exact = (
                    result.end_latest is not None
                    and result.end_earliest == result.end_latest
                )
                result.end_unknown = False
            result.witnesses = set(self.witnesses)
            result.witnesses.add(when)
            result._evidence = list(self._evidence) + [evidence]
            changed = _if_changed(self, result)
            if changed is None:
                return None, None
            return changed, None

        if kind == "absent":
            status = self.certainty(when)
            if status == "confirmed":
                return None, "evidence date %s contradicts confirmed membership" % _iso(when)
            if not self.covers(when):
                # Already known absent before the join or after the departure.
                return None, None
            result = self._clone()

            # An absence only proves one of two things: the join had not
            # happened yet, or the departure had already happened.  Decide
            # from the interval geometry, and if the day could be either,
            # do not guess.
            in_join_window = (
                self.begin_earliest is not None
                and self.begin_latest is not None
                and self.begin_earliest <= when <= self.begin_latest
            )
            after_join_window = (
                self.begin_latest is not None and when > self.begin_latest
            )
            in_leave_window = (
                self.end_earliest is not None
                and self.end_latest is not None
                and self.end_earliest <= when <= self.end_latest
            )
            before_leave_window = (
                self.end_earliest is not None and when < self.end_earliest
            )

            # The person was certainly a member before their departure window
            # and after their join window.  Absence there contradicts the
            # record rather than narrowing it.
            if after_join_window and before_leave_window:
                return None, "evidence date %s contradicts confirmed membership" % _iso(when)

            # Not yet joined: the day is in (or before) the join window.
            if (in_join_window or self.begin_latest is None) and not after_join_window:
                result.begin_earliest = when + datetime.timedelta(days=1)
                result.begin_precision = _range_label(
                    result.begin_earliest, result.begin_latest, result.begin_precision
                )
                result.begin_exact = (
                    result.begin_earliest == result.begin_latest
                )
            elif in_leave_window and after_join_window:
                # Already left: the day is in the departure window (or after
                # it), so the last possible membership day precedes it.
                result.end_latest = when - datetime.timedelta(days=1)
                result.end_precision = _range_label(
                    result.end_earliest, result.end_latest, result.end_precision
                )
                result.end_exact = (
                    result.end_earliest is not None
                    and result.end_earliest == result.end_latest
                )
                result.end_unknown = False
            else:
                # Ambiguous: the observation could mean "not joined yet" or
                # "already left".  Leave the record untouched.
                return None, None

            if (
                result.begin_latest is not None
                and result.begin_earliest is not None
                and result.begin_earliest > result.begin_latest
            ):
                return None, "evidence date %s leaves no possible membership" % _iso(when)
            if (
                result.end_earliest is not None
                and result.end_latest is not None
                and result.end_latest < result.end_earliest
            ):
                return None, "evidence date %s leaves no possible membership" % _iso(when)
            result._evidence = list(self._evidence) + [evidence]
            return _if_changed(self, result), None

        if kind == "joined":
            if not self.covers(when):
                return None, "join date %s is outside this tenure" % _iso(when)
            if self.end_latest is not None and when > self.end_latest:
                return None, "join date %s is after the recorded departure" % _iso(when)
            result = self._clone()
            result.begin_earliest = when
            result.begin_latest = when
            result.begin_precision = "day"
            result.begin_exact = True
            result.witnesses = set(self.witnesses)
            result.witnesses.add(when)
            result._evidence = list(self._evidence) + [evidence]
            return _if_changed(self, result), None

        if kind == "left":
            if not self.covers(when):
                return None, "leave date %s is outside this tenure" % _iso(when)
            if self.begin_latest is not None and when < self.begin_latest:
                return None, "leave date %s leaves no possible membership" % _iso(when)
            result = self._clone()
            result.end_earliest = when
            result.end_latest = when
            result.end_precision = "day"
            result.end_exact = True
            result.end_unknown = False
            result.witnesses = set(self.witnesses)
            result.witnesses.add(when)
            result._evidence = list(self._evidence) + [evidence]
            return _if_changed(self, result), None

        raise ValueError("unknown evidence kind: %r" % (kind,))

    def to_dict(self):
        return {
            "tenure-id": self.index,
            "artist-id": self.artist_id,
            "artist-name": self.artist_name,
            "role": self.role,
            "role-kind": self.role_kind,
            "guest": self.is_guest,
            "raw-role": self.raw_role,
            "begin": self.raw_begin,
            "end": self.raw_end,
            "begin-precision": self.begin_precision,
            "end-precision": self.end_precision,
            "begin-range": self.begin_range,
            "end-range": self.end_range,
            "open-ended": self.is_open and not self.end_unknown,
            "end-unknown": self.end_unknown,
            "attributes": list(self.attributes),
            "target-credit": self.target_credit,
            "evidence": [e.to_dict() for e in self._evidence],
            "changed-by-evidence": self.changed_since_recorded(),
        }

    def __repr__(self):
        return "<Tenure #%d %s %s..%s role=%s>" % (
            self.index,
            self.artist_name,
            self.raw_begin,
            self.raw_end,
            self.role,
        )


def _if_changed(before, after):
    """Whether boundary ranges (not witness days) differ."""
    before_bounds = dict(before._snapshot())
    after_bounds = dict(after._snapshot())
    before_bounds.pop("confirmed-days", None)
    after_bounds.pop("confirmed-days", None)
    return None if after_bounds == before_bounds else after


def _span_text(tenure):
    raw_begin = tenure.raw_begin if tenure.raw_begin is not None else "?"
    raw_end = tenure.raw_end if tenure.raw_end is not None else "?"
    return "%s..%s" % (raw_begin, raw_end)


class Evidence(object):
    """A dated observation that can narrow membership bounds.

    ``kind`` is one of:

    ``present``
        The person was in the band on ``when``.  This proves the join
        happened no later than that day and the departure no earlier, but
        it does not assert either exact boundary.
    ``absent``
        The person was not in the band on ``when``.  This can exclude a
        join on or before that day, or a departure on or after it.
    ``joined``
        ``when`` is the exact join date.
    ``left``
        ``when`` is the exact leave date (inclusive, matching MusicBrainz
        end-date semantics).

    ``target`` may be an artist id, an artist name, a tenure id, or ``None``
    to apply the evidence to every tenure whose bounds it can affect.
    ``source``/``note`` describe where the evidence came from.  They are
    also used by :meth:`Lineup.evidence_effect` for later lookup.
    """

    VALID_KINDS = ("present", "absent", "joined", "left")

    def __init__(self, when, kind="present", target=None, source=None, note=None,
                 evidence_id=None):
        self.when = _as_date(when)
        if kind not in self.VALID_KINDS:
            raise ValueError("evidence kind must be one of %s" % (self.VALID_KINDS,))
        self.kind = kind
        self.target = target
        self.source = source
        self.note = note
        self.id = evidence_id

    def matches(self, tenure):
        if self.target is None:
            return True
        if isinstance(self.target, int) and not isinstance(self.target, bool):
            return self.target == tenure.index
        return self.target in (
            tenure.artist_id,
            tenure.artist_name,
            tenure.target_credit,
        )

    def to_dict(self):
        return {
            "evidence-id": self.id,
            "when": _iso(self.when),
            "kind": self.kind,
            "target": self.target,
            "source": self.source,
            "note": self.note,
        }

    def __repr__(self):
        return "<Evidence %s %s target=%r>" % (self.kind, _iso(self.when), self.target)


class EvidenceResult(list):
    """List of tenure changes produced by :meth:`Lineup.apply_evidence`.

    It behaves like a list of change dictionaries for tenures whose outer
    date ranges were narrowed.  ``witnessed`` lists tenures that gained a
    confirmed day without changing either outer range (a presence record
    inside an already-certain core).  ``conflicts`` contains observations
    that contradicted a relationship or could not be applied; ``unchanged``
    counts relationships the evidence did not affect at all.
    """

    def __init__(self, changes=None, conflicts=None, unchanged=0, witnessed=None):
        super(EvidenceResult, self).__init__(changes or [])
        self.conflicts = list(conflicts or [])
        self.unchanged = unchanged
        self.witnessed = list(witnessed or [])

    @property
    def changes(self):
        return list(self)

    @property
    def changed_tenures(self):
        return [change["tenure-id"] for change in self]

    def describe(self):
        parts = []
        for change in self:
            parts.append(
                "%s (tenure #%d, relationship %s) tightened: begin %s -> %s; end %s -> %s"
                % (
                    change["artist-name"],
                    change["tenure-id"],
                    change["relationship"],
                    change["before"]["begin-range"],
                    change["after"]["begin-range"],
                    change["before"]["end-range"],
                    change["after"]["end-range"],
                )
            )
        for item in self.witnessed:
            parts.append(
                "%s (tenure #%d) confirmed present on %s; outer ranges unchanged"
                % (
                    item["artist-name"],
                    item["tenure-id"],
                    item["evidence"]["when"],
                )
            )
        if not parts:
            return "No membership tenure was narrowed."
        return "\n".join(parts)


class Membership(object):
    """One person's status on a queried date.

    ``tenures`` contains every relationship of this person of the same kind
    (member or guest) that covers the date.  ``status`` is ``confirmed``
    when at least one of those relationships confirms presence, otherwise
    ``candidate``.  Separate artist ids are never merged even when names
    are identical.
    """

    def __init__(self, artist_id, artist_name, tenures, status, role_kind):
        self.artist_id = artist_id
        self.artist_name = artist_name
        self.tenures = list(tenures)
        self.status = status
        self.role_kind = role_kind
        self.roles = [t.role for t in self.tenures]

    @property
    def role(self):
        return self.roles[0]

    @property
    def guest(self):
        return self.role_kind == "guest"

    @property
    def candidate(self):
        return self.status != "confirmed"

    def to_dict(self):
        return {
            "artist-id": self.artist_id,
            "artist-name": self.artist_name,
            "role": self.role,
            "roles": list(self.roles),
            "role-kind": self.role_kind,
            "guest": self.guest,
            "status": self.status,
            "tenure-ids": [t.index for t in self.tenures],
            "tenures": [t.to_dict() for t in self.tenures],
        }

    def __repr__(self):
        return "<Membership %s (%s) %s>" % (
            self.artist_name,
            self.artist_id,
            self.status,
        )


class Lineup(object):
    """A band lineup parsed from MusicBrainz artist relationships.

    ``artist`` may be:

    * the full result of ``get_artist_by_id(id, ["artist-rels"])``;
    * the nested ``result["artist"]`` dict;
    * a plain list of relationship dicts.

    ``role_map`` maps MusicBrainz relationship types, instrument attributes
    or free-form roles to role labels/kinds.  A mapping value can be a string
    such as ``"member"`` / ``"guest"`` or a dict::

        {"member of band": "member",
         "guest performer": {"role": "客席吉他", "kind": "guest"},
         "lead vocals": "主唱"}

    If a role map is supplied, relationship types explicitly in the map and
    types with a mapped attribute are considered, in addition to the built-in
    membership types.  Without a map, built-in membership types are used and
    non-membership relations (subgroups, URLs, ...) are ignored.
    """

    def __init__(self, artist, role_map=None):
        self.role_map = dict(role_map or {})
        self.artist_id = None
        self.artist_name = None
        self.tenures = []
        self._evidence = []
        self._evidence_log = []
        self._load(artist)

    def _load(self, artist):
        if isinstance(artist, Lineup):
            self.artist_id = artist.artist_id
            self.artist_name = artist.artist_name
            self.role_map = dict(artist.role_map)
            self.tenures = [t._clone() for t in artist.tenures]
            self._evidence = list(artist._evidence)
            self._evidence_log = list(artist._evidence_log)
            return

        relations = artist
        if isinstance(artist, dict):
            if isinstance(artist.get("artist"), dict):
                artist = artist["artist"]
            self.artist_id = artist.get("id")
            self.artist_name = artist.get("name")
            relations = artist.get("artist-relation-list")
            if relations is None:
                relations = artist.get("relation-list", [])
        if relations is None:
            relations = []
        if not isinstance(relations, (list, tuple)):
            raise TypeError("artist relationships must be a list")

        index = 0
        for relation in relations:
            if not isinstance(relation, dict):
                raise TypeError("relationship must be a dict")
            rel_type = relation.get("type", "member of band")
            attributes = self._attributes(relation)
            if not _is_membership(rel_type, attributes, self.role_map):
                continue
            self.tenures.append(
                self._tenure_from_relation(relation, rel_type, attributes, index)
            )
            index += 1

    @staticmethod
    def _attributes(relation):
        attributes = relation.get("attribute-list")
        if attributes is None:
            attributes = []
            for item in relation.get("attributes", []):
                if isinstance(item, dict):
                    attributes.append(item.get("attribute", item))
                else:
                    attributes.append(item)
        return list(attributes)

    def _tenure_from_relation(self, relation, rel_type, attributes, index):
        target = relation.get("artist") or {}
        artist_id = (
            relation.get("target")
            or target.get("id")
            or relation.get("target-id")
        )
        if not artist_id:
            raise ValueError(
                "membership relationship has no target artist (%r)" % (rel_type,)
            )
        role, kind = _resolve_role(rel_type, attributes, self.role_map)
        name = target.get("name") or relation.get("target-credit") or artist_id
        return Tenure(
            artist_id,
            artist_name=name,
            begin=relation.get("begin"),
            end=relation.get("end"),
            role=role,
            role_kind=kind,
            raw_role=rel_type,
            ended=_as_bool(relation.get("ended")),
            target_credit=relation.get("target-credit"),
            attributes=attributes,
            source=relation,
            index=index,
        )

    def _grouped(self, when, role_kind):
        when = _as_date(when)
        groups = {}
        order = []
        for tenure in self.tenures:
            if tenure.role_kind != role_kind:
                continue
            status = tenure.certainty(when)
            if status == "absent":
                continue
            key = tenure.artist_id
            if key not in groups:
                groups[key] = {
                    "name": tenure.artist_name,
                    "tenures": [],
                    "confirmed": False,
                }
                order.append(key)
            groups[key]["tenures"].append(tenure)
            if status == "confirmed":
                groups[key]["confirmed"] = True
        memberships = []
        for key in order:
            group = groups[key]
            memberships.append(
                Membership(
                    key,
                    group["name"],
                    group["tenures"],
                    "confirmed" if group["confirmed"] else "candidate",
                    role_kind,
                )
            )
        return memberships

    def on(self, when):
        """Return ``(confirmed_members, candidate_members)`` on ``when``.

        Guest performers are excluded here; use :meth:`guests` for them.
        A person appears at most once in each list, but separate MusicBrainz
        artist ids always stay separate even when their names are identical.
        All matching relationships of that person are attached to the
        membership, including two records for a leave-and-rejoin.
        """
        memberships = self._grouped(when, "member")
        confirmed = [m for m in memberships if m.status == "confirmed"]
        candidates = [m for m in memberships if m.status != "confirmed"]
        return confirmed, candidates

    def guests(self, when):
        """Return ``(confirmed_guests, candidate_guests)`` on ``when``."""
        memberships = self._grouped(when, "guest")
        confirmed = [m for m in memberships if m.status == "confirmed"]
        candidates = [m for m in memberships if m.status != "confirmed"]
        return confirmed, candidates

    def confirmed(self, when):
        """Return only confirmed member memberships on ``when``."""
        return self.on(when)[0]

    def candidates(self, when):
        """Return candidate member memberships on ``when``."""
        return self.on(when)[1]

    def members_on(self, when):
        """Return ``(confirmed_names, candidate_names)`` on ``when``.

        Guest performers are not included.  Use :meth:`guest_names` if they
        should be shown separately.
        """
        confirmed, candidates = self.on(when)
        return (
            [m.artist_name for m in confirmed],
            [m.artist_name for m in candidates],
        )

    def guest_names(self, when):
        """Return ``(confirmed_guest_names, candidate_guest_names)``."""
        confirmed, candidates = self.guests(when)
        return (
            [m.artist_name for m in confirmed],
            [m.artist_name for m in candidates],
        )

    def query(self, when):
        """Return a JSON-friendly snapshot for ``when``.

        ``confirmed`` and ``candidates`` are members; guests are listed
        separately so a guest with a member's name cannot be mistaken for
        that member.
        """
        confirmed, candidates = self.on(when)
        confirmed_guests, candidate_guests = self.guests(when)
        return {
            "date": _iso(_as_date(when)),
            "confirmed": [m.to_dict() for m in confirmed],
            "candidates": [m.to_dict() for m in candidates],
            "confirmed-guests": [m.to_dict() for m in confirmed_guests],
            "candidate-guests": [m.to_dict() for m in candidate_guests],
        }

    def tenures_for(self, artist):
        """Return every tenure for an artist id, name or tenure id."""
        if isinstance(artist, int) and not isinstance(artist, bool):
            return [t for t in self.tenures if t.index == artist]
        return [
            t
            for t in self.tenures
            if artist in (t.artist_id, t.artist_name, t.target_credit)
        ]

    def timeline(self):
        """Return all relationships in chronological order.

        This is deliberately a list of independent tenures rather than a
        merged "state at every date" timeline.  Merging touching records
        would silently assert membership during undocumented gaps, which
        MusicBrainz does not claim.
        """
        ordered = sorted(
            self.tenures,
            key=lambda t: (
                t.begin_earliest or datetime.date.min,
                t.end_latest or datetime.date.max,
                t.index,
            ),
        )
        return [t.to_dict() for t in ordered]

    def apply_evidence(self, evidence):
        """Apply one observation and report exactly which tenures it narrowed.

        Returns an :class:`EvidenceResult` (a list subclass) whose entries
        identify the tenure id, artist, original relationship dates and the
        before/after membership bounds.  Observations that contradict a
        confirmed membership are reported in ``result.conflicts`` instead of
        silently changing the timeline.

        The same association is stored in :attr:`evidence_log`, so it can
        later be retrieved with :meth:`evidence_effect` by evidence id,
        source or note.
        """
        if not isinstance(evidence, Evidence):
            raise TypeError("expected Evidence, got %r" % (evidence,))
        if evidence.target is None:
            # An observation about one day can match several artists who
            # happen to share a name.  Never silently narrow more than one
            # identity: require the caller to name the target instead.
            candidates = [t for t in self.tenures if t.covers(evidence.when)]
            identities = set(t.artist_id for t in candidates)
            if evidence.kind in ("joined", "left") and len(identities) > 1:
                return EvidenceResult(
                    conflicts=[
                        {
                            "evidence": evidence.to_dict(),
                            "reason": (
                                "ambiguous target: %d artist ids could match; "
                                "specify an artist id or tenure id"
                                % len(identities)
                            ),
                        }
                    ]
                )
        changes = []
        conflicts = []
        witnessed = []
        updated = []
        unchanged = 0
        anything_applied = False
        for tenure in self.tenures:
            targeted = evidence.target is not None
            if targeted and not evidence.matches(tenure):
                updated.append(tenure)
                continue
            if not targeted and not tenure.covers(evidence.when):
                # Untargeted evidence is about one date; a relationship that
                # cannot include that date is simply out of scope, not a
                # contradiction.
                updated.append(tenure)
                continue
            had_witness = evidence.when in tenure.witnesses
            narrowed, conflict = tenure.narrow(evidence)
            if conflict:
                if targeted:
                    conflicts.append(
                        {
                            "tenure-id": tenure.index,
                            "artist-id": tenure.artist_id,
                            "artist-name": tenure.artist_name,
                            "relationship": _span_text(tenure),
                            "evidence": evidence.to_dict(),
                            "reason": conflict,
                        }
                    )
                else:
                    unchanged += 1
                updated.append(tenure)
                continue
            if narrowed is None:
                if not had_witness and evidence.kind in ("present", "joined", "left"):
                    # A presence record can confirm a day without changing
                    # either outer range.  Retain that witness explicitly.
                    witness = tenure._clone()
                    witness.witnesses = set(tenure.witnesses)
                    witness.witnesses.add(evidence.when)
                    witness._evidence = list(tenure._evidence) + [evidence]
                    updated.append(witness)
                    anything_applied = True
                    witnessed.append(
                        {
                            "tenure-id": tenure.index,
                            "artist-id": tenure.artist_id,
                            "artist-name": tenure.artist_name,
                            "relationship": _span_text(tenure),
                            "evidence": evidence.to_dict(),
                        }
                    )
                    continue
                unchanged += 1
                updated.append(tenure)
                continue
            anything_applied = True
            before = tenure._snapshot()
            after = narrowed._snapshot()
            changes.append(
                {
                    "tenure-id": tenure.index,
                    "artist-id": tenure.artist_id,
                    "artist-name": tenure.artist_name,
                    "role": tenure.role,
                    "role-kind": tenure.role_kind,
                    "relationship": _span_text(tenure),
                    "before": before,
                    "after": after,
                    "evidence": evidence.to_dict(),
                }
            )
            updated.append(narrowed)
        self.tenures = updated
        if anything_applied or not conflicts:
            self._evidence.append(evidence)
        if (
            not anything_applied
            and evidence.target is not None
            and not conflicts
        ):
            conflicts.append(
                {
                    "evidence": evidence.to_dict(),
                    "reason": "no tenure matched the evidence target",
                }
            )
        result = EvidenceResult(
            changes=changes,
            conflicts=conflicts,
            unchanged=unchanged,
            witnessed=witnessed,
        )
        self._evidence_log.append(
            {
                "evidence": evidence.to_dict(),
                "changes": [dict(change) for change in changes],
                "witnessed": [dict(item) for item in witnessed],
                "conflicts": list(conflicts),
            }
        )
        return result

    def evidence_effect(self, ref):
        """Return the logged effect(s) of a specific piece of evidence.

        ``ref`` may be an evidence id, source, note value, or the
        :class:`Evidence` object itself.  Returns a list of log entries; each
        entry contains the evidence and the changes it caused, including
        tenure ids and before/after bounds.  An empty list means that piece
        of evidence was not applied or did not narrow anything.
        """
        if isinstance(ref, Evidence):
            values = (ref.id, ref.source, ref.note)
        else:
            values = (ref,)
        matches = []
        for entry in self._evidence_log:
            item = entry["evidence"]
            candidates = (item.get("evidence-id"), item.get("source"), item.get("note"))
            if any(value is not None and value in candidates for value in values):
                matches.append(entry)
        return matches

    @property
    def evidence(self):
        return list(self._evidence)

    @property
    def evidence_log(self):
        return list(self._evidence_log)

    def to_dict(self):
        return {
            "artist-id": self.artist_id,
            "artist-name": self.artist_name,
            "tenures": [t.to_dict() for t in self.tenures],
            "evidence": [e.to_dict() for e in self._evidence],
            "evidence-log": list(self._evidence_log),
        }

    def __repr__(self):
        return "<Lineup %s with %d tenures>" % (
            self.artist_name or self.artist_id,
            len(self.tenures),
        )


def build_lineup(artist, role_map=None):
    """Convenience wrapper around :class:`Lineup`."""
    return Lineup(artist, role_map=role_map)


def query_lineup(artist, when, role_map=None):
    """Build a lineup and return its confirmed/candidate snapshot."""
    return Lineup(artist, role_map=role_map).query(when)
