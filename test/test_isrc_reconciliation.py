# Tests for ISRC reconciliation.

import copy
import os
import unittest

import musicbrainzngs
from musicbrainzngs import isrc_reconciliation as recon
from test import _common


R1 = "11111111-1111-1111-1111-111111111111"
R2 = "22222222-2222-2222-2222-222222222222"
R3 = "33333333-3333-3333-3333-333333333333"
R4 = "44444444-4444-4444-4444-444444444444"
R5 = "55555555-5555-5555-5555-555555555555"


class IsrcQueryTest(unittest.TestCase):
    def setUp(self):
        self.datadir = os.path.join(os.path.dirname(__file__), "data", "isrc")
        self.result = _common.open_and_parse_test_data(
            self.datadir, "USAAA0000001.xml")

    def testParsedIsrcKeepsManyToMany(self):
        node = self.result["isrc"]
        self.assertEqual("USAAA0000001", node["id"])
        self.assertEqual(5, len(node["recording-list"]))
        self.assertEqual(R1, node["recording-list"][0]["id"])
        self.assertEqual("213000", node["recording-list"][0]["length"])
        self.assertEqual(
            "Signal Song (live)", node["recording-list"][2]["title"])

    def testRawRelationshipsSurviveResolution(self):
        raw = copy.deepcopy(self.result)
        resolved = recon.reconcile_isrc("USAAA0000001", self.result)
        self.assertEqual(raw, self.result)
        self.assertEqual([R1, R2, R3, R4, R5],
                         resolved["raw"]["recording-ids"])
        self.assertEqual(raw, resolved["raw"]["response"])
        self.assertEqual(
            [{"isrc": "USAAA0000001", "recording-id": rid}
             for rid in (R1, R2, R3, R4, R5)],
            resolved["raw"]["links"])

    def testObservationsIncludeDurationsAndArtists(self):
        reconciler = recon.IsrcReconciler(
            isrc="USAAA0000001", query_result=self.result)
        reconciler.add_observations(
            recon.observations_from_query_result(self.result, "musicbrainz"))
        resolved = reconciler.resolve()
        observations = resolved["observations"][R3]
        self.assertEqual("musicbrainz", observations[0]["source"])
        self.assertEqual(305000, observations[0]["length"])
        self.assertEqual(["Alice"], observations[0]["artists"])
        # Different durations and credits are evidence only, not identity.
        self.assertEqual("separate", resolved["recording-states"][R4])

    def testConflictingSourceMetadataIsRecordedForReview(self):
        reconciler = recon.IsrcReconciler(
            isrc="USAAA0000001", query_result=self.result)
        reconciler.add_observations(
            recon.observations_from_query_result(self.result, "musicbrainz"))
        reconciler.add_observation(R3, "label-feed", length=301500,
                                   artists=["Alice", "Guest"],
                                   title="Signal Song (live)")
        resolved = reconciler.resolve()
        summary = resolved["observation-summary"][R3]
        self.assertEqual(["label-feed", "musicbrainz"], summary["sources"])
        self.assertEqual([301500, 305000], summary["lengths"])
        self.assertEqual([["Alice"], ["Alice", "Guest"]],
                         summary["artist-credits"])
        diffs = dict((item["field"], item) for item in
                     resolved["observation-diffs"])
        self.assertEqual("length", diffs["length"]["field"])
        self.assertEqual("artists", diffs["artists"]["field"])
        self.assertEqual(
            [{"source": "musicbrainz", "length": 305000},
             {"source": "label-feed", "length": 301500}],
            diffs["length"]["values-by-source"])


class AliasConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.result = _common.open_and_parse_test_data(
            os.path.join(os.path.dirname(__file__), "data", "isrc"),
            "USAAA0000001.xml")

    def resolve(self, confirmations, observations=None):
        return recon.reconcile_isrc("USAAA0000001", self.result,
                                    observations=observations,
                                    confirmations=confirmations)

    def testAliasIsTransitive(self):
        resolved = self.resolve([
            (R1, R2, recon.ALIAS),
            (R2, R3, recon.ALIAS),
        ])
        self.assertEqual([R1, R2, R3], resolved["merged-groups"][0]["recording-ids"])
        self.assertEqual(
            [[R1, R2], [R2, R3]],
            resolved["merged-groups"][0]["alias-chains"][-1]["edges"])
        self.assertEqual(
            [R1, R2, R3],
            resolved["merged-groups"][0]["alias-chains"][-1]["recording-ids"])
        self.assertEqual("merged", resolved["recording-states"][R3])

    def testAliasChainsAreReportedForReview(self):
        resolved = self.resolve([
            (R1, R2, recon.ALIAS),
            (R2, R3, recon.ALIAS),
        ])
        group = resolved["merged-groups"][0]
        self.assertEqual([R1, R2, R3], group["alias-groups"][0])
        self.assertIn(
            [[R1, R2], [R2, R3]],
            [chain["edges"] for chain in group["alias-chains"]])
        self.assertEqual(
            {"type": "alias", "recording-ids": [R1, R2, R3]}, group["basis"][0])

    def testSamePairCannotBeAliasAndExclusive(self):
        with self.assertRaises(recon.ConfirmationConflict):
            self.resolve([
                (R1, R2, recon.ALIAS),
                (R2, R1, recon.MUTUALLY_EXCLUSIVE),
            ])

    def testUnknownStatusIsRejected(self):
        with self.assertRaises(recon.IsrcReconciliationError):
            self.resolve([(R1, R2, "maybe")])


class CompatibleConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.result = _common.open_and_parse_test_data(
            os.path.join(os.path.dirname(__file__), "data", "isrc"),
            "USAAA0000001.xml")

    def resolve(self, confirmations):
        return recon.reconcile_isrc("USAAA0000001", self.result,
                                    confirmations=confirmations)

    def testCompatibleMergesConfirmedPair(self):
        resolved = self.resolve([(R1, R2, recon.COMPATIBLE)])
        self.assertEqual([R1, R2],
                         resolved["merged-groups"][0]["recording-ids"])
        self.assertEqual(
            [{"type": "compatible", "recording-ids": [R1, R2]}],
            resolved["merged-groups"][0]["basis"])

    def testCompatibleIsNotTransitive(self):
        resolved = self.resolve([
            (R1, R2, recon.COMPATIBLE),
            (R2, R3, recon.COMPATIBLE),
        ])
        self.assertEqual([R1, R2],
                         resolved["merged-groups"][0]["recording-ids"])
        self.assertEqual("separate", resolved["recording-states"][R3])
        review = resolved["review-items"][0]
        self.assertEqual(recon.REASON_COMPATIBLE_CHAIN, review["reason"])
        self.assertEqual([R2, R3], review["compatible-edge"])
        self.assertEqual([[R1, R2], [R2, R3]], review["compatible-edges"])

    def testAliasCanCarryACompatibleMerge(self):
        resolved = self.resolve([
            (R1, R2, recon.ALIAS),
            (R2, R3, recon.COMPATIBLE),
            (R3, R4, recon.ALIAS),
        ])
        self.assertEqual([R1, R2, R3, R4],
                         resolved["merged-groups"][0]["recording-ids"])
        basis = resolved["merged-groups"][0]["basis"]
        self.assertEqual(
            [("alias", [R1, R2]), ("alias", [R3, R4]),
             ("compatible", [R2, R3])],
            [(item["type"], item["recording-ids"]) for item in basis])


class ExclusionTest(unittest.TestCase):
    def setUp(self):
        self.result = _common.open_and_parse_test_data(
            os.path.join(os.path.dirname(__file__), "data", "isrc"),
            "USAAA0000001.xml")

    def resolve(self, confirmations):
        return recon.reconcile_isrc("USAAA0000001", self.result,
                                    confirmations=confirmations)

    def testExclusionQuarantinesBothSidesOnly(self):
        resolved = self.resolve([(R3, R4, recon.MUTUALLY_EXCLUSIVE)])
        self.assertEqual([R3, R4], resolved["quarantined-recording-ids"])
        self.assertEqual("quarantined", resolved["recording-states"][R3])
        # An unrelated recording sharing an artist name and title family
        # must not be swept into quarantine.
        self.assertEqual("separate", resolved["recording-states"][R1])
        self.assertEqual("separate", resolved["recording-states"][R2])
        self.assertEqual("separate", resolved["recording-states"][R5])

    def testSharedArtistNameDoesNotDriveQuarantine(self):
        resolved = self.resolve([(R3, R4, recon.MUTUALLY_EXCLUSIVE)])
        review = resolved["review-items"][0]
        self.assertEqual(recon.REASON_EXPLICIT_EXCLUSION, review["reason"])
        self.assertEqual([R3, R4], review["recording-ids"])
        self.assertEqual(2, len(resolved["quarantine-groups"]))
        self.assertEqual([R3], resolved["quarantine-groups"][0]["recording-ids"])
        self.assertEqual([R4], resolved["quarantine-groups"][1]["recording-ids"])

    def testMergeAcrossExclusionNamesAliasChains(self):
        resolved = self.resolve([
            (R1, R2, recon.ALIAS),
            (R3, R4, recon.ALIAS),
            (R2, R3, recon.MUTUALLY_EXCLUSIVE),
            (R1, R4, recon.COMPATIBLE),
        ])
        self.assertEqual([R1, R2, R3, R4],
                         resolved["quarantined-recording-ids"])
        self.assertEqual([], resolved["merged-groups"])
        review = [item for item in resolved["review-items"]
                  if item["reason"] == recon.REASON_MERGE_EXCLUSION][0]
        self.assertEqual([R1, R4], review["compatible-edge"])
        self.assertEqual([R2, R3], review["exclusion"])
        chain_ids = [chain["recording-ids"] for chain in review["alias-chains"]]
        self.assertEqual([[R1, R2], [R4, R3]], chain_ids)

    def testExclusionInsideAliasChainIsReported(self):
        resolved = self.resolve([
            (R1, R2, recon.ALIAS),
            (R2, R3, recon.ALIAS),
            (R1, R3, recon.MUTUALLY_EXCLUSIVE),
        ])
        self.assertEqual([R1, R2, R3], resolved["quarantined-recording-ids"])
        self.assertEqual("quarantined", resolved["recording-states"][R2])
        review = resolved["review-items"][0]
        self.assertEqual(recon.REASON_ALIAS_EXCLUSION, review["reason"])
        self.assertEqual(
            [[R1, R2], [R2, R3]],
            review["alias-chains"][0]["edges"])
        self.assertEqual(review["alias-chains"],
                         resolved["quarantine-groups"][0]["conflict-chains"])

    def testCompatibleIntoQuarantineDoesNotQuarantineOtherSide(self):
        resolved = self.resolve([
            (R3, R4, recon.MUTUALLY_EXCLUSIVE),
            (R1, R3, recon.COMPATIBLE),
        ])
        self.assertEqual("quarantined", resolved["recording-states"][R3])
        self.assertEqual("separate", resolved["recording-states"][R1])
        review = [item for item in resolved["review-items"]
                  if item["reason"] == recon.REASON_MERGE_QUARANTINE][0]
        self.assertEqual([R1, R3], review["compatible-edge"])


class ReconcilerApiTest(unittest.TestCase):
    def testRawQueryIsNotMutated(self):
        result = {"isrc": {"id": "I", "recording-list": [{"id": "a"}]}}
        snapshot = copy.deepcopy(result)
        reconciler = recon.IsrcReconciler(isrc="I", query_result=result)
        reconciler.confirm_alias("a", "b")
        reconciler.resolve()
        self.assertEqual(snapshot, result)

    def testIsrcMismatchIsRejected(self):
        result = {"isrc": {"id": "I", "recording-list": []}}
        with self.assertRaises(recon.IsrcReconciliationError):
            recon.IsrcReconciler(isrc="J", query_result=result)

    def testPublicHelpersAreExported(self):
        self.assertIs(getattr(musicbrainzngs, "reconcile_isrc", None),
                      recon.reconcile_isrc)
