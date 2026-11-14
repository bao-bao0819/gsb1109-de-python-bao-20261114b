import os
import tempfile

import pytest

from intelhex import IntelHex

from ringlog import (
    CORRUPT,
    EMPTY,
    VALID,
    RingLog,
    RingLogError,
    recover_order,
)

SLOT_WIDTH = 4
SEQ_OFF = 0
CS_OFF = 3


def encode_slot(sequence, counter):
    """Default layout: [seq, payload_lo, payload_hi, checksum]."""
    data = bytearray(SLOT_WIDTH)
    data[0] = sequence & 0xFF
    data[1] = counter & 0xFF
    data[2] = (counter >> 8) & 0xFF
    data[3] = (data[0] + data[1] + data[2]) & 0xFF
    return bytes(data)


def build_image(slot_count, writes, base=0, modulus=None):
    """Simulate `writes` lap writes starting at slot 0, sequence 0.

    Sequence numbers wrap at `modulus` exactly as the logger firmware
    would store them in the slot.
    """
    image = IntelHex()
    pointer = 0
    for counter, sequence in enumerate(writes):
        if modulus is not None:
            sequence = sequence % modulus
        address = base + pointer * SLOT_WIDTH
        data = encode_slot(sequence, counter)
        for off, byte in enumerate(data):
            image[address + off] = byte
        pointer = (pointer + 1) % slot_count
    return image


def drop_slot(image, index, base=0):
    for off in range(SLOT_WIDTH):
        del image._buf[base + index * SLOT_WIDTH + off]


def corrupt_slot(image, index, base=0):
    """Flip a payload byte; sequence byte and old checksum stay in place."""
    address = base + index * SLOT_WIDTH
    image[address + 1] ^= 0x80


def test_scan_keeps_going_past_empty_slots():
    # Slot 0 and 2 empty, valid samples after them must still be read.
    image = build_image(4, writes=[0, 1, 2, 3], modulus=16)
    drop_slot(image, 0)
    drop_slot(image, 2)
    result = recover_order(image, slot_count=4, slot_width=SLOT_WIDTH,
                           modulus=16)
    statuses = [slot.status for slot in result.slots]
    assert statuses == [EMPTY, VALID, EMPTY, VALID]
    assert result.sequences == {1: 1, 3: 3}
    assert [seam.order for seam in result.seams] == [(1, 3)]


def test_wrapped_ring_with_sparse_and_half_written_slot():
    # 8 slots, modulus 5, 13 writes -> one full lap plus 5, pointer at 5.
    image = build_image(8, writes=range(13), modulus=5)
    corrupt_slot(image, 2)   # half-written: retains seq 0, bad checksum
    drop_slot(image, 6)      # sparse export: slot absent entirely

    result = recover_order(image, slot_count=8, slot_width=SLOT_WIDTH,
                           modulus=5, write_ptr_range=(3, 7))

    statuses = [slot.status for slot in result.slots]
    assert statuses[2] == CORRUPT
    assert statuses[6] == EMPTY
    assert result.slots[2].sequence == 0  # retained old sequence number

    # Only the true pointer survives; the corrupt slot and the empty slot
    # cannot anchor the seam, and bad sequence steps reject the rest.
    assert [seam.write_ptr for seam in result.seams] == [5]
    seam = result.seams[0]
    assert seam.head == 5 and seam.tail == 4
    assert seam.order == (5, 7, 0, 1, 3, 4)
    assert result.common_chains == [(5, 7, 0, 1, 3, 4)]
    assert (1, 3) in result.common_relations  # across the corrupt slot


def test_half_written_slot_cannot_anchor_seam_but_unblocks_step():
    # A corrupt slot hides the step across it, yet must never be the tail.
    image = IntelHex()
    for address, byte in enumerate(encode_slot(0, 0)):
        image[address] = byte
    for address, byte in enumerate(encode_slot(1, 1)):
        image[SLOT_WIDTH + address] = byte
    for address, byte in enumerate(encode_slot(9, 2)):
        image[2 * SLOT_WIDTH + address] = byte
    corrupt_slot(image, 1)  # slot 1 keeps seq 1 but fails checksum

    result = recover_order(image, slot_count=5, slot_width=SLOT_WIDTH,
                           modulus=10, write_ptr_range=(0, 4))
    pointers = [seam.write_ptr for seam in result.seams]
    assert 2 not in pointers  # tail would be the corrupt slot
    assert 3 in pointers      # step 0 -> 9 across corrupt slot is unknown
    seam = next(seam for seam in result.seams if seam.write_ptr == 3)
    assert seam.order == (0, 2)


def test_multiple_feasible_seams_report_common_order_only():
    # modulus 3 on 6 slots: every rotation is an equally feasible seam.
    image = build_image(6, writes=range(9), modulus=3)  # seqs 0,1,2,0,1,2
    result = recover_order(image, slot_count=6, slot_width=SLOT_WIDTH,
                           modulus=3)
    assert len(result.seams) == 6
    assert [seam.write_ptr for seam in result.seams] == list(range(6))
    # Nothing can be ordered across seams: every slot is itself a head.
    assert result.common_chains == [(0,), (1,), (2,), (3,), (4,), (5,)]
    assert result.common_relations == frozenset()

    narrowed = recover_order(image, slot_count=6, slot_width=SLOT_WIDTH,
                             modulus=3, write_ptr_range=(0, 2))
    assert [seam.write_ptr for seam in narrowed.seams] == [0, 1, 2]
    # Slots 2..5 keep their order under all three surviving seams
    # (slot 2 is never the tail for any of them).
    assert narrowed.common_chains == [(0,), (1,), (2, 3, 4, 5)]
    assert narrowed.common_relations == frozenset({
        (2, 3), (2, 4), (2, 5),
        (3, 4), (3, 5), (4, 5)})


def test_pointer_range_without_any_feasible_seam():
    image = build_image(4, writes=range(4), modulus=16)
    result = recover_order(image, slot_count=4, slot_width=SLOT_WIDTH,
                           modulus=16, write_ptr_range=(1, 2))
    # True pointer is 0; the range only admits inconsistent candidates.
    assert result.seams == ()
    assert result.common_chains == []
    assert result.common_relations == frozenset()


def test_single_valid_slot_has_unique_seam():
    image = build_image(3, writes=[7], modulus=16)
    result = recover_order(image, slot_count=3, slot_width=SLOT_WIDTH,
                           modulus=16)
    assert len(result.seams) == 1
    assert result.seams[0].write_ptr == 1
    assert result.seams[0].order == (0,)


def test_completely_empty_image():
    image = IntelHex()
    result = recover_order(image, slot_count=4, slot_width=SLOT_WIDTH,
                           modulus=16)
    assert all(slot.is_empty for slot in result.slots)
    assert result.seams == ()
    assert result.common_chains == []


def test_hex_file_round_trip_and_dict_input():
    image = build_image(6, writes=range(10), modulus=8)
    drop_slot(image, 2)
    corrupt_slot(image, 4)
    handle, path = tempfile.mkstemp(suffix=".hex")
    os.close(handle)
    try:
        image.write_hex_file(path)
        from_file = recover_order(path, slot_count=6, slot_width=SLOT_WIDTH,
                                  modulus=8, write_ptr_range=(0, 5))
        from_dict = recover_order(image.todict(), slot_count=6,
                                  slot_width=SLOT_WIDTH, modulus=8,
                                  write_ptr_range=(0, 5))
        from_object = recover_order(IntelHex(path), slot_count=6,
                                    slot_width=SLOT_WIDTH, modulus=8,
                                    write_ptr_range=(0, 5))
    finally:
        os.remove(path)

    expected = [(seam.write_ptr, seam.order) for seam in from_file.seams]
    assert expected == [(seam.write_ptr, seam.order)
                        for seam in from_dict.seams]
    assert expected == [(seam.write_ptr, seam.order)
                        for seam in from_object.seams]
    assert from_file.sequences == from_dict.sequences
    statuses = [slot.status for slot in from_file.slots]
    assert statuses[2] == EMPTY and statuses[4] == CORRUPT


def test_base_address_and_wide_fields():
    width = 6
    base = 0x100
    seq_off, seq_len = 1, 2
    cs_off, cs_len = 4, 2

    def checksum(data, offset, length):
        return sum(data[:offset]) & 0xFFFF

    def encode(sequence, counter):
        data = bytearray(width)
        data[0] = counter & 0xFF
        data[seq_off] = sequence & 0xFF
        data[seq_off + 1] = (sequence >> 8) & 0xFF
        value = checksum(data, cs_off, cs_len)
        data[cs_off] = value & 0xFF
        data[cs_off + 1] = (value >> 8) & 0xFF
        return bytes(data)

    image = IntelHex()
    for slot_index, sequence in enumerate((1000, 1001, 1002)):
        data = encode(sequence, slot_index)
        address = base + slot_index * width
        for off, byte in enumerate(data):
            image[address + off] = byte

    ring = RingLog(image, slot_count=3, slot_width=width, modulus=2000,
                   base_address=base, sequence_offset=seq_off,
                   sequence_length=seq_len, checksum_offset=cs_off,
                   checksum_length=cs_len, checksum_func=checksum)
    result = ring.recover()
    assert result.sequences == {0: 1000, 1: 1001, 2: 1002}
    assert [seam.order for seam in result.seams] == [(0, 1, 2)]


def test_sequence_outside_modulus_is_corrupt():
    image = build_image(2, writes=[0, 1], modulus=2)
    result = recover_order(image, slot_count=2, slot_width=SLOT_WIDTH,
                           modulus=2)  # valid sequences are 0..1 only
    image2 = build_image(2, writes=[0, 5])  # raw 5 stays above modulus 4
    result2 = recover_order(image2, slot_count=2, slot_width=SLOT_WIDTH,
                            modulus=4)
    assert all(slot.is_valid for slot in result.slots)
    assert result2.slots[1].status == CORRUPT
    assert result2.slots[1].sequence == 5


def test_partially_exported_slot_is_corrupt_not_empty():
    image = build_image(4, writes=range(4), modulus=16)
    # Leave the sequence byte but drop the checksum byte of slot 2: the
    # slot must not be mistaken for an empty slot and skipped silently.
    address = 2 * SLOT_WIDTH
    del image._buf[address + CS_OFF]
    result = recover_order(image, slot_count=4, slot_width=SLOT_WIDTH,
                           modulus=16)
    assert result.slots[2].status == CORRUPT
    assert result.slots[2].sequence == 2  # retained sequence field
    assert 2 not in result.sequences      # not treated as a valid sample


def test_stepped_pointer_range_is_rejected():
    image = IntelHex()
    with pytest.raises(RingLogError):
        RingLog(image, slot_count=4, slot_width=SLOT_WIDTH, modulus=4,
                write_ptr_range=range(0, 4, 2))


def test_invalid_construction():
    image = IntelHex()
    with pytest.raises(RingLogError):
        RingLog(image, slot_count=0, slot_width=4, modulus=4)
    with pytest.raises(RingLogError):
        RingLog(image, slot_count=4, slot_width=4, modulus=1)
    with pytest.raises(RingLogError):
        RingLog(image, slot_count=4, slot_width=2, modulus=4,
                sequence_offset=1, sequence_length=2)
    with pytest.raises(RingLogError):
        RingLog(image, slot_count=4, slot_width=4, modulus=4,
                write_ptr_range=(3, 1))
    with pytest.raises(RingLogError):
        RingLog(object(), slot_count=4, slot_width=4, modulus=4)


def test_exact_pointer_and_range_pointer_forms():
    image = build_image(5, writes=range(8), modulus=7)  # pointer ends at 3
    exact = recover_order(image, slot_count=5, slot_width=SLOT_WIDTH,
                          modulus=7, write_ptr_range=3)
    assert [seam.write_ptr for seam in exact.seams] == [3]
    as_range = recover_order(image, slot_count=5, slot_width=SLOT_WIDTH,
                             modulus=7, write_ptr_range=range(1, 4))
    assert 3 in [seam.write_ptr for seam in as_range.seams]
