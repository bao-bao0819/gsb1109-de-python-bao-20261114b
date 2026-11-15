import unittest

import musicbrainzngs
from musicbrainzngs.sync import SyncArchive


OLD_RG = "11111111-1111-1111-1111-111111111111"
NEW_RG = "22222222-2222-2222-2222-222222222222"
OTHER_RG = "33333333-3333-3333-3333-333333333333"

ARTIST_REL = {"type": "composer", "target": "aaaaaaaa-0000-0000-0000-000000000000"}
URL_REL = {"type": "official homepage", "target": "http://example.com"}


def rg_response(mbid, artist_rels=None, url_rels=None):
    """Build a parsed release-group lookup response."""
    rg = {"id": mbid, "title": "A Release Group"}
    if artist_rels is not None:
        rg["artist-relation-list"] = artist_rels
    if url_rels is not None:
        rg["url-relation-list"] = url_rels
    return {"release-group": rg}


class RedirectTest(unittest.TestCase):
    """A merged release group keeps its history reachable via the new
    MBID while records remember the requested MBID."""

    def setUp(self):
        self.archive = SyncArchive()

    def test_history_follows_redirect(self):
        self.archive.ingest("release-group", OLD_RG,
                            rg_response(NEW_RG, artist_rels=[ARTIST_REL]),
                            includes=["artist-rels"], fetched_at=1)

        history = self.archive.history("release-group", NEW_RG)
        self.assertEqual(1, len(history))
        self.assertEqual(OLD_RG, history[0].requested_id)
        self.assertEqual(NEW_RG, history[0].resolved_id)

        # the old MBID resolves to the new one and finds the same history
        self.assertEqual(NEW_RG, self.archive.resolve("release-group", OLD_RG))
        self.assertEqual(history, self.archive.history("release-group", OLD_RG))
        self.assertEqual(set([OLD_RG, NEW_RG]),
                         self.archive.aliases("release-group", NEW_RG))

    def test_current_view_shared_by_old_and_new_id(self):
        self.archive.ingest("release-group", OLD_RG,
                            rg_response(NEW_RG, artist_rels=[ARTIST_REL]),
                            includes=["artist-rels"], fetched_at=1)
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG, url_rels=[URL_REL]),
                            includes=["url-rels"], fetched_at=2)

        expected = {"artist-relation-list": [ARTIST_REL],
                    "url-relation-list": [URL_REL]}
        self.assertEqual(expected, self.archive.get_current("release-group", NEW_RG))
        self.assertEqual(expected, self.archive.get_current("release-group", OLD_RG))


class PartialScopeTest(unittest.TestCase):
    """A lookup only replaces the relation scopes it requested."""

    def setUp(self):
        self.archive = SyncArchive()

    def test_unrequested_scopes_are_kept(self):
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG, artist_rels=[ARTIST_REL]),
                            includes=["artist-rels"], fetched_at=1)
        # a later lookup only expands url relations
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG, url_rels=[URL_REL]),
                            includes=["url-rels"], fetched_at=2)

        current = self.archive.get_current("release-group", NEW_RG)
        self.assertEqual([ARTIST_REL], current["artist-relation-list"])
        self.assertEqual([URL_REL], current["url-relation-list"])

    def test_explicit_empty_list_clears_only_that_scope(self):
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG, artist_rels=[ARTIST_REL],
                                        url_rels=[URL_REL]),
                            includes=["artist-rels", "url-rels"],
                            fetched_at=1)
        # url-rels explicitly fetched again, now empty
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG),
                            includes=["url-rels"], fetched_at=2)

        current = self.archive.get_current("release-group", NEW_RG)
        self.assertEqual([], current["url-relation-list"])
        self.assertEqual([ARTIST_REL], current["artist-relation-list"])


class RedirectCycleTest(unittest.TestCase):
    """Redirects forming a cycle are left for review; other entities
    update as usual."""

    def setUp(self):
        self.archive = SyncArchive()
        self.archive.ingest("release-group", OLD_RG,
                            rg_response(NEW_RG, artist_rels=[ARTIST_REL]),
                            includes=["artist-rels"], fetched_at=1)
        # the second redirect closes a cycle NEW -> OLD -> NEW
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(OLD_RG, artist_rels=[]),
                            includes=["artist-rels"], fetched_at=2)

    def test_cycle_members_are_queued_for_review(self):
        queue = self.archive.review_queue()
        self.assertEqual([("release-group", OLD_RG, "redirect cycle"),
                          ("release-group", NEW_RG, "redirect cycle")],
                         queue)

    def test_cycle_members_are_not_updated(self):
        # the empty list from the cyclic response must not be applied
        current = self.archive.get_current("release-group", NEW_RG)
        self.assertEqual([ARTIST_REL], current["artist-relation-list"])
        # the closing redirect is not recorded
        self.assertEqual(NEW_RG, self.archive.resolve("release-group", OLD_RG))

    def test_other_entities_update_normally(self):
        self.archive.ingest("release-group", OTHER_RG,
                            rg_response(OTHER_RG, url_rels=[URL_REL]),
                            includes=["url-rels"], fetched_at=3)
        current = self.archive.get_current("release-group", OTHER_RG)
        self.assertEqual([URL_REL], current["url-relation-list"])
        self.assertNotIn(("release-group", OTHER_RG, "redirect cycle"),
                         self.archive.review_queue())

    def test_history_is_kept_for_review(self):
        history = self.archive.history("release-group", NEW_RG)
        self.assertEqual(2, len(history))
        self.assertEqual(OLD_RG, history[1].resolved_id)

    def test_clear_review_rebuilds_view_from_history(self):
        self.archive.clear_review("release-group", OLD_RG)
        self.archive.clear_review("release-group", NEW_RG)
        current = self.archive.get_current("release-group", NEW_RG)
        self.assertEqual([], current["artist-relation-list"])
        self.assertEqual([], self.archive.review_queue())


class ReplayTest(unittest.TestCase):
    """Replaying archived responses reproduces the view of that time."""

    def setUp(self):
        self.archive = SyncArchive()
        other_rel = {"type": "lyricist",
                     "target": "bbbbbbbb-0000-0000-0000-000000000000"}
        self.archive.ingest("release-group", OLD_RG,
                            rg_response(NEW_RG, artist_rels=[ARTIST_REL]),
                            includes=["artist-rels"], fetched_at=10)
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG, url_rels=[URL_REL]),
                            includes=["url-rels"], fetched_at=20)
        self.archive.ingest("release-group", NEW_RG,
                            rg_response(NEW_RG, artist_rels=[other_rel]),
                            includes=["artist-rels"], fetched_at=30)

    def test_replay_to_latest_matches_current(self):
        self.assertEqual(self.archive.get_current("release-group", NEW_RG),
                         self.archive.replay("release-group", NEW_RG))

    def test_replay_at_a_point_in_time(self):
        view = self.archive.replay("release-group", NEW_RG, at=20)
        self.assertEqual([ARTIST_REL], view["artist-relation-list"])
        self.assertEqual([URL_REL], view["url-relation-list"])

        view = self.archive.replay("release-group", NEW_RG, at=10)
        self.assertEqual([ARTIST_REL], view["artist-relation-list"])
        self.assertNotIn("url-relation-list", view)

    def test_replay_single_record(self):
        record = self.archive.history("release-group", NEW_RG)[0]
        view = self.archive.replay_record(record)
        self.assertEqual({"artist-relation-list": [ARTIST_REL]}, view)


class IngestTest(unittest.TestCase):

    def test_ingest_raw_xml(self):
        xml = """<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
          <release-group id="{new}" type="Album">
            <title>Test</title>
            <relation-list target-type="url">
              <relation type="official homepage">
                <target>http://example.com</target>
              </relation>
            </relation-list>
          </release-group>
        </metadata>""".format(new=NEW_RG)
        archive = SyncArchive()
        record = archive.ingest("release-group", OLD_RG, xml,
                                includes=["url-rels"], fetched_at=1)
        self.assertEqual(OLD_RG, record.requested_id)
        self.assertEqual(NEW_RG, record.resolved_id)
        current = archive.get_current("release-group", NEW_RG)
        self.assertEqual("official homepage",
                         current["url-relation-list"][0]["type"])

    def test_missing_entity_in_response(self):
        archive = SyncArchive()
        self.assertRaises(ValueError, archive.ingest,
                          "release-group", NEW_RG, {"artist": {"id": "x"}})

    def test_exported_from_package(self):
        self.assertIs(SyncArchive, musicbrainzngs.SyncArchive)


if __name__ == "__main__":
    unittest.main()
