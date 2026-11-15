import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from requeue.__main__ import main


SAMPLE = {
    "idempotency_rule": {
        "explicit": True,
        "same_key_same_body_returns_same_receipt": True,
    },
    "events": [
        {
            "event_id": "c:req",
            "kind": "request_registered",
            "request_id": "c",
            "data": {"method": "GET", "url": "/catalog"},
        },
        {
            "event_id": "c:h",
            "kind": "attempt_headers_sent",
            "request_id": "c",
            "attempt_id": "a1",
        },
    ],
}


class CliTests(unittest.TestCase):
    def _run(self, payload):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(payload, handle)
            handle.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([handle.name])
            return code, json.loads(out.getvalue())
        finally:
            os.unlink(handle.name)

    def test_cli_lists_resend(self):
        code, payload = self._run(SAMPLE)
        self.assertEqual(code, 0)
        self.assertEqual([d["request_id"] for d in payload["resend"]], ["c"])
        self.assertEqual(payload["verify_remote"], [])
        self.assertEqual(payload["delivered_local"], [])

    def test_cli_reports_jobs(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(SAMPLE, handle)
            handle.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([handle.name, "--jobs"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["supplement_jobs"][0]["request_id"], "c")
        finally:
            os.unlink(handle.name)

    def test_cli_rejects_unknown_event_kind(self):
        bad = dict(SAMPLE)
        bad["events"] = [{"event_id": "x", "kind": "bogus", "request_id": "x"}]
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            json.dump(bad, handle)
            handle.close()
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main([handle.name])
            self.assertEqual(code, 2)
            self.assertIn("bogus", err.getvalue())
        finally:
            os.unlink(handle.name)


if __name__ == "__main__":
    unittest.main()
