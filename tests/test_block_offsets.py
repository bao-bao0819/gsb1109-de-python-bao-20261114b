import pytest

from tftpy.BlockOffsets import (
    ACK_KIND,
    DATA_KIND,
    MOD,
    BlockOffsetResolver,
    BlockRecord,
    parse_record,
    parse_records,
    resolve_block_offsets,
)

BLKSIZE = 512
TURN_BYTES = MOD * BLKSIZE


def data(block, size=BLKSIZE, retransmit=False):
    return BlockRecord(DATA_KIND, block, size=size, retransmit=retransmit)


def ack(block, retransmit=False):
    return BlockRecord(ACK_KIND, block, retransmit=retransmit)


def offsets(placements):
    return [placement.offsets for placement in placements]


class TestBasicOrdering:
    def test_sequential_blocks_map_to_consecutive_offsets(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        placements = [resolver.feed(data(block)) for block in (1, 2, 3)]

        assert all(placement.confirmed for placement in placements)
        assert [placement.offset for placement in placements] == [0, 512, 1024]
        assert [placement.ordinals for placement in placements] == [
            (0,),
            (1,),
            (2,),
        ]

    def test_acknowledgements_track_frontier_and_duplicates(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        first = resolver.feed(ack(1))
        duplicate = resolver.feed(ack(1))
        jumped = resolver.feed(ack(3))

        assert not first.duplicate
        assert first.confirmed
        assert first.ordinals == (0,)
        assert first.offset == 0
        assert duplicate.duplicate
        assert duplicate.offsets == ()
        assert not jumped.duplicate
        assert jumped.ordinals == (2,)
        assert resolver._state.ack_seq == 2

    def test_acknowledgement_follows_the_wrap(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver._state.ack_seq = 65534
        resolver._state.data_seen = True
        zero_ack = resolver.feed(ack(0))
        wrapped_ack = resolver.feed(ack(1))
        stale = resolver.feed(ack(0))

        assert zero_ack.ordinals == (65535,)
        assert wrapped_ack.ordinals == (65536,)
        assert stale.duplicate
        assert "ordinal 65535" in stale.note

    def test_early_block_zero_ack_is_the_option_handshake(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        handshake = resolver.feed(ack(0))
        first = resolver.feed(data(1))

        assert handshake.offsets == ()
        assert not handshake.duplicate
        assert "option handshake" in handshake.note
        assert resolver._state.ack_seq == -1
        assert first.ordinals == (0,)

    def test_block_zero_ack_after_wrap_confirms_data(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        handshake = resolver.feed(ack(0))
        for block in range(1, MOD):
            resolver.feed(data(block))
        resolver.feed(data(0))
        wrap_ack = resolver.feed(ack(0))

        assert handshake.offsets == ()
        assert wrap_ack.ordinals == (65535,)
        assert wrap_ack.offset == 65535 * BLKSIZE

    def test_tuple_dict_and_convenience_helper(self):
        records = [
            ("data", 1, 512),
            {"kind": "ack", "block": 1},
            ("data", 2, 512, False),
        ]
        placements = resolve_block_offsets(records, blksize=BLKSIZE)
        assert placements[0].offset == 0
        assert placements[1].kind == ACK_KIND
        assert placements[2].offset == 512

    def test_invalid_arguments_are_rejected(self):
        with pytest.raises(ValueError):
            BlockOffsetResolver(4)
        with pytest.raises(ValueError):
            BlockOffsetResolver(BLKSIZE, filesize=-1)
        with pytest.raises(ValueError):
            BlockRecord(DATA_KIND, MOD)
        with pytest.raises(ValueError):
            BlockRecord(ACK_KIND, 1, size=10)


class TestWraparound:
    def test_block_zero_is_the_last_block_of_a_turn(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        # Simulate the 65534 earlier blocks, then ordinal 65534 carries block
        # 65535, ordinal 65535 carries block 0 and ordinal 65536 wraps to 1.
        resolver._state.next_seq = 65534
        wrap = resolver.feed(data(65535))
        zero = resolver.feed(data(0))
        wrapped = resolver.feed(data(1))

        assert wrap.confirmed and wrap.ordinals == (65534,)
        assert zero.confirmed and zero.ordinals == (65535,)
        assert zero.offset == 65535 * BLKSIZE
        assert wrapped.confirmed and wrapped.ordinals == (65536,)
        assert wrapped.offset == 65536 * BLKSIZE

    def test_full_turn_sequence_through_the_wrap(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        for block in range(1, MOD):
            resolver.feed(data(block))
        zero = resolver.feed(data(0))
        one = resolver.feed(data(1))

        assert zero.ordinals == (65535,)
        assert one.ordinals == (65536,)
        assert one.offset - zero.offset == BLKSIZE

    def test_late_retransmit_cannot_overwrite_new_turn_block(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver.feed(data(1))
        # The wrap happens without another block 1 being logged yet.
        resolver._state.next_seq = 65536
        stale = resolver.feed(data(1, retransmit=True))
        fresh = resolver.feed(data(1))

        assert stale.confirmed
        assert stale.ordinals == (0,)
        assert stale.offset == 0
        assert fresh.confirmed
        assert fresh.ordinals == (65536,)
        assert fresh.offset == TURN_BYTES

    def test_retransmit_inside_one_turn_keeps_old_offset(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(2))
        stale = resolver.feed(data(1, retransmit=True))
        next_block = resolver.feed(data(3))

        assert stale.ordinals == (0,)
        assert next_block.ordinals == (2,)
        assert resolver._state.next_seq == 3


class TestAmbiguity:
    def test_gap_leaves_both_turns_plausible(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(2))
        # A chunk of the capture is missing; block 2 shows up again with no
        # retransmit marker.
        ambiguous = resolver.feed(data(2))

        assert ambiguous.ambiguous
        assert ambiguous.offsets == (512, TURN_BYTES + 512)
        assert ambiguous.offset is None
        assert ambiguous.retransmit is False
        assert ambiguous.duplicate is True

    def test_file_length_resolves_the_ambiguity(self):
        resolver = BlockOffsetResolver(BLKSIZE, filesize=4 * BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(2))
        resolved = resolver.feed(data(2))

        assert resolved.confirmed
        assert resolved.offsets == (512,)

    def test_ambiguity_persists_when_file_spans_both_turns(self):
        resolver = BlockOffsetResolver(BLKSIZE, filesize=TURN_BYTES + 4 * BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(2))
        ambiguous = resolver.feed(data(2))

        assert ambiguous.ambiguous
        assert ambiguous.offsets == (512, TURN_BYTES + 512)

    def test_retransmit_marker_pins_the_old_turn_despite_gap(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(2))
        stale = resolver.feed(data(2, retransmit=True))

        assert stale.confirmed
        assert stale.offsets == (512,)

    def test_ambiguity_keeps_at_most_two_turns_after_two_wraps(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        for block in range(1, MOD):
            resolver.feed(data(block))
        resolver.feed(data(0))  # ordinal 65535, frontier is now 65536
        ambiguous = resolver.feed(data(65535))

        assert ambiguous.ambiguous
        assert ambiguous.ordinals == (65534, 65534 + MOD)
        assert ambiguous.offsets[1] - ambiguous.offsets[0] == TURN_BYTES

    def test_same_ordinal_is_tracked_once(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(1, retransmit=True))
        resolver.feed(data(1, retransmit=True))

        assert resolver._state.ordinals_by_block[1] == [0]


class TestShortTail:
    def test_short_block_terminates_and_later_traffic_adds_nothing(self):
        resolver = BlockOffsetResolver(BLKSIZE)
        resolver.feed(data(1))
        tail = resolver.feed(data(2, size=100))
        dup_ack = resolver.feed(ack(2))
        dup_ack_again = resolver.feed(ack(2))
        stray = resolver.feed(data(3))
        stray_ack = resolver.feed(ack(3))

        assert "terminating short block" in tail.note
        assert dup_ack.duplicate and dup_ack_again.duplicate
        assert stray.ignored and stray.offsets == ()
        assert stray_ack.duplicate

    def test_empty_terminating_block_for_exact_multiple_length(self):
        resolver = BlockOffsetResolver(BLKSIZE, filesize=2 * BLKSIZE)
        resolver.feed(data(1))
        resolver.feed(data(2))
        tail = resolver.feed(data(3, size=0))

        assert tail.confirmed
        assert tail.ordinals == (2,)
        assert "terminating short block" in tail.note

    def test_short_block_disagreement_with_file_length_is_flagged(self):
        resolver = BlockOffsetResolver(BLKSIZE, filesize=3 * BLKSIZE)
        resolver.feed(data(1))
        wrong_tail = resolver.feed(data(2, size=10))

        assert "file length expects" in wrong_tail.note

    def test_full_size_block_disagreement_is_flagged(self):
        resolver = BlockOffsetResolver(BLKSIZE, filesize=100)
        wrong = resolver.feed(data(1, size=BLKSIZE))

        assert "disagrees with file length" in wrong.note


class TestEndToEnd:
    def test_wrapped_transfer_with_retransmits_and_dup_acks(self):
        filesize = TURN_BYTES + 3 * BLKSIZE + 27
        resolver = BlockOffsetResolver(BLKSIZE, filesize=filesize)
        for block in range(1, MOD):
            resolver.feed(data(block))
            if block % 997 == 0:
                resolver.feed(ack(block))
        resolver.feed(data(0))
        resolver.feed(ack(0))
        # An old-turn block 1 arrives late, then the new-turn block 1.
        stale = resolver.feed(data(1, retransmit=True))
        resolver.feed(data(1))
        resolver.feed(data(2))
        resolver.feed(data(3))
        resolver.feed(ack(3))
        tail = resolver.feed(data(4, size=27))
        after = resolver.feed(ack(4))

        assert stale.offsets == (0,)
        assert tail.ordinals == (65539,)
        assert tail.offset == 65539 * BLKSIZE
        assert "terminating short block" in tail.note
        assert after.duplicate
        assert resolver.data_placements()[-1].ignored is False

    def test_records_after_termination_never_extend_the_file(self):
        resolver = BlockOffsetResolver(BLKSIZE, filesize=BLKSIZE + 5)
        resolver.feed(data(1))
        resolver.feed(data(2, size=5))
        resolver.feed(ack(2))
        resolver.feed(ack(2))
        extra = resolver.feed(data(2, size=5, retransmit=True))

        assert extra.ignored
        assert all(
            placement.offsets == () or placement.offsets[0] <= BLKSIZE
            for placement in resolver.data_placements()
        )


class TestTextParsing:
    def test_parse_record_shapes(self):
        assert parse_record("DATA 1 512") == data(1)
        assert parse_record("dat 0 512 retrans") == data(0, retransmit=True)
        assert parse_record("ACK 3 dup") == ack(3, retransmit=True)
        assert parse_record("# a comment") is None
        assert parse_record("   ") is None

    def test_parse_records_ignores_blank_lines(self):
        lines = ["DATA 1 512", "", "# note", "ACK 1"]
        records = parse_records(lines)
        assert records == [data(1), ack(1)]

    def test_parse_record_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            parse_record("OOPS 1")
