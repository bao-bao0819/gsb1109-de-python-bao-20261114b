#!/usr/bin/env python3
"""Generate per-work coverage details from fetched MusicBrainz-style data.

Inputs (already fetched, passed as JSON):
  * work parts        -- parent work -> list of direct sub-work (movement) ids
  * segment windows   -- recording->work relations, optionally with begin/end
                         offsets (ms) into the recording
  * recording lengths -- duration of each recording in ms

Problem this solves:
  A medley recording is linked both to the whole work AND to several of its
  movements.  Naively summing the durations of every recording/work relation
  counts the movements twice.  Instead, the total for the whole work is
  computed from the union of the recording intervals actually covered.

Rules implemented:
  * A relation with explicit begin/end is a *certain* segment.
  * A relation without offsets only means the recording *may* cover the work;
    it contributes a *possible* segment spanning the whole recording.  Its
    duration is NOT split evenly among the remaining movements.
  * Totals per work are interval unions (per recording), so overlapping
    segments are never double counted.
  * When the same segment genuinely belongs to two sub-works, both keep their
    reference: every segment stays listed under its own work, and the parent
    work's detail lists each contributing relation separately.

Usage:
    python work_coverage.py data.json          # report for fetched data
    python work_coverage.py --demo             # run the built-in medley demo
"""

import json
import sys


# ---------------------------------------------------------------------------
# interval helpers
# ---------------------------------------------------------------------------

def union_length(intervals):
    """Total length of the union of [begin, end) intervals (ms)."""
    total = 0
    cur_begin = cur_end = None
    for begin, end in sorted(intervals):
        if cur_begin is None:
            cur_begin, cur_end = begin, end
        elif begin <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_begin
            cur_begin, cur_end = begin, end
    if cur_begin is not None:
        total += cur_end - cur_begin
    return total


# ---------------------------------------------------------------------------
# work tree
# ---------------------------------------------------------------------------

def descendants(work_id, parts_of):
    """All transitive sub-work ids of ``work_id`` (not including itself)."""
    result = set()
    stack = list(parts_of.get(work_id, []))
    while stack:
        child = stack.pop()
        if child not in result:
            result.add(child)
            stack.extend(parts_of.get(child, []))
    return result


# ---------------------------------------------------------------------------
# coverage computation
# ---------------------------------------------------------------------------

def build_segments(work_id, works, recordings, parts_of):
    """Collect every segment contributing to ``work_id``.

    A segment comes from a recording->work relation whose target is the work
    itself or one of its (transitive) parts.  Each segment keeps a reference
    to the work it was related through (``via_work``), so a stretch of audio
    genuinely shared by two sub-works appears once per relation.
    """
    related = {work_id} | descendants(work_id, parts_of)
    segments = []
    for rec in recordings:
        length = rec.get("length_ms")
        for rel in rec.get("relations", []):
            target = rel.get("work")
            if target not in related:
                continue
            begin = rel.get("begin_ms")
            end = rel.get("end_ms")
            certain = begin is not None and end is not None
            if not certain:
                # Unmarked relation: the whole recording is only a *possible*
                # match.  Fall back to the recording bounds without ever
                # splitting the duration across sibling movements.
                begin = 0 if begin is None else begin
                end = length if end is None else end
            segments.append({
                "recording": rec["id"],
                "recording_title": rec.get("title"),
                "via_work": target,
                "via_work_title": works.get(target, {}).get("title"),
                "begin_ms": begin,
                "end_ms": end,
                "status": "certain" if certain else "possible",
            })
    return segments


def coverage_for_work(work_id, works, recordings, parts_of):
    segments = build_segments(work_id, works, recordings, parts_of)

    certain_by_rec = {}
    possible_by_rec = {}
    unbounded = []
    for seg in segments:
        rec_id = seg["recording"]
        if seg["begin_ms"] is None or seg["end_ms"] is None:
            # recording length unknown and relation unmarked -> unquantifiable
            unbounded.append(seg)
            continue
        interval = (seg["begin_ms"], seg["end_ms"])
        possible_by_rec.setdefault(rec_id, []).append(interval)
        if seg["status"] == "certain":
            certain_by_rec.setdefault(rec_id, []).append(interval)

    certain_ms = sum(union_length(iv) for iv in certain_by_rec.values())
    potential_ms = sum(union_length(iv) for iv in possible_by_rec.values())

    return {
        "work": work_id,
        "title": works.get(work_id, {}).get("title"),
        "certain_ms": certain_ms,
        "potential_ms": potential_ms,
        "possible_only_ms": potential_ms - certain_ms,
        "unquantifiable_segments": len(unbounded),
        "segments": segments,
    }


def build_coverage(data):
    """Build the full per-work coverage report from fetched ``data``."""
    works = data.get("works", {})
    recordings = data.get("recordings", [])
    parts_of = {wid: list(w.get("parts", [])) for wid, w in works.items()}

    report = {}
    for work_id in works:
        report[work_id] = coverage_for_work(work_id, works, recordings,
                                            parts_of)
    return report


# ---------------------------------------------------------------------------
# demo data: a medley linked to a whole work and to two of its movements
# ---------------------------------------------------------------------------

DEMO_DATA = {
    "works": {
        "W": {"title": "Suite (whole work)", "parts": ["W1", "W2", "W3"]},
        "W1": {"title": "Movement I", "parts": []},
        "W2": {"title": "Movement II", "parts": []},
        "W3": {"title": "Movement III", "parts": []},
    },
    "recordings": [
        {
            "id": "R-medley",
            "title": "Medley (whole suite + movements I & II marked)",
            "length_ms": 300000,
            "relations": [
                {"work": "W"},                                   # unmarked
                {"work": "W1", "begin_ms": 0, "end_ms": 120000},
                {"work": "W2", "begin_ms": 120000, "end_ms": 250000},
            ],
        },
        {
            "id": "R-partial",
            "title": "Only movement III, offsets unknown",
            "length_ms": 90000,
            "relations": [
                {"work": "W3"},                                  # unmarked
            ],
        },
        {
            "id": "R-shared",
            "title": "Transition genuinely in movements II and III",
            "length_ms": 60000,
            "relations": [
                {"work": "W2", "begin_ms": 0, "end_ms": 60000},
                {"work": "W3", "begin_ms": 0, "end_ms": 60000},
            ],
        },
    ],
}


def format_ms(ms):
    if ms is None:
        return "unknown"
    return "%d:%02d" % (ms // 60000, (ms // 1000) % 60)


def print_report(report):
    for work_id, entry in report.items():
        print("=" * 72)
        print("%s  (%s)" % (entry["title"], work_id))
        print("  certain coverage   : %s" % format_ms(entry["certain_ms"]))
        print("  potential coverage : %s (+%s possible-only)"
              % (format_ms(entry["potential_ms"]),
                 format_ms(entry["possible_only_ms"])))
        if entry["unquantifiable_segments"]:
            print("  unquantifiable segments (no offsets, no length): %d"
                  % entry["unquantifiable_segments"])
        for seg in entry["segments"]:
            print("    [%s] %s via %s (%s)  %s-%s"
                  % (seg["status"], seg["recording"], seg["via_work"],
                     seg["via_work_title"],
                     format_ms(seg["begin_ms"]), format_ms(seg["end_ms"])))


def main(argv):
    if "--demo" in argv or len(argv) < 2:
        data = DEMO_DATA
    else:
        with open(argv[1]) as fh:
            data = json.load(fh)
    report = build_coverage(data)
    print_report(report)


if __name__ == "__main__":
    main(sys.argv)
