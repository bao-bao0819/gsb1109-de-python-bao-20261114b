# vim: ts=4 sw=4 et ai:
"""Offline, repeatable event-replay tests for TFTP option negotiation and
transfer teardown.

These tests never touch the network. They drive tftpy's real context and
state objects through a FakeSock (see tftpy.TftpReplay) while maintaining two
independent, evidence-gated completion verdicts:

* the receiver verdict follows the DAT sequence and only completes on
  end-of-file evidence (short block, or the zero tail for an exact-multiple
  file);
* the sender verdict follows the DAT/ACK sequence and only completes once the
  final DAT has been sent and the ACK for that block arrives.

Run with either ``python -m pytest tests/test_replay.py`` or
``python tests/test_replay.py`` from the repository root.
"""

import logging
import os
import tempfile
import unittest

from tftpy.TftpPacketTypes import (
    TftpPacketACK,
    TftpPacketDAT,
    TftpPacketOACK,
    TftpPacketRRQ,
)
from tftpy.TftpReplay import (
    ReceiverCompletionVerdict,
    SenderCompletionVerdict,
    TIMEOUT,
    drive_ack,
    drive_dat,
    drive_timeout,
    new_server_context,
    replay_client_download,
    sent_packets,
    start_rrq,
    start_wrq,
)
from tftpy.TftpShared import DEF_BLKSIZE, TftpTimeout

logging.getLogger("tftpy").setLevel(logging.CRITICAL)


def encode_dat(blocknumber, data):
    pkt = TftpPacketDAT()
    pkt.blocknumber = blocknumber
    pkt.data = data
    return pkt.encode().buffer


def encode_oack(options):
    pkt = TftpPacketOACK()
    pkt.options = options
    return pkt.encode().buffer


def make_file(root, name, size, fill=b"A"):
    path = os.path.join(root, name)
    with open(path, "wb") as handle:
        handle.write(fill * size)
    return path


class ReceiverCompletionVerdictTest(unittest.TestCase):
    """The receiver verdict is driven solely by the DAT sequence."""

    def test_short_block_completes(self):
        verdict = ReceiverCompletionVerdict()
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.dat_received(1, b"A" * DEF_BLKSIZE), "full")
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.dat_received(2, b"B" * 37), "eof")
        self.assertTrue(verdict.complete)

    def test_exact_multiple_needs_zero_tail(self):
        verdict = ReceiverCompletionVerdict()
        verdict.dat_received(1, b"A" * DEF_BLKSIZE)
        verdict.dat_received(2, b"B" * DEF_BLKSIZE)
        # Two full blocks carry no end-of-file evidence on their own.
        self.assertFalse(verdict.complete)
        # The zero-length tail block is the required end-of-file marker.
        self.assertEqual(verdict.dat_received(3, b""), "eof")
        self.assertTrue(verdict.complete)
        self.assertEqual(verdict.evidence[-1], (3, 0, "zero-tail"))

    def test_full_blocks_never_complete(self):
        verdict = ReceiverCompletionVerdict()
        for block in range(1, 10):
            self.assertEqual(
                verdict.dat_received(block, b"X" * DEF_BLKSIZE), "full"
            )
        self.assertFalse(verdict.complete)

    def test_duplicate_and_future_blocks_do_not_complete(self):
        verdict = ReceiverCompletionVerdict()
        verdict.dat_received(1, b"A" * DEF_BLKSIZE)
        self.assertEqual(
            verdict.dat_received(1, b"A" * DEF_BLKSIZE), "duplicate"
        )
        self.assertEqual(verdict.dat_received(3, b"?"), "future")
        self.assertFalse(verdict.complete)
        # In-sequence short block still completes.
        self.assertEqual(verdict.dat_received(2, b"z"), "eof")
        self.assertTrue(verdict.complete)

    def test_negotiated_blksize_governs_short_block(self):
        verdict = ReceiverCompletionVerdict()
        verdict.propose_blksize(1024)
        # A bare proposal must not change the effective block size yet.
        self.assertEqual(verdict.effective_blksize, DEF_BLKSIZE)
        verdict.oack_accepted({"blksize": "1024"})
        self.assertEqual(verdict.effective_blksize, 1024)
        self.assertEqual(verdict.dat_received(1, b"A" * 1024), "full")
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.dat_received(2, b""), "eof")
        self.assertTrue(verdict.complete)


class SenderCompletionVerdictTest(unittest.TestCase):
    """The sender verdict is driven by its DATs and the ACK sequence."""

    def test_final_dat_alone_does_not_complete(self):
        verdict = SenderCompletionVerdict()
        verdict.dat_sent(1, b"A" * DEF_BLKSIZE)
        self.assertIsNone(verdict.final_block)
        verdict.dat_sent(2, b"")  # zero tail marks the final DAT
        self.assertEqual(verdict.final_block, 2)
        # The final DAT has been sent but no final ACK has been observed.
        self.assertFalse(verdict.complete)

    def test_lost_final_ack_resends_do_not_complete(self):
        verdict = SenderCompletionVerdict()
        verdict.dat_sent(1, b"A" * DEF_BLKSIZE)
        verdict.dat_sent(2, b"")
        # Final ACK is lost, so the final DAT is retransmitted twice.
        verdict.dat_sent(2, b"")
        verdict.dat_sent(2, b"")
        self.assertFalse(verdict.complete)
        # A stale ACK for the previous block must not complete it either.
        self.assertEqual(verdict.ack_received(1), "duplicate-ack")
        self.assertFalse(verdict.complete)

    def test_final_ack_completes_only_when_it_matches(self):
        verdict = SenderCompletionVerdict()
        verdict.dat_sent(1, b"A" * DEF_BLKSIZE)
        verdict.dat_sent(2, b"B" * 12)
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.ack_received(2), "final-ack")
        self.assertTrue(verdict.complete)

    def test_ack_before_final_dat_is_ignored_for_completion(self):
        verdict = SenderCompletionVerdict()
        self.assertEqual(verdict.ack_received(1), "ack")
        self.assertFalse(verdict.complete)
        verdict.dat_sent(1, b"A" * DEF_BLKSIZE)
        verdict.dat_sent(2, b"")
        self.assertFalse(verdict.complete)
        verdict.ack_received(2)
        self.assertTrue(verdict.complete)


class BlocksizeNegotiationReplayTest(unittest.TestCase):
    """Rejected negotiation must fall back to the default block size."""

    def test_rejected_proposal_resets_to_default(self):
        # Client proposes 1024; the server ignores options and starts sending
        # default-length (512) data blocks instead of an OACK.
        incoming = [
            encode_dat(1, b"B" * DEF_BLKSIZE),
            encode_dat(2, b"C" * 88),
        ]
        context, _fake, output = replay_client_download(
            incoming, {"blksize": 1024}
        )
        self.assertEqual(context.options, {"blksize": DEF_BLKSIZE})
        self.assertIsNone(context.state)
        self.assertEqual(len(output.getvalue()), DEF_BLKSIZE + 88)

        # The independent verdict must judge short blocks against 512, not the
        # rejected proposal of 1024.
        verdict = ReceiverCompletionVerdict()
        verdict.propose_blksize(1024)
        verdict.negotiation_rejected()
        self.assertEqual(verdict.effective_blksize, DEF_BLKSIZE)
        self.assertEqual(
            verdict.dat_received(1, b"B" * DEF_BLKSIZE), "full"
        )
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.dat_received(2, b"C" * 88), "eof")
        self.assertTrue(verdict.complete)

    def test_accepted_oack_uses_negotiated_length(self):
        incoming = [
            encode_oack({"blksize": "1024"}),
            encode_dat(1, b"A" * 1024),
            encode_dat(2, b"B" * 5),
        ]
        context, _fake, output = replay_client_download(
            incoming, {"blksize": 1024}
        )
        self.assertEqual(context.options, {"blksize": "1024"})
        self.assertIsNone(context.state)
        self.assertEqual(len(output.getvalue()), 1029)


class ServerDownloadReplayTest(unittest.TestCase):
    """Server as sender: replay RRQ/DAT/ACK transcripts fully offline."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_exact_multiple_emits_zero_tail_and_waits_for_final_ack(self):
        make_file(self.root, "exact.bin", 2 * DEF_BLKSIZE)
        context, fake = new_server_context(self.root)
        start_rrq(context, "exact.bin", {})

        receiver = ReceiverCompletionVerdict()
        sender = SenderCompletionVerdict()

        dats = [p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)]
        self.assertEqual([(p.blocknumber, len(p.data)) for p in dats], [(1, 512)])
        sender.dat_sent(dats[0].blocknumber, dats[0].data)
        self.assertEqual(receiver.dat_received(1, dats[0].data), "full")

        drive_ack(context, 1)
        dats = [p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)]
        sender.dat_sent(dats[-1].blocknumber, dats[-1].data)
        receiver.dat_received(dats[-1].blocknumber, dats[-1].data)
        self.assertEqual((dats[-1].blocknumber, len(dats[-1].data)), (2, 512))

        # Sending the second full block must not finish either side.
        self.assertFalse(receiver.complete)
        self.assertFalse(sender.complete)
        self.assertIsNotNone(context.state)

        drive_ack(context, 2)
        tail = [p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)][-1]
        self.assertEqual((tail.blocknumber, len(tail.data)), (3, 0))
        sender.dat_sent(tail.blocknumber, tail.data)
        self.assertEqual(
            receiver.dat_received(tail.blocknumber, tail.data), "eof"
        )
        # Receiver now has every byte; the sender still lacks the final ACK.
        self.assertTrue(receiver.complete)
        self.assertFalse(sender.complete)
        self.assertIsNotNone(context.state)

        self.assertEqual(sender.ack_received(3), "final-ack")
        drive_ack(context, 3)
        self.assertTrue(sender.complete)
        self.assertIsNone(context.state)

    def test_non_multiple_ends_on_short_block(self):
        make_file(self.root, "short.bin", DEF_BLKSIZE + 40)
        context, fake = new_server_context(self.root)
        start_rrq(context, "short.bin", {})
        drive_ack(context, 1)
        last = [p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)][-1]
        self.assertEqual((last.blocknumber, len(last.data)), (2, 40))

        receiver = ReceiverCompletionVerdict()
        receiver.dat_received(1, b"A" * DEF_BLKSIZE)
        self.assertEqual(receiver.dat_received(2, last.data), "eof")
        self.assertTrue(receiver.complete)

        sender = SenderCompletionVerdict()
        sender.dat_sent(1, b"A" * DEF_BLKSIZE)
        sender.dat_sent(2, last.data)
        self.assertFalse(sender.complete)
        drive_ack(context, 2)
        self.assertEqual(sender.ack_received(2), "final-ack")
        self.assertTrue(sender.complete)
        self.assertIsNone(context.state)

    def test_lost_final_ack_two_resends_then_ack(self):
        make_file(self.root, "exact.bin", DEF_BLKSIZE)
        context, fake = new_server_context(self.root, retries=5)
        start_rrq(context, "exact.bin", {})

        sender = SenderCompletionVerdict()
        first = [p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)][0]
        sender.dat_sent(first.blocknumber, first.data)
        drive_ack(context, 1)

        zero_tail = [
            p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)
        ][-1]
        self.assertEqual(len(zero_tail.data), 0)
        sender.dat_sent(zero_tail.blocknumber, zero_tail.data)
        self.assertFalse(sender.complete)

        # Final ACK lost twice: two retransmissions of the zero-tail DAT.
        drive_timeout(context)
        resent1 = [
            p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)
        ][-1]
        sender.dat_sent(resent1.blocknumber, resent1.data)
        drive_timeout(context)
        resent2 = [
            p for p in sent_packets(fake) if isinstance(p, TftpPacketDAT)
        ][-1]
        sender.dat_sent(resent2.blocknumber, resent2.data)

        self.assertIsNotNone(context.state)
        self.assertFalse(sender.complete)
        wire = [
            (p.blocknumber, len(p.data))
            for p in sent_packets(fake)
            if isinstance(p, TftpPacketDAT)
        ]
        self.assertEqual(wire, [(1, 512), (2, 0), (2, 0), (2, 0)])

        # Only the matching final ACK completes the sender side.
        drive_ack(context, 2)
        self.assertEqual(sender.ack_received(2), "final-ack")
        self.assertTrue(sender.complete)
        self.assertIsNone(context.state)

    def test_exhausted_retries_never_complete(self):
        make_file(self.root, "exact.bin", DEF_BLKSIZE)
        context, _fake = new_server_context(self.root, retries=3)
        start_rrq(context, "exact.bin", {})
        drive_ack(context, 1)
        # The final ACK never arrives and retries are exhausted.
        drive_timeout(context)
        drive_timeout(context)
        with self.assertRaises(TftpTimeout):
            drive_timeout(context)
        self.assertIsNotNone(context.state)


class ClientDownloadReplayTest(unittest.TestCase):
    """Client as receiver: replay server traffic through the client context."""

    def test_accepted_oack_exact_multiple_zero_tail(self):
        incoming = [
            encode_oack({"blksize": "1024"}),
            encode_dat(1, b"A" * 1024),
            encode_dat(2, b"B" * 1024),
            encode_dat(3, b""),
        ]
        context, fake, output = replay_client_download(
            incoming, {"blksize": 1024}
        )
        self.assertEqual(context.options, {"blksize": "1024"})
        self.assertIsNone(context.state)
        self.assertEqual(len(output.getvalue()), 2048)
        self.assertEqual(
            [p.blocknumber for p in sent_packets(fake) if isinstance(p, TftpPacketACK)],
            [0, 1, 2, 3],
        )

    def test_dropped_first_packet_triggers_rrq_resend(self):
        incoming = [
            TIMEOUT,
            encode_dat(1, b"X" * DEF_BLKSIZE),
            encode_dat(2, b"Y" * 7),
        ]
        context, fake, output = replay_client_download(incoming, {})
        rrq_count = sum(
            1 for p in sent_packets(fake) if isinstance(p, TftpPacketRRQ)
        )
        self.assertEqual(rrq_count, 2)
        self.assertEqual(len(output.getvalue()), DEF_BLKSIZE + 7)
        self.assertIsNone(context.state)
        self.assertEqual(context.retry_count, 0)


class ServerUploadReplayTest(unittest.TestCase):
    """Server as receiver: replay WRQ/DAT/ACK transcripts offline."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_exact_multiple_zero_tail_completes_receiver(self):
        context, fake = new_server_context(self.root)
        start_wrq(context, "up.bin", {"blksize": "1024"})
        self.assertIsInstance(
            sent_packets(fake)[0], TftpPacketOACK
        )

        receiver = ReceiverCompletionVerdict()
        receiver.oack_accepted({"blksize": "1024"})

        self.assertIsNotNone(drive_dat(context, 1, b"A" * 1024))
        self.assertEqual(receiver.dat_received(1, b"A" * 1024), "full")
        self.assertFalse(receiver.complete)

        self.assertIsNotNone(drive_dat(context, 2, b"B" * 1024))
        self.assertEqual(receiver.dat_received(2, b"B" * 1024), "full")
        self.assertFalse(receiver.complete)

        # Zero-length tail ends the server (receiver) state.
        self.assertIsNone(drive_dat(context, 3, b""))
        self.assertEqual(receiver.dat_received(3, b""), "eof")
        self.assertTrue(receiver.complete)
        self.assertEqual(
            [p.blocknumber for p in sent_packets(fake) if isinstance(p, TftpPacketACK)],
            [1, 2, 3],
        )


class ReplayDeterminismTest(unittest.TestCase):
    """Replaying the same transcript twice must yield identical results."""

    def _run_once(self, root):
        make_file(root, "exact.bin", DEF_BLKSIZE)
        context, fake = new_server_context(root, retries=5)
        start_rrq(context, "exact.bin", {})
        drive_ack(context, 1)
        drive_timeout(context)
        drive_ack(context, 2)
        wire = [buf for buf, _addr in fake.sent]
        return wire, context.state is None

    def test_repeated_replay_is_identical(self):
        root_a = tempfile.mkdtemp()
        root_b = tempfile.mkdtemp()
        wire_a, done_a = self._run_once(root_a)
        wire_b, done_b = self._run_once(root_b)
        self.assertEqual(wire_a, wire_b)
        self.assertTrue(done_a and done_b)


if __name__ == "__main__":
    unittest.main()
