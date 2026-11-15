#!/usr/bin/env python
"""Merge partial search-result rankings from three sources.

Three "halls" each hand in only the first pages of their MusicBrainz
recording search (the last page of one hall is even tied).  Given the
hall weights and the total number of candidates, this prints, for every
recording, the merged rank it can still reach in the best and worst
legal completion, and whether it is certain to land in the top five.

    $ ./rankmerge.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from musicbrainzngs.rankmerge import PartialRanking, merged_rank_ranges

HALL_A = PartialRanking([
    ["rec-01", "rec-02", "rec-03", "rec-04", "rec-05"],
    ["rec-06", "rec-07", "rec-08"],
])
HALL_B = PartialRanking([
    ["rec-02", "rec-01", "rec-09"],
    ["rec-03", "rec-05", "rec-06"],
])
# the last page of hall C is a tie group
HALL_C = PartialRanking([
    ["rec-01", "rec-10"],
    ["rec-02", "rec-03", "rec-11"],
], last_page_tied=True)

WEIGHTS = [3, 2, 1]
TOTAL = 14          # candidates in total, listed or not
TOP = 5             # "definite" means top five in every legal completion


def main():
    rankings = [HALL_A, HALL_B, HALL_C]
    catalog = []
    for ranking in rankings:
        for page in ranking.pages:
            for rid in page:
                if rid not in catalog:
                    catalog.append(rid)
    ranges = merged_rank_ranges(rankings, WEIGHTS, TOTAL,
                                catalog=catalog, top=TOP)
    print("%-8s %14s %s" % ("id", "rank range", "definite?"))
    for rid in catalog:
        r = ranges[rid]
        print("%-8s %5d .. %-5d  %s"
              % (rid, r.best, r.worst,
                 "yes" if r.definite else "no"))


if __name__ == "__main__":
    main()
