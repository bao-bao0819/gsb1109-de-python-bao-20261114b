import os
import subprocess
import sys
import tempfile
import unittest

import tftpy
from tftpy.TftpObservation import (
    TimeInterval,
    analyze_observations,
    load_observations,
    parse_observation_line,
    parse_observations,
    render_report,
    analysis_to_json,
)

HOME = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HOME)
SCRIPT = os.path.join(ROOT, "bin", "tftpy_observation.py")


def dat(time_value, block, length):
    return "%s DAT block=%d len=%d" % (time_value, block, length)


def ack(time_value, block):
    return "%s ACK block=%d" % (time_value, block)


class ObservationParserTest(unittest.TestCase):
    def test_parses_dat_variants(self):
        observation = parse_observation_line("100.05 DAT block=5 len=512")
        self.assertEqual(observation.opcode, "DAT")
        self.assertEqual(observation.block, 5)
        self.assertEqual(observation.data_bytes, 512)
        self.assertEqual(observation.time, 100.05)

        observation = parse_observation_line(
            "100.05 DATA block: 6 length 256")
        self.assertEqual(observation.opcode, "DAT")
        self.assertEqual(observation.block, 6)
        self.assertEqual(observation.data_bytes, 256)

        observation = parse_observation_line(
            "100.05 DAT block 7 bytes 128")
        self.assertEqual(observation.data_bytes, 128)

    def test_parses_iso_timestamp(self):
        observation = parse_observation_line(
            "2026-11-14T10:00:00 DAT block=1 len=512")
        self.assertEqual(observation.block, 1)
        self.assertGreater(observation.time, 0)

    def test_parses_utc_suffixed_timestamp(self):
        zulu = parse_observation_line(
            "2026-11-14T10:00:00Z DAT block=1 len=8")
        explicit = parse_observation_line(
            "2026-11-14T10:00:00+00:00 DAT block=1 len=8")
        self.assertAlmostEqual(zulu.time, explicit.time)

    def test_ignores_non_tftp_and_blank_lines(self):
        self.assertIsNone(parse_observation_line("100.0 unrelated chatter"))
        self.assertIsNone(parse_observation_line(""))
        self.assertIsNone(parse_observation_line("   "))

    def test_dat_without_length_is_strict_by_default(self):
        with self.assertRaises(ValueError):
            parse_observation_line("100.0 DAT block=5")
        self.assertIsNone(
            parse_observation_line("100.0 DAT block=5", strict=False))

    def test_dat_requires_valid_block_number(self):
        with self.assertRaises(ValueError):
            parse_observation_line("100.0 DAT len=512")
        with self.assertRaises(ValueError):
            parse_observation_line("100.0 DAT block=70000 len=512")
        self.assertIsNone(
            parse_observation_line("100.0 DAT len=512", strict=False))

    def test_control_packets_have_no_payload(self):
        observation = parse_observation_line("100.0 ACK block=4")
        self.assertEqual(observation.opcode, "ACK")
        self.assertEqual(observation.block, 4)
        self.assertEqual(observation.data_bytes, 0)


class FullyObservedTransferTest(unittest.TestCase):
    def setUp(self):
        # Handshake and the whole transfer are in the log. Block 2 is
        # delivered twice due to a lost ACK.
        lines = [
            "0.000 RRQ filename=firmware mode=octet",
            "0.010 ACK block=0",
            dat(0.100, 1, 512),
            ack(0.110, 1),
            dat(0.200, 2, 512),
            dat(0.500, 2, 512),
            ack(0.510, 2),
            dat(0.600, 3, 100),
        ]
        self.analysis = analyze_observations(parse_observations(lines))

    def test_classifies_payload(self):
        self.assertEqual(self.analysis.observed_dat_packets, 4)
        self.assertEqual(self.analysis.observed_dat_bytes, 1636)
        self.assertEqual(self.analysis.unique_payload_bytes, 1124)
        self.assertEqual(self.analysis.unique_blocks, 3)
        self.assertEqual(self.analysis.retransmit_bytes, 512)
        self.assertEqual(self.analysis.retransmit_packets, 1)
        self.assertEqual(self.analysis.undeterminable_bytes, 0)
        self.assertEqual(self.analysis.undeterminable_packets, 0)
        self.assertEqual(self.analysis.control_packets, 4)
        self.assertTrue(self.analysis.handshake_observed)

    def test_first_deliveries_are_exact(self):
        for record in self.analysis.blocks:
            self.assertNotIsInstance(record.first_delivery, TimeInterval)
        self.assertEqual(
            [record.first_delivery for record in self.analysis.blocks],
            [0.100, 0.200, 0.600],
        )

    def test_rate_is_exact(self):
        rate = self.analysis.rate
        self.assertTrue(rate.exact)
        self.assertEqual(rate.lower_bps, rate.upper_bps)
        # 1124 bytes over a 0.5 second span.
        self.assertAlmostEqual(rate.upper_bps, 1124 * 8.0 / 0.5)

    def test_report_and_json_reconcile(self):
        report = render_report(self.analysis)
        self.assertIn("1124 + 512 + 0 = 1636", report)
        decoded = __import__("json").loads(analysis_to_json(self.analysis))
        self.assertEqual(
            decoded["unique_payload_bytes"]
            + decoded["retransmit_bytes"]
            + decoded["undeterminable_bytes"],
            decoded["observed_dat_bytes"],
        )


class MidTransferLogTest(unittest.TestCase):
    def setUp(self):
        # No handshake: capture joined while blocks 5 and 6 were in flight.
        # Block 5 is seen twice; the genuine first copy predates the log.
        lines = [
            dat(10.0, 5, 512),
            dat(12.0, 6, 512),
            dat(20.0, 5, 512),
            dat(22.0, 7, 256),
        ]
        self.analysis = analyze_observations(parse_observations(lines))

    def test_first_sightings_are_undeterminable(self):
        self.assertFalse(self.analysis.handshake_observed)
        self.assertEqual(self.analysis.unique_payload_bytes, 0)
        self.assertEqual(self.analysis.unique_blocks, 0)
        self.assertEqual(self.analysis.undeterminable_bytes, 1280)
        self.assertEqual(self.analysis.undeterminable_packets, 3)
        self.assertEqual(self.analysis.retransmit_bytes, 512)
        self.assertEqual(self.analysis.retransmit_packets, 1)

    def test_duplicate_never_anchors_first_delivery(self):
        block5 = self.analysis.blocks[0]
        self.assertIsInstance(block5.first_delivery, TimeInterval)
        # No predecessor on record, so no lower bound; the inclusive upper
        # bound is the first arrival, never the duplicate at t=20.
        self.assertIsNone(block5.first_delivery.lower)
        self.assertEqual(block5.first_delivery.upper, 10.0)
        self.assertEqual(block5.arrivals, [10.0, 20.0])
        self.assertEqual(block5.duplicate_arrivals, 1)

        block6 = self.analysis.blocks[1]
        self.assertIsInstance(block6.first_delivery, TimeInterval)
        self.assertEqual(block6.first_delivery.lower, 10.0)
        self.assertEqual(block6.first_delivery.upper, 12.0)

    def test_rate_has_fast_bound_but_no_slow_bound(self):
        rate = self.analysis.rate
        self.assertFalse(rate.exact)
        # The transfer started before the log, so no positive slow bound
        # exists. The fastest defensible rate uses the narrowest span:
        # last lower bound (12) - first upper bound (10) = 2 seconds.
        self.assertIsNone(rate.lower_bps)
        self.assertAlmostEqual(rate.upper_bps, 1280 * 8.0 / 2.0)

    def test_accounting_reconciles(self):
        self.analysis.verify_accounting()


class BracketedFirstDeliveryTest(unittest.TestCase):
    def test_two_arrivals_bracket_the_missed_first_copy(self):
        # Predecessor block 4 is on record at t=8. Block 5 arrives at t=18
        # and again at t=90. The first delivery of 5 lies in (8, 18]; using
        # t=90 as the first delivery would be wrong.
        lines = [
            dat(8.0, 4, 512),
            dat(18.0, 5, 512),
            dat(90.0, 5, 512),
        ]
        analysis = analyze_observations(parse_observations(lines))
        block5 = next(
            record for record in analysis.blocks if record.block == 5)
        self.assertIsInstance(block5.first_delivery, TimeInterval)
        self.assertEqual(block5.first_delivery.lower, 8.0)
        self.assertEqual(block5.first_delivery.upper, 18.0)
        self.assertEqual(analysis.retransmit_packets, 1)
        self.assertEqual(analysis.retransmit_bytes, 512)
        analysis.verify_accounting()


class BrokenChainTest(unittest.TestCase):
    def test_chain_break_marks_successors_undeterminable(self):
        lines = [
            "0.0 RRQ filename=f mode=octet",
            "0.01 ACK block=0",
            dat(0.1, 1, 512),
            # Block 2 was delivered before this point but never logged.
            dat(0.3, 3, 512),
            dat(0.4, 4, 512),
        ]
        analysis = analyze_observations(parse_observations(lines))
        classifications = {
            record.block: record.classification
            for record in analysis.blocks
        }
        self.assertEqual(classifications[1], "unique")
        self.assertEqual(classifications[3], "undeterminable")
        self.assertEqual(classifications[4], "undeterminable")
        self.assertEqual(analysis.unique_payload_bytes, 512)
        self.assertEqual(analysis.undeterminable_bytes, 1024)
        block3 = next(
            record for record in analysis.blocks if record.block == 3)
        # No record for block 2, so there is no lower bound for block 3.
        self.assertIsNone(block3.first_delivery.lower)
        analysis.verify_accounting()


class RolloverTest(unittest.TestCase):
    def test_block_number_rollover_starts_new_generation(self):
        # The log joins at block 65535, so the chain is not anchored to a
        # handshake; the checks focus on generation tracking and repeats.
        lines = [
            dat(0.1, 65535, 512),
            dat(0.2, 0, 512),
            dat(0.3, 1, 64),
            dat(0.4, 0, 512),
        ]
        analysis = analyze_observations(parse_observations(lines))
        self.assertEqual(
            [record.generation for record in analysis.blocks],
            [0, 1, 1],
        )
        self.assertTrue(all(
            record.classification == "undeterminable"
            for record in analysis.blocks
        ))
        self.assertEqual(analysis.undeterminable_bytes, 1088)
        self.assertEqual(analysis.retransmit_bytes, 512)
        self.assertEqual(analysis.retransmit_packets, 1)
        analysis.verify_accounting()

    def test_straggler_retransmit_after_rollover(self):
        # Block 65535 is re-sent after the wrapped generation has already
        # reached block 1; it must be billed as a retransmit, not as a new
        # block of the current generation.
        lines = [
            dat(0.1, 65535, 512),
            dat(0.2, 0, 512),
            dat(0.3, 1, 512),
            dat(0.4, 65535, 512),
        ]
        analysis = analyze_observations(parse_observations(lines))
        block_last = next(
            record for record in analysis.blocks if record.block == 65535)
        self.assertEqual(len(analysis.blocks), 3)
        self.assertEqual(block_last.arrivals, [0.1, 0.4])
        self.assertEqual(analysis.retransmit_packets, 1)
        self.assertEqual(analysis.retransmit_bytes, 512)
        analysis.verify_accounting()

    def test_missing_block_zero_at_rollover_starts_new_generation(self):
        # Block 0 of the wrapped generation was never logged; block 1 must
        # still open the new generation rather than resolve against the
        # block 1 that existed before the wrap.
        lines = [
            dat(0.1, 65534, 512),
            dat(0.2, 65535, 512),
            dat(0.3, 1, 64),
        ]
        analysis = analyze_observations(parse_observations(lines))
        self.assertEqual(
            [record.generation for record in analysis.blocks],
            [0, 0, 1],
        )
        self.assertEqual(len(analysis.blocks), 3)
        analysis.verify_accounting()


class EmptyLogTest(unittest.TestCase):
    def test_empty_observation_set_reconciles(self):
        analysis = analyze_observations([])
        self.assertEqual(analysis.observed_dat_bytes, 0)
        self.assertEqual(analysis.unique_payload_bytes, 0)
        self.assertEqual(analysis.retransmit_bytes, 0)
        self.assertEqual(analysis.undeterminable_bytes, 0)
        self.assertIsNone(analysis.rate.lower_bps)
        self.assertIsNone(analysis.rate.upper_bps)
        analysis.verify_accounting()
        self.assertIn("TFTP observation analysis", render_report(analysis))

    def test_only_control_traffic_reconciles(self):
        analysis = analyze_observations(
            parse_observations(["0.0 RRQ filename=f mode=octet"]))
        self.assertEqual(analysis.control_packets, 1)
        self.assertEqual(analysis.observed_dat_packets, 0)
        analysis.verify_accounting()


class InstantaneousCaptureTest(unittest.TestCase):
    def test_zero_span_uses_one_second_convention(self):
        lines = [
            "0.0 RRQ filename=f mode=octet",
            "0.01 ACK block=0",
            dat(0.5, 1, 512),
        ]
        analysis = analyze_observations(parse_observations(lines))
        self.assertTrue(analysis.rate.exact)
        self.assertEqual(analysis.rate.upper_bps, 512 * 8.0)
        self.assertIn("1 second", analysis.duration_note)


class CliTest(unittest.TestCase):
    def test_cli_emits_reconciled_report(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".log", delete=False) as logfile:
            logfile.write("0.0 RRQ filename=f mode=octet\n")
            logfile.write("0.01 ACK block=0\n")
            logfile.write(dat(0.1, 1, 512) + "\n")
            logfile.write(dat(0.2, 1, 512) + "\n")
            path = logfile.name
        try:
            env = dict(os.environ)
            env["PYTHONPATH"] = HOME + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [sys.executable, SCRIPT, path],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            self.assertIn("512 + 512 + 0 = 1024", completed.stdout)

            completed = subprocess.run(
                [sys.executable, SCRIPT, "--json", path],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            self.assertIn("retransmit_bytes", completed.stdout)
        finally:
            os.unlink(path)

    def test_load_observations_from_file(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".log", delete=False) as logfile:
            logfile.write(dat(1.0, 1, 10) + "\n")
            logfile.write("garbage line without opcode\n")
            path = logfile.name
        try:
            observations = load_observations(path)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].data_bytes, 10)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
