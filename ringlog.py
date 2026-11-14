"""Recovery of record order from a sparse Intel HEX image of a cyclic
ring-buffer logger after power loss.

The logger occupies a fixed number of fixed-width slots. When the last
slot is full it wraps to slot 0 and keeps overwriting. Each committed
slot holds a short sequence number (which wraps at a given modulus) and
a checksum. On power loss a slot may be half written: it keeps the old
sequence number but fails the checksum. Such a slot must never be used
as the wrap seam.

The dump is a *sparse* Intel HEX image: slots that were not exported (or
were never written) simply have no bytes at their addresses, so scanning
must wrap over empty slots instead of stopping at the first gap.

Because the last write pointer is only known to lie inside a range, the
whole ring can admit several feasible seams. Every feasible seam is
reported separately, together with the order that is common to all of
them (the only conclusions that can be confirmed without guessing).

Ordering model
--------------
The writer visits slots in physical order, one write per slot per lap.
For two valid slots ``a`` followed by ``b`` in a traversal that does not
cross the seam, the physical distance ``d`` fixes the sequence-number
step: ``(seq[b] - seq[a]) mod modulus == d mod modulus``. Empty slots in
between are absent from the export only; physical distance still counts.
A half-written (corrupt) slot makes the step across it unknowable, so
the pair straddling it is left unconstrained and the corrupt slot can
never anchor a seam.
"""

import os

from intelhex import IntelHex

__all__ = [
    "RingLogError",
    "Slot",
    "Seam",
    "RecoveryResult",
    "RingLog",
    "recover_order",
]

EMPTY = "empty"
VALID = "valid"
CORRUPT = "corrupt"


class RingLogError(ValueError):
    """Raised when the ring layout or the pointer range is invalid."""


class Slot(object):
    """Parsed state of one slot.

    @ivar index:    slot number (0 .. slot_count-1)
    @ivar address:  first byte address of the slot in the HEX image
    @ivar status:   L{VALID}, L{EMPTY} or L{CORRUPT}
    @ivar sequence: parsed short sequence number, or C{None} for an
                    empty slot. A corrupt slot usually still carries its
                    retained old sequence number here.
    """

    __slots__ = ("index", "address", "status", "sequence")

    def __init__(self, index, address, status, sequence=None):
        self.index = index
        self.address = address
        self.status = status
        self.sequence = sequence

    @property
    def is_valid(self):
        return self.status == VALID

    @property
    def is_empty(self):
        return self.status == EMPTY

    @property
    def is_corrupt(self):
        return self.status == CORRUPT

    def __repr__(self):
        return "Slot(index=%d, status=%r, sequence=%r)" % (
            self.index, self.status, self.sequence)


class Seam(object):
    """One feasible wrap seam (a candidate last write pointer).

    @ivar write_ptr: slot that the next write would target
    @ivar head:      oldest valid slot under this seam
    @ivar tail:      newest (last successfully written) valid slot;
                     always C{(write_ptr - 1) mod slot_count}
    @ivar order:     valid slot indices from old to new under this seam
    """

    __slots__ = ("write_ptr", "head", "tail", "order")

    def __init__(self, write_ptr, head, tail, order):
        self.write_ptr = write_ptr
        self.head = head
        self.tail = tail
        self.order = tuple(order)

    def __repr__(self):
        return "Seam(write_ptr=%d, order=%r)" % (self.write_ptr, self.order)


class RecoveryResult(object):
    """Result of a ring recovery pass.

    @ivar slots:            all slots, indexed by slot number
    @ivar seams:            every feasible L{Seam}, sorted by write pointer
    @ivar write_ptr_range:  inclusive (lo, hi) pointer range that was used
    @ivar common_chains:    chains of slot indices whose old-to-new order
                            holds under every feasible seam; ordering
                            between different chains cannot be confirmed
    @ivar common_relations: frozenset of (older, newer) slot-index pairs
                            guaranteed under every feasible seam
    """

    __slots__ = ("slots", "seams", "write_ptr_range",
                 "common_chains", "common_relations")

    def __init__(self, slots, seams, write_ptr_range,
                 common_chains, common_relations):
        self.slots = slots
        self.seams = seams
        self.write_ptr_range = write_ptr_range
        self.common_chains = common_chains
        self.common_relations = common_relations

    @property
    def sequences(self):
        """Mapping of valid slot index to its sequence number."""
        return dict((s.index, s.sequence) for s in self.slots if s.is_valid)

    @property
    def ambiguous(self):
        """True when more than one seam survives the constraints."""
        return len(self.seams) > 1

    def __repr__(self):
        return ("RecoveryResult(seams=%d, valid=%d, ambiguous=%r)" % (
            len(self.seams),
            sum(1 for s in self.slots if s.is_valid),
            self.ambiguous))


def _default_checksum(slot_bytes, checksum_offset, checksum_length):
    """Low-bits sum of every byte outside the checksum field."""
    mask = (1 << (8 * checksum_length)) - 1
    total = 0
    for pos, byte in enumerate(slot_bytes):
        if not (checksum_offset <= pos < checksum_offset + checksum_length):
            total += byte
    return total & mask


class RingLog(object):
    """Parser and order recoverer for a cyclic logger image."""

    def __init__(self, image, slot_count, slot_width, modulus,
                 base_address=0, sequence_offset=0, sequence_length=1,
                 checksum_offset=None, checksum_length=1, checksum_func=None,
                 write_ptr_range=None):
        """
        @param image:           IntelHex object, path to a HEX file, or
                                an {address: byte} dict
        @param slot_count:      number of slots in the full ring
        @param slot_width:      bytes occupied by each slot
        @param modulus:         modulus of the short sequence number (>= 2)
        @param base_address:    byte address of slot 0
        @param sequence_offset: offset of the sequence field inside a slot
        @param sequence_length: sequence field width in bytes (little endian)
        @param checksum_offset: checksum field offset; defaults to the
                                last C{checksum_length} bytes of the slot
        @param checksum_length: checksum field width in bytes (little endian)
        @param checksum_func:   callable(slot_bytes, offset, length) returning
                                the expected checksum integer; defaults to the
                                low-bits sum of the non-checksum bytes
        @param write_ptr_range: None (any pointer), an int (exact pointer),
                                a range, or an inclusive (lo, hi) tuple
        """
        if not isinstance(slot_count, int) or slot_count <= 0:
            raise RingLogError("slot_count must be a positive integer")
        if not isinstance(slot_width, int) or slot_width <= 0:
            raise RingLogError("slot_width must be a positive integer")
        if not isinstance(modulus, int) or modulus < 2:
            raise RingLogError("modulus must be an integer >= 2")
        if not isinstance(sequence_length, int) or sequence_length <= 0:
            raise RingLogError("sequence_length must be a positive integer")
        if not isinstance(checksum_length, int) or checksum_length <= 0:
            raise RingLogError("checksum_length must be a positive integer")
        if (sequence_offset < 0 or
                sequence_offset + sequence_length > slot_width):
            raise RingLogError("sequence field lies outside the slot")
        if checksum_offset is None:
            checksum_offset = slot_width - checksum_length
        if (checksum_offset < 0 or
                checksum_offset + checksum_length > slot_width):
            raise RingLogError("checksum field lies outside the slot")
        if checksum_func is None:
            checksum_func = _default_checksum
        elif not callable(checksum_func):
            raise RingLogError("checksum_func must be callable")

        if isinstance(image, IntelHex):
            self._image = image
        elif isinstance(image, dict):
            self._image = IntelHex(image)
        elif isinstance(image, os.PathLike):
            self._image = IntelHex(os.fspath(image))
        elif isinstance(image, str) or hasattr(image, "read"):
            self._image = IntelHex(image)
        else:
            raise RingLogError("image must be an IntelHex object, HEX path "
                               "or address dict")

        self.slot_count = slot_count
        self.slot_width = slot_width
        self.modulus = modulus
        self.base_address = base_address
        self.sequence_offset = sequence_offset
        self.sequence_length = sequence_length
        self.checksum_offset = checksum_offset
        self.checksum_length = checksum_length
        self.checksum_func = checksum_func
        self.write_ptr_range = self._normalize_ptr_range(write_ptr_range)

    def _normalize_ptr_range(self, write_ptr_range):
        n = self.slot_count
        if write_ptr_range is None:
            return (0, n - 1)
        if isinstance(write_ptr_range, int):
            lo = hi = write_ptr_range
        elif isinstance(write_ptr_range, range):
            if write_ptr_range.step != 1:
                raise RingLogError(
                    "write_ptr_range only supports ranges with step 1; "
                    "pass an inclusive (lo, hi) tuple instead")
            lo, hi = write_ptr_range.start, write_ptr_range.stop - 1
        else:
            try:
                lo, hi = write_ptr_range
            except (TypeError, ValueError):
                raise RingLogError(
                    "write_ptr_range must be None, an int, a range or an "
                    "inclusive (lo, hi) tuple")
        lo = max(0, min(n - 1, int(lo)))
        hi = max(0, min(n - 1, int(hi)))
        if lo > hi:
            raise RingLogError("write_ptr_range does not intersect the ring")
        return (lo, hi)

    def _slot_address(self, index):
        return self.base_address + index * self.slot_width

    def _read_field(self, data, offset, length):
        value = 0
        for pos in range(offset + length - 1, offset - 1, -1):
            value = (value << 8) | data[pos]
        return value

    def scan(self):
        """Parse every slot without stopping at empty ones.

        @return: tuple of L{Slot} objects indexed by slot number.
        """
        slots = []
        for index in range(self.slot_count):
            address = self._slot_address(index)
            presence = [(address + off) in self._image
                        for off in range(self.slot_width)]
            if not any(presence):
                slots.append(Slot(index, address, EMPTY))
                continue
            if not all(presence):
                # A partially exported slot cannot be validated; keep the
                # retained sequence number only when its whole field made it.
                sequence = None
                if all(presence[self.sequence_offset + off]
                       for off in range(self.sequence_length)):
                    sequence = self._read_field(
                        bytes(self._image[address + off]
                              if presence[off] else 0
                              for off in range(self.slot_width)),
                        self.sequence_offset, self.sequence_length)
                slots.append(Slot(index, address, CORRUPT, sequence))
                continue
            data = bytes(self._image[address + off]
                         for off in range(self.slot_width))
            sequence = self._read_field(
                data, self.sequence_offset, self.sequence_length)
            stored_checksum = self._read_field(
                data, self.checksum_offset, self.checksum_length)
            expected_checksum = self.checksum_func(
                data, self.checksum_offset, self.checksum_length)
            if (sequence >= self.modulus or
                    stored_checksum != expected_checksum):
                slots.append(Slot(index, address, CORRUPT, sequence))
            else:
                slots.append(Slot(index, address, VALID, sequence))
        return tuple(slots)

    def _step_is_consistent(self, slots, first, second):
        """Check the sequence step from one valid slot to the next.

        Returns True when the step is confirmed, None when a corrupt
        slot in between makes it unknowable.
        """
        n = self.slot_count
        distance = (second - first) % n
        step = first
        while True:
            middle = (step + 1) % n
            if middle == second:
                break
            if slots[middle].is_corrupt:
                return None
            step = middle
        expected = distance % self.modulus
        actual = ((slots[second].sequence - slots[first].sequence)
                  % self.modulus)
        return actual == expected

    def _feasible(self, slots, valid_indices, write_ptr):
        n = self.slot_count
        tail = (write_ptr - 1) % n
        if not slots[tail].is_valid:
            # A half-written or empty slot cannot be the last commit,
            # so it can never anchor the wrap seam.
            return None
        order = tuple(index for index in valid_indices
                      if index >= write_ptr) + \
                tuple(index for index in valid_indices
                      if index < write_ptr)
        for first, second in zip(order, order[1:]):
            if self._step_is_consistent(slots, first, second) is False:
                return None
        return order

    def _common_order(self, valid_indices, seams):
        if not seams:
            return [], frozenset()
        cut_heads = set(seam.head for seam in seams)
        cycle = list(valid_indices)
        if cut_heads:
            start = min(index for index in cycle if index in cut_heads)
        else:
            start = cycle[0]
        ordered = cycle[cycle.index(start):] + cycle[:cycle.index(start)]
        chains = []
        for index in ordered:
            if not chains or index in cut_heads:
                chains.append([index])
            else:
                chains[-1].append(index)
        relations = set()
        for chain in chains:
            for pos, older in enumerate(chain):
                for newer in chain[pos + 1:]:
                    relations.add((older, newer))
        return [tuple(chain) for chain in chains], frozenset(relations)

    def recover(self):
        """Run the full recovery and return a L{RecoveryResult}."""
        slots = self.scan()
        valid_indices = tuple(slot.index for slot in slots if slot.is_valid)
        seams = []
        if valid_indices:
            lo, hi = self.write_ptr_range
            for write_ptr in range(lo, hi + 1):
                order = self._feasible(slots, valid_indices, write_ptr)
                if order is not None:
                    seams.append(Seam(
                        write_ptr=write_ptr,
                        head=order[0],
                        tail=order[-1],
                        order=order))
        common_chains, common_relations = self._common_order(
            valid_indices, seams)
        return RecoveryResult(
            slots=slots,
            seams=tuple(seams),
            write_ptr_range=self.write_ptr_range,
            common_chains=common_chains,
            common_relations=common_relations)


def recover_order(image, slot_count, slot_width, modulus,
                  write_ptr_range=None, **options):
    """Convenience wrapper around L{RingLog.recover}."""
    ring = RingLog(image, slot_count, slot_width, modulus,
                   write_ptr_range=write_ptr_range, **options)
    return ring.recover()
