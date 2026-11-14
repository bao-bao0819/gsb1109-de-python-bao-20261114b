# Tests for band lineup timelines built from artist relationships.

import datetime
import json
import os
import unittest

from test import _common
from musicbrainzngs import timeline


def member(artist_id, name, begin=None, end=None, rel_type="member of band", **extra):
    relation = {
        "type": rel_type,
        "target": artist_id,
        "artist": {"id": artist_id, "name": name},
        "begin": begin,
        "end": end,
    }
    relation.update(extra)
    return relation


class DatePrecisionTest(unittest.TestCase):
    def testParseDatePrecision(self):
        self.assertEqual(
            (datetime.date(2011, 1, 1), "year"), timeline.parse_date("2011")
        )
        self.assertEqual(
            (datetime.date(2011, 12, 1), "month"), timeline.parse_date("2011-12")
        )
        self.assertEqual(
            (datetime.date(2011, 12, 23), "day"),
            timeline.parse_date("2011-12-23"),
        )
        self.assertEqual((None, None), timeline.parse_date(""))
        self.assertEqual((None, None), timeline.parse_date(None))
        self.assertEqual("year", timeline.date_precision("2011"))

    def testInvalidDatesRejected(self):
        for bad in ("2011-13", "2011-02-30", "2011/12", "not-a-date", "2011-1-1"):
            self.assertRaises(timeline.DatePrecisionError, timeline.parse_date, bad)


class QueryPrecisionTest(unittest.TestCase):
    def setUp(self):
        self.lineup = timeline.Lineup([member("a", "Alice", "2011", "2015")])

    def testYearStartIsCandidateUntilYearPasses(self):
        confirmed, candidates = self.lineup.on("2011-01-01")
        self.assertEqual([], confirmed)
        self.assertEqual(["Alice"], [m.artist_name for m in candidates])

        confirmed, candidates = self.lineup.members_on("2011-12-31")
        self.assertEqual([], confirmed)
        self.assertEqual(["Alice"], candidates)

        confirmed, candidates = self.lineup.members_on("2012-01-01")
        self.assertEqual(["Alice"], confirmed)
        self.assertEqual([], candidates)

    def testYearEndIsCandidateInDepartureYear(self):
        confirmed, candidates = self.lineup.members_on("2015-01-01")
        self.assertEqual([], confirmed)
        self.assertEqual(["Alice"], candidates)
        confirmed, candidates = self.lineup.members_on("2015-12-31")
        self.assertEqual([], confirmed)
        self.assertEqual(["Alice"], candidates)
        self.assertEqual(([], []), self.lineup.members_on("2016-01-01"))

    def testMonthPrecision(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011-12", "2012-03")])
        self.assertEqual(([], ["Alice"]), lineup.members_on("2011-12-31"))
        self.assertEqual((["Alice"], []), lineup.members_on("2012-01-01"))
        self.assertEqual(([], ["Alice"]), lineup.members_on("2012-03-31"))
        self.assertEqual(([], []), lineup.members_on("2012-04-01"))

    def testDayPrecisionIsInclusive(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011-12-23", "2012-01-02")])
        self.assertEqual(([], []), lineup.members_on("2011-12-22"))
        self.assertEqual((["Alice"], []), lineup.members_on("2011-12-23"))
        self.assertEqual((["Alice"], []), lineup.members_on("2012-01-02"))
        self.assertEqual(([], []), lineup.members_on("2012-01-03"))

    def testOpenEndedMembership(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011", None)])
        self.assertEqual(([], ["Alice"]), lineup.members_on("2011-06-01"))
        self.assertEqual((["Alice"], []), lineup.members_on("2030-01-01"))

    def testEndedButNoEndDateStaysCandidate(self):
        lineup = timeline.Lineup(
            [member("a", "Alice", "2011", None, ended="true")]
        )
        self.assertEqual(([], ["Alice"]), lineup.members_on("2030-01-01"))
        tenure = lineup.tenures[0]
        self.assertTrue(tenure.end_unknown)
        self.assertFalse(tenure.is_open)


class SeparateTenureTest(unittest.TestCase):
    def testLeaveAndRejoinAreTwoTenures(self):
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2010", "2012"),
                member("a", "Alice", "2015", "2018"),
            ]
        )
        self.assertEqual(2, len(lineup.tenures))
        self.assertEqual(([], []), lineup.members_on("2013-06-01"))
        self.assertEqual(([], ["Alice"]), lineup.members_on("2012-06-01"))
        self.assertEqual((["Alice"], []), lineup.members_on("2016-06-01"))

        confirmed, _ = lineup.on("2016-06-01")
        self.assertEqual([1], [t.index for t in confirmed[0].tenures])
        self.assertEqual(2, len(lineup.timeline()))

    def testTouchingRangesAreNotMergedOrFilled(self):
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2011", "2013"),
                member("b", "Bob", "2013", "2015"),
            ]
        )
        # 2013 is the shared boundary. Both records are only year-precise,
        # so neither proves membership on any particular day in 2013.
        confirmed, candidates = lineup.on("2013-06-01")
        self.assertEqual([], confirmed)
        self.assertEqual(["Alice", "Bob"], [m.artist_name for m in candidates])
        self.assertEqual(2, len(lineup.timeline()))
        self.assertEqual((["Alice"], []), lineup.members_on("2012-06-01"))
        self.assertEqual((["Bob"], []), lineup.members_on("2014-06-01"))

    def testAdjacentYearRecordsDoNotFillBoundary(self):
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2011", "2013"),
                member("b", "Bob", "2014", "2016"),
            ]
        )
        # No record covers 2013-12-31 except Alice's candidate window, and
        # Bob's start is only a candidate. The turn of the year is not
        # silently treated as a confirmed continuous lineup.
        self.assertEqual(([], ["Alice"]), lineup.members_on("2013-12-31"))
        self.assertEqual(([], ["Bob"]), lineup.members_on("2014-01-01"))
        self.assertEqual(2, len(lineup.timeline()))

    def testOverlappingRangesKeepBothRecords(self):
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2010-01-01", "2014-12-31"),
                member("a", "Alice", "2013-01-01", "2016-12-31"),
            ]
        )
        confirmed, candidates = lineup.on("2013-06-01")
        self.assertEqual([], candidates)
        self.assertEqual(1, len(confirmed))
        self.assertEqual([0, 1], [t.index for t in confirmed[0].tenures])
        self.assertEqual(2, len(lineup.timeline()))


class RoleAndIdentityTest(unittest.TestCase):
    def testGuestWithSameNameIsNotMergedIntoMember(self):
        lineup = timeline.Lineup(
            [
                member("member-id", "Alex", "2011", "2015"),
                member(
                    "guest-id",
                    "Alex",
                    "2014-06-01",
                    "2014-06-01",
                    rel_type="guest performer",
                    **{"attribute-list": ["guitar"]}
                ),
            ]
        )
        confirmed, candidates = lineup.on("2014-06-01")
        self.assertEqual(1, len(confirmed))
        self.assertEqual("member-id", confirmed[0].artist_id)
        self.assertEqual([], candidates)

        guests, guest_candidates = lineup.guests("2014-06-01")
        self.assertEqual(1, len(guests))
        self.assertEqual("guest-id", guests[0].artist_id)
        self.assertEqual([], guest_candidates)

        snapshot = lineup.query("2014-06-01")
        self.assertEqual(1, len(snapshot["confirmed"]))
        self.assertEqual(1, len(snapshot["confirmed-guests"]))

    def testRoleMappingForRelationType(self):
        role_map = {
            "member of band": "member",
            "guest performer": {"role": "客席吉他", "kind": "guest"},
        }
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2011", "2015"),
                member("b", "Bob", "2014-06-01", "2014-06-01",
                       rel_type="guest performer"),
            ],
            role_map=role_map,
        )
        guests, _ = lineup.guests("2014-06-01")
        self.assertEqual("客席吉他", guests[0].role)
        self.assertTrue(guests[0].guest)

    def testRoleMappingForAttribute(self):
        role_map = {
            "member of band": "member",
            "lead vocals": "主唱",
            "guitar": "吉他",
        }
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2011", "2015",
                       **{"attribute-list": ["lead vocals"]}),
                member("b", "Bob", "2011", "2015",
                       **{"attribute-list": ["guitar"]}),
            ],
            role_map=role_map,
        )
        roles = dict(
            (m.artist_name, m.role) for m in lineup.confirmed("2012-06-01")
        )
        self.assertEqual({"Alice": "主唱", "Bob": "吉他"}, roles)

    def testUnmappedRelationTypesAreIgnored(self):
        lineup = timeline.Lineup(
            [
                member("a", "Alice", "2011", "2015"),
                member("sub", "EXO-K", "2012", rel_type="subgroup"),
            ]
        )
        self.assertEqual(1, len(lineup.tenures))


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.lineup = timeline.Lineup(
            [
                member("a", "Alice", "2010", "2012"),
                member("a", "Alice", "2015", "2018"),
            ]
        )

    def testJoinedEvidenceNarrowsExactlyOneTenure(self):
        evidence = timeline.Evidence(
            "2015-09-01",
            kind="joined",
            target=1,
            source="interview-2015",
            evidence_id="ev-1",
        )
        result = self.lineup.apply_evidence(evidence)

        self.assertEqual(1, len(result))
        change = result[0]
        self.assertEqual(1, change["tenure-id"])
        self.assertEqual("Alice", change["artist-name"])
        self.assertEqual("2015..2018", change["relationship"])
        self.assertEqual(
            ["2015-01-01", "2015-12-31"], change["before"]["begin-range"]
        )
        self.assertEqual(
            ["2015-09-01", "2015-09-01"], change["after"]["begin-range"]
        )
        # The unrelated tenure predates the evidence date and was not touched.
        self.assertEqual(0, result.unchanged)
        self.assertEqual([], result.conflicts)

        self.assertEqual(
            (["Alice"], []), self.lineup.members_on("2015-09-01")
        )
        # An exact join date rules out membership before that day.
        self.assertEqual(([], []), self.lineup.members_on("2015-08-31"))

    def testEvidenceEffectCanBeLookedUpBySource(self):
        self.lineup.apply_evidence(
            timeline.Evidence(
                "2015-09-01", kind="joined", target="Alice",
                source="interview-2015", evidence_id="ev-1"
            )
        )
        effects = self.lineup.evidence_effect("interview-2015")
        self.assertEqual(1, len(effects))
        self.assertEqual("ev-1", effects[0]["evidence"]["evidence-id"])
        self.assertEqual([1], [c["tenure-id"] for c in effects[0]["changes"]])

        self.assertEqual([], self.lineup.evidence_effect("missing"))

    def testAbsentEvidenceNarrowsJoinOrLeaveBoundary(self):
        early = timeline.Lineup([member("a", "Alice", "2011", "2015")])
        result = early.apply_evidence(
            timeline.Evidence("2011-06-01", kind="absent", target="a")
        )
        self.assertEqual(1, len(result))
        self.assertEqual(
            ["2011-06-02", "2011-12-31"], result[0]["after"]["begin-range"]
        )
        self.assertEqual(([], ["Alice"]), early.members_on("2011-06-02"))

        late = timeline.Lineup([member("a", "Alice", "2011", "2015")])
        result = late.apply_evidence(
            timeline.Evidence("2015-06-01", kind="absent", target="a")
        )
        self.assertEqual(1, len(result))
        self.assertEqual(
            ["2015-01-01", "2015-05-31"], result[0]["after"]["end-range"]
        )
        self.assertEqual(([], []), late.members_on("2015-06-01"))

    def testPresentEvidenceNarrowsOuterBounds(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011", "2015")])
        result = lineup.apply_evidence(
            timeline.Evidence("2011-06-01", kind="present", target="a")
        )
        self.assertEqual(1, len(result))
        self.assertEqual(
            ["2011-01-01", "2011-06-01"], result[0]["after"]["begin-range"]
        )
        self.assertEqual(
            ["2015-01-01", "2015-12-31"], result[0]["after"]["end-range"]
        )
        # The observed day itself is now a confirmed member day even though
        # the outer bounds remain ranges.
        self.assertEqual((["Alice"], []), lineup.members_on("2011-06-01"))

    def testAmbiguousUntargetedJoinIsRejected(self):
        lineup = timeline.Lineup(
            [
                member("member-id", "Alex", "2012", "2013"),
                member("guest-id", "Alex", "2012-06-01", "2012-06-01",
                       rel_type="guest performer"),
            ]
        )
        result = lineup.apply_evidence(
            timeline.Evidence("2012-06-01", kind="joined")
        )
        self.assertEqual([], result)
        self.assertEqual(1, len(result.conflicts))
        self.assertIn("ambiguous", result.conflicts[0]["reason"])
        # Nothing was silently narrowed.
        self.assertEqual("2012-12-31", lineup.tenures[0].begin_latest.isoformat())

    def testPresentEvidenceInsideCertainCoreChangesNoBounds(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011-01-01", "2015")])
        result = lineup.apply_evidence(
            timeline.Evidence("2013-06-01", kind="present", target="a")
        )
        # 2013 is already inside the certain core of a day-precise start; a
        # presence record cannot tighten either outer boundary.  It is
        # reported as a witness, not as a range change.
        self.assertEqual([], result)
        self.assertEqual(1, len(result.witnessed))
        self.assertEqual("2013-06-01", result.witnessed[0]["evidence"]["when"])
        self.assertEqual((["Alice"], []), lineup.members_on("2013-06-01"))

    def testConflictingAbsentEvidenceIsReportedNotApplied(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011", "2015")])
        before = lineup.timeline()
        result = lineup.apply_evidence(
            timeline.Evidence("2013-06-01", kind="absent", target="a",
                              source="bad-source")
        )
        self.assertEqual([], result)
        self.assertEqual(1, len(result.conflicts))
        self.assertEqual(0, len(lineup.evidence))
        self.assertEqual(before, lineup.timeline())

    def testEvidenceWithoutTargetIsSessionWideOnlyWhereApplicable(self):
        result = self.lineup.apply_evidence(
            timeline.Evidence("2015-06-01", kind="present", source="live-bootleg")
        )
        # Only the 2015..2018 relationship can include the evidence date; the
        # 2010..2012 relationship is out of scope rather than a conflict.
        self.assertEqual([1], [c["tenure-id"] for c in result])
        self.assertEqual(0, result.unchanged)
        self.assertEqual([], result.conflicts)


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.datadir = os.path.join(
            os.path.dirname(__file__), "data", "artist"
        )

    def testParsedArtistFixture(self):
        res = _common.open_and_parse_test_data(
            self.datadir,
            "b3785a55-2cf6-497d-b8e3-cfa21a36f997-artist-rels.xml",
        )
        lineup = timeline.Lineup(res)
        self.assertEqual(12, len(lineup.tenures))

        # Subgroups (EXO-K / EXO-M) are not members.
        confirmed, candidates = lineup.on("2011-12-31")
        self.assertEqual([], confirmed)
        self.assertEqual(12, len(candidates))
        self.assertIn("鹿晗", [m.artist_name for m in candidates])

        confirmed, candidates = lineup.on("2014-10-10")
        names = [m.artist_name for m in confirmed]
        self.assertIn("鹿晗", names)
        self.assertEqual(["吴亦凡"], [m.artist_name for m in candidates])

    def testQueryIsJsonSerialisable(self):
        lineup = timeline.Lineup([member("a", "Alice", "2011", "2015")])
        snapshot = lineup.query("2013-06-01")
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn("Alice", encoded)

    def testQueryLineupConvenience(self):
        snapshot = timeline.query_lineup(
            [member("a", "Alice", "2011", "2015")], "2013-06-01"
        )
        self.assertEqual("2013-06-01", snapshot["date"])
        self.assertEqual(1, len(snapshot["confirmed"]))


if __name__ == "__main__":
    unittest.main()
