import pytest

from serial_layout import (
    ArrayRange,
    BranchSelectionError,
    ConstraintViolation,
    MissingCountError,
    RecordLayout,
    SerialLayoutError,
    UnknownFieldError,
)

BASIC_DECLS = """
typedef struct {
    uint16_t msg_id;
    uint16_t count;
    uint32_t payload[16];
} Record;
"""

NESTED_DECLS = """
typedef struct {
    uint8_t kind;
    uint8_t n_items;
} Header;

typedef struct {
    Header hdr;
    uint16_t items[8];
    uint8_t tail_count;
    uint16_t tail[4];
} Record;
"""

BRANCH_DECLS = """
typedef struct {
    uint8_t tag;
    union {
        struct {
            uint8_t a_count;
            uint16_t a_data[8];
        } a;
        struct {
            uint8_t b_count;
            uint32_t b_data[4];
        } b;
    } choice;
    uint8_t checksum;
} Record;
"""

LAYERED_DECLS = """
typedef struct {
    uint8_t n;
    uint32_t values[8];
} Bucket;

typedef struct {
    uint8_t n_outer;
    Bucket bucket;
    uint16_t extra[4];
} Record;
"""


@pytest.fixture
def basic():
    return RecordLayout(BASIC_DECLS, "Record")


@pytest.fixture
def nested():
    return RecordLayout(NESTED_DECLS, "Record")


@pytest.fixture
def branch():
    return RecordLayout(BRANCH_DECLS, "Record")


@pytest.fixture
def layered():
    return RecordLayout(LAYERED_DECLS, "Record")


class TestMinReadPosition:
    def test_static_layout_needs_no_reads(self):
        layout = RecordLayout("typedef struct { uint32_t a; uint8_t b[3]; } R;",
                              "R")
        assert layout.min_read_position({}, {}) == 0
        assert layout.record_length({}, {}) == 7

    def test_position_is_just_past_count_field(self, basic):
        bindings = {"payload": "count"}
        # msg_id(2) + count(2) -> must have read 4 bytes to know the layout
        assert basic.min_read_position(bindings, {"count": 3}) == 4

    def test_later_count_field_extends_position(self, nested):
        bindings = {"items": "hdr.n_items", "tail": "tail_count"}
        counts = {"hdr.n_items": 2, "tail_count": 1}
        # hdr(2) + items(2*2) + tail_count ends at 2 + 4 + 1 = 7
        assert nested.min_read_position(bindings, counts) == 7

    def test_inactive_branch_is_not_pre_read(self, branch):
        bindings = {"choice.a.a_data": "choice.a.a_count"}
        # active branch a: tag(1) + a_count ends at 2; branch b never read
        assert branch.min_read_position(bindings, {"choice.a.a_count": 2}) == 2

    def test_branch_b_position(self, branch):
        bindings = {"choice.b.b_data": "choice.b.b_count"}
        assert branch.min_read_position(bindings, {"choice.b.b_count": 1}) == 2


class TestArrayRanges:
    def test_actual_range(self, basic):
        ranges = basic.array_ranges({"payload": "count"}, {"count": 3})
        assert ranges == {"payload": ArrayRange("payload", 4, 3, 4)}
        assert ranges["payload"].end == 16
        assert basic.record_length({"payload": "count"}, {"count": 3}) == 16

    def test_zero_count_array(self, basic):
        ranges = basic.array_ranges({"payload": "count"}, {"count": 0})
        assert ranges["payload"].offset == 4
        assert ranges["payload"].end == 4
        assert basic.record_length({"payload": "count"}, {"count": 0}) == 4

    def test_nested_ranges(self, nested):
        bindings = {"items": "hdr.n_items", "tail": "tail_count"}
        counts = {"hdr.n_items": 2, "tail_count": 3}
        ranges = nested.array_ranges(bindings, counts)
        assert ranges["items"] == ArrayRange("items", 2, 2, 2)
        # tail follows items: 2 + 4 = 6, tail_count at 6, tail at 7
        assert ranges["tail"] == ArrayRange("tail", 7, 3, 2)
        assert nested.record_length(bindings, counts) == 13

    def test_branch_layout_uses_active_member(self, branch):
        bindings = {"choice.a.a_data": "choice.a.a_count"}
        ranges = branch.array_ranges(bindings, {"choice.a.a_count": 2})
        assert ranges["choice.a.a_data"] == ArrayRange("choice.a.a_data", 2, 2, 2)
        # tag(1) + a_count(1) + 2*2 + checksum(1)
        assert branch.record_length(bindings, {"choice.a.a_count": 2}) == 7

    def test_explicit_branch_selection(self, branch):
        bindings = {"choice.b.b_data": "choice.b.b_count"}
        ranges = branch.array_ranges(
            bindings, {"choice.b.b_count": 2}, branches={"choice": "b"})
        assert ranges["choice.b.b_data"] == ArrayRange("choice.b.b_data", 2, 2, 4)
        # tag(1) + b_count(1) + 2*4 + checksum(1)
        assert branch.record_length(bindings, {"choice.b.b_count": 2},
                                    branches={"choice": "b"}) == 11


class TestConstraints:
    def test_inner_layer_breached_first(self, layered):
        bindings = {"bucket.values": "bucket.n", "extra": "n_outer"}
        counts = {"bucket.n": 5, "n_outer": 2}
        limits = {"bucket": 12, "": 64}
        with pytest.raises(ConstraintViolation) as excinfo:
            layered.array_ranges(bindings, counts, limits=limits)
        assert excinfo.value.layer == "bucket"
        assert excinfo.value.limit == 12
        assert excinfo.value.actual == 1 + 5 * 4

    def test_outer_layer_breached_when_inner_ok(self, layered):
        bindings = {"bucket.values": "bucket.n", "extra": "n_outer"}
        counts = {"bucket.n": 2, "n_outer": 4}
        limits = {"bucket": 12, "": 17}
        with pytest.raises(ConstraintViolation) as excinfo:
            layered.array_ranges(bindings, counts, limits=limits)
        assert excinfo.value.layer == ""
        # n_outer(1) + bucket(1 + 2*4) + extra(4*2) = 18
        assert excinfo.value.actual == 18

    def test_within_limits_passes(self, layered):
        bindings = {"bucket.values": "bucket.n", "extra": "n_outer"}
        counts = {"bucket.n": 2, "n_outer": 1}
        ranges = layered.array_ranges(bindings, counts,
                                      limits={"bucket": 12, "": 20})
        assert ranges["bucket.values"] == ArrayRange("bucket.values", 2, 2, 4)

    def test_count_beyond_declared_capacity(self, basic):
        with pytest.raises(ConstraintViolation) as excinfo:
            basic.array_ranges({"payload": "count"}, {"count": 17})
        assert excinfo.value.layer == "payload"
        assert excinfo.value.limit == 16
        assert excinfo.value.actual == 17

    def test_unknown_limit_layer(self, basic):
        with pytest.raises(UnknownFieldError):
            basic.array_ranges({"payload": "count"}, {"count": 1},
                               limits={"nowhere": 4})


class TestErrors:
    def test_missing_count(self, basic):
        with pytest.raises(MissingCountError):
            basic.array_ranges({"payload": "count"}, {})

    def test_negative_count(self, basic):
        with pytest.raises(SerialLayoutError):
            basic.array_ranges({"payload": "count"}, {"count": -1})

    def test_unknown_binding_path(self, basic):
        with pytest.raises(UnknownFieldError):
            basic.array_ranges({"nope": "count"}, {"count": 1})

    def test_binding_target_must_be_array(self, basic):
        with pytest.raises(UnknownFieldError):
            basic.array_ranges({"msg_id": "count"}, {"count": 1})

    def test_ambiguous_branch(self, branch):
        bindings = {"choice.a.a_data": "choice.a.a_count"}
        counts = {"choice.a.a_count": 1, "choice.b.b_count": 1}
        with pytest.raises(BranchSelectionError):
            branch.min_read_position(bindings, counts)

    def test_unknown_branch_member(self, branch):
        with pytest.raises(BranchSelectionError):
            branch.min_read_position({}, {}, branches={"choice": "zzz"})


class TestAlignment:
    def test_unpacked_natural_alignment(self):
        layout = RecordLayout(
            "typedef struct { uint8_t tag; uint32_t count; uint16_t data[4]; } R;",
            "R", packed=False)
        bindings = {"data": "count"}
        # tag at 0, count aligned to 4 -> ends at 8, data at 8
        assert layout.min_read_position(bindings, {"count": 2}) == 8
        ranges = layout.array_ranges(bindings, {"count": 2})
        assert ranges["data"] == ArrayRange("data", 8, 2, 2)
        # struct size rounded up to alignment 4: 8 + 4 -> 12
        assert layout.record_length(bindings, {"count": 2}) == 12
