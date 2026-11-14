"""Match concert-poster credits against MusicBrainz artists.

A concert poster often prints stage names that no longer match the
artist's current profile name, so cataloguing cannot just take the
first search hit.  Given

* the poster's date range,
* the region (area) of the show,
* verifiable alias validity periods (MusicBrainz alias begin/end
  dates), and
* the full same-show lineup,

this module returns every candidate pairing that can explain *all*
credits on the poster at the same time.

Rules implemented (from the cataloguing requirements):

* Every search hit is considered, not only the top-ranked one.
* The same artist may cover two different spellings only when the
  poster explicitly marks them as the same role (an ``aka`` group).
* If a credit is only explained by an alias that became valid after
  part of the poster's date range (i.e. a name change), the possible
  performance dates are narrowed accordingly and reported together
  with the pairing.

The matching logic is backend-agnostic: ``match_poster`` takes a
``search`` callable so it can be driven by the real MusicBrainz web
service (:class:`MusicBrainzBackend`) or by fixtures in tests.
"""

import json
import re
import sys
from datetime import date, timedelta
from itertools import product

__all__ = [
    "MusicBrainzBackend",
    "match_poster",
    "candidates_for_group",
]


# ---------------------------------------------------------------------------
# partial dates
# ---------------------------------------------------------------------------

def _parse_partial(value):
    """Parse a MusicBrainz partial date ('YYYY', 'YYYY-MM' or
    'YYYY-MM-DD') into a (first_day, last_day) pair of ``date``."""
    parts = [int(p) for p in value.split("-")]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else None
    day = parts[2] if len(parts) > 2 else None
    if month is None:
        return date(year, 1, 1), date(year, 12, 31)
    if day is None:
        if month == 12:
            last = date(year, 12, 31)
        else:
            last = date(year, month + 1, 1) - timedelta(days=1)
        return date(year, month, 1), last
    return date(year, month, day), date(year, month, day)


def _window(begin, end):
    """Normalise a (begin, end) pair of partial-date strings (either may
    be ``None`` for an open side) into dates."""
    first = _parse_partial(begin)[0] if begin else None
    last = _parse_partial(end)[1] if end else None
    return (first, last)


def _intersect(w1, w2):
    """Intersect two (begin, end) windows; ``None`` means unbounded.
    Returns ``None`` when the intersection is empty."""
    begin = max(b for b in (w1[0], w2[0]) if b is not None) \
        if w1[0] is not None or w2[0] is not None else None
    ends = [e for e in (w1[1], w2[1]) if e is not None]
    end = min(ends) if ends else None
    if begin is not None and end is not None and begin > end:
        return None
    return (begin, end)


def _fmt_window(window):
    return {
        "begin": window[0].isoformat() if window[0] else None,
        "end": window[1].isoformat() if window[1] else None,
    }


def _union(w1, w2):
    """Union of two (begin, end) windows; ``None`` means unbounded."""
    begins = [b for b in (w1[0], w2[0]) if b is not None]
    ends = [e for e in (w1[1], w2[1]) if e is not None]
    begin = min(begins) if len(begins) == 2 else None
    end = max(ends) if len(ends) == 2 else None
    return (begin, end)


# ---------------------------------------------------------------------------
# name handling
# ---------------------------------------------------------------------------

def _norm(text):
    return re.sub(r"\s+", " ", text.strip().casefold())


def _spelling_windows(artist):
    """Yield ``(spelling, window, source)`` for the artist's current
    name and every alias.

    The current name is valid for the artist's life-span; an alias is
    valid between its own begin/end dates when recorded, falling back
    to the life-span otherwise.
    """
    lifespan = artist.get("life-span") or {}
    life_window = _window(lifespan.get("begin"), lifespan.get("end"))
    yield artist.get("name", ""), life_window, {"kind": "name"}
    for alias in artist.get("alias-list", []):
        window = _window(alias.get("begin-date"), alias.get("end-date"))
        window = (_intersect(window, life_window)
                  if window != (None, None) else life_window)
        if window is None:
            continue
        yield alias.get("alias", ""), window, {
            "kind": "alias",
            "locale": alias.get("locale"),
            "type": alias.get("type"),
            "begin": alias.get("begin-date"),
            "end": alias.get("end-date"),
        }


def _area_check(poster_area, artist):
    """Return ``(accepted, verified)`` for the poster's region.

    A known, mismatched area rejects the candidate; an artist without
    area information is kept but flagged as unverified.
    """
    if not poster_area:
        return True, False
    want = _norm(poster_area)
    names = []
    area = artist.get("area")
    if area and area.get("name"):
        names.append(area["name"])
    if artist.get("country"):
        names.append(artist["country"])
    if not names:
        return True, False
    return any(_norm(n) == want for n in names), True


# ---------------------------------------------------------------------------
# candidate collection
# ---------------------------------------------------------------------------

def candidates_for_spelling(spelling, poster_window, poster_area, artists):
    """All artists that can explain one printed *spelling*.

    Every artist is checked (never just the top search hit); a
    candidate must match the spelling by current name or alias, be
    compatible with the poster's region, and have a name/alias
    validity window overlapping the poster's date range.
    """
    found = []
    seen = set()
    for artist in artists:
        if artist.get("id") in seen:
            continue
        ok, verified = _area_check(poster_area, artist)
        if not ok:
            continue
        for sp, window, source in _spelling_windows(artist):
            if _norm(sp) != _norm(spelling):
                continue
            overlap = _intersect(window, poster_window)
            if overlap is None:
                continue
            seen.add(artist.get("id"))
            found.append({
                "artist_id": artist.get("id"),
                "artist_name": artist.get("name"),
                "spelling": spelling,
                "matched_as": sp,
                "match": source,
                "window": overlap,
                "area_verified": verified,
            })
            break
    return found


def _group_credits(credits):
    """Group poster credits.  Credits sharing an explicit ``aka`` key
    are marked on the poster as the same role and may be covered by a
    single artist; every other credit stands alone."""
    groups = {}
    order = []
    for index, credit in enumerate(credits):
        key = ("aka", credit["aka"]) if credit.get("aka") else ("solo", index)
        if key not in groups:
            groups[key] = {"role": credit.get("role"), "credits": []}
            order.append(key)
        groups[key]["credits"].append(credit)
    return [groups[key] for key in order]


def candidates_for_group(group, poster_window, poster_area, search):
    """Artists explaining every spelling of one credit group.

    For an ``aka`` group the same artist must match *all* spellings;
    the group's validity window is the intersection of the individual
    spelling windows.
    """
    per_spelling = []
    for credit in group["credits"]:
        artists = search(credit["name"])
        hits = candidates_for_spelling(
            credit["name"], poster_window, poster_area, artists)
        if not hits:
            return []
        per_spelling.append(hits)

    grouped = {}
    for combo in product(*per_spelling):
        artist_id = combo[0]["artist_id"]
        if any(c["artist_id"] != artist_id for c in combo):
            continue
        window = combo[0]["window"]
        for c in combo[1:]:
            window = _intersect(window, c["window"])
            if window is None:
                break
        if window is None:
            continue
        entry = grouped.setdefault(artist_id, {
            "artist_id": artist_id,
            "artist_name": combo[0]["artist_name"],
            "role": group["role"],
            "credited_as": [c["spelling"] for c in combo],
            "matched_by": [{
                "spelling": c["spelling"],
                "matched_as": c["matched_as"],
                "match": c["match"],
            } for c in combo],
            "area_verified": all(c["area_verified"] for c in combo),
            "window": window,
        })
        # keep the widest explaining window per artist
        entry["window"] = _union(entry["window"], window)
    return list(grouped.values())


# ---------------------------------------------------------------------------
# full-lineup matching
# ---------------------------------------------------------------------------

def match_poster(poster, search):
    """Return candidate pairings that explain the whole billing.

    *poster* is a dict with ``date_begin``/``date_end`` (partial
    dates, either may be ``None``), ``area`` and ``credits`` (a list
    of ``{"name", "role", "aka"?}``).  *search* maps a printed name to
    artist dicts (MusicBrainz shape).

    Each solution carries the narrowed ``possible_dates`` for the show
    together with the per-role assignments; a solution is only
    returned when every credit is explained and the same artist is
    never reused across roles unless the poster marked the credits as
    one ``aka`` group.
    """
    poster_window = _window(poster.get("date_begin"), poster.get("date_end"))
    poster_area = poster.get("area")
    groups = _group_credits(poster.get("credits", []))

    per_group = []
    for group in groups:
        candidates = candidates_for_group(
            group, poster_window, poster_area, search)
        if not candidates:
            return []
        per_group.append(candidates)

    solutions = []
    for combo in product(*per_group):
        ids = [c["artist_id"] for c in combo]
        if len(set(ids)) != len(ids):
            # one artist may only cover several credits when the poster
            # explicitly grouped them via "aka"
            continue
        window = poster_window
        for c in combo:
            window = _intersect(window, c["window"])
            if window is None:
                break
        if window is None:
            continue
        narrowed = window != poster_window
        solutions.append({
            "possible_dates": _fmt_window(window),
            "dates_narrowed": narrowed,
            "assignments": [{
                "role": c["role"],
                "credited_as": c["credited_as"],
                "artist_id": c["artist_id"],
                "artist_name": c["artist_name"],
                "matched_by": c["matched_by"],
                "valid_window": _fmt_window(c["window"]),
                "area_verified": c["area_verified"],
            } for c in combo],
        })
    return solutions


# ---------------------------------------------------------------------------
# MusicBrainz backend
# ---------------------------------------------------------------------------

class MusicBrainzBackend(object):
    """``search`` callable backed by the MusicBrainz web service.

    Searches both the artist name and the alias index and fetches full
    alias lists (with begin/end dates) for every hit, so candidates
    are never limited to the top search result.
    """

    def __init__(self, limit=25):
        import musicbrainzngs
        self._mb = musicbrainzngs
        self._limit = limit
        self._cache = {}

    def _full_artist(self, artist_id):
        if artist_id not in self._cache:
            res = self._mb.get_artist_by_id(artist_id, includes=["aliases"])
            self._cache[artist_id] = res["artist"]
        return self._cache[artist_id]

    def __call__(self, name):
        hits = {}
        for fields in ({"artist": name}, {"alias": name}):
            res = self._mb.search_artists(limit=self._limit, **fields)
            for hit in res.get("artist-list", []):
                hits[hit["id"]] = hit["id"]
        return [self._full_artist(artist_id) for artist_id in hits]


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: python poster_match.py POSTER.json\n")
        return 2
    with open(argv[1]) as fh:
        poster = json.load(fh)
    import musicbrainzngs
    musicbrainzngs.set_useragent("poster-match", "0.1",
                                 "https://example.org/poster-match")
    solutions = match_poster(poster, MusicBrainzBackend())
    json.dump(solutions, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
