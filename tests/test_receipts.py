"""Tests for upload identity receipts.

These cover the three dispositions the receipt store must distinguish
(new sample, re-send, reboot resume), the content-addressed storage sharing,
the rule that a boot-less transfer may never be spliced onto an old partial,
and the wiring into the TFTP server state machine.
"""

import hashlib
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import tftpy
from tftpy import (
    BOOT_OPTION,
    DISPOSITION_NEW,
    DISPOSITION_RESEND,
    DISPOSITION_RESUME,
    TftpReceiptStore,
)
from tftpy.TftpContexts import TftpContextServer
from tftpy.TftpPacketTypes import TftpPacketDAT, TftpPacketWRQ
from tftpy.TftpShared import TftpException


class ReceiptStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.dest = os.path.join(self.tempdir, "RESULT.DAT")
        self.blksize = 4

    def make_store(self):
        return TftpReceiptStore(self.tempdir)

    def upload(self, store, blocks, boot="B1", peer="peer"):
        handle = store.begin_upload(
            "RESULT.DAT", self.dest, boot, self.blksize, peer
        )
        for number, data in enumerate(blocks, start=1):
            handle.write_block(number, data)
        return handle

    def test_new_sample_publishes_observation_and_blob(self):
        store = self.make_store()
        handle = self.upload(store, [b"abcd", b"ef"])
        handle.finalize()

        records = store.observations("RESULT.DAT")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["round"], 1)
        self.assertEqual(record["boot"], "B1")
        self.assertEqual(record["size"], 6)
        expected_digest = hashlib.sha256(b"abcdef").hexdigest()
        self.assertEqual(record["sha256"], expected_digest)
        self.assertEqual(
            open(os.path.join(self.tempdir, record["observation"]), "rb").read(),
            b"abcdef",
        )
        self.assertEqual(
            open(os.path.join(self.tempdir, record["blob"]), "rb").read(),
            b"abcdef",
        )
        self.assertEqual(record["attempts"][0]["disposition"], DISPOSITION_NEW)
        # The requested filename must never be overwritten directly.
        self.assertFalse(os.path.exists(self.dest))

    def test_rounds_are_kept_distinct_but_share_one_blob(self):
        first = self.upload(self.make_store(), [b"abcd", b"ef"], boot="B1")
        first.finalize()
        second = self.upload(self.make_store(), [b"abcd", b"ef"], boot="B2")
        second.finalize()

        store = self.make_store()  # reload the ledger from disk
        records = store.observations("RESULT.DAT")
        self.assertEqual([r["round"] for r in records], [1, 2])
        self.assertEqual(len({r["id"] for r in records}), 2)
        self.assertEqual(len({r["blob"] for r in records}), 1)
        self.assertEqual(
            len({r["observation"] for r in records}), 2,
            "identical content still yields two observations",
        )
        inodes = {
            os.stat(os.path.join(self.tempdir, r["observation"])).st_ino
            for r in records
        }
        self.assertEqual(len(inodes), 1, "observations hard-link one blob")

    def test_reboot_resume_verifies_prefix_and_completes(self):
        store = self.make_store()
        interrupted = self.upload(store, [b"aaaa", b"bbbb"], peer="tid-1")
        interrupted.abort()

        partial = store.observations("RESULT.DAT")[0]
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["blocks_received"], 2)
        self.assertTrue(
            os.path.exists(os.path.join(self.tempdir, partial["partial"]))
        )

        resumed = self.upload(
            store, [b"aaaa", b"bbbb", b"cc"], boot="B1", peer="tid-2"
        )
        self.assertEqual(resumed.disposition, DISPOSITION_RESUME)
        resumed.finalize()

        record = store.observations("RESULT.DAT")[0]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["size"], 10)
        self.assertEqual(
            [a["disposition"] for a in record["attempts"]],
            [DISPOSITION_NEW, DISPOSITION_RESUME],
        )
        self.assertEqual(
            open(os.path.join(self.tempdir, record["observation"]), "rb").read(),
            b"aaaabbbbcc",
        )
        self.assertIsNone(record["partial"])

    def test_resume_rejects_divergent_prefix(self):
        store = self.make_store()
        self.upload(store, [b"aaaa", b"bbbb"]).abort()

        # Block one activates the resume and matches; the divergent second
        # block must be rejected before anything is appended.
        resumed = self.upload(store, [b"aaaa"], boot="B1")
        self.assertEqual(resumed.disposition, DISPOSITION_RESUME)
        with self.assertRaises(TftpException):
            resumed.write_block(2, b"XXXX")
        resumed.abort()

        record = store.observations("RESULT.DAT")[0]
        self.assertEqual(record["status"], "partial")
        self.assertEqual(
            record["blocks_received"], 2,
            "a failed resume must not shrink the stored prefix",
        )

    def test_resume_self_heals_when_ledger_lags_the_bytes(self):
        store = self.make_store()
        handle = self.upload(store, [b"aaaa", b"bbbb"], peer="tid-1")
        handle.abort()

        # Simulate a hard crash that flushed block two but only persisted a
        # one-block ledger entry.
        ledger_path = os.path.join(self.tempdir, ".tftp-receipts.json")
        import json
        with open(ledger_path, encoding="utf-8") as ledger:
            payload = json.load(ledger)
        payload["records"][0]["blocks_received"] = 1
        with open(ledger_path, "w", encoding="utf-8") as ledger:
            json.dump(payload, ledger)

        store = self.make_store()  # reload the stale ledger
        record = store.observations("RESULT.DAT")[0]
        self.assertEqual(record["blocks_received"], 1)

        resumed = self.upload(
            store, [b"aaaa", b"bbbb", b"cc"], boot="B1", peer="tid-2"
        )
        self.assertEqual(resumed.disposition, DISPOSITION_RESUME)
        resumed.finalize()

        record = store.observations("RESULT.DAT")[0]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["blocks_received"], 3)
        self.assertEqual(
            open(
                os.path.join(self.tempdir, record["observation"]), "rb"
            ).read(),
            b"aaaabbbbcc",
        )

    def test_missing_boot_never_splices_onto_partial(self):
        store = self.make_store()
        self.upload(store, [b"aaaa", b"bbbb"], boot="B1").abort()

        # Same filename and identical first block, but no boot code: this
        # must be treated as a separate sampling round.
        bootless = self.upload(
            store, [b"aaaa", b"zzzz", b"q"], boot=None
        )
        self.assertEqual(bootless.disposition, DISPOSITION_NEW)
        bootless.finalize()

        records = store.observations("RESULT.DAT")
        self.assertEqual(len(records), 2)
        self.assertEqual([r["round"] for r in records], [1, 2])
        self.assertIsNone(records[1]["boot"])
        self.assertEqual(
            open(
                os.path.join(self.tempdir, records[1]["observation"]), "rb"
            ).read(),
            b"aaaazzzzq",
        )

        evidence = store.evidence_required_for_unverified()
        keys = {item["evidence_key"] for item in evidence["catalog"]}
        self.assertEqual(
            keys,
            {
                "boot",
                "round",
                "block-continuity",
                "prefix-digest-match",
                "session-correlation",
                "instrument-log",
            },
        )
        open_partial = evidence["open_partials"][0]
        self.assertEqual(open_partial["record_id"], records[0]["id"])
        self.assertEqual(open_partial["next_expected_block"], 3)
        self.assertIn("boot", open_partial["missing_evidence"])
        self.assertIn("prefix-digest-match", open_partial["missing_evidence"])

    def test_different_boot_starts_new_sample(self):
        store = self.make_store()
        self.upload(store, [b"aaaa", b"bb"], boot="B1").finalize()
        again = self.upload(store, [b"aaaa", b"bb"], boot="B2")
        self.assertEqual(again.disposition, DISPOSITION_NEW)
        again.finalize()
        self.assertEqual(
            len(store.observations("RESULT.DAT")), 2
        )

    def test_resend_is_verified_and_mismatch_is_rejected(self):
        store = self.make_store()
        self.upload(store, [b"aaaa", b"bb"], boot="B1").finalize()

        good = self.upload(store, [b"aaaa", b"bb"], boot="B1")
        self.assertEqual(good.disposition, DISPOSITION_RESEND)
        good.finalize()
        record = store.observations("RESULT.DAT")[0]
        self.assertEqual(
            [a["disposition"] for a in record["attempts"]],
            [DISPOSITION_NEW, DISPOSITION_RESEND],
        )
        self.assertEqual(record["attempts"][-1]["result"], "verified")

        bad = self.upload(store, [b"aaaa", b"XX"], boot="B1")
        with self.assertRaises(TftpException):
            bad.finalize()

        # The failed verification must not block a later retry.
        retry = self.upload(store, [b"aaaa", b"bb"], boot="B1")
        self.assertEqual(retry.disposition, DISPOSITION_RESEND)
        retry.finalize()
        self.assertEqual(
            store.observations("RESULT.DAT")[0]["attempts"][-1]["result"],
            "verified",
        )

    def test_identity_is_only_established_on_block_one(self):
        store = self.make_store()
        handle = store.begin_upload(
            "RESULT.DAT", self.dest, "B1", self.blksize, "peer"
        )
        handle.abort()  # WRQ arrived but no DAT block ever did
        self.assertEqual(store.observations("RESULT.DAT"), [])


class ServerStateReceiptTestCase(unittest.TestCase):
    """Drive the server state machine directly, no sockets."""

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.store = TftpReceiptStore(self.tempdir)
        self.raddress = "127.0.0.2"
        self.rport = 10000

    def new_context(self):
        return TftpContextServer(
            self.raddress, self.rport, 5, self.tempdir,
            receipt_store=self.store,
        )

    def wrq(self, boot):
        packet = TftpPacketWRQ()
        packet.filename = "RESULT.DAT"
        packet.mode = "octet"
        # 8 is the minimum negotiable TFTP blksize (MIN_BLKSIZE).
        options = {"blksize": 8}
        if boot:
            options[BOOT_OPTION] = boot
        packet.options = options
        return packet.encode().buffer

    def dat(self, blocknumber, data):
        packet = TftpPacketDAT()
        packet.blocknumber = blocknumber
        packet.data = data
        return packet

    def test_interrupted_then_rebooted_transfer_is_resumed(self):
        with patch("tftpy.TftpStates.TftpState.sendACK"), \
                patch("tftpy.TftpStates.TftpState.sendOACK"):
            first = self.new_context()
            first.start(self.wrq("B1"))
            first.state = first.state.handle(
                self.dat(1, b"aaaaaaaa"), self.raddress, self.rport
            )
            first.state = first.state.handle(
                self.dat(2, b"bbbbbbbb"), self.raddress, self.rport
            )
            self.assertFalse(first.transfer_complete)
            first.end()  # instrument rebooted mid-transfer

            record = self.store.observations("RESULT.DAT")[0]
            self.assertEqual(record["status"], "partial")

            second = self.new_context()
            second.start(self.wrq("B1"))
            for blocknumber, data in enumerate(
                [b"aaaaaaaa", b"bbbbbbbb", b"cc"], start=1
            ):
                second.state = second.state.handle(
                    self.dat(blocknumber, data), self.raddress,
                    self.rport,
                )
            self.assertIsNone(second.state)
            self.assertTrue(second.transfer_complete)
            second.end()

        record = self.store.observations("RESULT.DAT")[0]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(
            [a["disposition"] for a in record["attempts"]],
            [DISPOSITION_NEW, DISPOSITION_RESUME],
        )
        self.assertEqual(
            open(
                os.path.join(self.tempdir, record["observation"]), "rb"
            ).read(),
            b"aaaaaaaabbbbbbbbcc",
        )

    def test_boot_option_is_not_negotiated_over_the_wire(self):
        context = self.new_context()
        with patch("tftpy.TftpStates.TftpState.sendACK"), \
                patch("tftpy.TftpStates.TftpState.sendOACK"):
            context.start(self.wrq("B1"))
        # The boot option is identity metadata and must not survive into
        # TFTP option negotiation or an OACK.
        self.assertNotIn(BOOT_OPTION, context.options)
        self.assertEqual(context.receipt.boot, "B1")
        context.end()


class ServerSocketReceiptTestCase(unittest.TestCase):
    """One end-to-end upload through real UDP sockets."""

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.store = TftpReceiptStore(self.tempdir)
        # Some sandboxes forbid binding UDP sockets; the end-to-end test
        # cannot run there (the state-machine tests still cover the wiring).
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError as err:
            probe.close()
            self.skipTest("UDP bind not permitted in this environment: %s" % err)
        else:
            probe.close()
        self.server = tftpy.TftpServer(
            self.tempdir, receipt_store=self.store
        )
        self.thread = threading.Thread(
            target=self.server.listen,
            kwargs={
                "listenip": "127.0.0.1",
                "listenport": 0,
                "timeout": 0.5,
            },
        )
        self.thread.start()
        self.server.is_running.wait(5)

    def tearDown(self):
        self.server.stop(now=True)
        self.thread.join(timeout=6)

    def test_client_upload_with_boot_is_recorded(self):
        client = tftpy.TftpClient(
            "127.0.0.1",
            self.server.listenport,
            {BOOT_OPTION: "BOOT-42", "blksize": 1024},
        )
        payload = bytes((i % 251 for i in range(5000)))
        input_path = os.path.join(self.tempdir, "input.bin")
        with open(input_path, "wb") as source:
            source.write(payload)

        client.upload("RESULT.DAT", input_path)

        records = self.store.observations("RESULT.DAT")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["boot"], "BOOT-42")
        self.assertEqual(record["blksize"], 1024)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(), record["sha256"]
        )
        self.assertEqual(
            open(
                os.path.join(self.tempdir, record["observation"]), "rb"
            ).read(),
            payload,
        )


if __name__ == "__main__":
    unittest.main()
