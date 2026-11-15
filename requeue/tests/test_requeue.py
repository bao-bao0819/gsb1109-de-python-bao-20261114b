import json
import unittest

from requeue import (
    Action,
    Bucket,
    Event,
    IdempotencyRule,
    Journal,
    JournalError,
    Phase,
    plan_jobs,
    reduce_events,
)


def _promise(**overrides):
    values = {
        "explicit": True,
        "same_key_same_body_returns_same_receipt": True,
        "source": "vendor docs v3, section 4.2",
    }
    values.update(overrides)
    return IdempotencyRule(**values)


def _post(request_id, *, attempt="a1", key=None, body="b1", url="/exports"):
    events = [
        {
            "event_id": f"{request_id}:request",
            "kind": "request_registered",
            "request_id": request_id,
            "data": {
                "method": "POST",
                "url": url,
                "body_sha256": body,
                "idempotency_key": key,
            },
        },
        {
            "event_id": f"{request_id}:{attempt}:created",
            "kind": "attempt_created",
            "request_id": request_id,
            "attempt_id": attempt,
        },
    ]
    return events


def _bucket(plan):
    return {
        "resend": [d.request_id for d in plan.resend],
        "verify": [d.request_id for d in plan.verify_remote],
        "delivered": [d.request_id for d in plan.delivered_local],
    }


class ExposureTests(unittest.TestCase):
    def test_get_with_partial_body_is_still_resendable(self):
        events = [
            {
                "event_id": "g:req",
                "kind": "request_registered",
                "request_id": "g",
                "data": {"method": "GET", "url": "/items?page=2"},
            },
            {
                "event_id": "g:headers",
                "kind": "attempt_headers_sent",
                "request_id": "g",
                "attempt_id": "a1",
            },
            {
                "event_id": "g:body",
                "kind": "attempt_body_progress",
                "request_id": "g",
                "attempt_id": "a1",
                "data": {"bytes": 10, "total": 100},
            },
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["resend"], ["g"])
        self.assertEqual(plan.resend[0].progress, Phase.BODY_PARTIAL)

    def test_post_never_flushed_is_resendable(self):
        plan = reduce_events(_post("p1"))
        self.assertEqual(_bucket(plan)["resend"], ["p1"])
        self.assertEqual(plan.resend[0].reason, "no_exposure")

    def test_headers_only_post_requires_receipt_check(self):
        events = _post("p2") + [
            {
                "event_id": "p2:headers",
                "kind": "attempt_headers_sent",
                "request_id": "p2",
                "attempt_id": "a1",
                "data": {"body_total": 4096},
            }
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["verify"], ["p2"])
        self.assertEqual(plan.verify_remote[0].action, Action.VERIFY_RECEIPT)
        self.assertIn("headers were flushed", plan.verify_remote[0].detail)

    def test_partial_body_is_not_treated_as_unprocessed(self):
        events = _post("p3") + [
            {
                "event_id": "p3:headers",
                "kind": "attempt_headers_sent",
                "request_id": "p3",
                "attempt_id": "a1",
                "data": {"body_total": 4096},
            },
            {
                "event_id": "p3:body",
                "kind": "attempt_body_progress",
                "request_id": "p3",
                "attempt_id": "a1",
                "data": {"bytes": 512},
            },
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["verify"], ["p3"])
        self.assertIn("partially", plan.verify_remote[0].detail)

    def test_complete_body_without_receipt_requires_check(self):
        events = _post("p4") + [
            {
                "event_id": "p4:done",
                "kind": "attempt_body_complete",
                "request_id": "p4",
                "attempt_id": "a1",
                "data": {"body_total": 4096},
            }
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["verify"], ["p4"])
        self.assertEqual(plan.verify_remote[0].progress, Phase.REQUEST_SENT)


class IdempotencyPromiseTests(unittest.TestCase):
    def test_explicit_promise_permits_resend(self):
        events = _post("p5", key="exp-1") + [
            {
                "event_id": "p5:headers",
                "kind": "attempt_headers_sent",
                "request_id": "p5",
                "attempt_id": "a1",
                "data": {"body_total": 4096},
            }
        ]
        plan = reduce_events(events, rule=_promise())
        self.assertEqual(_bucket(plan)["resend"], ["p5"])
        self.assertEqual(plan.resend[0].reason, "explicit_idempotency_promise")

    def test_key_without_documented_promise_does_not_authorize_resend(self):
        events = _post("p6", key="exp-2") + [
            {
                "event_id": "p6:body",
                "kind": "attempt_body_progress",
                "request_id": "p6",
                "attempt_id": "a1",
                "data": {"bytes": 12, "total": 100},
            }
        ]
        for rule in (IdempotencyRule(), _promise(explicit=False),
                     _promise(same_key_same_body_returns_same_receipt=False)):
            plan = reduce_events(events, rule=rule)
            self.assertEqual(_bucket(plan)["verify"], ["p6"])

    def test_promise_only_covers_documented_methods(self):
        events = _post("p7", key="exp-3") + [
            {
                "event_id": "p7:body",
                "kind": "attempt_body_progress",
                "request_id": "p7",
                "attempt_id": "a1",
                "data": {"bytes": 1, "total": 10},
            }
        ]
        rule = _promise(methods=frozenset({"PUT"}))
        plan = reduce_events(events, rule=rule)
        self.assertEqual(_bucket(plan)["verify"], ["p7"])


class LocalDeliveryTests(unittest.TestCase):
    def test_persisted_receipt_is_delivered_locally(self):
        events = _post("p8") + [
            {
                "event_id": "p8:done",
                "kind": "attempt_body_complete",
                "request_id": "p8",
                "attempt_id": "a1",
            },
            {
                "event_id": "p8:receipt",
                "kind": "receipt_persisted",
                "request_id": "p8",
                "attempt_id": "a1",
                "data": {"receipt_id": "export-77"},
            },
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["delivered"], ["p8"])
        self.assertEqual(plan.delivered_local[0].action, Action.NONE)
        self.assertEqual(plan.delivered_local[0].progress, Phase.RECEIPT_STORED)

    def test_stored_success_response_counts_as_delivered(self):
        events = _post("p9") + [
            {
                "event_id": "p9:resp",
                "kind": "attempt_response",
                "request_id": "p9",
                "attempt_id": "a1",
                "data": {"status": 202, "body_stored": True, "receipt_id": "r-9"},
            }
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["delivered"], ["p9"])

    def test_unstored_response_is_not_delivered(self):
        events = _post("p10") + [
            {
                "event_id": "p10:resp",
                "kind": "attempt_response",
                "request_id": "p10",
                "attempt_id": "a1",
                "data": {"status": 202, "body_stored": False},
            }
        ]
        plan = reduce_events(events)
        self.assertEqual(_bucket(plan)["verify"], ["p10"])
        self.assertEqual(plan.verify_remote[0].reason,
                         "response_seen_without_stored_receipt")


class ReplayTests(unittest.TestCase):
    def test_duplicate_event_ids_do_not_duplicate_jobs(self):
        base = _post("r1", key="k-1") + [
            {
                "event_id": "r1:headers",
                "kind": "attempt_headers_sent",
                "request_id": "r1",
                "attempt_id": "a1",
                "data": {"body_total": 10},
            }
        ]
        once = reduce_events(base, rule=_promise())
        twice = reduce_events(base + base, rule=_promise())
        self.assertEqual(_bucket(once)["resend"], ["r1"])
        self.assertEqual(_bucket(twice)["resend"], ["r1"])
        self.assertEqual(len(supplement := __import__(
            "requeue").supplement_jobs(twice)), 1)
        self.assertEqual(supplement[0].request_id, "r1")
        self.assertEqual(len(twice.ignored_event_ids), len(base))

    def test_explicit_replay_alias_merges_into_one_job(self):
        events = _post("r6", key=None) + [
            {
                "event_id": "r6:req",
                "kind": "request_registered",
                "request_id": "r6-replay",
                "data": {"method": "POST", "url": "/exports", "replay_of": "r6"},
            },
        ]
        plan, jobs = plan_jobs(events)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(_bucket(plan)["resend"], ["r6"])

    def test_duplicate_body_progress_events_are_idempotent_by_event_id(self):
        progress = {
            "event_id": "d1:body",
            "kind": "attempt_body_progress",
            "request_id": "d1",
            "attempt_id": "a1",
            "data": {"bytes": 60, "total": 100},
        }
        events = _post("d1") + [progress, dict(progress)]
        plan = reduce_events(events)
        self.assertEqual(plan.verify_remote[0].progress, Phase.BODY_PARTIAL)
        self.assertIn("d1:body", plan.ignored_event_ids)

    def test_replayed_request_alias_yields_one_job(self):
        first = _post("r2", key="k-2")
        replay = _post("r2-replay", key="k-2")
        events = first + replay
        plan, jobs = plan_jobs(events, rule=_promise())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(_bucket(plan)["resend"], ["r2"])

    def test_unrelated_posts_without_body_hash_are_not_merged(self):
        events = _post("r4", key=None) + _post("r5", key=None)
        for event in events:
            if event["kind"] == "request_registered":
                event["data"].pop("body_sha256", None)
        plan, jobs = plan_jobs(events)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sorted(_bucket(plan)["resend"]), ["r4", "r5"])

    def test_conflicting_payload_for_same_event_id_is_reported(self):
        events = _post("r3") + [
            {
                "event_id": "r3:body",
                "kind": "attempt_body_progress",
                "request_id": "r3",
                "attempt_id": "a1",
                "data": {"bytes": 1, "total": 10},
            },
            {
                "event_id": "r3:body",
                "kind": "attempt_body_progress",
                "request_id": "r3",
                "attempt_id": "a1",
                "data": {"bytes": 9, "total": 10},
            },
        ]
        plan = reduce_events(events, rule=_promise())
        self.assertTrue(any("different payload" in a for a in plan.anomalies))
        self.assertEqual(plan.verify_remote[0].progress, Phase.BODY_PARTIAL)


class KeyIsolationTests(unittest.TestCase):
    def test_same_key_with_different_bodies_is_isolated(self):
        events = (
            _post("s1", key="shared", body="body-A")
            + _post("s2", key="shared", body="body-B")
            + [
                {
                    "event_id": "s1:headers",
                    "kind": "attempt_headers_sent",
                    "request_id": "s1",
                    "attempt_id": "a1",
                },
                {
                    "event_id": "s2:headers",
                    "kind": "attempt_headers_sent",
                    "request_id": "s2",
                    "attempt_id": "a1",
                },
            ]
        )
        plan = reduce_events(events, rule=_promise())
        self.assertEqual(sorted(_bucket(plan)["verify"]), ["s1", "s2"])
        self.assertEqual(len(plan.isolated_keys["shared"]), 2)
        groups = {d.request_id: d.isolation_group for d in plan.verify_remote}
        self.assertIsNotNone(groups["s1"])
        self.assertNotEqual(groups["s1"], groups["s2"])
        for decision in plan.verify_remote:
            self.assertEqual(decision.reason, "ambiguous_idempotency_key")

    def test_isolated_requests_never_share_a_group(self):
        events = (
            _post("s3", key="dup", body="x")
            + _post("s4", key="dup", body="y")
            + [
                {
                    "event_id": "s3:h",
                    "kind": "attempt_headers_sent",
                    "request_id": "s3",
                    "attempt_id": "a1",
                },
                {
                    "event_id": "s4:h",
                    "kind": "attempt_headers_sent",
                    "request_id": "s4",
                    "attempt_id": "a1",
                },
            ]
        )
        plan = reduce_events(events, rule=_promise())
        groups = [d.isolation_group for d in plan.verify_remote]
        self.assertEqual(len(set(groups)), 2)

    def test_same_key_same_body_is_not_isolated(self):
        events = (
            _post("s5", key="same", body="same-body")
            + _post("s6", key="same", body="same-body")
        )
        plan = reduce_events(events, rule=_promise())
        self.assertEqual(plan.isolated_keys, {})


class ResponseStatusTests(unittest.TestCase):
    def _post_with_status(self, request_id, status, stored=False):
        return _post(request_id) + [
            {
                "event_id": f"{request_id}:headers",
                "kind": "attempt_headers_sent",
                "request_id": request_id,
                "attempt_id": "a1",
            },
            {
                "event_id": f"{request_id}:resp",
                "kind": "attempt_response",
                "request_id": request_id,
                "attempt_id": "a1",
                "data": {"status": status, "body_stored": stored},
            },
        ]

    def test_complete_rejection_is_safe_to_resend(self):
        plan = reduce_events(self._post_with_status("c1", 422))
        self.assertEqual(_bucket(plan)["resend"], ["c1"])
        self.assertEqual(plan.resend[0].reason, "origin_rejected_request")

    def test_conflict_requires_receipt_check(self):
        plan = reduce_events(self._post_with_status("c2", 409))
        self.assertEqual(_bucket(plan)["verify"], ["c2"])
        self.assertEqual(plan.verify_remote[0].reason, "origin_reports_conflict")

    def test_server_error_without_receipt_requires_check(self):
        plan = reduce_events(self._post_with_status("c3", 500))
        self.assertEqual(_bucket(plan)["verify"], ["c3"])

    def test_stored_success_response_is_delivered_not_verified(self):
        plan = reduce_events(self._post_with_status("c4", 202, stored=True))
        self.assertEqual(_bucket(plan)["delivered"], ["c4"])


class MissingBodyHashTests(unittest.TestCase):
    def test_key_without_persisted_body_hash_is_not_trusted(self):
        events = [
            {
                "event_id": "n1:req",
                "kind": "request_registered",
                "request_id": "n1",
                "data": {
                    "method": "POST",
                    "url": "/exports",
                    "idempotency_key": "k-n1",
                },
            },
            {
                "event_id": "n1:h",
                "kind": "attempt_headers_sent",
                "request_id": "n1",
                "attempt_id": "a1",
                "data": {"body_total": 100},
            },
        ]
        plan = reduce_events(events, rule=_promise())
        self.assertEqual(_bucket(plan)["verify"], ["n1"])
        self.assertEqual(plan.verify_remote[0].reason, "body_hash_not_persisted")

    def test_two_requests_same_key_unknown_bodies_are_isolated(self):
        def register(request_id):
            return {
                "event_id": f"{request_id}:req",
                "kind": "request_registered",
                "request_id": request_id,
                "data": {
                    "method": "POST",
                    "url": "/exports",
                    "idempotency_key": "shared-unknown",
                },
            }

        events = [register("u1"), register("u2")]
        plan = reduce_events(events, rule=_promise())
        self.assertEqual(sorted(plan.isolated_keys["shared-unknown"]),
                         ["u1", "u2"])
        self.assertEqual(len(plan.resend), 0)
        self.assertEqual(_bucket(plan)["verify"], ["u1", "u2"])
        groups = {d.request_id: d.isolation_group for d in plan.verify_remote}
        self.assertNotEqual(groups["u1"], groups["u2"])


class OutputShapeTests(unittest.TestCase):
    def test_plan_serializes_to_json(self):
        plan = reduce_events(_post("o1"))
        payload = plan.to_dict()
        text = json.dumps(payload)
        self.assertIn("resend", text)
        self.assertEqual(payload["resend"][0]["bucket"], "resend")
        self.assertEqual(payload["resend"][0]["action"], "resend")

    def test_unknown_event_kind_raises(self):
        journal = Journal()
        with self.assertRaises(JournalError):
            journal.append(
                Event(
                    event_id="x",
                    kind="mystery",
                    request_id="x",
                    data={},
                )
            )

    def test_progress_merges_across_attempts(self):
        events = _post("m1") + [
            {
                "event_id": "m1:h",
                "kind": "attempt_headers_sent",
                "request_id": "m1",
                "attempt_id": "a1",
            },
            {
                "event_id": "m1:body",
                "kind": "attempt_body_progress",
                "request_id": "m1",
                "attempt_id": "a1",
                "data": {"bytes": 5, "total": 20},
            },
            {
                "event_id": "m1:attempt2:h",
                "kind": "attempt_headers_sent",
                "request_id": "m1",
                "attempt_id": "a2",
            },
            {
                "event_id": "m1:attempt2:resp",
                "kind": "attempt_response",
                "request_id": "m1",
                "attempt_id": "a2",
                "data": {"status": 500, "body_stored": False},
            },
        ]
        plan = reduce_events(events)
        self.assertEqual(plan.verify_remote[0].attempt_id, "a2")


if __name__ == "__main__":
    unittest.main()
