# Tests for the auditable recording report

import json
import os
import unittest

from musicbrainzngs import mbxml
from musicbrainzngs import report
from test import _common


class ReportFixtureTest(unittest.TestCase):
    def setUp(self):
        self.datadir = os.path.join(
            os.path.dirname(__file__), "data", "report"
        )

    def _fixture(self, filename):
        with open(os.path.join(self.datadir, filename), "rb") as handle:
            return mbxml.parse_message(handle)

    def test_single_counts(self):
        single = report.build_recording_report(
            self._fixture("single.xml")
        )
        self.assertEqual(2, single["track_position_count"])
        self.assertEqual(2, single["independent_recording_count"])
        self.assertEqual(0, single["evidenced_remaster_count"])

    def test_original_album_counts(self):
        album = report.build_recording_report(
            self._fixture("original-album.xml")
        )
        self.assertEqual(3, album["track_position_count"])
        self.assertEqual(3, album["independent_recording_count"])
        self.assertEqual(0, album["evidenced_remaster_count"])

    def test_remaster_compilation_keeps_both_media_positions(self):
        compilation = report.build_recording_report(
            self._fixture("remaster-compilation.xml")
        )
        release_id = "11111111-0000-4000-8000-000000000003"
        positions = [
            position for position in compilation["positions"]
            if position["release_id"] == release_id
        ]
        self.assertEqual(8, len(positions))
        self.assertEqual(
            ["1", "2"],
            sorted(set(position["medium_position"] for position in positions)),
        )
        reissued = [
            position for position in positions
            if position["recording_id"]
            == "10000000-0000-4000-8000-000000000001"
        ]
        self.assertEqual(2, len(reissued))
        self.assertEqual(
            [("1", "1"), ("2", "4")],
            [
                (position["medium_position"], position["track_position"])
                for position in reissued
            ],
        )
        self.assertEqual(8, compilation["track_position_count"])
        self.assertEqual(7, compilation["independent_recording_count"])
        self.assertEqual(0, compilation["evidenced_remaster_count"])

    def test_title_suffix_is_not_remaster_evidence(self):
        compilation = report.build_recording_report(
            self._fixture("remaster-compilation.xml")
        )
        hinted = [
            recording for recording in compilation["recordings"]
            if recording["title_hint"]
        ]
        self.assertEqual(4, len(hinted))
        for recording in hinted:
            self.assertEqual(
                "insufficient_evidence", recording["evidence_status"]
            )
        self.assertEqual(4, compilation["title_hint_only_count"])
        self.assertEqual(0, compilation["evidenced_remaster_count"])

    def test_cross_release_reuse_does_not_inflate_recording_count(self):
        combined = report.build_recording_report(
            [
                self._fixture("single.xml"),
                self._fixture("original-album.xml"),
                self._fixture("remaster-compilation.xml"),
            ]
        )
        self.assertEqual(13, combined["track_position_count"])
        self.assertEqual(7, combined["independent_recording_count"])
        self.assertEqual(0, combined["evidenced_remaster_count"])
        self.assertEqual(
            [
                "11111111-0000-4000-8000-000000000002",
                "11111111-0000-4000-8000-000000000001",
                "11111111-0000-4000-8000-000000000003",
            ],
            combined["release_ids"],
        )

    def test_formal_relations_add_remaster_evidence(self):
        releases = [
            self._fixture("single.xml"),
            self._fixture("original-album.xml"),
            self._fixture("remaster-compilation.xml"),
        ]
        without = report.build_recording_report(releases)
        with_relations = report.build_recording_report(
            releases,
            relations=self._fixture("remaster-relations.xml"),
        )

        self.assertEqual(
            {
                "track_position_count": 13,
                "independent_recording_count": 7,
                "evidenced_remaster_count": 3,
            },
            with_relations.counts(),
        )
        self.assertEqual(3, len(with_relations["remasters"]))
        remastered = set(
            remaster["recording_id"]
            for remaster in with_relations["remasters"]
        )
        self.assertEqual(
            set([
                "10000000-0000-4000-8000-000000000004",
                "10000000-0000-4000-8000-000000000005",
                "10000000-0000-4000-8000-000000000006",
            ]),
            remastered,
        )
        self.assertEqual(
            "evidenced_original",
            self._recording(with_relations,
                            "10000000-0000-4000-8000-000000000001")[
                "evidence_status"
            ],
        )

        comparison = report.compare_reports(without, with_relations)
        self.assertEqual(["evidenced_remaster_count"],
                         comparison.changed_counts())
        self.assertEqual(
            [
                "track_position_count",
                "independent_recording_count",
            ],
            comparison.unchanged_counts(),
        )
        self.assertEqual(3,
                         comparison["evidenced_remaster_count"].delta)
        self.assertEqual(0,
                         comparison["track_position_count"].delta)
        self.assertEqual(0,
                         comparison["independent_recording_count"].delta)
        self.assertEqual(13,
                         comparison["track_position_count"].before)
        self.assertEqual(13,
                         comparison["track_position_count"].after)

    def test_with_relations_replays_comparison(self):
        releases = [
            self._fixture("single.xml"),
            self._fixture("original-album.xml"),
            self._fixture("remaster-compilation.xml"),
        ]
        without = report.build_recording_report(releases)
        after = without.with_relations(
            self._fixture("remaster-relations.xml")
        )
        self.assertEqual(3, after["evidenced_remaster_count"])
        self.assertEqual(
            ["evidenced_remaster_count"],
            after.changes_from(without).changed_counts(),
        )
        self.assertEqual(
            0, after.changes_from(without)["track_position_count"]
        )

    def test_relation_mapping_and_pairs_forms_agree(self):
        fixture = self._fixture("remaster-relations.xml")
        xml_relations = report.build_recording_report(
            self._fixture("remaster-compilation.xml"),
            relations=fixture,
        )
        mapping = {
            recording["id"]: recording["recording-relation-list"]
            for recording in fixture["recording-list"]
        }
        pairs = [
            (recording_id, relation)
            for recording_id, relations in mapping.items()
            for relation in relations
        ]
        from_mapping = report.build_recording_report(
            self._fixture("remaster-compilation.xml"),
            relations=mapping,
        )
        from_pairs = report.build_recording_report(
            self._fixture("remaster-compilation.xml"),
            relations=pairs,
        )
        self.assertEqual(xml_relations.counts(), from_mapping.counts())
        self.assertEqual(xml_relations.counts(), from_pairs.counts())

    def test_embedded_relations_are_read_by_default(self):
        compilation = self._fixture("remaster-compilation.xml")
        relation_list = [
            {
                "type": "remaster",
                "target": "10000000-0000-4000-8000-000000000001",
                "direction": "forward",
            }
        ]
        compilation["release"]["medium-list"][1]["track-list"][0][
            "recording"
        ]["recording-relation-list"] = relation_list
        with_embedded = report.build_recording_report(compilation)
        without_embedded = report.build_recording_report(
            compilation, include_embedded_relations=False
        )
        self.assertEqual(1, with_embedded["evidenced_remaster_count"])
        self.assertEqual(0, without_embedded["evidenced_remaster_count"])

    def test_remaster_relation_without_direction_is_labelled(self):
        relation = {
            "type": "remaster",
            "target": "10000000-0000-4000-8000-000000000001",
        }
        built = report.build_recording_report(
            self._fixture("original-album.xml"),
            relations=[("10000000-0000-4000-8000-000000000004", relation)],
        )
        self.assertEqual(0, built["evidenced_remaster_count"])
        self.assertTrue(
            any("has no direction" in warning
                for warning in built["warnings"])
        )
        self.assertEqual(
            "evidenced_original",
            built["recordings"][0]["evidence_status"],
        )

    def test_reciprocal_relations_are_deduplicated(self):
        release = {
            "id": "release-1",
            "title": "Reciprocal",
            "medium-list": [{
                "position": "1",
                "track-list": [
                    {"position": "1", "recording": {"id": "rem"}},
                    {"position": "2", "recording": {"id": "orig"}},
                ],
            }],
        }
        built = report.build_recording_report(
            release,
            relations=[
                ("rem", {"type": "remaster", "target": "orig",
                         "direction": "forward"}),
                ("orig", {"type": "remaster", "target": "rem",
                          "direction": "backward"}),
            ],
        )
        self.assertEqual(1, len(built["relations"]))
        self.assertEqual(1, built["evidenced_remaster_count"])

    def test_irrelevant_relations_are_ignored(self):
        relation = {
            "type": "producer",
            "target": "10000000-0000-4000-8000-000000000002",
        }
        built = report.build_recording_report(
            self._fixture("original-album.xml"),
            relations=[("10000000-0000-4000-8000-000000000001", relation)],
        )
        self.assertEqual(0, built["evidenced_remaster_count"])
        self.assertEqual([], built["relations"])

    def test_pregap_and_data_tracks_are_positions(self):
        release = {
            "id": "release-1",
            "title": "Mixed media",
            "medium-list": [
                {
                    "position": "1",
                    "pregap": {
                        "position": "0",
                        "recording": {"id": "pregap-recording"},
                    },
                    "track-list": [
                        {
                            "position": "1",
                            "recording": {"id": "audio-recording"},
                        }
                    ],
                    "data-track-list": [
                        {
                            "position": "1",
                            "recording": {"id": "data-recording"},
                        }
                    ],
                }
            ],
        }
        built = report.build_recording_report(release)
        self.assertEqual(3, built["track_position_count"])
        self.assertEqual(3, built["independent_recording_count"])
        self.assertEqual(
            ["pregap", "audio", "data"],
            [position["source"] for position in built["positions"]],
        )

    def test_duplicate_medium_position_is_not_counted_twice(self):
        release = {
            "id": "release-1",
            "title": "Duplicate disc",
            "medium-list": [
                {"position": "1", "track-list": [
                    {"position": "1", "recording": {"id": "rec-a"}}
                ]},
                {"position": "1", "track-list": [
                    {"position": "1", "recording": {"id": "rec-a"}}
                ]},
            ],
        }
        built = report.build_recording_report(release)
        self.assertEqual(1, built["track_position_count"])
        self.assertEqual(1, built["independent_recording_count"])
        self.assertTrue(
            any("duplicate position" in warning
                for warning in built["warnings"])
        )

    def test_missing_recording_ids_are_visible(self):
        release = {
            "id": "release-1",
            "title": "Incomplete",
            "medium-list": [
                {"position": "1", "track-list": [{"position": "1"}]}
            ],
        }
        built = report.build_recording_report(release)
        self.assertEqual(1, built["track_position_count"])
        self.assertEqual(0, built["independent_recording_count"])
        self.assertEqual(1, built["positions_without_recording_count"])
        self.assertEqual("missing_recording_id",
                         built["recordings"][0]["evidence_status"])

    def test_report_serialises_with_evidence(self):
        built = report.build_recording_report(
            self._fixture("remaster-compilation.xml")
        )
        payload = json.loads(built.to_json())
        self.assertEqual(8, payload["track_position_count"])
        self.assertEqual(8, len(payload["positions"]))
        self.assertEqual(
            ["10000000-0000-4000-8000-000000000001",
             "10000000-0000-4000-8000-000000000004"],
            [
                recording["recording_id"]
                for recording in payload["recordings"]
                if recording["titles"] == ["Signal Fires"]
            ][:2],
        )

    def test_recording_response_releases_inherit_recording_id(self):
        response = {
            "recording": {
                "id": "remaster-recording",
                "title": "Signal Fires",
                "disambiguation": "2009 remaster",
                "recording-relation-list": [{
                    "type": "remaster",
                    "target": "original-recording",
                    "direction": "forward",
                }],
                "release-list": [{
                    "id": "deluxe-release",
                    "title": "Signal Fires (Remastered Deluxe Edition)",
                    "medium-list": [
                        {
                            "position": "1",
                            "track-list": [{"position": "1"}],
                        },
                        {
                            "position": "2",
                            "track-list": [{"position": "4"}],
                        },
                    ],
                }],
            }
        }
        built = report.build_recording_report(response)
        self.assertEqual(2, built["track_position_count"])
        self.assertEqual(1, built["independent_recording_count"])
        self.assertEqual(1, built["evidenced_remaster_count"])
        self.assertEqual(
            ["remaster-recording", "remaster-recording"],
            [position["recording_id"] for position in built["positions"]],
        )
        self.assertEqual(
            ["1", "2"],
            [position["medium_position"] for position in built["positions"]],
        )

    def test_two_release_tuple_is_not_mistaken_for_context(self):
        releases = (
            self._fixture("original-album.xml"),
            self._fixture("remaster-compilation.xml"),
        )
        built = report.build_recording_report(releases)
        self.assertEqual(11, built["track_position_count"])
        self.assertEqual(7, built["independent_recording_count"])

    def test_comparison_detailed_output(self):
        without = report.build_recording_report(
            self._fixture("remaster-compilation.xml")
        )
        with_relations = report.build_recording_report(
            self._fixture("remaster-compilation.xml"),
            relations=self._fixture("remaster-relations.xml"),
        )
        comparison = report.compare_reports(without, with_relations)
        detailed = comparison.detailed()
        self.assertEqual(
            {"before": 0, "after": 3, "delta": 3, "changed": True},
            detailed["evidenced_remaster_count"],
        )
        self.assertEqual(
            ["evidenced_remaster_count"], detailed["changed_counts"]
        )
        self.assertEqual(
            ["track_position_count", "independent_recording_count"],
            detailed["unchanged_counts"],
        )

    def test_format_changes_shows_unchanged_totals(self):
        without = report.build_recording_report(
            self._fixture("remaster-compilation.xml")
        )
        with_relations = report.build_recording_report(
            self._fixture("remaster-compilation.xml"),
            relations=self._fixture("remaster-relations.xml"),
        )
        lines = report.format_changes(
            report.compare_reports(without, with_relations)
        )
        self.assertEqual(
            [
                "track_position_count: 8 (unchanged)",
                "independent_recording_count: 7 (unchanged)",
                "evidenced_remaster_count: 0 -> 3 (+3)",
            ],
            lines,
        )

    def test_existing_multidisc_fixture_counts_every_track(self):
        filename = (
            "fe29e7f0-eb46-44ba-9348-694166f47885-recordings.xml"
        )
        parsed = _common.open_and_parse_test_data(
            os.path.join(os.path.dirname(__file__), "data", "release"),
            filename,
        )
        built = report.build_recording_report(parsed)
        self.assertEqual(48, built["track_position_count"])
        self.assertEqual(48, built["independent_recording_count"])

    def test_existing_data_track_fixture_counts_every_track(self):
        filename = (
            "9ce41d09-40e4-4d33-af0c-7fed1e558dba-recordings.xml"
        )
        parsed = _common.open_and_parse_test_data(
            os.path.join(os.path.dirname(__file__), "data", "release"),
            filename,
        )
        built = report.build_recording_report(parsed)
        self.assertEqual(198, built["track_position_count"])
        self.assertEqual("data", built["positions"][0]["source"])

    def _recording(self, built, recording_id):
        for recording in built["recordings"]:
            if recording["recording_id"] == recording_id:
                return recording
        self.fail("recording not found: %s" % recording_id)


if __name__ == "__main__":
    unittest.main()
