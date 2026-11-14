import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poster_match import match_poster


def artist(id, name, area=None, country=None, life=None, aliases=None):
    a = {"id": id, "name": name, "sort-name": name}
    if area:
        a["area"] = {"name": area}
    if country:
        a["country"] = country
    if life:
        a["life-span"] = life
    if aliases:
        a["alias-list"] = aliases
    return a


# The Wellington band renamed itself: the old spelling is a dated alias.
DYNAMO_GO = artist(
    "a1", "Dynamo Go", area="Wellington", country="NZ",
    life={"begin": "2005-06"},
    aliases=[
        {"alias": "Dynamo Squad", "begin-date": "2014-06",
         "end-date": "2016-12-31"},
        {"alias": "DG"},
    ])

# A same-named but foreign band: the top search hit that must not win.
DYNAMO_US = artist(
    "a2", "Dynamo Squad", area="Chicago", country="US",
    life={"begin": "2010"},
    aliases=[{"alias": "Dynamo Squad"}])

# A band with no area data on file.
MYSTERY = artist(
    "a3", "Mystery Lights", life={"begin": "2012"},
    aliases=[{"alias": "The Mystery Lights"}])

# Support act, only active under this name until 2015.
GOGO = artist(
    "a4", "GO!GO!7188", area="Tokyo", country="JP",
    life={"begin": "1998-06", "end": "2012-02-10"},
    aliases=[{"alias": "GOGO 7188"}])

ARTISTS = {a["id"]: a for a in (DYNAMO_GO, DYNAMO_US, MYSTERY, GOGO)}


def fake_search(name):
    """Return every fixture artist whose name or alias contains the
    query, mimicking a fuzzy MusicBrainz search (wrong hits included)."""
    query = name.strip().casefold()
    out = []
    for a in ARTISTS.values():
        names = [a["name"]] + [al["alias"] for al in a.get("alias-list", [])]
        if any(query in n.casefold() for n in names):
            out.append(a)
    return out


class PosterMatchTest(unittest.TestCase):

    def test_top_hit_does_not_win(self):
        # The US band sorts first on score but the poster is in NZ.
        poster = {
            "date_begin": "2015-03-01", "date_end": "2015-03-31",
            "area": "NZ",
            "credits": [{"name": "Dynamo Squad", "role": "headliner"}],
        }
        solutions = match_poster(poster, fake_search)
        self.assertEqual(1, len(solutions))
        self.assertEqual("a1", solutions[0]["assignments"][0]["artist_id"])

    def test_alias_validity_narrows_dates(self):
        # "Dynamo Squad" was only used from 2014-06, so a poster dated
        # 2014 can only refer to a show after the rename.
        poster = {
            "date_begin": "2014-01", "date_end": "2014-12",
            "area": "NZ",
            "credits": [{"name": "Dynamo Squad", "role": "headliner"}],
        }
        solutions = match_poster(poster, fake_search)
        self.assertEqual(1, len(solutions))
        self.assertTrue(solutions[0]["dates_narrowed"])
        self.assertEqual("2014-06-01",
                         solutions[0]["possible_dates"]["begin"])
        self.assertEqual("2014-12-31",
                         solutions[0]["possible_dates"]["end"])

    def test_expired_alias_explains_nothing(self):
        # After 2016-12-31 the old spelling is no longer verifiable.
        poster = {
            "date_begin": "2017-02-01", "date_end": "2017-02-28",
            "area": "NZ",
            "credits": [{"name": "Dynamo Squad", "role": "headliner"}],
        }
        self.assertEqual([], match_poster(poster, fake_search))

    def test_aka_group_allows_one_artist_two_spellings(self):
        poster = {
            "date_begin": "2017-05-10", "date_end": "2017-05-10",
            "area": "NZ",
            "credits": [
                {"name": "Dynamo Go", "role": "headliner", "aka": "x"},
                {"name": "DG", "role": "headliner", "aka": "x"},
            ],
        }
        solutions = match_poster(poster, fake_search)
        self.assertEqual(1, len(solutions))
        assignment = solutions[0]["assignments"][0]
        self.assertEqual("a1", assignment["artist_id"])
        self.assertEqual(["Dynamo Go", "DG"], assignment["credited_as"])

    def test_reuse_without_aka_marking_rejected(self):
        # Two separate credits may not be filled by the same artist.
        poster = {
            "date_begin": "2017-05-10", "date_end": "2017-05-10",
            "area": "NZ",
            "credits": [
                {"name": "Dynamo Go", "role": "headliner"},
                {"name": "DG", "role": "dj"},
            ],
        }
        self.assertEqual([], match_poster(poster, fake_search))

    def test_lineup_must_be_explained_simultaneously(self):
        # GO!GO!7188 broke up in 2012, so a 2015 poster with them on
        # the bill has no consistent explanation.
        poster = {
            "date_begin": "2015-03-01", "date_end": "2015-03-31",
            "area": "NZ",
            "credits": [
                {"name": "Dynamo Squad", "role": "headliner"},
                {"name": "GO!GO!7188", "role": "support"},
            ],
        }
        self.assertEqual([], match_poster(poster, fake_search))

    def test_lineup_intersection_narrows_show_dates(self):
        # The alias starts 2014-06; GO!GO!7188 ended 2012, so use the
        # Mystery Lights (active since 2012) as the co-act instead.
        poster = {
            "date_begin": "2014-01", "date_end": "2014-12",
            "area": "NZ",
            "credits": [
                {"name": "Dynamo Squad", "role": "headliner"},
                {"name": "Mystery Lights", "role": "support"},
            ],
        }
        solutions = match_poster(poster, fake_search)
        self.assertEqual(1, len(solutions))
        self.assertEqual("2014-06-01",
                         solutions[0]["possible_dates"]["begin"])
        roles = {a["role"]: a["artist_id"]
                 for a in solutions[0]["assignments"]}
        self.assertEqual({"headliner": "a1", "support": "a3"}, roles)

    def test_unknown_area_kept_but_unverified(self):
        poster = {
            "date_begin": "2013-01-01", "date_end": "2013-12-31",
            "area": "NZ",
            "credits": [{"name": "Mystery Lights", "role": "support"}],
        }
        solutions = match_poster(poster, fake_search)
        self.assertEqual(1, len(solutions))
        self.assertFalse(solutions[0]["assignments"][0]["area_verified"])


if __name__ == "__main__":
    unittest.main()
