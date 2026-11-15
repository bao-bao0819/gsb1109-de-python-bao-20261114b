# vim: ts=4 sw=4 et ai:
"""Tests for offline transfer diagnosis and the structured packet tracer."""

import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tftpy
from tftpy.TftpContexts import (
    TftpContextClientUpload,
    TftpContextServer,
)
from tftpy.offline_diag import (
    StorageDigest,
    TransferAnalysis,
    format_ranges,
    load_events,
    load_storage,
    main as diag_main,
    render_text,
    to_ranges,
)
from tftpy.transfer_trace import TransferTracer


class FakeUdpSocket:
    """In-process UDP socket replacement.

    With ``auto_ack`` it answers WRQ with ACK 0 and every DAT with a matching
    ACK, emulating a receiver for the client state machine. With it disabled
    it only records outbound packets, suitable for driving the server state
    machine with scripted inbound DAT packets.
    """

    def __init__(self, *args, auto_ack=True, **kwargs):
        self._queue = []
        self.sent = []
        self.closed = False
        self.auto_ack = auto_ack

    def bind(self, addr):
        pass

    def settimeout(self, timeout):
        pass

    def setblocking(self, flag):
        pass

    def getsockname(self):
        return ("127.0.0.1", 40000)

    def fileno(self):
        return -1

    def close(self):
        self.closed = True

    def sendto(self, buffer, addr):
        self.sent.append((bytes(buffer), addr))
        if not self.auto_ack:
            return
        opcode = struct.unpack("!H", buffer[:2])[0]
        if opcode == 2:  # WRQ -> ACK block 0
            self.enqueue(struct.pack("!HH", 4, 0), addr)
        elif opcode == 3:  # DAT -> ACK its block number
            block = struct.unpack("!H", buffer[2:4])[0]
            self.enqueue(struct.pack("!HH", 4, block), addr)

    def enqueue(self, buffer, addr=("127.0.0.1", 50000)):
        self._queue.append((bytes(buffer), addr))

    def recvfrom(self, size):
        if not self._queue:
            raise socket.timeout("no scripted packet")
        return self._queue.pop(0)


def _fake_socket_factory(holder, **socket_kwargs):
    def factory(*args, **kwargs):
        holder["sock"] = FakeUdpSocket(*args, **socket_kwargs)
        return holder["sock"]
    return factory



def loopback_bind_allowed():
    """Restricted sandboxes can deny bind(); such environments cannot run
    the live socket integration tests."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def write_synthetic_traces(tmpdir):
    """Build a pair of traces with all three gap classes present.

    Blocks 1..10 are sent by the client; the receiver only observes 1..7.
    The receiver stores 1..4 and ACKs 1..7, but the client only receives
    ACKs for 1..6. Storage only accounts for blocks 1..4.
    """
    sender_path = os.path.join(tmpdir, "client.jsonl")
    receiver_path = os.path.join(tmpdir, "server.jsonl")
    sender = TransferTracer(sender_path, role="client")
    receiver = TransferTracer(receiver_path, role="server")
    base = 1_700_000_000.0
    for block in range(1, 11):
        size = 4 if block == 10 else 516
        sender.event("tx", "DAT", block=block, size=size, wall=base + block)
        if block <= 7:
            receiver.event("rx", "DAT", block=block, size=size,
                           wall=base + block + 0.05)
        if block <= 4:
            receiver.event("store", "DAT", block=block, size=size,
                           wall=base + block + 0.06)
        if block <= 7:
            receiver.event("tx", "ACK", block=block, size=4,
                           wall=base + block + 0.07)
        if block <= 6:
            sender.event("rx", "ACK", block=block, size=4,
                         wall=base + block + 0.1)
    sender.close()
    receiver.close()
    return sender_path, receiver_path


class OfflineDiagClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sender_path, self.receiver_path = write_synthetic_traces(
            self.tmpdir)

    def _analysis(self, skew=2.0, storage_size=4 * 512):
        sender = load_events(self.sender_path, "sender", "sender")
        receiver = load_events(self.receiver_path, "receiver", "receiver")
        storage = StorageDigest(
            path=os.path.join(self.tmpdir, "out.bin"),
            size=storage_size, blksize=512, derived=True)
        return TransferAnalysis(sender, receiver, storage, clock_skew=skew)

    def test_three_gap_categories(self):
        analysis = self._analysis()
        categories = {c.key: c for c in analysis.classify()}

        self.assertEqual(
            categories["SENT_NOT_SEEN"].blocks, [8, 9, 10])
        self.assertEqual(categories["SEEN_NOT_ACKED"].blocks, [7])
        # ACKed 1..6, storage only holds 1..4.
        self.assertEqual(categories["ACKED_NOT_STORED"].blocks, [5, 6])

    def test_ranges_are_contiguous(self):
        self.assertEqual(to_ranges([1, 2, 3, 7, 8, 11]),
                         [(1, 3), (7, 8), (11, 11)])
        self.assertEqual(format_ranges([1, 2, 3, 7]), "1-3, 7")

    def test_empty_terminal_block_excluded_from_storage_gap(self):
        from tftpy.offline_diag import Event, SideTimeline
        sender = SideTimeline("sender", "sender")
        receiver = SideTimeline("receiver", "receiver")
        # Two data blocks and one empty terminal DAT, all ACKed.
        for block, size in ((1, 516), (2, 516), (3, 4)):
            sender.add(Event("sender", 1.0, 1.0, "tx", "DAT", block, size))
            receiver.add(Event("receiver", 1.0, 1.0, "rx", "DAT", block, size))
            sender.add(Event("sender", 1.1, 1.1, "rx", "ACK", block, 4))
        # Storage holds only the two data-bearing blocks.
        storage = StorageDigest(path="x", size=2 * 512, blksize=512)
        analysis = TransferAnalysis(sender, receiver, storage, clock_skew=1.0)
        categories = {c.key: c for c in analysis.classify()}
        self.assertEqual(categories["ACKED_NOT_STORED"].blocks, [])

    def test_multiple_hypotheses_retained_without_narrowing_evidence(self):
        analysis = self._analysis()
        categories = {c.key: c for c in analysis.classify()}
        sent_gap = categories["SENT_NOT_SEEN"]
        verdicts = {h.verdict for h in sent_gap.hypotheses}
        # No single stage may be asserted as the culprit; several candidates
        # remain possible and each carries an instrumentation point.
        self.assertIn("possible", verdicts)
        self.assertGreaterEqual(
            sum(1 for h in sent_gap.hypotheses if h.verdict == "possible"), 3)
        for hypothesis in sent_gap.hypotheses:
            self.assertTrue(hypothesis.instrument.strip())

    def test_clock_skew_band_forbids_ordering_claims(self):
        analysis = self._analysis(skew=2.0)
        # Delta of 0.05s sits inside the 2s skew band.
        verdict, _ = analysis.order_confidence(100.0, 100.05)
        self.assertEqual(verdict, "indeterminate-in-skew-band")
        verdict, _ = analysis.order_confidence(100.0, 103.0)
        self.assertEqual(verdict, "consistent")
        verdict, _ = analysis.order_confidence(100.0, 97.0)
        self.assertEqual(verdict, "inconsistent-check-skew")
        verdict, _ = analysis.order_confidence(None, 100.0)
        self.assertEqual(verdict, "no-timestamp")

    def test_report_contains_all_categories_and_instrumentation(self):
        report = render_text(self._analysis())
        for marker in ("SENT_NOT_SEEN", "SEEN_NOT_ACKED", "ACKED_NOT_STORED",
                       "sendDAT", "handleDat", "clock skew bound"):
            self.assertIn(marker, report)

    def test_cli_main_writes_report(self):
        output = os.path.join(self.tmpdir, "report.txt")
        rc = diag_main([
            "--sender-trace", self.sender_path,
            "--receiver-trace", self.receiver_path,
            "--clock-skew", "2",
            "--storage-size", str(4 * 512),
            "--blksize", "512",
            "--output", output,
        ])
        self.assertEqual(rc, 0)
        self.assertIn("SENT_NOT_SEEN", Path(output).read_text())

    def test_json_output_has_ranges(self):
        analysis = self._analysis()
        from tftpy.offline_diag import render_json
        payload = json.loads(render_json(analysis))
        sent_gap = next(
            c for c in payload["categories"] if c["key"] == "SENT_NOT_SEEN")
        self.assertEqual(sent_gap["ranges"], [[8, 10]])


class TextLogParsingTest(unittest.TestCase):
    def test_stock_text_log_is_parsed(self):
        tmpdir = tempfile.mkdtemp()
        log_path = os.path.join(tmpdir, "tftpy.log")
        Path(log_path).write_text(
            "[2026-11-15 10:00:00,100] Sending DAT packet 7\n"
            "[2026-11-15 10:00:00,150] Handling DAT packet - block 7\n"
            "[2026-11-15 10:00:00,160] Sending ack to block 7\n"
            "[2026-11-15 10:00:00,200] Received ACK for packet 7\n"
            "[2026-11-15 10:00:01,000] Timeout waiting for traffic\n"
            "[2026-11-15 10:00:02,000] Resending packet DAT packet: block 7\n",
            encoding="utf-8",
        )
        timeline = load_events(log_path, "side")
        self.assertIn(7, timeline.dat_tx)
        self.assertIn(7, timeline.dat_rx)
        self.assertIn(7, timeline.ack_tx)
        self.assertIn(7, timeline.ack_rx)
        self.assertEqual(len(timeline.dat_tx[7]), 2)
        self.assertTrue(
            any(e.note == "timeout" for e in timeline.notes))
        self.assertAlmostEqual(
            timeline.dat_rx[7][0].wall,
            timeline.dat_tx[7][0].wall + 0.05, places=3)


class StorageDigestTest(unittest.TestCase):
    def test_explicit_block_map_is_not_derived(self):
        tmpdir = tempfile.mkdtemp()
        digest_path = os.path.join(tmpdir, "digest.json")
        Path(digest_path).write_text(json.dumps({
            "path": "/var/tftp/out.bin",
            "size": 1536,
            "blksize": 512,
            "blocks_present": [1, 2, 4],
            "sha256": "abc",
        }), encoding="utf-8")
        digest = load_storage(digest_path, None, 512)
        self.assertFalse(digest.derived)
        self.assertEqual(digest.stored_blocks(), {1, 2, 4})

    def test_size_derives_contiguous_blocks(self):
        digest = StorageDigest(path="x", size=1025, blksize=512)
        self.assertTrue(digest.derived)
        self.assertEqual(digest.stored_blocks(), {1, 2, 3})

    def test_digest_built_directly_from_file(self):
        from tftpy.offline_diag import build_storage_digest
        tmpdir = tempfile.mkdtemp()
        target = os.path.join(tmpdir, "out.bin")
        payload = b"z" * (512 + 7)
        Path(target).write_bytes(payload)
        digest = build_storage_digest(target, blksize=512, expected_size=4096)
        self.assertEqual(digest.size, len(payload))
        self.assertTrue(digest.derived)
        self.assertEqual(digest.stored_blocks(), {1, 2})
        self.assertEqual(digest.expected_size, 4096)
        self.assertIsNotNone(digest.digest_sha256)
        self.assertIsNotNone(digest.taken_at)


@unittest.skipUnless(loopback_bind_allowed(),
                     "loopback UDP bind() denied in this environment")
class TracerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()
        self.server_trace_dir = os.path.join(self.tmpdir, "server-traces")
        self.client_trace = os.path.join(self.tmpdir, "client.jsonl")
        self.payload = bytes((i % 256 for i in range(4096)))
        self.input_path = os.path.join(self.tmpdir, "input.bin")
        Path(self.input_path).write_bytes(self.payload)
        self.output_name = "uploaded.bin"

        self.server = tftpy.TftpServer(
            self.root, trace_dir=self.server_trace_dir)
        self.thread = threading.Thread(
            target=self.server.listen,
            kwargs={"listenip": "127.0.0.1", "listenport": 0, "timeout": 1},
            daemon=True,
        )
        self.thread.start()
        deadline = time.time() + 5
        while self.server.listenport is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.server.listenport)

    def tearDown(self):
        self.server.stop(now=True)
        self.thread.join(timeout=5)

    def _client(self):
        return tftpy.TftpClient(
            "127.0.0.1", self.server.listenport, {},
            tracer=TransferTracer(self.client_trace, role="client"))

    def test_full_upload_traces_align_without_gaps(self):
        self._client().upload(self.output_name, self.input_path)

        server_traces = [
            os.path.join(self.server_trace_dir, name)
            for name in os.listdir(self.server_trace_dir)
        ]
        self.assertEqual(len(server_traces), 1)
        sender = load_events(self.client_trace, "sender", "sender")
        receiver = load_events(server_traces[0], "receiver", "receiver")

        output_path = os.path.join(self.root, self.output_name)
        self.assertEqual(os.path.getsize(output_path), len(self.payload))
        storage = StorageDigest(
            path=output_path, size=len(self.payload), blksize=512)
        analysis = TransferAnalysis(
            sender, receiver, storage, clock_skew=1.0)
        categories = analysis.classify()
        for category in categories:
            self.assertEqual(category.blocks, [], msg=category.key)
        # Eight full data blocks plus the empty terminal block were sent.
        self.assertEqual(len(analysis.sent), 9)
        self.assertEqual(len(analysis.stored), 8)

    def test_truncated_storage_is_flagged_acked_not_stored(self):
        self._client().upload(self.output_name, self.input_path)
        server_traces = [
            os.path.join(self.server_trace_dir, name)
            for name in os.listdir(self.server_trace_dir)
        ]
        sender = load_events(self.client_trace, "sender", "sender")
        receiver = load_events(server_traces[0], "receiver", "receiver")

        truncated = 2 * 512
        storage = StorageDigest(
            path=os.path.join(self.root, self.output_name),
            size=truncated, blksize=512)
        analysis = TransferAnalysis(
            sender, receiver, storage, clock_skew=1.0)
        categories = {c.key: c for c in analysis.classify()}
        self.assertEqual(
            categories["ACKED_NOT_STORED"].blocks, [3, 4, 5, 6, 7, 8])
        write_stage = next(
            h for h in categories["ACKED_NOT_STORED"].hypotheses
            if "write" in h.stage)
        self.assertEqual(write_stage.verdict, "possible")


class StateMachineTraceTest(unittest.TestCase):
    """Drives the real client/server state machines over a fake socket so the
    tracer wiring is covered even where loopback bind() is unavailable."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.blksize = 512
        # 2.5 blocks: two full blocks plus a short final block, no empty DAT.
        self.payload = b"a" * self.blksize + b"b" * self.blksize + b"c" * 10

    def test_client_upload_state_machine_emits_trace(self):
        input_path = os.path.join(self.tmpdir, "input.bin")
        Path(input_path).write_bytes(self.payload)
        trace_path = os.path.join(self.tmpdir, "client.jsonl")
        holder = {}
        tracer = TransferTracer(trace_path, role="client")
        with patch("tftpy.TftpContexts.socket.socket",
                   _fake_socket_factory(holder)):
            context = TftpContextClientUpload(
                "127.0.0.1", 69, "up.bin", input_path, {}, None,
                timeout=5, tracer=tracer)
            context.start()
            context.end()
        timeline = load_events(trace_path, "sender", "sender")
        # Blocks 1..3 sent; block 3 is the short final block.
        self.assertEqual(set(timeline.dat_tx), {1, 2, 3})
        self.assertEqual(set(timeline.ack_rx), {0, 1, 2, 3})
        self.assertTrue(
            any(e.note == "wrq-sent" for e in timeline.events))

    def test_server_receiver_state_machine_writes_and_traces(self):
        output_path = os.path.join(self.tmpdir, "out.bin")
        trace_path = os.path.join(self.tmpdir, "server.jsonl")
        holder = {}
        tracer = TransferTracer(trace_path, role="server")
        with patch("tftpy.TftpContexts.socket.socket",
                   _fake_socket_factory(holder, auto_ack=False)):
            context = TftpContextServer(
                "127.0.0.1", 50000, 5, self.tmpdir, tracer=tracer)
            wrq = tftpy.TftpPacketTypes.TftpPacketWRQ()
            wrq.filename = "out.bin"
            wrq.mode = "octet"
            wrq.options = {}
            context.start(wrq.encode().buffer)
            for block in (1, 2):
                dat = tftpy.TftpPacketTypes.TftpPacketDAT()
                dat.blocknumber = block
                dat.data = b"x" * self.blksize
                holder["sock"].enqueue(dat.encode().buffer)
                context.cycle()
            dat = tftpy.TftpPacketTypes.TftpPacketDAT()
            dat.blocknumber = 3
            dat.data = b"y" * 10
            holder["sock"].enqueue(dat.encode().buffer)
            context.cycle()
            self.assertIsNone(context.state)
            context.end()
        self.assertEqual(
            os.path.getsize(output_path), 2 * self.blksize + 10)
        timeline = load_events(trace_path, "receiver", "receiver")
        self.assertEqual(set(timeline.dat_rx), {1, 2, 3})
        self.assertEqual(set(timeline.dat_stored), {1, 2, 3})
        self.assertEqual(set(timeline.ack_tx), {0, 1, 2, 3})


if __name__ == "__main__":
    unittest.main()
