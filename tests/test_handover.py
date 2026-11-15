import logging
import os
import tempfile
import unittest
from shutil import rmtree
from unittest.mock import MagicMock

import tftpy
from tftpy.TftpHandover import (
    INCOMPLETE,
    RECEIVING,
    REGISTERED,
    STAGED,
    TftpHandoverJournal,
    hash_file,
)
from tftpy.TftpPacketTypes import TftpPacketDAT, TftpPacketWRQ
from tftpy.TftpStates import TftpStateExpectDAT
from tftpy.TftpShared import TftpException

log = logging.getLogger("tftpy")

CONTENT_V1 = b"measurement report, first revision\n" * 40
CONTENT_V2 = b"measurement report, second revision\n" * 40


class TftpHandoverJournalTestCase(unittest.TestCase):
    """Unit tests for the recoverable handover journal."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.journal_dir = os.path.join(self.root, ".handover")
        self.journal = TftpHandoverJournal(self.journal_dir, root=self.root)

    def tearDown(self):
        rmtree(self.root, ignore_errors=True)

    def stage_content(self, handover, content):
        with open(handover.staging_path, "wb") as fobj:
            fobj.write(content)

    def test_states_and_reconcile(self):
        handover = self.journal.begin("report.bin", expected_size=len(CONTENT_V1))
        self.assertEqual(handover.state, RECEIVING)
        self.stage_content(handover, CONTENT_V1)
        handover = self.journal.complete(handover)
        self.assertEqual(handover.state, STAGED)
        self.assertEqual(handover.size, len(CONTENT_V1))
        self.assertEqual(handover.sha256, hash_file(handover.staging_path))
        self.assertTrue(self.journal.verify(handover))

        handover = self.journal.register(handover)
        self.assertEqual(handover.state, REGISTERED)
        self.assertEqual(
            handover.destination, os.path.join(self.root, "report.bin"))
        with open(handover.destination, "rb") as fobj:
            self.assertEqual(fobj.read(), CONTENT_V1)
        # The final registry entry reconciles with content and expected size.
        self.assertTrue(self.journal.verify(handover))

    def test_expected_size_mismatch_is_not_staged(self):
        handover = self.journal.begin("report.bin", expected_size=len(CONTENT_V1) + 1)
        self.stage_content(handover, CONTENT_V1)
        with self.assertRaises(TftpException):
            self.journal.complete(handover)
        self.assertEqual(self.journal.get(handover.id).state, INCOMPLETE)

    def test_identical_reupload_reuses_handover(self):
        first = self.journal.begin("report.bin")
        self.stage_content(first, CONTENT_V1)
        first = self.journal.complete(first)

        second = self.journal.begin("report.bin")
        self.stage_content(second, CONTENT_V1)
        reused = self.journal.complete(second)

        self.assertEqual(reused.id, first.id)
        self.assertIsNone(self.journal.get(second.id))
        self.assertEqual(len(list(self.journal.entries(state=STAGED))), 1)

    def test_same_name_different_content_archived_separately(self):
        first = self.journal.begin("report.bin")
        self.stage_content(first, CONTENT_V1)
        first = self.journal.register(self.journal.complete(first))

        second = self.journal.begin("report.bin")
        self.stage_content(second, CONTENT_V2)
        second = self.journal.complete(second)
        self.assertNotEqual(second.id, first.id)
        second = self.journal.register(second)

        self.assertNotEqual(second.destination, first.destination)
        with open(first.destination, "rb") as fobj:
            self.assertEqual(fobj.read(), CONTENT_V1)
        with open(second.destination, "rb") as fobj:
            self.assertEqual(fobj.read(), CONTENT_V2)
        self.assertEqual(
            len(list(self.journal.entries(state=REGISTERED))), 2)

    def test_recovery_never_trusts_half_files(self):
        # A transfer interrupted mid-way leaves a receiving record and a
        # partial staging file behind.
        partial = self.journal.begin("partial.bin",
                                     expected_size=len(CONTENT_V1))
        self.stage_content(partial, CONTENT_V1[:100])
        self.journal.note_progress(partial, 100)

        # A staged record whose file was later truncated on disk.
        damaged = self.journal.begin("damaged.bin")
        self.stage_content(damaged, CONTENT_V1)
        damaged = self.journal.complete(damaged)
        with open(damaged.staging_path, "r+b") as fobj:
            fobj.truncate(50)

        # A half file that just happens to exist, with no record at all.
        with open(os.path.join(self.journal.staging_dir, "orphan.part"),
                  "wb") as fobj:
            fobj.write(CONTENT_V1[:60])

        # Simulate a restart with a fresh journal instance.
        recovered = TftpHandoverJournal(self.journal_dir, root=self.root)
        summary = recovered.recover()

        self.assertEqual(len(summary[STAGED]), 0)
        self.assertEqual(len(summary[INCOMPLETE]), 2)
        self.assertEqual(len(summary["orphaned"]), 1)
        self.assertEqual(
            recovered.get(partial.id).state, INCOMPLETE)
        self.assertEqual(
            recovered.get(damaged.id).state, INCOMPLETE)
        # Nothing may be registered from this state.
        for handover in recovered.entries():
            with self.assertRaises(TftpException):
                recovered.register(
                    handover, os.path.join(self.root, "out.bin"))


class TftpHandoverServerTestCase(unittest.TestCase):
    """Server-side integration of the handover journal, driving the
    receiving state machine directly so no network access is needed."""

    def setUp(self):
        self.server_root = tempfile.mkdtemp()
        self.handover_dir = os.path.join(self.server_root, ".handover")

    def tearDown(self):
        rmtree(self.server_root, ignore_errors=True)

    def make_context(self, journal):
        context = tftpy.TftpContexts.TftpContextServer(
            "127.0.0.1", 11111, 2, self.server_root,
            handover_journal=journal)
        context.sock.close()
        context.sock = MagicMock()
        return context

    def start_upload(self, journal, filename, options=None):
        context = self.make_context(journal)
        wrq = TftpPacketWRQ()
        wrq.filename = filename
        wrq.mode = "octet"
        wrq.options = options or {}
        context.start(wrq.encode().buffer)
        return context

    def send_dat(self, context, blocknumber, data):
        dat = TftpPacketDAT()
        dat.blocknumber = blocknumber
        dat.data = data
        context.state = context.state.handle(dat, "127.0.0.1", 11111)

    def run_upload(self, journal, filename, content,
                   options=None, blksize=512):
        """Drive a complete upload through the server state machine."""
        context = self.start_upload(journal, filename, options)
        block = 1
        offset = 0
        while context.state is not None:
            data = content[offset:offset + blksize]
            offset += len(data)
            self.send_dat(context, block, data)
            block += 1
        return context

    def test_upload_is_staged_then_registered(self):
        home = os.path.dirname(os.path.abspath(__file__))
        input_path = os.path.join(home, "640KBFILE")
        with open(input_path, "rb") as fobj:
            content = fobj.read()
        size = len(content)
        journal = TftpHandoverJournal(self.handover_dir,
                                      root=self.server_root)
        context = self.run_upload(journal, "nightly/report.bin", content,
                                  options={"tsize": str(size)})
        handover = context.handover
        self.assertEqual(handover.state, STAGED)
        self.assertEqual(handover.filename, "nightly/report.bin")
        self.assertEqual(handover.expected_size, size)
        self.assertEqual(handover.size, size)
        self.assertEqual(handover.sha256, hash_file(input_path))
        self.assertTrue(journal.verify(handover))
        # Nothing leaks into the final tree before registration.
        self.assertFalse(
            os.path.exists(os.path.join(self.server_root, "nightly")))

        # The archiving process registers the staged handover, possibly
        # after a restart (fresh journal instance).
        journal = TftpHandoverJournal(self.handover_dir,
                                      root=self.server_root)
        summary = journal.recover()
        self.assertEqual(len(summary[STAGED]), 1)
        handover = journal.register(summary[STAGED][0])
        self.assertEqual(handover.state, REGISTERED)
        self.assertEqual(hash_file(handover.destination),
                         hash_file(input_path))
        context.end()

    def test_duplicate_final_block_reuses_handover(self):
        """A retransmitted last block is answered and keeps the original
        handover instead of completing a second one."""
        journal = TftpHandoverJournal(self.handover_dir,
                                      root=self.server_root)
        content = b"x" * 100
        context = self.start_upload(journal, "report.bin",
                                    options={"tsize": str(len(content))})
        # In-sequence final block completes the handover.
        self.send_dat(context, 1, content)
        self.assertIsNone(context.state)
        handover = context.handover
        self.assertEqual(handover.state, STAGED)
        acks_before = context.sock.sendto.call_count

        # The client never saw the final ACK and resends the last block:
        # the server answers it again and the handover is untouched.
        context.state = TftpStateExpectDAT(context)
        context.next_block = 2
        self.send_dat(context, 1, content)
        self.assertGreater(context.sock.sendto.call_count, acks_before)
        self.assertEqual(len(list(journal.entries(state=STAGED))), 1)
        self.assertEqual(journal.get(handover.id).state, STAGED)
        context.end()

    def test_interrupted_transfer_recovers_as_incomplete(self):
        """A transfer that dies mid-way is never staged, and recovery after
        a restart refuses to treat the partial staging file as complete."""
        journal = TftpHandoverJournal(self.handover_dir,
                                      root=self.server_root)
        content = CONTENT_V1 * 100
        context = self.start_upload(journal, "report.bin",
                                    options={"tsize": str(len(content))})
        # Only the first block arrives, then the server "restarts".
        self.send_dat(context, 1, content[:512])
        handover = context.handover
        self.assertEqual(handover.state, RECEIVING)

        journal = TftpHandoverJournal(self.handover_dir,
                                      root=self.server_root)
        summary = journal.recover()
        self.assertEqual(len(summary[STAGED]), 0)
        self.assertEqual(len(summary[INCOMPLETE]), 1)
        with self.assertRaises(TftpException):
            journal.register(journal.get(handover.id))
        context.end()


if __name__ == "__main__":
    unittest.main()
