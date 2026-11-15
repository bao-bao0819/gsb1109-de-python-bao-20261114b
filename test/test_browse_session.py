import json
import threading
import unittest

try:
    from urlparse import urlsplit, parse_qs
except ImportError:
    from urllib.parse import urlsplit, parse_qs

import musicbrainzngs
from musicbrainzngs import compat
from musicbrainzngs.browse import BrowseSession, BrowseIncomplete

try:
    from urllib2 import OpenerDirector
except ImportError:
    from urllib.request import OpenerDirector

try:
    import io as StringIO
except ImportError:
    import StringIO


NS = 'xmlns="http://musicbrainz.org/ns/mmd-2.0#"'


def make_rows(prefix, count):
    """An ordered, mutable directory of (mbid, title) rows."""
    return [("%s-%03d" % (prefix, i), "title %d" % i)
            for i in range(count)]


def make_page_xml(entity, rows, offset, limit):
    total = len(rows)
    page = rows[offset:offset + limit]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<metadata %s>' % NS,
           '<%s-list count="%d" offset="%d">' % (entity, total, offset)]
    for mbid, title in page:
        out.append('<%s id="%s"><title>%s</title></%s>'
                   % (entity, mbid, title, entity))
    out.append('</%s-list></metadata>' % entity)
    return "".join(out)


class MutableBrowseOpener(OpenerDirector):
    """Serves pages out of a mutable row list so tests can mutate the
    'live' directory between requests."""

    def __init__(self, entity, rows):
        self.entity = entity
        self.rows = rows
        self.urls = []
        self.request_count = 0
        self.exception = None
        self.before_request = None
        self.handlers = []

    def open(self, request, body=None):
        self.request_count += 1
        if self.before_request is not None:
            self.before_request(self)
        if self.exception is not None:
            raise self.exception
        url = request.get_full_url()
        self.urls.append(url)
        query = parse_qs(urlsplit(url).query)
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["25"])[0])
        xml = make_page_xml(self.entity, self.rows, offset, limit)
        return StringIO.StringIO(xml)


class BrowseSessionTest(unittest.TestCase):

    def setUp(self):
        self.rows = make_rows("rel", 250)
        self.opener = MutableBrowseOpener("release", self.rows)
        musicbrainzngs.compat.build_opener = lambda *args: self.opener
        musicbrainzngs.set_useragent("a", "1")
        musicbrainzngs.set_rate_limit(False)

    def tearDown(self):
        musicbrainzngs.set_rate_limit(True)

    def make_session(self, **kwargs):
        kwargs.setdefault("max_rechecks", 3)
        kwargs.setdefault("limit", 100)
        return BrowseSession("release", artist="artist-1", **kwargs)

    def test_stable_directory_completes_after_one_verify(self):
        session = self.make_session()
        result = session.collect()

        self.assertTrue(result["complete"])
        self.assertEqual(250, result["release-count"])
        self.assertEqual(250, len(result["release-list"]))
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(0, result["rechecks"])
        self.assertIsNone(result["unstable-boundary"])
        self.assertTrue(all(c == 250 for c in result["observed-counts"]))
        # Sweep (3 pages) plus one full verify pass (3 pages).
        self.assertEqual(6, self.opener.request_count)
        # Every page boundary has a recorded witness.
        self.assertEqual(set([0, 100, 200]),
                         set(session.boundary_witnesses.keys()))

    def test_request_url_uses_browse_arguments(self):
        self.make_session().collect()
        self.assertIn("artist=artist-1", self.opener.urls[0])
        self.assertIn("limit=100", self.opener.urls[0])
        # offset=0 is omitted by the underlying browse implementation.
        self.assertNotIn("offset", self.opener.urls[0])
        self.assertIn("offset=100", self.opener.urls[1])
        self.assertIn("offset=200", self.opener.urls[2])

    def test_front_insert_drifts_boundary_and_converges(self):
        def insert_on_second_request(opener):
            if opener.request_count == 2:
                opener.rows.insert(0, ("rel-new", "newly entered release"))
        self.opener.before_request = insert_on_second_request

        session = self.make_session()
        result = session.collect()

        self.assertTrue(result["complete"])
        self.assertEqual(251, result["release-count"])
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(251, len(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("rel-new", ids)
        # The drift forced exactly one extra re-walk.
        self.assertEqual(1, result["rechecks"])

    def test_callback_receives_every_entity_once(self):
        def insert_on_second_request(opener):
            if opener.request_count == 2:
                opener.rows.insert(0, ("rel-new", "newly entered release"))
        self.opener.before_request = insert_on_second_request

        delivered = []
        result = self.make_session().collect(
            on_new_entities=lambda entities: delivered.extend(entities))

        callback_ids = [e["id"] for e in delivered]
        final_ids = [e["id"] for e in result["release-list"]]
        self.assertEqual(len(callback_ids), len(set(callback_ids)))
        self.assertEqual(sorted(final_ids), sorted(callback_ids))

    def test_deletion_during_scan_keeps_confirmed_entity(self):
        def delete_after_first_page(opener):
            if opener.request_count == 2:
                del opener.rows[0]
        self.opener.before_request = delete_after_first_page

        session = self.make_session()
        result = session.collect()

        # The directory settles at 249 entities.
        self.assertTrue(result["complete"])
        self.assertEqual(249, result["release-count"])
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(len(ids), len(set(ids)))
        # The entity confirmed on the first page is retained even though the
        # server later removed it.
        self.assertEqual(250, len(ids))
        self.assertIn("rel-000", ids)

    def test_unstable_directory_raises_incomplete_with_confirmed_set(self):
        def insert_every_request(opener):
            opener.rows.insert(0, (
                "rel-drift-%03d" % opener.request_count, "drift"))
        self.opener.before_request = insert_every_request

        session = self.make_session(max_rechecks=1)
        with self.assertRaises(BrowseIncomplete) as ctx:
            session.collect()

        result = ctx.exception.result
        self.assertFalse(result["complete"])
        self.assertIsNotNone(result["unstable-boundary"])
        self.assertGreater(len(result["release-list"]), 0)
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(len(ids), len(set(ids)))
        # The totals kept moving, so nothing could be proven complete.
        self.assertTrue(len(set(result["observed-counts"])) > 1)

    def test_retry_revives_incomplete_session(self):
        def insert_every_request(opener):
            opener.rows.insert(0, (
                "rel-drift-%03d" % opener.request_count, "drift"))
        self.opener.before_request = insert_every_request

        session = self.make_session(max_rechecks=0)
        with self.assertRaises(BrowseIncomplete):
            session.collect()
        self.assertEqual(BrowseSession.PHASE_INCOMPLETE, session.phase)

        # The directory settles; give the session a fresh budget.
        self.opener.before_request = None
        session.retry(max_rechecks=2)
        result = session.collect()
        self.assertTrue(result["complete"])
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cancel_event_interrupts_and_resumes(self):
        session = self.make_session()
        partial = session.collect(
            cancel_event=lambda: self.opener.request_count >= 2)

        self.assertTrue(partial["cancelled"])
        self.assertFalse(partial["complete"])
        self.assertLess(len(partial["release-list"]), 250)

        result = session.collect()
        self.assertTrue(result["complete"])
        self.assertEqual(250, len(result["release-list"]))
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_threading_event_cancels(self):
        event = threading.Event()
        event.set()
        session = self.make_session()
        result = session.collect(cancel_event=event)
        self.assertTrue(result["cancelled"])
        self.assertEqual(0, len(result["release-list"]))

    def test_session_cancel_method(self):
        session = self.make_session()
        session.cancel()
        result = session.collect()
        self.assertTrue(result["cancelled"])
        self.assertEqual(0, len(result["release-list"]))

    def test_network_error_is_resumable_without_duplicates(self):
        session = self.make_session()
        self.opener.exception = compat.URLError("boom")
        with self.assertRaises(musicbrainzngs.NetworkError):
            session.collect()

        self.opener.exception = None
        result = session.collect()
        self.assertTrue(result["complete"])
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(250, len(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_persistence_roundtrip_when_complete(self):
        session = self.make_session()
        result = session.collect()
        restored = BrowseSession.from_dict(
            json.loads(json.dumps(session.to_dict())))

        self.assertTrue(restored.complete)
        self.assertEqual(result, restored.result())

    def test_persistence_resumes_mid_scan(self):
        session = self.make_session()
        session.collect(cancel_event=lambda: self.opener.request_count >= 2)
        self.assertFalse(session.complete)

        restored = BrowseSession.from_dict(
            json.loads(json.dumps(session.to_dict())))
        result = restored.collect()
        self.assertTrue(result["complete"])
        ids = [r["id"] for r in result["release-list"]]
        self.assertEqual(250, len(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_directory(self):
        self.rows[:] = []
        result = self.make_session().collect()
        self.assertTrue(result["complete"])
        self.assertEqual(0, result["release-count"])
        self.assertEqual([], result["release-list"])

    def test_validation(self):
        with self.assertRaises(musicbrainzngs.UsageError):
            BrowseSession("not-an-entity", artist="x")
        with self.assertRaises(musicbrainzngs.UsageError):
            BrowseSession("release", artist="x", max_rechecks=-1)
        with self.assertRaises(musicbrainzngs.UsageError):
            BrowseSession("release", artist="x", limit=0)
        with self.assertRaises(musicbrainzngs.UsageError):
            BrowseSession("release")
        with self.assertRaises(musicbrainzngs.UsageError):
            BrowseSession("release", artist="a", label="b")
        with self.assertRaises(musicbrainzngs.UsageError):
            BrowseSession("release", area="not-a-valid-link")

    def test_underscore_keyword_becomes_wire_parameter(self):
        BrowseSession("release", release_group="rg-1").collect()
        self.assertIn("release-group=rg-1", self.opener.urls[0])

    def test_factory_and_other_entity_type(self):
        rows = make_rows("work", 30)
        opener = MutableBrowseOpener("work", rows)
        musicbrainzngs.compat.build_opener = lambda *args: opener

        session = musicbrainzngs.browse_session(
            "work", artist="artist-1", limit=25, max_rechecks=1)
        result = session.collect()

        self.assertTrue(result["complete"])
        self.assertEqual(30, result["work-count"])
        self.assertIn("artist=artist-1", opener.urls[0])

    def test_finished_session_cannot_fetch_again(self):
        session = self.make_session()
        session.collect()
        with self.assertRaises(musicbrainzngs.UsageError):
            session.fetch_page()
