# Tests for claiming a whole box from a local TOC

import copy
import unittest
import os

import musicbrainzngs
from musicbrainzngs import toc
from test import _common


class NormaliseTest(unittest.TestCase):
    def testMapping(self):
        normal = toc.normalize_toc({
            "offset-list": [17990, 26452, 38762],
            "sectors": 50000,
        })
        self.assertEqual([17990, 26452, 38762], normal["offset-list"])
        self.assertEqual(3, normal["track-count"])
        self.assertEqual([0, 8462, 20772], normal["relative-offset-list"])
        self.assertEqual(50000 - 17990, normal["program-sectors"])
        self.assertEqual(17990 - toc.PREGAP_SECTORS, normal["pregap-sectors"])
        self.assertTrue(normal["hidden-before-first"])

    def testTocString(self):
        normal = toc.normalize_toc("1 3 50000 17990 26452 38762")
        self.assertEqual([17990, 26452, 38762], normal["offset-list"])
        self.assertEqual(50000, normal["sectors"])

    def testBareOffsets(self):
        normal = toc.normalize_toc([150, 5000, 9000], sectors=20000)
        self.assertEqual(0, normal["pregap-sectors"])
        self.assertFalse(normal["hidden-before-first"])

    def testFirstOffsetWithoutPregapUsesLeadIn(self):
        normal = toc.normalize_toc([toc.PREGAP_SECTORS, 5000], sectors=9000)
        self.assertEqual(0, normal["pregap-sectors"])

    def testRoundTripTocString(self):
        source = {"offset-list": [150, 5000, 9000], "sectors": 12000}
        self.assertEqual("1 3 12000 150 5000 9000", toc.toc_string(source))
        self.assertTrue(toc.tocs_match(source, "1 3 12000 150 5000 9000"))

    def testRejectsBadInput(self):
        self.assertRaises(ValueError, toc.normalize_toc, [5000, 1000],
                          sectors=9000)
        self.assertRaises(ValueError, toc.normalize_toc, "1 3 5000 150")
        self.assertRaises(ValueError, toc.normalize_toc, {"sectors": 9000})


class GeometryMatchTest(unittest.TestCase):
    def testRelativeOffsetsIgnoreShifting(self):
        # Same programme, a different amount of hidden content before track 1.
        first = {"offset-list": [150, 5000, 9000], "sectors": 12000}
        second = {"offset-list": [250, 5100, 9100], "sectors": 12100}
        self.assertTrue(toc.tocs_match(first, second))
        self.assertFalse(toc.tocs_match(first, second, absolute=True))

    def testProgrammeLengthMustAgree(self):
        first = {"offset-list": [150, 5000, 9000], "sectors": 12000}
        second = {"offset-list": [150, 5000, 9000], "sectors": 13000}
        self.assertFalse(toc.tocs_match(first, second))

    def testTrackCountMustAgree(self):
        first = {"offset-list": [150, 5000], "sectors": 9000}
        second = {"offset-list": [150, 5000, 7000], "sectors": 9000}
        self.assertFalse(toc.tocs_match(first, second))

    def testTolerance(self):
        first = {"offset-list": [150, 5000, 9000], "sectors": 12000}
        second = {"offset-list": [150, 5003, 9006], "sectors": 12006}
        self.assertFalse(toc.tocs_match(first, second))
        self.assertTrue(toc.tocs_match(first, second, tolerance=8))


class MediumTocTest(unittest.TestCase):
    def setUp(self):
        self.datadir = os.path.join(os.path.dirname(__file__),
                                    "data", "release")

    def testPregapIsIncluded(self):
        res = _common.open_and_parse_test_data(
            self.datadir, "8eb2b179-643d-3507-b64c-29fcc6745156-recordings.xml")
        medium = res["release"]["medium-list"][0]
        derived = toc.medium_toc(medium)
        self.assertEqual(11, derived["track-count"])
        # 35000 ms of pre-gap plus the 150 sector lead-in.
        self.assertEqual(150 + 2625, derived["first-offset"])
        self.assertTrue(derived["hidden-before-first"])

    def testDataMediumHasNoAudioToc(self):
        res = _common.open_and_parse_test_data(
            self.datadir, "9ce41d09-40e4-4d33-af0c-7fed1e558dba-recordings.xml")
        medium = res["release"]["medium-list"][0]
        self.assertIsNone(toc.medium_toc(medium))
        self.assertEqual("data", toc._medium_role(medium))

    def testMissingLengthGivesNoToc(self):
        medium = {"track-list": [{"position": "1"}, {"position": "2"}]}
        self.assertIsNone(toc.medium_toc(medium))


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.datadir = os.path.join(os.path.dirname(__file__), "data", "discid")

    def load(self, filename):
        return _common.open_and_parse_test_data(self.datadir, filename)

    def testSingleDiscCountriesAllStayValid(self):
        result = self.load("xp5tz6rE4OHrBafj0bLfDRMGK48-.xml")
        claim = toc.claim_whole_box(discid_result=result)
        self.assertEqual(3, len(claim["release-claims"]))
        self.assertEqual(3, len(claim["confirmed"]))
        for release in claim["release-claims"]:
            self.assertEqual("confirmed", release["status"])
        # The disc sets are identical, so no disc number can separate them.
        self.assertEqual([], claim["discriminating"]["disc-numbers"])
        self.assertFalse(claim["discriminating"]["separated"])

    def testSingleDiscOfMultiDiscBoxIsOnlyPartial(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        claim = toc.claim_whole_box(discid_result=result)
        self.assertEqual([], claim["confirmed"])
        self.assertEqual(2, len(claim["partial"]))
        for release in claim["release-claims"]:
            self.assertEqual("partial", release["status"])
            self.assertEqual([2], release["missing-disc-numbers"])

    def testWholeBoxNeedsEverySuppliedDisc(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        media = result["disc"]["release-list"][0]["medium-list"]
        second = media[1]["disc-list"][0]
        claim = toc.claim_whole_box(discid_result=result, same_box=[second])
        for release in claim["release-claims"]:
            self.assertEqual("confirmed", release["status"])
            self.assertEqual([], release["missing-disc-numbers"])

    def testDiscOrderIsResolved(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        media = result["disc"]["release-list"][0]["medium-list"]
        first = result["disc"]
        second = media[1]["disc-list"][0]
        # Supply the second disc first: it must still land on medium 2.
        claim = toc.claim_whole_box(toc=second, same_box=[first],
                                    releases=result["disc"]["release-list"])
        for release in claim["release-claims"]:
            self.assertEqual("confirmed", release["status"])
            self.assertEqual(2, release["assignment"]["1"])
            self.assertEqual(1, release["assignment"]["2"])

    def testMinimalDistinguishingDiscNumber(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        media = result["disc"]["release-list"][0]["medium-list"]
        second = media[1]["disc-list"][0]
        claim = toc.claim_whole_box(discid_result=result, same_box=[second])
        self.assertEqual([2], claim["discriminating"]["disc-numbers"])
        self.assertTrue(claim["discriminating"]["separated"])
        values = claim["discriminating"]["discriminating-values"][2]
        # The second medium of the German release has an extra Disc ID.
        self.assertEqual(2, len(values))

    def testCandidateNotMatchingIsExcluded(self):
        result = self.load("xp5tz6rE4OHrBafj0bLfDRMGK48-.xml")
        other = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        releases = (result["disc"]["release-list"]
                    + other["disc"]["release-list"])
        claim = toc.claim_whole_box(discid_result=result, releases=releases)
        self.assertEqual(3, len(claim["still-valid"]))
        self.assertEqual(2, len(claim["excluded"]))

    def testDataDiscIsExempt(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        releases = copy.deepcopy(result["disc"]["release-list"])
        medium = releases[0]["medium-list"][1]
        medium.pop("disc-list", None)
        medium.pop("track-list", None)
        medium["track-count"] = 0
        medium["data-track-list"] = [{"position": "1", "number": "1",
                                      "length": "120000"}]
        medium["data-track-count"] = 1
        claim = toc.claim_whole_box(discid_result=result, releases=releases)
        first = claim["release-claims"][0]
        self.assertEqual("confirmed", first["status"])
        self.assertEqual([2], first["data-disc-numbers"])
        self.assertEqual([], first["missing-disc-numbers"])

    def testMixedMediumStillNeedsAudioMatch(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        releases = copy.deepcopy(result["disc"]["release-list"])
        medium = releases[0]["medium-list"][0]
        medium["data-track-list"] = [{"position": "14", "number": "14",
                                      "length": "60000"}]
        medium["data-track-count"] = 1
        self.assertEqual("mixed", toc._medium_role(medium))
        claim = toc.claim_whole_box(discid_result=result, releases=releases)
        # The supplied disc still matches its audio track list.
        self.assertEqual("partial", claim["release-claims"][0]["status"])
        self.assertEqual(2, len(claim["still-valid"]))

        # A mixed medium whose audio geometry does not match is not claimed
        # by the supplied disc, even though it still has data tracks.
        releases[0]["medium-list"][0].pop("disc-list", None)
        releases[0]["medium-list"][0]["track-list"] = [
            {"position": "1", "length": "60000"},
            {"position": "2", "length": "60000"},
            {"position": "3", "length": "60000"},
            {"position": "4", "length": "60000"}]
        claim = toc.claim_whole_box(discid_result=result, releases=releases)
        self.assertEqual("excluded", claim["release-claims"][0]["status"])

    def testMediumOrderFollowsDeclaredDiscs(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        releases = copy.deepcopy(result["disc"]["release-list"])
        media = releases[0]["medium-list"]
        media[0], media[1] = media[1], media[0]
        media[0]["position"], media[1]["position"] = "1", "2"
        claim = toc.claim_whole_box(discid_result=result, releases=releases,
                                    same_box=[media[0]["disc-list"][0]])
        first = claim["release-claims"][0]
        self.assertEqual("confirmed", first["status"])
        # The supplied disc lies on medium 1 of the unswapped release.
        self.assertEqual(2, first["assignment"]["1"])

    def testReleasesWithDifferentMediumCounts(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        releases = copy.deepcopy(result["disc"]["release-list"])
        releases[0]["medium-list"] = [releases[0]["medium-list"][0]]
        claim = toc.claim_whole_box(discid_result=result, releases=releases)
        self.assertEqual("confirmed", claim["release-claims"][0]["status"])
        self.assertEqual("partial", claim["release-claims"][1]["status"])

    def testExtraDiscEliminatesCandidate(self):
        result = self.load("f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        releases = result["disc"]["release-list"]
        # Only the German release lists this second Disc ID.
        extra = releases[1]["medium-list"][1]["disc-list"][1]
        claim = toc.claim_whole_box(discid_result=result, same_box=[extra])
        statuses = dict((release["country"], release["status"])
                        for release in claim["release-claims"])
        self.assertEqual("excluded", statuses["AT"])
        self.assertEqual("confirmed", statuses["DE"])
        self.assertEqual([releases[1]["id"]], claim["confirmed"])

    def testPregapMustAgreeWhenRequired(self):
        result = self.load("xp5tz6rE4OHrBafj0bLfDRMGK48-.xml")
        release = copy.deepcopy(result["disc"]["release-list"][0])
        release["medium-list"][0]["pregap"] = {"position": "0",
                                               "number": "0",
                                               "length": "60000"}
        permissive = toc.claim_whole_box(discid_result=result,
                                         releases=[release])
        self.assertEqual("confirmed", permissive["release-claims"][0]["status"])
        self.assertTrue(permissive["release-claims"][0]["notes"])

        strict = toc.claim_whole_box(discid_result=result, releases=[release],
                                     require_pregap=True)
        self.assertEqual("excluded", strict["release-claims"][0]["status"])

    def testTrackLengthFallback(self):
        datadir = os.path.join(os.path.dirname(__file__), "data", "release")
        release = _common.open_and_parse_test_data(
            datadir, "8eb2b179-643d-3507-b64c-29fcc6745156-recordings.xml",
        )["release"]
        local = toc.medium_toc(release["medium-list"][0])
        claim = toc.claim_whole_box(toc=local, releases=[release])
        first = claim["release-claims"][0]
        self.assertEqual("confirmed", first["status"])
        self.assertTrue(any("track lengths" in note for note in first["notes"]))

    def testNoCandidates(self):
        result = self.load("xp5tz6rE4OHrBafj0bLfDRMGK48-.xml")
        claim = toc.claim_whole_box(discid_result=result, releases=[])
        self.assertEqual([], claim["release-claims"])
        self.assertEqual([], claim["discriminating"]["disc-numbers"])

    def testRequiresInput(self):
        self.assertRaises(ValueError, toc.claim_whole_box)


class DistinguishingHelperTest(unittest.TestCase):
    def testHelperReadsResult(self):
        datadir = os.path.join(os.path.dirname(__file__), "data", "discid")
        result = _common.open_and_parse_test_data(
            datadir, "f7agNZK1HMQ2WUWq9bwDymw9aHA-.xml")
        media = result["disc"]["release-list"][0]["medium-list"]
        claim = toc.claim_whole_box(discid_result=result,
                                    same_box=[media[1]["disc-list"][0]])
        self.assertEqual([2], toc.distinguishing_disc_numbers(claim))
