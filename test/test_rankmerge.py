import itertools
import unittest

from musicbrainzngs.rankmerge import PartialRanking, merged_rank_ranges


def _hall_groups(ranking, total, all_ids):
    """Groups of a ranking as (slots, members) pairs, test-side."""
    groups = []
    pos = 1
    seen = set()
    for page_nr, page in enumerate(ranking.pages):
        tied = (ranking.last_page_tied and len(page) > 1
                and page_nr == len(ranking.pages) - 1)
        for rid in page:
            seen.add(rid)
        if tied:
            groups.append((list(range(pos, pos + len(page))), list(page)))
            pos += len(page)
        else:
            for rid in page:
                groups.append(([pos], [rid]))
                pos += 1
    unlisted = [rid for rid in all_ids if rid not in seen]
    if unlisted:
        groups.append((list(range(pos, total + 1)), unlisted))
    return groups


def _hall_completions(groups):
    """All legal completions of one hall as dicts id -> position."""
    per_group = []
    for slots, members in groups:
        assert len(slots) == len(members)
        per_group.append([dict(zip(members, perm))
                          for perm in itertools.permutations(slots)])
    completions = []
    for combo in itertools.product(*per_group):
        merged = {}
        for part in combo:
            merged.update(part)
        completions.append(merged)
    return completions


def brute_force_ranges(rankings, weights, total, catalog, top=5):
    """Exact rank ranges by enumerating every legal completion."""
    all_ids = list(catalog) + [("phantom", i)
                               for i in range(total - len(catalog))]
    per_hall = [_hall_completions(_hall_groups(r, total, all_ids))
                for r in rankings]
    best = dict((rid, total) for rid in catalog)
    worst = dict((rid, 1) for rid in catalog)
    for combo in itertools.product(*per_hall):
        scores = {}
        for rid in all_ids:
            scores[rid] = sum(weights[h] * combo[h][rid]
                              for h in range(len(rankings)))
        for rid in catalog:
            rank = 1 + sum(1 for other in all_ids
                           if other != rid and scores[other] < scores[rid])
            best[rid] = min(best[rid], rank)
            worst[rid] = max(worst[rid], rank)
    return dict((rid, (best[rid], worst[rid], worst[rid] <= top))
                for rid in catalog)


def naive_ranges(rankings, weights, total, catalog, top=5):
    """Pairwise bounds that ignore slot scarcity between competitors."""
    ranges = dict((rid, [[], []]) for rid in catalog)
    for ranking, weight in zip(rankings, weights):
        pos = 1
        seen = set()
        for page_nr, page in enumerate(ranking.pages):
            tied = (ranking.last_page_tied and len(page) > 1
                    and page_nr == len(ranking.pages) - 1)
            for rid in page:
                seen.add(rid)
                lo = pos if tied else pos
                hi = pos + len(page) - 1 if tied else pos
                ranges[rid][0].append(weight * lo)
                ranges[rid][1].append(weight * hi)
                if not tied:
                    pos += 1
            if tied:
                pos += len(page)
        for rid in catalog:
            if rid not in seen:
                ranges[rid][0].append(weight * pos)
                ranges[rid][1].append(weight * total)
    smin = dict((rid, sum(lo)) for rid, (lo, hi) in ranges.items())
    smax = dict((rid, sum(hi)) for rid, (lo, hi) in ranges.items())
    result = {}
    for rid in catalog:
        best = 1 + sum(1 for o in catalog
                       if o != rid and smax[o] < smin[rid])
        worst = 1 + sum(1 for o in catalog
                        if o != rid and smin[o] < smax[rid])
        result[rid] = (best, worst, worst <= top)
    return result


class RankMergeTest(unittest.TestCase):

    def assert_ranges(self, rankings, weights, total, catalog, top=5):
        got = merged_rank_ranges(rankings, weights, total,
                                 catalog=catalog, top=top)
        expected = brute_force_ranges(rankings, weights, total,
                                      catalog, top=top)
        for rid in catalog:
            self.assertEqual(expected[rid], tuple(got[rid]),
                             "ranges differ for %r" % (rid,))
        return got

    def test_unlisted_is_not_automatically_last(self):
        # one source, two listed, eight more candidates unlisted
        ranking = PartialRanking([["a", "b"]])
        got = merged_rank_ranges([ranking], [1], 10,
                                 catalog=["a", "b", "z"])
        self.assertEqual((1, 1), (got["a"].best, got["a"].worst))
        self.assertEqual((2, 2), (got["b"].best, got["b"].worst))
        # "z" never made the submitted pages, but it is not stuck on
        # the last slot: it shares slots 3..10 with the other unlisted
        # candidates and can still finish anywhere in between
        self.assertEqual((3, 10), (got["z"].best, got["z"].worst))

    def test_unlisted_candidates_share_the_tail(self):
        ranking = PartialRanking([["a", "b"]])
        got = merged_rank_ranges([ranking], [1], 4,
                                 catalog=["a", "b", "c", "d"])
        # the unlisted candidates share slots 3 and 4: neither of them
        # is stuck on the last slot, but they cannot both avoid it
        self.assertEqual((3, 4), (got["c"].best, got["c"].worst))
        self.assertEqual((3, 4), (got["d"].best, got["d"].worst))

    def test_tied_last_page(self):
        ranking = PartialRanking([["a"], ["b", "c"]], last_page_tied=True)
        got = merged_rank_ranges([ranking], [1], 3)
        self.assertEqual((2, 3), (got["b"].best, got["b"].worst))
        self.assertEqual((2, 3), (got["c"].best, got["c"].worst))
        self.assertEqual((1, 1), (got["a"].best, got["a"].worst))

    def test_weighted_merging(self):
        rankings = [PartialRanking([["a", "b"]]),
                    PartialRanking([["b", "a"]])]
        got = merged_rank_ranges(rankings, [3, 1], 2)
        # a: 3*1+1*2 = 5, b: 3*2+1*1 = 7 in the only completion
        self.assertEqual((1, 1), (got["a"].best, got["a"].worst))
        self.assertEqual((2, 2), (got["b"].best, got["b"].worst))

    def test_definite_flag(self):
        rankings = [PartialRanking([["a", "b", "c", "d", "e", "f"]]),
                    PartialRanking([["a", "c", "b", "e", "d", "f"]])]
        got = merged_rank_ranges(rankings, [2, 1], 6, top=3)
        self.assertTrue(got["a"].definite)
        self.assertFalse(got["f"].definite)

    def test_slot_scarcity_beats_pairwise_naive(self):
        # found by searching random instances: the pairwise formulas
        # disagree with the exact ranges, so slot scarcity really bites
        rankings = [PartialRanking([["a", "b"], ["c", "d"]],
                                   last_page_tied=True),
                    PartialRanking([["c", "a"], ["d", "b"]],
                                   last_page_tied=True)]
        catalog = ["a", "b", "c", "d"]
        exact = merged_rank_ranges(rankings, [1, 1], 4, catalog=catalog)
        naive = naive_ranges(rankings, [1, 1], 4, catalog)
        self.assertNotEqual([tuple(r) for r in naive],
                            [tuple(r) for r in exact.values()])
        self.assert_ranges(rankings, [1, 1], 4, catalog)

    def test_phantom_candidates_take_part(self):
        # the catalog holds 2 ids but there is a third, anonymous
        # candidate; it shares the unlisted slots and keeps "b" off
        # the certain second rank it would have with total == 2
        ranking = PartialRanking([["a"]])
        got = merged_rank_ranges([ranking], [1], 3, catalog=["a", "b"])
        self.assertEqual((2, 3), (got["b"].best, got["b"].worst))

    def test_against_brute_force(self):
        rankings = [PartialRanking([["a", "b"], ["c", "d"]],
                                   last_page_tied=True),
                    PartialRanking([["d", "a", "c"]]),
                    PartialRanking([["c"], ["b", "a"]],
                                   last_page_tied=True)]
        catalog = ["a", "b", "c", "d"]
        got = self.assert_ranges(rankings, [3, 1, 2], 6, catalog, top=2)
        for rid in catalog:
            self.assertLessEqual(got[rid].best, got[rid].worst)
            self.assertGreaterEqual(got[rid].best, 1)
            self.assertLessEqual(got[rid].worst, 6)

    def test_against_brute_force_random(self):
        import random
        rng = random.Random(20261114)
        for _ in range(30):
            total = rng.randint(3, 6)
            catalog = ["r%d" % i for i in range(total)]
            rankings = []
            for _h in range(3):
                ids = list(catalog)
                rng.shuffle(ids)
                listed = ids[:rng.randint(1, total)]
                pages = []
                while listed:
                    take = rng.randint(1, len(listed))
                    pages.append(listed[:take])
                    listed = listed[take:]
                tied = len(pages[-1]) > 1 and rng.random() < 0.5
                rankings.append(PartialRanking(pages, last_page_tied=tied))
            weights = [rng.randint(1, 4) for _ in rankings]
            self.assert_ranges(rankings, weights, total, catalog,
                               top=rng.randint(1, total))

    def test_from_search_result(self):
        result = {
            "recording-list": [
                {"id": "r1", "ext:score": "100"},
                {"id": "r2", "ext:score": "87"},
                {"id": "r3", "ext:score": "65"},
                {"id": "r4", "ext:score": "65"},
            ],
            "recording-count": 4,
        }
        ranking = PartialRanking.from_search_result(result, page_size=2)
        self.assertEqual([["r1", "r2"], ["r3", "r4"]], ranking.pages)
        self.assertTrue(ranking.last_page_tied)
        ranking = PartialRanking.from_search_result(
            result, page_size=2, last_page_tied=False)
        self.assertFalse(ranking.last_page_tied)

    def test_validation(self):
        ranking = PartialRanking([["a", "b"]])
        self.assertRaises(ValueError, merged_rank_ranges,
                          [ranking], [1], 1)  # fewer slots than listed
        self.assertRaises(ValueError, merged_rank_ranges,
                          [ranking], [1], 2, catalog=["a", "a"])
        self.assertRaises(ValueError, merged_rank_ranges,
                          [ranking], [1], 2, catalog=["a"])
        self.assertRaises(ValueError, merged_rank_ranges,
                          [ranking], [], 2)
        self.assertRaises(ValueError, merged_rank_ranges,
                          [], [], 2)
        dup = PartialRanking([["a"], ["a"]])
        self.assertRaises(ValueError, merged_rank_ranges, [dup], [1], 2)


if __name__ == "__main__":
    unittest.main()
