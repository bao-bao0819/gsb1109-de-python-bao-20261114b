# This file is part of the musicbrainzngs library
# Copyright (C) Alastair Porter, Adrian Sampson, and others
# This file is distributed under a BSD-2-Clause type license.
# See the COPYING file for more information.

"""Offline claiming of a recovered CD box from its table of contents.

A recovered disc usually only carries a table of contents (TOC): a list of
track start sectors plus the leadout.  The same Disc ID can be attached to
releases from several countries, and a release can span several mediums
(a box set).  This module matches a local TOC against release/medium data
returned by :func:`musicbrainzngs.get_releases_by_discid` (or by
:func:`musicbrainzngs.get_release_by_id`) and decides which releases are
still viable for the *whole box*.

The matching is deliberately staged, mirroring the information actually
present on a recovered disc:

1. Track geometry: relative track offsets and the total programme length are
   aligned first.  Absolute offsets and the leadout are *not* compared by
   default, because they shift with unlisted pre-gap content.
2. Disc order: supplied TOCs are assigned to distinct medium positions.
3. Data discs: mediums that carry only data tracks have no audio TOC and are
   exempt from claiming; mixed mediums still need an audio match.
4. Pre-gap: content hidden before the first track is compared with a declared
   pre-gap track when one exists.

A candidate that only accounts for one disc of a multi-disc box is reported
as ``partial`` and is not treated as confirmed.

The public entry point is :func:`claim_whole_box`.
"""

import itertools

from musicbrainzngs import compat

__all__ = [
    "PREGAP_SECTORS",
    "SECTORS_PER_SECOND",
    "normalize_toc",
    "toc_string",
    "toc_geometry",
    "tocs_match",
    "medium_toc",
    "claim_whole_box",
    "distinguishing_disc_numbers",
]

#: Sectors reserved for the lead-in before track 1 on an audio CD.
PREGAP_SECTORS = 150

#: Audio CD sectors per second.
SECTORS_PER_SECOND = 75

#: Tolerance, in sectors, used when offsets are derived from track lengths.
LENGTH_TOLERANCE = 2


def _as_int(value):
    """Return *value* as an int, or ``None`` when it is not numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick(mapping, *names):
    """Return the first present key in *names* from *mapping*."""
    if not mapping:
        return None
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _ms_to_sectors(length):
    """Convert a MusicBrainz length in milliseconds to CD sectors."""
    length = _as_int(length)
    if length is None:
        return None
    return int(round(length * SECTORS_PER_SECOND / 1000.0))


def normalize_toc(toc, sectors=None, first_track=1):
    """Normalise a TOC to a dictionary with absolute and relative offsets.

    *toc* may be:

    * a mapping with an ``offset-list`` (or ``offsets``) key and a ``sectors``
      (or ``leadout``) key, such as a ``disc`` element returned by
      :func:`musicbrainzngs.get_releases_by_discid`;
    * a string in :attr:`discid.Disc.toc_string` format,
      ``"first last leadout offset1 offset2 ..."``;
    * a sequence of absolute offsets, in which case *sectors* must be given.

    ``first_track`` is used for the string/sequence forms.  The returned
    dictionary contains:

    ``offset-list``
        Absolute track start sectors, ascending.
    ``sectors``
        Leadout sector (total sectors on the disc).
    ``track-count``
        Number of tracks in the TOC.
    ``first-track``
        Number of the first track (normally 1).
    ``first-offset``
        Absolute offset of the first track.
    ``relative-offset-list``
        Offsets measured from the first track, always starting with ``0``.
    ``program-sectors``
        Programme length, ``leadout - first offset``.
    ``pregap-sectors``
        Content before the first track beyond the standard lead-in
        (``first offset - 150``, clamped at zero).
    ``hidden-before-first``
        ``True`` when ``pregap-sectors`` is non-zero.
    """
    track_number = _as_int(first_track) or 1

    if isinstance(toc, compat.basestring):
        parts = toc.split()
        if len(parts) < 4:
            raise ValueError("TOC string needs first, last, leadout and offsets")
        first = _as_int(parts[0])
        last = _as_int(parts[1])
        leadout = _as_int(parts[2])
        offsets = [_as_int(part) for part in parts[3:]]
        if first is None or last is None or leadout is None:
            raise ValueError("TOC string has a non-numeric field")
        if len(offsets) != last - first + 1:
            raise ValueError("TOC string offset count does not match last track")
        return _normalise(offsets, leadout, first)

    if isinstance(toc, dict):
        offsets = _pick(toc, "offset-list", "offsets", "track-offset-list")
        if offsets is None:
            raise ValueError("TOC mapping has no offset-list")
        leadout = _pick(toc, "sectors", "leadout")
        if leadout is None and sectors is not None:
            leadout = sectors
        if leadout is None:
            raise ValueError("TOC mapping has no sectors/leadout")
        number = _as_int(_pick(toc, "first-track", "first-track-num"))
        return _normalise(list(offsets), leadout,
                          number if number is not None else track_number)

    if sectors is None:
        raise ValueError("a leadout sector is required with an offset list")
    return _normalise(list(toc), sectors, track_number)


def _normalise(offsets, leadout, first_track):
    clean = []
    for offset in offsets:
        value = _as_int(offset)
        if value is None:
            raise ValueError("TOC has a non-numeric offset")
        clean.append(value)
    if not clean:
        raise ValueError("TOC has no tracks")
    if any(b <= a for a, b in zip(clean, clean[1:])):
        raise ValueError("TOC offsets are not strictly ascending")
    leadout = _as_int(leadout)
    if leadout is None or leadout <= clean[-1]:
        raise ValueError("TOC leadout is not after the last track")

    first_offset = clean[0]
    return {
        "offset-list": clean,
        "sectors": leadout,
        "track-count": len(clean),
        "first-track": first_track,
        "first-offset": first_offset,
        "relative-offset-list": [offset - first_offset for offset in clean],
        "program-sectors": leadout - first_offset,
        "pregap-sectors": max(0, first_offset - PREGAP_SECTORS),
        "hidden-before-first": first_offset > PREGAP_SECTORS,
    }


def toc_string(toc):
    """Return *toc* in ``discid`` ``toc_string`` form.

    This is the form accepted by the ``toc`` argument of
    :func:`musicbrainzngs.get_releases_by_discid`.
    """
    normal = normalize_toc(toc)
    first = normal["first-track"]
    last = first + normal["track-count"] - 1
    fields = [str(first), str(last), str(normal["sectors"])]
    fields.extend(str(offset) for offset in normal["offset-list"])
    return " ".join(fields)


def toc_geometry(toc):
    """Return the comparison fingerprint of *toc*.

    The fingerprint is ``(relative offsets, programme length, track count)``.
    Absolute offsets and the leadout are excluded so that the same audio
    programme ripped with different pre-gap content still matches.
    """
    normal = normalize_toc(toc)
    return (tuple(normal["relative-offset-list"]),
            normal["program-sectors"],
            normal["track-count"])


def tocs_match(first, second, tolerance=0, absolute=False,
               compare_pregap=False, pregap_tolerance=SECTORS_PER_SECOND * 2):
    """Return ``True`` when two TOCs describe the same audio programme.

    Relative track offsets and the programme length must agree within
    *tolerance* sectors, and the track counts must be equal.  When
    *absolute* is true the absolute offsets and leadout must match too.

    When *compare_pregap* is true, the content hidden before the first track
    (``pregap-sectors``) must also agree within *pregap_tolerance*.
    """
    try:
        left = normalize_toc(first)
        right = normalize_toc(second)
    except ValueError:
        return False

    if left["track-count"] != right["track-count"]:
        return False
    for a, b in zip(left["relative-offset-list"],
                    right["relative-offset-list"]):
        if abs(a - b) > tolerance:
            return False
    if abs(left["program-sectors"] - right["program-sectors"]) > tolerance:
        return False
    if absolute:
        for a, b in zip(left["offset-list"], right["offset-list"]):
            if abs(a - b) > tolerance:
                return False
        if abs(left["sectors"] - right["sectors"]) > tolerance:
            return False
    if compare_pregap:
        if abs(left["pregap-sectors"] - right["pregap-sectors"]) > pregap_tolerance:
            return False
    return True


def medium_toc(medium, first_offset=PREGAP_SECTORS):
    """Build an audio TOC from the track lengths of a medium.

    This is a fallback for medium data fetched without disc IDs, e.g. with
    ``includes=["recordings"]``.  Each audio track contributes its
    ``track_or_recording_length`` (milliseconds); a declared pre-gap is
    included before the first track.  Data tracks are not part of the audio
    programme and are ignored.  Returns ``None`` when the medium has no audio
    tracks or any audio track lacks a usable length.
    """
    if not medium:
        return None

    positions = []
    offset = _as_int(first_offset)
    if offset is None:
        offset = PREGAP_SECTORS

    pregap = medium.get("pregap")
    if pregap:
        pregap_sectors = _ms_to_sectors(_pick(pregap, "length"))
        if pregap_sectors is None:
            pregap_sectors = _ms_to_sectors(
                _pick(pregap, "track_or_recording_length"))
        if pregap_sectors:
            offset += pregap_sectors

    for track in medium.get("track-list", []):
        length = _pick(track, "track_or_recording_length", "length")
        if length is None:
            recording = track.get("recording") or {}
            length = _pick(recording, "length")
        sectors = _ms_to_sectors(length)
        if sectors is None:
            return None
        positions.append(offset)
        offset += sectors

    if not positions:
        return None
    return _normalise(positions, offset, 1)


def _medium_role(medium):
    """Classify a medium as ``audio``, ``data``, ``mixed`` or ``unknown``."""
    audio = _as_int(_pick(medium, "track-count"))
    if audio is None:
        audio = len(medium.get("track-list", []))
    data = _as_int(_pick(medium, "data-track-count"))
    if data is None:
        data = len(medium.get("data-track-list", []))

    if audio and data:
        return "mixed"
    if audio:
        return "audio"
    if data:
        return "data"
    return "unknown"


def _medium_pregap_sectors(medium):
    """Return the declared pre-gap length of *medium* in sectors."""
    pregap = (medium or {}).get("pregap")
    if not pregap:
        return 0
    length = _pick(pregap, "track_or_recording_length", "length")
    sectors = _ms_to_sectors(length)
    return sectors or 0


def _medium_discs(medium):
    """Return the disc entries declared for *medium*."""
    return [disc for disc in (medium or {}).get("disc-list", []) if disc]


def _medium_geometry(medium, derive_from_tracks):
    """Return candidate geometries for *medium*.

    Each candidate is a dictionary with ``disc-id`` (``None`` for a geometry
    derived from track lengths), ``toc`` and ``source``.
    """
    candidates = []
    for disc in _medium_discs(medium):
        try:
            normal = normalize_toc(disc)
        except ValueError:
            continue
        candidates.append({"disc-id": disc.get("id"),
                           "toc": normal,
                           "source": "discid"})
    if not candidates and derive_from_tracks:
        derived = medium_toc(medium)
        if derived is not None:
            candidates.append({"disc-id": None,
                               "toc": derived,
                               "source": "track-lengths"})
    return candidates


def _pregap_evaluation(toc, candidate_toc, medium, pregap_tolerance):
    """Compare hidden pre-track content with what the medium declares.

    The reference is the matched candidate's own pre-gap geometry (derived
    from its first offset), or an explicit ``pregap`` track on the medium
    when one exists.

    Returns ``(state, detail)`` where *state* is:

    ``ok``
        Both sides agree, or neither has hidden content.
    ``unlisted``
        The local TOC shows hidden content the medium does not document.
    ``not-shown``
        The medium documents a pre-gap the local TOC does not show.
    ``differs``
        Both sides document hidden content, but the lengths differ.
    """
    hidden = toc["pregap-sectors"]
    declared = candidate_toc["pregap-sectors"]
    explicit = _medium_pregap_sectors(medium)
    if explicit:
        declared = explicit

    if not hidden and not declared:
        return "ok", 0
    if hidden and declared:
        if abs(hidden - declared) <= pregap_tolerance:
            return "ok", declared
        return "differs", declared
    if hidden:
        return "unlisted", 0
    return "not-shown", declared


def _match_toc_to_medium(toc, medium, tolerance, derive_from_tracks,
                         pregap_tolerance, require_pregap):
    """Return match details when *toc* can describe *medium*, else ``None``."""
    role = _medium_role(medium)
    if role == "data":
        return None

    for candidate in _medium_geometry(medium, derive_from_tracks):
        allowed = tolerance
        if candidate["source"] == "track-lengths":
            # Rounding each track length to whole sectors accumulates error,
            # so allow a little per track in addition to the caller's limit.
            allowed = tolerance + LENGTH_TOLERANCE * candidate["toc"]["track-count"]
        if not tocs_match(toc, candidate["toc"], tolerance=allowed):
            continue
        state, declared = _pregap_evaluation(toc, candidate["toc"], medium,
                                             pregap_tolerance)
        if state != "ok" and require_pregap:
            continue
        return {"medium-role": role,
                "disc-id": candidate["disc-id"],
                "source": candidate["source"],
                "pregap": state,
                "declared-pregap-sectors": declared,
                "hidden-before-first": toc["pregap-sectors"]}
    return None


def _release_media(release):
    """Return the mediums of *release* ordered by position."""
    media = list(release.get("medium-list", []))
    for index, medium in enumerate(media):
        if _as_int(medium.get("position")) is None:
            medium = dict(medium)
            medium["position"] = index + 1
            media[index] = medium
    media.sort(key=lambda medium: _as_int(medium.get("position")) or 0)
    return media


def _disc_number(media, index):
    """Return the disc number (declared position) of ``media[index]``."""
    position = _as_int(media[index].get("position"))
    return position if position is not None else index + 1


def _assign_tocs(tocs, media, tolerance, derive_from_tracks,
                 pregap_tolerance, require_pregap):
    """Assign every TOC in *tocs* to a distinct medium position.

    Returns ``(assignment, matches)`` where *assignment* maps a TOC index to
    a medium position, or ``None`` when the TOCs cannot all be placed.
    *matches* maps ``(toc index, medium position)`` to match details.
    """
    matches = {}
    options = []
    for toc_index, toc in enumerate(tocs):
        available = []
        for medium_index, medium in enumerate(media):
            detail = _match_toc_to_medium(toc, medium, tolerance,
                                          derive_from_tracks,
                                          pregap_tolerance, require_pregap)
            if detail is not None:
                available.append(medium_index)
                matches[(toc_index, medium_index)] = detail
        options.append(available)

    if any(not available for available in options):
        return None, matches

    order = sorted(range(len(tocs)), key=lambda index: len(options[index]))
    assignment = {}

    def place(depth, used):
        if depth == len(order):
            return True
        toc_index = order[depth]
        for medium_index in options[toc_index]:
            if medium_index in used:
                continue
            assignment[toc_index] = medium_index
            used.add(medium_index)
            if place(depth + 1, used):
                return True
            used.remove(medium_index)
            del assignment[toc_index]
        return False

    if not place(0, set()):
        return None, matches
    return assignment, matches


def _medium_signature(medium):
    """Return a comparable identity for the disc(s) of *medium*."""
    role = _medium_role(medium)
    if role == "data":
        return ("data",)
    discs = _medium_discs(medium)
    if discs:
        return ("discs", tuple(sorted(disc.get("id") or "" for disc in discs)))
    return ("no-disc-id", role)


def _distinguishing(claims):
    """Find the smallest set of disc numbers separating the claims.

    *claims* maps release IDs to claims, each holding the release's ordered
    raw ``medium-list``.  Data-only discs are ignored: they carry no audio
    TOC and therefore cannot discriminate audio claims by Disc ID.
    """
    release_ids = sorted(claims)
    if len(release_ids) < 2:
        return {"disc-numbers": [],
                "by-release": dict((rid, {}) for rid in release_ids),
                "discriminating-values": {},
                "indistinguishable": [],
                "note": "fewer than two viable releases"}

    signatures = {}
    disc_numbers = set()
    for rid in release_ids:
        media = claims[rid]["medium-list"]
        signatures[rid] = {}
        for index, medium in enumerate(media):
            number = _disc_number(media, index)
            signatures[rid][number] = _medium_signature(medium)
            disc_numbers.add(number)

    candidates = []
    for number in sorted(disc_numbers):
        roles = set()
        for rid in release_ids:
            signature = signatures[rid].get(number)
            if signature is not None:
                roles.add("data" if signature[0] == "data" else "audio")
        if roles == set(["data"]):
            continue
        candidates.append(number)

    def distinguishes(subset):
        seen = set()
        for rid in release_ids:
            key = tuple(signatures[rid].get(number) for number in subset)
            if key in seen:
                return False
            seen.add(key)
        return True

    improvable = [number for number in candidates
                  if len(set(signatures[rid].get(number)
                             for rid in release_ids)) > 1]

    selected = None
    for size in range(0, len(improvable) + 1):
        for subset in itertools.combinations(improvable, size):
            if distinguishes(subset):
                selected = list(subset)
                break
        if selected is not None:
            break

    separated = selected is not None
    if not separated:
        selected = improvable

    groups = {}
    for rid in release_ids:
        key = tuple(signatures[rid].get(number) for number in candidates)
        groups.setdefault(key, []).append(rid)
    indistinguishable = [group for group in groups.values() if len(group) > 1]

    detail = {}
    for number in selected:
        values = {}
        for rid in release_ids:
            signature = signatures[rid].get(number)
            if signature is None:
                continue
            if signature[0] == "discs":
                key = ", ".join(signature[1])
            elif signature[0] == "data":
                key = "data disc"
            else:
                key = "no disc id"
            values.setdefault(key, []).append(rid)
        detail[number] = values

    return {"disc-numbers": selected,
            "by-release": dict((rid, dict((number,
                _describe_signature(signatures[rid].get(number)))
                for number in candidates))
                for rid in release_ids),
            "discriminating-values": detail,
            "separated": separated,
            "indistinguishable": indistinguishable,
            "note": ("no disc number separates these releases; only release "
                     "metadata (country, date, barcode) differs"
                     if not separated else "")}


def _describe_signature(signature):
    """Render a medium signature for a caller."""
    if signature is None:
        return {"role": "absent", "disc-ids": []}
    if signature[0] == "discs":
        return {"role": "audio", "disc-ids": list(signature[1])}
    if signature[0] == "data":
        return {"role": "data", "disc-ids": []}
    return {"role": signature[1], "disc-ids": []}


def claim_whole_box(toc=None, discid_result=None, releases=None, same_box=None,
                    tolerance=0, derive_from_tracks=True,
                    pregap_tolerance=SECTORS_PER_SECOND * 2,
                    require_pregap=False):
    """Claim a whole box for the recovered TOC(s).

    Parameters
    ----------
    toc
        The local TOC: a mapping with ``offset-list``/``sectors``, a
        ``toc_string``-formatted string, or a bare offset list (then
        *discid_result* supplies the leadout).  When ``None`` the ``disc``
        element of *discid_result* is used.
    discid_result
        The dictionary returned by
        :func:`musicbrainzngs.get_releases_by_discid`.  Its ``disc`` element
        supplies the local TOC (when *toc* is ``None``) and its
        ``release-list`` supplies the candidates.
    releases
        An explicit candidate release list, overriding the one found in
        *discid_result*.
    same_box
        A TOC or list of TOCs that are known to come from the same box as
        *toc*.  Every one of them must be placeable for a release to stay
        viable.
    tolerance
        Allowed difference, in sectors, for relative offsets and programme
        length.
    derive_from_tracks
        When a medium has no disc IDs, derive a TOC from track lengths.
    pregap_tolerance
        Allowed difference, in sectors, for content hidden before track 1.
    require_pregap
        Reject a match when hidden pre-track content and a declared pre-gap
        disagree.  When false such a mismatch is only reported.

    Returns
    -------
    dict
        ``discs`` (the normalised supplied TOCs), ``release-claims`` in the
        order of the candidates, ``still-valid``/``confirmed``/``partial``/
        ``excluded`` release IDs, and ``discriminating``, the smallest set of
        medium numbers whose Disc IDs separate the surviving releases.
    """
    if discid_result is None and releases is None:
        raise ValueError("discid_result or releases is required")

    disc = None
    if discid_result is not None:
        disc = discid_result.get("disc")
        if releases is None:
            releases = disc.get("release-list", []) if disc else []

    supplied = []
    if toc is not None:
        supplied.append(toc)
    elif disc is not None:
        supplied.append(disc)
    else:
        raise ValueError("no local TOC and no disc in the query result")

    if same_box is not None:
        if isinstance(same_box, (dict, compat.basestring)):
            same_box = [same_box]
        supplied.extend(same_box)

    tocs = [normalize_toc(item) for item in supplied]

    claims = []
    by_id = {}
    for release in releases or []:
        media = _release_media(release)
        assignment, matches = _assign_tocs(tocs, media, tolerance,
                                           derive_from_tracks,
                                           pregap_tolerance, require_pregap)
        roles = [_medium_role(medium) for medium in media]
        audio_positions = [index for index, role in enumerate(roles)
                           if role != "data"]
        data_positions = [index for index, role in enumerate(roles)
                          if role == "data"]

        placed = set(assignment.values()) if assignment else set()
        missing = [index for index in audio_positions if index not in placed]

        notes = []
        if assignment is None:
            status = "excluded"
            notes.append("no consistent placement for the supplied discs")
        elif missing:
            status = "partial"
            notes.append("%d audio disc(s) of the box are not accounted for "
                         "(mediums %s)"
                         % (len(missing),
                            ", ".join(str(_disc_number(media, index))
                                      for index in missing)))
        else:
            status = "confirmed"

        if assignment is not None and len(placed) + len(data_positions) < len(media):
            notes.append("box has more mediums than supplied discs")

        for (toc_index, medium_index), detail in sorted(matches.items()):
            if (assignment or {}).get(toc_index) != medium_index:
                continue
            disc_number = _disc_number(media, medium_index)
            if detail["pregap"] == "unlisted":
                notes.append("disc %d shows hidden content before track 1 "
                             "that the medium does not document" % disc_number)
            elif detail["pregap"] == "not-shown":
                notes.append("disc %d documents a pre-gap the local TOC does "
                             "not show" % disc_number)
            elif detail["pregap"] == "differs":
                notes.append("disc %d pre-gap length disagrees with the local "
                             "TOC" % disc_number)
            if detail["source"] == "track-lengths":
                notes.append("disc %d matched from track lengths, not a Disc "
                             "ID" % disc_number)

        media_summary = []
        for index, medium in enumerate(media):
            entry = {"position": _as_int(medium.get("position")) or index + 1,
                     "role": roles[index],
                     "disc-ids": [disc_entry.get("id")
                                  for disc_entry in _medium_discs(medium)],
                     "matched": index in placed,
                     "pregap-sectors": _medium_pregap_sectors(medium)}
            media_summary.append(entry)

        claim = {
            "release-id": release.get("id"),
            "title": release.get("title"),
            "country": release.get("country"),
            "date": release.get("date"),
            "barcode": release.get("barcode"),
            "status": status,
            "assignment": dict((str(toc_index + 1),
                                _disc_number(media, medium_index))
                               for toc_index, medium_index in
                               (assignment or {}).items()),
            "media": media_summary,
            "medium-list": media,
            "missing-disc-numbers": [_disc_number(media, index)
                                     for index in missing],
            "data-disc-numbers": [_disc_number(media, index)
                                  for index in data_positions],
            "notes": notes,
        }
        claims.append(claim)
        by_id[claim["release-id"]] = claim

    still_valid = [claim["release-id"] for claim in claims
                   if claim["status"] in ("confirmed", "partial")]
    confirmed = [claim["release-id"] for claim in claims
                 if claim["status"] == "confirmed"]
    partial = [claim["release-id"] for claim in claims
               if claim["status"] == "partial"]
    excluded = [claim["release-id"] for claim in claims
                if claim["status"] == "excluded"]

    valid_claims = dict((rid, by_id[rid]) for rid in still_valid)
    discriminating = _distinguishing(valid_claims)

    return {
        "discs": tocs,
        "release-claims": claims,
        "still-valid": still_valid,
        "confirmed": confirmed,
        "partial": partial,
        "excluded": excluded,
        "discriminating": discriminating,
    }


def distinguishing_disc_numbers(claim):
    """Return just the minimal medium numbers from a claim result."""
    return list(claim.get("discriminating", {}).get("disc-numbers", []))
