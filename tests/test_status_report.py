import importlib.util
import os
import unittest

sys_path_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_status_report():
    path = os.path.join(sys_path_root, "examples", "status_report.py")
    spec = importlib.util.spec_from_file_location("status_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status_report = _load_status_report()

HEADER = os.path.join(sys_path_root, "examples", "c_files", "device_status.h")


def _constants(report):
    return {name: val for name, val in report.constants}


def _diags_for(report, needle):
    return [d for d in report.diagnostics if needle in d.message]


class TestStatusReport32(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = status_report.evaluate_file(HEADER, 32)
        cls.consts = _constants(cls.report)

    def test_high_flag_undefined_on_32bit(self):
        # 1UL << 40 is undefined when long is 32 bits wide.
        self.assertEqual(self.consts["DEV_FLAG_HIGH"].v, 0)
        diags = _diags_for(self.report, "shift count 40")
        self.assertEqual(len(diags), 2)
        for diag in diags:
            self.assertEqual(diag.kind, "undefined behavior")
            self.assertIn("device_status.h:", diag.coord)

    def test_diagnostic_locations(self):
        coords = {d.coord.rsplit(":", 2)[-2] for d in self.report.diagnostics}
        # Lines 39, 42, 46 and 49 of device_status.h hold the risky expressions.
        self.assertTrue({"39", "42", "46", "49"} <= coords)

    def test_mixed_signed_unsigned(self):
        # 1u + -1: the int operand converts to unsigned, result wraps to 0.
        self.assertEqual(self.consts["DEV_FLAG_MIXED"].v, 0)

    def test_bitwise_not_and_shift(self):
        self.assertEqual(self.consts["DEV_FLAG_LOW16"].v, 0xFFFF)

    def test_impl_defined_cast(self):
        self.assertEqual(self.consts["DEV_FLAG_SIGNBIT"].v, -(1 << 31))
        diags = _diags_for(self.report, "not representable")
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].kind, "implementation-defined")

    def test_impl_defined_negative_shift(self):
        self.assertEqual(self.consts["DEV_FLAG_ARITH"].v, -4)
        diags = _diags_for(self.report, "right shift of negative")
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].kind, "implementation-defined")

    def test_conflicts_have_concrete_samples(self):
        conflicts = {
            enum: (names, sample.v) for enum, names, sample in self.report.conflicts
        }
        self.assertEqual(
            conflicts["DevState"], (["DEV_STATE_ERROR", "DEV_STATE_UNKNOWN"], 2)
        )
        self.assertEqual(
            conflicts["DevClass"], (["DEV_CLASS_EXT", "DEV_CLASS_LEGACY"], 48)
        )

    def test_single_and_combined_flags_hit_together(self):
        result = status_report.interpret(self.report, 0x3)
        self.assertEqual(result["single"], ["DEV_FLAG_READY", "DEV_FLAG_BUSY"])
        self.assertEqual(result["combined"], ["DEV_FLAG_PAIR"])
        self.assertEqual(result["states"], [])

    def test_conflict_sample_satisfies_both_conditions(self):
        result = status_report.interpret(self.report, 2)
        self.assertEqual(result["states"], ["DEV_STATE_ERROR", "DEV_STATE_UNKNOWN"])


class TestStatusReport64(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = status_report.evaluate_file(HEADER, 64)
        cls.consts = _constants(cls.report)

    def test_high_flag_valid_on_64bit(self):
        # On LP64, 1UL << 40 is well defined.
        self.assertEqual(self.consts["DEV_FLAG_HIGH"].v, 1 << 40)
        self.assertEqual(
            _diags_for(self.report, "shift count 40 is negative or >= width 64"), []
        )
        # 1u << 40 stays undefined: unsigned int is still 32 bits.
        diags = _diags_for(self.report, "shift count 40")
        self.assertEqual(len(diags), 1)
        self.assertIn("device_status.h:49", diags[0].coord)

    def test_mixed_signed_unsigned(self):
        self.assertEqual(self.consts["DEV_FLAG_MIXED"].v, 0)

    def test_conflicts_match_32bit(self):
        conflicts = {enum: sample.v for enum, _names, sample in self.report.conflicts}
        self.assertEqual(conflicts, {"DevState": 2, "DevClass": 48})

    def test_interpretation_uses_64bit_status_word(self):
        result = status_report.interpret(self.report, 1 << 40)
        self.assertEqual(result["single"], ["DEV_FLAG_HIGH"])
        self.assertEqual(result["combined"], [])


if __name__ == "__main__":
    unittest.main()
