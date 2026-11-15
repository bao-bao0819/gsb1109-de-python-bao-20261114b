"""
Tests for frontmatter.measurements: parsing measurement strings from
experiment-note headers and exporting unified-dimension intervals.
"""

from decimal import Decimal

import pytest

from frontmatter.measurements import (
    MeasurementCollector,
    UnitTable,
    normalize,
    parse_measurement,
)


@pytest.fixture
def table():
    return UnitTable(
        {
            "m": ("length", "1"),
            "cm": ("length", "0.01"),
            "mm": ("length", "0.001"),
            "K": ("temperature", "1"),
            "°C": ("temperature", "1", "273.15"),
        }
    )


class TestParsing:
    def test_plus_minus(self):
        m = parse_measurement("2.50±0.02 cm")
        assert m.value == Decimal("2.50")
        assert m.error == Decimal("0.02")
        assert m.lower == Decimal("2.48")
        assert m.upper == Decimal("2.52")
        assert m.unit == "cm"
        assert m.raw == "2.50±0.02 cm"
        assert m.sig_figs == 3

    def test_plus_minus_ascii(self):
        m = parse_measurement("2.50+/-0.02 cm")
        assert m.lower == Decimal("2.48")
        assert m.upper == Decimal("2.52")

    @pytest.mark.parametrize(
        "text",
        ["2.48–2.52 cm", "2.48—2.52 cm", "2.48..2.52 cm", "2.48 to 2.52 cm"],
    )
    def test_limits(self, text):
        m = parse_measurement(text)
        assert m.lower == Decimal("2.48")
        assert m.upper == Decimal("2.52")
        assert m.unit == "cm"
        assert m.value is None and m.error is None

    def test_plain_value(self):
        m = parse_measurement("2.50 cm")
        assert m.lower == m.upper == Decimal("2.50")
        assert m.sig_figs == 3

    def test_inherits_default_unit(self):
        m = parse_measurement("2.50±0.02", unit="cm")
        assert m.unit == "cm"

    def test_explicit_unit_beats_default(self):
        m = parse_measurement("25.0±0.2 mm", unit="cm")
        assert m.unit == "mm"

    def test_inverted_limits_rejected(self):
        with pytest.raises(ValueError):
            parse_measurement("2.52–2.48 cm")

    def test_unparseable_rejected(self):
        with pytest.raises(ValueError):
            parse_measurement("not a measurement")

    @pytest.mark.parametrize(
        "token,expected",
        [("2.50", 3), ("0.02", 1), ("100.", 3), ("2500", 2), ("0.010", 2)],
    )
    def test_sig_figs(self, token, expected):
        m = parse_measurement(token, unit="cm")
        assert m.sig_figs == expected


class TestNormalize:
    def test_unified_dimension(self, table):
        collector = MeasurementCollector(table, unit="cm")
        results = collector.collect_all(
            ["2.50±0.02 cm", "25.0±0.2 mm", "2.50±0.02"]
        )
        for interval in results:
            assert interval.unit == "m"
            assert interval.lower == Decimal("0.0248")
            assert interval.upper == Decimal("0.0252")

    def test_raw_and_sig_figs_preserved(self, table):
        interval = MeasurementCollector(table, unit="cm").collect("2.50±0.02 cm")
        assert interval.raw == "2.50±0.02 cm"
        assert interval.sig_figs == 3

    def test_offset_unit_absolute_vs_error(self, table):
        # absolute values: scale * x + offset; errors: scale * x (no offset)
        assert table.convert_value(Decimal("20.0"), "°C") == Decimal("293.15")
        assert table.convert_error(Decimal("0.5"), "°C") == Decimal("0.5")

        interval = MeasurementCollector(table, unit="°C").collect("20.0±0.5 °C")
        assert interval.unit == "K"
        assert interval.lower == Decimal("292.65")
        assert interval.upper == Decimal("293.65")

    def test_offset_unit_limits(self, table):
        interval = MeasurementCollector(table, unit="°C").collect("20.0–21.0 °C")
        assert interval.lower == Decimal("293.15")
        assert interval.upper == Decimal("294.15")

    def test_missing_unit_rejected(self, table):
        with pytest.raises(ValueError):
            normalize(parse_measurement("2.50±0.02"), table)

    def test_unknown_unit_rejected(self, table):
        with pytest.raises(KeyError):
            MeasurementCollector(table).collect("2.50±0.02 furlongs")

    def test_sig_figs_and_places_are_exclusive(self, table):
        m = parse_measurement("2.50±0.02 cm")
        with pytest.raises(ValueError):
            normalize(m, table, sig_figs=2, places=3)


class TestGroupUnitScope:
    def test_inline_unit_switch_does_not_leak(self, table):
        collector = MeasurementCollector(table, unit="cm")
        collector.collect("25.0±0.2 mm")  # temporary switch for one sample
        interval = collector.collect("2.50±0.02")
        assert interval.lower == Decimal("0.0248")
        assert interval.upper == Decimal("0.0252")

    def test_per_call_unit_does_not_leak(self, table):
        collector = MeasurementCollector(table, unit="cm")
        collector.collect("25.0±0.2", unit="mm")
        assert collector.unit == "cm"
        interval = collector.collect("2.50±0.02")
        assert interval.lower == Decimal("0.0248")


class TestOutwardRounding:
    def test_sig_figs_rounds_outward(self, table):
        collector = MeasurementCollector(table, unit="cm", sig_figs=2)
        interval = collector.collect("2.547±0.001 cm")
        assert interval.lower == Decimal("0.025")
        assert interval.upper == Decimal("0.026")

    def test_places_rounds_outward(self, table):
        collector = MeasurementCollector(table, unit="cm", places=4)
        interval = collector.collect("2.547±0.001 cm")
        assert interval.lower == Decimal("0.0254")
        assert interval.upper == Decimal("0.0255")

    @pytest.mark.parametrize(
        "text",
        ["2.547±0.001 cm", "2.50±0.02 cm", "0.05±0.04 cm", "9.99±0.01 cm"],
    )
    def test_interval_never_shrinks(self, table, text):
        collector = MeasurementCollector(table, unit="cm")
        exact = collector.collect(text)
        for sig_figs in (1, 2, 3):
            rounded = collector.collect(text, sig_figs=sig_figs)
            assert rounded.lower <= exact.lower
            assert rounded.upper >= exact.upper

    def test_exact_endpoints_stay_put(self, table):
        collector = MeasurementCollector(table, unit="cm", sig_figs=3)
        interval = collector.collect("2.50±0.02 cm")
        assert interval.lower == Decimal("0.0248")
        assert interval.upper == Decimal("0.0252")


class TestExport:
    def test_to_dict(self, table):
        interval = MeasurementCollector(table, unit="cm").collect("2.50±0.02 cm")
        assert interval.to_dict() == {
            "raw": "2.50±0.02 cm",
            "lower": 0.0248,
            "upper": 0.0252,
            "unit": "m",
            "sig_figs": 3,
        }

    def test_base_unit_override(self):
        table = UnitTable(
            {"m": ("length", "1"), "cm": ("length", "0.01")},
            base_units={"length": "cm"},
        )
        interval = MeasurementCollector(table, unit="m").collect("2.50±0.02 m")
        assert interval.unit == "cm"
        assert interval.lower == Decimal("248")
        assert interval.upper == Decimal("252")

    def test_mapping_unit_spec(self):
        table = UnitTable(
            {
                "K": {"dimension": "temperature"},
                "°C": {"dimension": "temperature", "scale": 1, "offset": "273.15"},
            }
        )
        assert table.convert_value(Decimal("0"), "°C") == Decimal("273.15")
