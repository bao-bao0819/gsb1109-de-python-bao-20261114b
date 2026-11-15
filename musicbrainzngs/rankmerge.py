# This file is part of the musicbrainzngs library
# Merging of partial search-result rankings from several sources.

"""Merge partial search-result rankings from several sources ("halls").

Each source submits only the first few pages of its MusicBrainz search
result, so its ranking over the shared set of candidate ids is partial:

* pages are strictly ordered (page 1 outranks page 2, ...),
* inside a non-final page the listed order is strict,
* the final submitted page may be a tie group (its recordings are tied,
  so their relative order is unknown),
* every candidate that was never listed ranks strictly below all listed
  ones, but the unlisted candidates are not ordered among themselves;
  not appearing in the submitted pages does *not* mean ranking last.

A *legal completion* of a source is any total ordering of all ``total``
candidates that respects these constraints (positions are a permutation
of ``1..total`` -- two candidates can never share a slot, so it is not
valid to place every competitor on the same last slot at once).

The merged score of a recording is the weighted sum of its positions
over the sources.  The merged rank is the competition ranking by that
score: ``1 + #{other recordings with a strictly smaller score}``.

For every recording in the catalog this module computes the exact range
of merged ranks it can still achieve over all legal completions, and
flags it as "definite" when it lands in the top ``top`` ranks in every
legal completion.

Note: deciding how many competitors can simultaneously outscore a
recording is a combinatorial problem (akin to numerical 3-dimensional
matching), so the exact computation is exponential in the worst case.
It is fast for the small catalogs and few-page rankings this is meant
for, and interchangeable candidates are folded together to keep the
search small.
"""

__all__ = ["PartialRanking", "RankRange", "merged_rank_ranges"]

from bisect import bisect_left, insort
from collections import namedtuple


class PartialRanking(object):
    """One source's partial ranking over a shared set of candidate ids.

    ``pages`` is a sequence of sequences of recording ids, best first.
    If ``last_page_tied`` is true, the recordings on the final page are
    treated as tied with each other; on every other page (and on the
    final page otherwise) the listed order is strict.
    """

    def __init__(self, pages, last_page_tied=False):
        self.pages = [list(page) for page in pages]
        self.last_page_tied = bool(last_page_tied)

    @classmethod
    def from_search_result(cls, result, entity="recording", page_size=25,
                           last_page_tied="auto"):
        """Build a ranking from a parsed musicbrainzngs search result.

        ``result`` is the dict returned by e.g.
        :func:`musicbrainzngs.search_recordings`; the ids are taken from
        its ``"<entity>-list"`` and chunked into pages of ``page_size``.
        With ``last_page_tied="auto"`` the final page is treated as tied
        when all of its entries carry the same ``ext:score``.
        """
        items = result.get("%s-list" % entity, [])
        ids = [item["id"] for item in items]
        pages = [ids[start:start + page_size]
                 for start in range(0, len(ids), page_size)]
        if last_page_tied == "auto":
            tied = False
            if pages and len(pages[-1]) > 1:
                scores = set(item.get("ext:score")
                             for item in items[-len(pages[-1]):])
                tied = len(scores) == 1 and None not in scores
            last_page_tied = tied
        return cls(pages, last_page_tied=bool(last_page_tied))


class RankRange(namedtuple("RankRange", ["best", "worst", "definite"])):
    """Achievable merged-rank range of one recording.

    ``best``/``worst`` are the best and worst merged ranks the recording
    reaches in at least one legal completion; ``definite`` is true when
    even the worst completion keeps the recording inside the top ranks.
    """
    __slots__ = ()


def _frontier(free_lists, weights, threshold, sense):
    """Pareto frontier of satisfying slot vectors for one candidate.

    ``free_lists[h]`` is the sorted list of slots still free for the
    candidate in hall ``h``.  For ``sense == "lt"`` this returns the
    componentwise-maximal vectors with ``dot(weights, v) < threshold``;
    for ``sense == "ge"`` the componentwise-minimal vectors with
    ``dot(weights, v) >= threshold``.  Only frontier vectors can be part
    of an optimal assignment: any dominated vector can be exchanged for
    the dominating one (displaced candidates receive the freed, weakly
    better slots) without reducing the number of satisfiers.

    The frontier is found by enumerating the first ``n - 1`` coordinates
    and taking the extremal feasible last coordinate; a point is kept
    exactly when no single coordinate can be moved one step outward
    (inward for ``"ge"``) without losing the last coordinate -- by
    monotonicity that local test matches global domination.
    """
    n = len(free_lists)
    weighted = [[w * v for v in slots]
                for slots, w in zip(free_lists, weights)]
    last_values = free_lists[-1]
    last_weighted = weighted[-1]
    points = []
    if sense == "lt":
        # suffix minima prune prefixes that cannot stay below threshold
        suffix_min = [0.0] * (n + 1)
        for h in range(n - 1, -1, -1):
            suffix_min[h] = suffix_min[h + 1] + weighted[h][0]

        def rec(h, partial, prefix, idxs):
            if h == n - 1:
                i = bisect_left(last_weighted, threshold - partial) - 1
                if i < 0:
                    return
                # dominated if advancing any coordinate keeps a last
                # coordinate at least as large
                for k in range(n - 1):
                    nxt = idxs[k] + 1
                    if nxt == len(free_lists[k]):
                        continue
                    moved = partial - weighted[k][idxs[k]] \
                        + weighted[k][nxt]
                    if bisect_left(last_weighted,
                                   threshold - moved) - 1 >= i:
                        return
                points.append(prefix + (last_values[i],))
                return
            for idx, (v, wv) in enumerate(zip(free_lists[h],
                                              weighted[h])):
                if partial + wv + suffix_min[h + 1] >= threshold:
                    break  # larger slots here only make it worse
                rec(h + 1, partial + wv, prefix + (v,), idxs + (idx,))
        rec(0, 0.0, (), ())
    else:
        # suffix maxima prune prefixes that can never reach threshold
        suffix_max = [0.0] * (n + 1)
        for h in range(n - 1, -1, -1):
            suffix_max[h] = suffix_max[h + 1] + weighted[h][-1]

        def rec(h, partial, prefix, idxs):
            if h == n - 1:
                i = bisect_left(last_weighted, threshold - partial)
                if i == len(last_weighted):
                    return
                # dominated if retreating any coordinate keeps a last
                # coordinate at least as small
                for k in range(n - 1):
                    prv = idxs[k] - 1
                    if prv < 0:
                        continue
                    moved = partial - weighted[k][idxs[k]] \
                        + weighted[k][prv]
                    if bisect_left(last_weighted,
                                   threshold - moved) <= i:
                        return
                points.append(prefix + (last_values[i],))
                return
            for idx, (v, wv) in enumerate(zip(free_lists[h],
                                              weighted[h])):
                if partial + wv + suffix_max[h + 1] >= threshold:
                    rec(h + 1, partial + wv, prefix + (v,), idxs + (idx,))
        rec(0, 0.0, (), ())
    return points


def _max_satisfiers(free_slots, cand_groups, weights, threshold, sense):
    """Exact maximum number of candidates that can satisfy the goal.

    ``free_slots[h][g]`` is the sorted list of slots still free in group
    ``g`` of hall ``h``; ``cand_groups[y][h]`` is the group of candidate
    ``y`` in hall ``h``.  A candidate *satisfies* when its weighted
    position sum is ``< threshold`` (``sense == "lt"``) resp.
    ``>= threshold`` (``sense == "ge"``).  Returns the largest number of
    candidates that can satisfy simultaneously in one legal completion.

    Solved by branch and bound over candidate *types* (candidates with
    identical feasible slots are interchangeable).  Branching on a type
    considers only the Pareto frontier of its satisfying vectors plus
    the single dominant non-satisfying corner vector, and states are
    pruned by an individual-feasibility bound and a slot-sum bound.
    """
    n_halls = len(weights)
    n_cand = len(cand_groups)
    if n_cand == 0:
        return 0

    # group interchangeable candidates into types
    type_of = {}
    types = []
    remaining = []
    for groups in cand_groups:
        key = tuple(groups)
        if key not in type_of:
            type_of[key] = len(types)
            types.append(key)
            remaining.append(0)
        remaining[type_of[key]] += 1
    n_types = len(types)
    total_rem = [n_cand]

    # all remaining slots per hall, for the slot-sum bound
    hall_slots = []
    for h in range(n_halls):
        slots = []
        for group in free_slots[h]:
            slots.extend(group)
        slots.sort()
        hall_slots.append(slots)

    def extremes(t):
        lo = 0.0
        hi = 0.0
        groups = types[t]
        for h in range(n_halls):
            slots = free_slots[h][groups[h]]
            lo += weights[h] * slots[0]
            hi += weights[h] * slots[-1]
        return lo, hi

    def can_sat(t):
        lo, hi = extremes(t)
        return lo < threshold if sense == "lt" else hi >= threshold

    def can_nonsat(t):
        lo, hi = extremes(t)
        return hi >= threshold if sense == "lt" else lo < threshold

    def branches(t):
        groups = types[t]
        free_lists = [free_slots[h][groups[h]] for h in range(n_halls)]
        lo, hi = extremes(t)
        sat = []
        if lo < threshold if sense == "lt" else hi >= threshold:
            sat = _frontier(free_lists, weights, threshold, sense)
            # try the most resource-saving satisfiers first
            sat.sort(key=lambda v: sum(w * x for w, x in zip(weights, v)),
                     reverse=(sense == "lt"))
        nonsat = []
        if hi >= threshold if sense == "lt" else lo < threshold:
            corner = tuple(free_lists[h][-1] if sense == "lt"
                           else free_lists[h][0]
                           for h in range(n_halls))
            nonsat.append(corner)
        return [(v, 1) for v in sat] + [(v, 0) for v in nonsat]

    def assign(t, vec):
        groups = types[t]
        for h in range(n_halls):
            slots = free_slots[h][groups[h]]
            slots.pop(bisect_left(slots, vec[h]))
            hall_slots[h].pop(bisect_left(hall_slots[h], vec[h]))

    def unassign(t, vec):
        groups = types[t]
        for h in range(n_halls):
            insort(free_slots[h][groups[h]], vec[h])
            insort(hall_slots[h], vec[h])

    def bound():
        """Upper bound on the number of further satisfiers."""
        potential = 0
        for t in range(n_types):
            if remaining[t] and can_sat(t):
                potential += remaining[t]
        if potential == 0:
            return 0
        # slot-sum bound: t satisfiers need a total score on the wrong
        # side of t * threshold, but the slots they can occupy limit it
        limit = min(potential, total_rem[0])
        reachable = 0
        if sense == "lt":
            for t in range(1, limit + 1):
                reachable += sum(weights[h] * hall_slots[h][t - 1]
                                 for h in range(n_halls))
                if reachable >= t * threshold:
                    return min(potential, t - 1)
        else:
            for t in range(1, limit + 1):
                reachable += sum(weights[h] * hall_slots[h][-t]
                                 for h in range(n_halls))
                if reachable < t * threshold:
                    return min(potential, t - 1)
        return potential

    # greedy lower bound: satisfy the easiest types first
    saved = [[list(slots) for slots in hall] for hall in free_slots]
    saved_hall = [list(slots) for slots in hall_slots]
    saved_remaining = list(remaining)
    count = 0
    if sense == "lt":
        order = sorted(range(n_types), key=lambda t: extremes(t)[0])
    else:
        order = sorted(range(n_types), key=lambda t: -extremes(t)[1])
    for t in order:
        while remaining[t]:
            vec, is_sat = branches(t)[0]
            count += is_sat
            assign(t, vec)
            remaining[t] -= 1
    best = [count]
    for h in range(n_halls):
        free_slots[h] = saved[h]
        hall_slots[h] = saved_hall[h]
    remaining = saved_remaining

    def rec(count):
        if total_rem[0] == 0:
            if count > best[0]:
                best[0] = count
            return
        add_bound = bound()
        if count + add_bound <= best[0]:
            return
        if add_bound == 0:
            return
        # branch on the most constrained remaining type
        forced = None
        flexible = None
        for t in range(n_types):
            if not remaining[t]:
                continue
            if not can_nonsat(t) or not can_sat(t):
                forced = t
                break
            if flexible is None:
                flexible = t
        t = forced if forced is not None else flexible
        remaining[t] -= 1
        total_rem[0] -= 1
        for vec, is_sat in branches(t):
            assign(t, vec)
            rec(count + is_sat)
            unassign(t, vec)
        remaining[t] += 1
        total_rem[0] += 1

    rec(0)
    return best[0]


def merged_rank_ranges(rankings, weights, total, catalog=None, top=5):
    """Compute achievable merged-rank ranges for a catalog of recordings.

    ``rankings`` is a list of :class:`PartialRanking` objects (one per
    source), ``weights`` the matching source weights, ``total`` the
    total number of candidates and ``catalog`` the recordings to report
    on (default: every id listed by at least one source, in order of
    first appearance).  If ``total`` exceeds the catalog size, the
    remaining candidates take part as anonymous unlisted competitors.

    Returns a dict mapping each catalog id to a :class:`RankRange`.
    A recording is ``definite`` exactly when its merged rank stays
    ``<= top`` in every legal completion.
    """
    rankings = list(rankings)
    weights = list(weights)
    if not rankings:
        raise ValueError("at least one ranking is required")
    if len(rankings) != len(weights):
        raise ValueError("need exactly one weight per ranking")
    if any(w < 0 for w in weights):
        raise ValueError("weights must not be negative")
    if total < 1:
        raise ValueError("total must be positive")

    if catalog is None:
        catalog = []
        seen = set()
        for ranking in rankings:
            for page in ranking.pages:
                for rid in page:
                    if rid not in seen:
                        seen.add(rid)
                        catalog.append(rid)
    else:
        catalog = list(catalog)
    if len(set(catalog)) != len(catalog):
        raise ValueError("catalog contains duplicate ids")
    catalog_set = set(catalog)
    for ranking in rankings:
        for page in ranking.pages:
            for rid in page:
                if rid not in catalog_set:
                    raise ValueError("listed id %r is not in the catalog"
                                     % (rid,))
    n_phantom = total - len(catalog)
    if n_phantom < 0:
        raise ValueError("total is smaller than the catalog")

    n_halls = len(rankings)
    all_ids = catalog + [object() for _ in range(n_phantom)]

    # per hall, partition the candidates into groups that share a
    # position interval: fixed singletons, one optional tie group and
    # the unlisted tail
    halls_groups = []
    for ranking in rankings:
        groups = []
        pos = 1
        seen = set()
        for page_nr, page in enumerate(ranking.pages):
            tied = (ranking.last_page_tied and len(page) > 1
                    and page_nr == len(ranking.pages) - 1)
            for rid in page:
                if rid in seen:
                    raise ValueError("id %r listed twice by one source"
                                     % (rid,))
                seen.add(rid)
            if tied:
                groups.append((pos, pos + len(page) - 1, list(page)))
                pos += len(page)
            else:
                for rid in page:
                    groups.append((pos, pos, [rid]))
                    pos += 1
        if pos - 1 > total:
            raise ValueError("more listed recordings than total candidates")
        unlisted = [rid for rid in all_ids if rid not in seen]
        if unlisted:
            groups.append((pos, total, unlisted))
        halls_groups.append(groups)

    cand_group = {}
    cand_lo = {}
    cand_hi = {}
    for h, groups in enumerate(halls_groups):
        for group_nr, (lo, hi, members) in enumerate(groups):
            for rid in members:
                cand_group.setdefault(rid, [None] * n_halls)[h] = group_nr
                cand_lo.setdefault(rid, [0] * n_halls)[h] = lo
                cand_hi.setdefault(rid, [0] * n_halls)[h] = hi

    def free_slots_state(x, x_slots):
        state = []
        for h, groups in enumerate(halls_groups):
            hall = []
            for lo, hi, members in groups:
                slots = list(range(lo, hi + 1))
                if x in members:
                    slots.remove(x_slots[h])
                hall.append(slots)
            state.append(hall)
        return state

    def rank_range(x):
        others = [tuple(cand_group[rid]) for rid in all_ids if rid != x]
        # Worst rank: x takes its worst slot in every hall.  Moving x to
        # a worse slot only raises its own score and frees a better slot
        # for the competitors, so the number of candidates that can
        # outscore x is maximal there.
        score_hi = sum(weights[h] * cand_hi[x][h] for h in range(n_halls))
        beaters = _max_satisfiers(free_slots_state(x, cand_hi[x]),
                                  others, weights, score_hi, "lt")
        # Best rank: x takes its best slot in every hall, and we
        # maximise the number of candidates that do *not* outscore it.
        score_lo = sum(weights[h] * cand_lo[x][h] for h in range(n_halls))
        non_beaters = _max_satisfiers(free_slots_state(x, cand_lo[x]),
                                      others, weights, score_lo, "ge")
        worst = 1 + beaters
        best = 1 + (len(all_ids) - 1 - non_beaters)
        return RankRange(best, worst, worst <= top)

    # candidates with identical feasible slots share one evaluation
    results = {}
    representatives = {}
    for x in catalog:
        signature = (tuple(cand_lo[x]), tuple(cand_hi[x]))
        if signature in representatives:
            results[x] = results[representatives[signature]]
        else:
            representatives[signature] = x
            results[x] = rank_range(x)
    return results
