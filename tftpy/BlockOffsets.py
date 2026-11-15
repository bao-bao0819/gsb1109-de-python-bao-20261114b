# vim: ts=4 sw=4 et ai:
"""Recover logical byte offsets of TFTP data blocks from captured logs.

TFTP identifies data packets with a 16 bit block number that starts at one,
runs through 65535, wraps to zero and then back to one. A transfer of a large
file therefore visits the same block number in several "turns" (轮次), one
turn spanning ``MOD = 65536`` blocks. Given the negotiated block size, an
ordered capture of DAT sends and ACK receives and, optionally, the file
length, this module works out which logical byte offset each captured data
block can be proven to carry.

Ordinal ``s`` is the zero based position of a data block in the file, so the
first data block has ordinal zero, logical offset ``s * blksize`` and wire
block number ``(s + 1) % 65536``.

The rules implemented here mirror the protocol behaviour:

* First transmissions are strictly monotonic. A block re-using an already
  seen number is either a retransmit from an earlier turn or the block of the
  current turn after a wrap; a late block from an old turn can never
  overwrite the same-numbered block of the newer turn. Records explicitly
  marked as retransmits are mapped to past turns only.
* ACKs are cumulative and only move the acknowledgement frontier forward;
  duplicate ACKs prove nothing new. The frontier also bounds where an
  unmarked, out-of-order block could plausibly belong.
* The first short block (fewer bytes than the negotiated block size,
  including an empty block) terminates the transfer. Repeated ACKs and
  stray data seen afterwards add no data.
* When a gap in the log leaves both turns plausible, every candidate offset
  is returned instead of guessing; candidates of the same block always
  differ by one full turn, ``65536 * blksize`` bytes.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, Union

from tftpy.TftpShared import MAX_BLKSIZE, MIN_BLKSIZE

MOD = 2 ** 16
TURN_BLOCKS = MOD

DATA_KIND = "data"
ACK_KIND = "ack"


@dataclass(frozen=True)
class BlockRecord:
    """One entry of the captured send/acknowledgement log.

    kind is either :data:`DATA_KIND` or :data:`ACK_KIND`. block is the 16 bit
    block number on the wire (0..65535). size is the data length in bytes and
    is only meaningful for data records. retransmit marks an entry known to be
    a repeat (a re-sent data block or a duplicate acknowledgement).
    """

    kind: str
    block: int
    size: Optional[int] = None
    retransmit: bool = False

    def __post_init__(self):
        if self.kind not in (DATA_KIND, ACK_KIND):
            raise ValueError("record kind must be %r or %r" % (DATA_KIND, ACK_KIND))
        if not 0 <= self.block < MOD:
            raise ValueError("block number must be in 0..65535")
        if self.kind == ACK_KIND and self.size is not None:
            raise ValueError("acknowledgement records carry no data size")
        if self.size is not None and self.size < 0:
            raise ValueError("data size must not be negative")


@dataclass
class BlockPlacement:
    """The offset information proven for one captured record."""

    index: int
    kind: str
    block: int
    ordinals: Tuple[int, ...] = ()
    offsets: Tuple[int, ...] = ()
    size: Optional[int] = None
    retransmit: bool = False
    duplicate: bool = False
    confirmed: bool = False
    ambiguous: bool = False
    ignored: bool = False
    note: str = ""

    @property
    def offset(self) -> Optional[int]:
        """The unique proven logical offset, or None when ambiguous/ignored."""
        return self.offsets[0] if self.confirmed else None


@dataclass
class _State:
    blksize: int
    filesize: Optional[int]
    next_seq: int = 0
    ack_seq: int = -1
    short_seq: Optional[int] = None
    data_seen: bool = False
    ordinals_by_block: dict = field(default_factory=dict)
    placements: List[BlockPlacement] = field(default_factory=list)

    @property
    def frontier(self) -> int:
        """Ordinal from which the next not-yet-seen block is expected."""
        return max(self.next_seq, self.ack_seq + 1)

    def max_ordinal(self) -> Optional[int]:
        """Last ordinal allowed by the known file length, if any.

        A length of ``full * blksize + rem`` occupies data ordinals
        ``0..full-1`` plus a short tail at ordinal ``full`` when ``rem`` is
        non zero, or an empty terminating block at ordinal ``full`` when the
        length is an exact multiple of the block size. Both cases end at
        ``filesize // blksize``.
        """
        if self.filesize is None:
            return None
        return self.filesize // self.blksize

    def expected_size(self, ordinal: int) -> Optional[int]:
        """Data length the file length predicts for ordinal, if known."""
        if self.filesize is None:
            return None
        remaining = self.filesize - ordinal * self.blksize
        if remaining < 0:
            return None
        return min(remaining, self.blksize)

    def ordinal_allowed(self, ordinal: int) -> bool:
        maximum = self.max_ordinal()
        if maximum is not None and ordinal > maximum:
            return False
        if self.short_seq is not None and ordinal > self.short_seq:
            return False
        return True


def _as_record(record) -> BlockRecord:
    if isinstance(record, BlockRecord):
        return record
    if isinstance(record, dict):
        return BlockRecord(
            kind=record["kind"],
            block=record["block"],
            size=record.get("size"),
            retransmit=record.get("retransmit", False),
        )
    if isinstance(record, (tuple, list)):
        return BlockRecord(*record)
    raise TypeError("unsupported record type: %r" % type(record).__name__)


def _ordinal_for_block(block: int, minimum: int) -> int:
    """Smallest ordinal >= minimum whose wire block number equals block.

    Ordinal s carries wire block (s + 1) % 65536, so s ≡ block - 1 (mod 65536).
    """
    return minimum + ((block - 1 - minimum) % MOD)


class BlockOffsetResolver:
    """Walk an ordered TFTP capture and pin data blocks to logical offsets."""

    def __init__(
        self,
        blksize: int,
        filesize: Optional[int] = None,
        records: Optional[Sequence] = None,
    ):
        if not isinstance(blksize, int) or not MIN_BLKSIZE <= blksize <= MAX_BLKSIZE:
            raise ValueError(
                "blksize must be an int in %d..%d" % (MIN_BLKSIZE, MAX_BLKSIZE)
            )
        if filesize is not None and (not isinstance(filesize, int) or filesize < 0):
            raise ValueError("filesize must be a non-negative int")
        self.blksize = blksize
        self.filesize = filesize
        self.turn_bytes = TURN_BLOCKS * blksize
        self._state = _State(blksize=blksize, filesize=filesize)
        if records is not None:
            for record in records:
                self.feed(record)

    def feed(self, record) -> BlockPlacement:
        """Process one capture record and return its placement."""
        parsed = _as_record(record)
        if parsed.kind == ACK_KIND:
            return self._feed_ack(parsed)
        return self._feed_data(parsed)

    def _feed_ack(self, record: BlockRecord) -> BlockPlacement:
        state = self._state
        index = len(state.placements)
        duplicate = False
        note = ""

        if state.short_seq is not None:
            duplicate = True
            note = "acknowledgement after the terminating short block adds no data"
        else:
            handshake = False
            candidate = _ordinal_for_block(record.block, state.ack_seq + 1)
            previous_turn = candidate - MOD
            if candidate == state.ack_seq + 1:
                state.ack_seq = candidate
                note = "acknowledgement frontier advanced to ordinal %d" % candidate
            elif 0 <= previous_turn <= state.ack_seq:
                duplicate = True
                note = "duplicate acknowledgement of ordinal %d" % previous_turn
            elif record.block == 0 and not state.data_seen:
                # Before any data block exists, block zero cannot acknowledge
                # data; an early ACK zero is the OACK option handshake.
                handshake = True
                note = (
                    "block zero acknowledgement before the first data block is "
                    "the option handshake and advances no data"
                )
            elif not state.ordinal_allowed(candidate):
                duplicate = True
                note = "acknowledgement past end of file adds no data"
            else:
                state.ack_seq = candidate
                note = (
                    "cumulative acknowledgement advanced the frontier to "
                    "ordinal %d" % candidate
            )

        advances = not duplicate and not handshake
        ordinals = (state.ack_seq,) if advances else ()
        placement = BlockPlacement(
            index=index,
            kind=ACK_KIND,
            block=record.block,
            ordinals=ordinals,
            offsets=tuple(ordinal * self.blksize for ordinal in ordinals),
            retransmit=record.retransmit or duplicate,
            duplicate=duplicate,
            confirmed=advances,
            note=note,
        )
        state.placements.append(placement)
        return placement

    def _feed_data(self, record: BlockRecord) -> BlockPlacement:
        state = self._state
        index = len(state.placements)
        state.data_seen = True

        if state.short_seq is not None:
            placement = BlockPlacement(
                index=index,
                kind=DATA_KIND,
                block=record.block,
                size=record.size,
                retransmit=True,
                duplicate=True,
                ignored=True,
                note="data received after the terminating short block is ignored",
            )
            state.placements.append(placement)
            return placement

        base = state.frontier
        forward = _ordinal_for_block(record.block, base)
        past_tracked = list(state.ordinals_by_block.get(record.block, ()))

        past_ordinals = []
        ordinal = forward - MOD
        while ordinal >= 0:
            past_ordinals.append(ordinal)
            ordinal -= MOD
        past_ordinals = sorted(set(past_ordinals) | set(past_tracked))
        latest_past = past_ordinals[-1] if past_ordinals else None

        in_order = (
            not record.retransmit
            and forward == base
            and forward not in past_tracked
        )
        if in_order:
            candidates = [forward]
        elif record.retransmit:
            # A known re-send can only claim the most recent earlier turn; it
            # must never overwrite the same-numbered block of the newer turn.
            candidates = [latest_past] if latest_past is not None else []
        else:
            # No retransmit marker and the block is not due next: either a
            # late block from the previous turn or a new-turn block after a
            # gap in the log. At most these two offsets are plausible.
            candidates = []
            if latest_past is not None:
                candidates.append(latest_past)
            candidates.append(forward)

        candidates = sorted(
            value
            for value in dict.fromkeys(candidates)
            if state.ordinal_allowed(value)
        )

        confirmed = len(candidates) == 1
        ambiguous = len(candidates) > 1
        placement = BlockPlacement(
            index=index,
            kind=DATA_KIND,
            block=record.block,
            ordinals=tuple(candidates),
            offsets=tuple(ordinal * self.blksize for ordinal in candidates),
            size=record.size,
            retransmit=record.retransmit or (confirmed and not in_order),
            duplicate=bool(past_tracked) and not in_order,
            confirmed=confirmed,
            ambiguous=ambiguous,
            note=self._describe(record, candidates, in_order),
        )

        if confirmed and in_order:
            ordinal = candidates[0]
            tracked = state.ordinals_by_block.setdefault(record.block, [])
            if ordinal not in tracked:
                tracked.append(ordinal)
            state.next_seq = max(state.next_seq, ordinal + 1)
            self._check_termination(record, ordinal, placement)
        elif confirmed:
            ordinal = candidates[0]
            tracked = state.ordinals_by_block.setdefault(record.block, [])
            if ordinal not in tracked:
                tracked.append(ordinal)

        state.placements.append(placement)
        return placement

    def _check_termination(
        self, record: BlockRecord, ordinal: int, placement: BlockPlacement
    ) -> None:
        state = self._state
        if record.size is None or record.size >= self.blksize:
            expected = state.expected_size(ordinal)
            if expected is not None and expected < self.blksize and record.size is not None:
                placement.note = (
                    "size %d disagrees with file length, which predicts %d bytes"
                    % (record.size, expected)
                )
            return
        state.short_seq = ordinal
        maximum = state.max_ordinal()
        if maximum is not None and ordinal != maximum:
            placement.note = (
                "short block at ordinal %d but file length expects ordinal %d"
                % (ordinal, maximum)
            )
            return
        expected = state.expected_size(ordinal)
        if expected is not None and record.size != expected:
            placement.note = (
                "short block size %d disagrees with file length, which predicts "
                "%d bytes at ordinal %d" % (record.size, expected, ordinal)
            )
            return
        placement.note = "terminating short block, transfer ends at ordinal %d" % ordinal

    def _describe(
        self, record: BlockRecord, ordinals: List[int], in_order: bool
    ) -> str:
        if not ordinals:
            return "no plausible offset within the known file length"
        if len(ordinals) > 1:
            return (
                "missing log segment leaves %d turns plausible; offsets differ by "
                "%d bytes" % (len(ordinals), self.turn_bytes)
            )
        if in_order:
            return "first transmission in sequence"
        if record.retransmit:
            return (
                "retransmitted block kept at the earlier turn ordinal %d"
                % ordinals[0]
            )
        return "block mapped to its only remaining plausible ordinal"

    @property
    def placements(self) -> Tuple[BlockPlacement, ...]:
        return tuple(self._state.placements)

    def data_placements(self) -> Tuple[BlockPlacement, ...]:
        return tuple(
            placement
            for placement in self._state.placements
            if placement.kind == DATA_KIND
        )


def resolve_block_offsets(
    records: Sequence[Union[BlockRecord, tuple, list, dict]],
    blksize: int,
    filesize: Optional[int] = None,
) -> Tuple[BlockPlacement, ...]:
    """Convenience wrapper returning one placement per capture record.

    records is the ordered mix of data and acknowledgement entries. Each entry
    may be a :class:`BlockRecord`, a mapping with the same fields or a tuple
    shaped ``(kind, block)``, ``(kind, block, size)`` or
    ``(kind, block, size, retransmit)``.
    """
    resolver = BlockOffsetResolver(blksize=blksize, filesize=filesize)
    for record in records:
        resolver.feed(record)
    return resolver.placements


def parse_record(line: str) -> Optional[BlockRecord]:
    """Parse one text capture line, returning None for blank/comment lines.

    Accepted shapes (case insensitive, whitespace separated)::

        DATA  <block> [size] [RETRANS]
        ACK   <block> [DUP]

    for example ``DATA 0 512 retrans`` or ``ack 12 dup``.
    """
    text = line.split("#", 1)[0].strip()
    if not text:
        return None
    tokens = text.split()
    kind_token = tokens[0].lower()
    if kind_token in ("data", "dat"):
        kind = DATA_KIND
    elif kind_token == "ack":
        kind = ACK_KIND
    else:
        raise ValueError("unknown record kind: %r" % tokens[0])

    block = int(tokens[1])
    flags = {token.lower() for token in tokens[2:]}
    retransmit = bool(flags & {"retrans", "retransmit", "dup", "duplicate"})
    numeric = [int(token) for token in tokens[2:] if token.lstrip("-").isdigit()]
    size = numeric[0] if kind == DATA_KIND and numeric else None
    return BlockRecord(kind=kind, block=block, size=size, retransmit=retransmit)


def parse_records(lines) -> List[BlockRecord]:
    """Parse an iterable of text capture lines into records."""
    records = []
    for line in lines:
        record = parse_record(line)
        if record is not None:
            records.append(record)
    return records
