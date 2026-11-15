# vim: ts=4 sw=4 et ai:
"""Offline post-mortem alignment of a TFTP transfer.

This module is deliberately read-only: it never touches the live peers or the
transferred file. It consumes:

* the structured JSONL traces written by ``tftpy.transfer_trace.TransferTracer``
  (preferred), or the existing human-readable tftpy text logs (best effort);
* a storage digest of the receiver-side output file (size, blksize, optionally
  an explicit block set and hash).

It aligns the sender packet timeline, the receiver packet timeline and the
receiver storage digest, and reports three gap classes:

1. SENT_NOT_SEEN      - DAT blocks the sender logged as transmitted but the
                        receiver has no DAT receive event for.
2. SEEN_NOT_ACKED     - DAT blocks the receiver logged as received but the
                        sender has no matching ACK receive event for.
3. ACKED_NOT_STORED   - blocks the sender holds an ACK for but the receiver
                        storage digest cannot account for.

Cross-side ordering is only claimed when the timestamp difference exceeds the
user-supplied clock skew bound. Differences inside the skew band are reported
as indeterminate: a log line appearing two seconds late is never, by itself,
treated as evidence of which side dropped data. When the evidence cannot narrow
the fault to one stage, every stage still consistent with the evidence is
retained, each with the exact instrumentation point needed to disambiguate it.
"""

import argparse
import dataclasses
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

OPCODES = ("RRQ", "WRQ", "DAT", "ACK", "ERR", "OACK")

# Stages a loss can be attributed to. Kept as constants so the report and the
# instrumentation table stay in sync.
STAGE_NET_DAT = "network DAT path (sender -> receiver)"
STAGE_NET_ACK = "network ACK path (receiver -> sender)"
STAGE_SENDER_TX = "sender local send path (sendto / local drop)"
STAGE_SENDER_RX = "sender receive path (socket discard / timeout logic)"
STAGE_RCVR_DEMUX = "receiver demux / TID rejection before session"
STAGE_RCVR_SOCKET = "receiver UDP socket (buffer overflow / not scheduled)"
STAGE_RCVR_CRASH = "receiver process stop / crash"
STAGE_RCVR_WRITE = "receiver file write / flush / fsync / disk"
STAGE_DIGEST = "storage digest itself (stale read, truncation, measurement)"


@dataclass
class Event:
    """One packet-level observation extracted from a trace or text log."""

    side: str
    wall: Optional[float]
    mono: Optional[float]
    direction: str          # tx, rx, store or trace
    opcode: Optional[str]
    block: Optional[int]
    size: Optional[int] = None
    note: Optional[str] = None
    raw: str = ""


@dataclass
class StorageDigest:
    """Receiver-side facts about the file actually on disk."""

    path: str
    size: int
    blksize: int = 512
    expected_size: Optional[int] = None
    blocks_present: Optional[Set[int]] = None
    final_block: Optional[int] = None
    digest_sha256: Optional[str] = None
    taken_at: Optional[float] = None
    derived: bool = False

    def __post_init__(self):
        # Without an explicit block map the stored block set is inferred from
        # the file size and must be labelled as such.
        if self.blocks_present is None:
            self.derived = True

    def stored_blocks(self) -> Set[int]:
        """Blocks accounted for by storage.

        Without an explicit block map, contiguous blocks 1..ceil(size/blksize)
        are inferred from the file size. The inference is flagged as derived
        because a non-contiguous write cannot be distinguished from a
        contiguous one from size alone.
        """
        if self.blocks_present is not None:
            return set(self.blocks_present)
        if self.size <= 0:
            return set()
        count = (self.size + self.blksize - 1) // self.blksize
        return set(range(1, count + 1))


@dataclass
class SideTimeline:
    """Packet events of one peer, indexed by block number."""

    name: str
    role: str
    events: List[Event] = field(default_factory=list)
    dat_tx: Dict[int, List[Event]] = field(default_factory=dict)
    dat_rx: Dict[int, List[Event]] = field(default_factory=dict)
    dat_stored: Dict[int, List[Event]] = field(default_factory=dict)
    ack_tx: Dict[int, List[Event]] = field(default_factory=dict)
    ack_rx: Dict[int, List[Event]] = field(default_factory=dict)
    err_events: List[Event] = field(default_factory=list)
    notes: List[Event] = field(default_factory=list)

    def add(self, event: Event) -> None:
        self.events.append(event)
        block = event.block
        if event.opcode == "DAT" and block is not None:
            if event.direction == "tx":
                self.dat_tx.setdefault(block, []).append(event)
            elif event.direction == "rx":
                self.dat_rx.setdefault(block, []).append(event)
            elif event.direction == "store":
                self.dat_stored.setdefault(block, []).append(event)
        elif event.opcode == "ACK" and block is not None:
            if event.direction == "tx":
                self.ack_tx.setdefault(block, []).append(event)
            elif event.direction == "rx":
                self.ack_rx.setdefault(block, []).append(event)
        if event.opcode == "ERR":
            self.err_events.append(event)
        if event.direction == "trace":
            self.notes.append(event)

    @property
    def first_wall(self) -> Optional[float]:
        stamped = [e.wall for e in self.events if e.wall is not None]
        return min(stamped) if stamped else None

    @property
    def last_wall(self) -> Optional[float]:
        stamped = [e.wall for e in self.events if e.wall is not None]
        return max(stamped) if stamped else None

    def has_note(self, fragment: str) -> List[Event]:
        return [e for e in self.notes if e.note and fragment in e.note]

    def rollover_seen(self) -> bool:
        return 0 in self.dat_tx or 0 in self.dat_rx


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_events(path: str, side: str, role: Optional[str] = None) -> SideTimeline:
    """Load a trace file, auto-detecting structured JSONL vs text log."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(1)
    if head == "{":
        events = _load_jsonl(path, side)
    else:
        events = _load_text_log(path, side)
    inferred_role = role or _infer_role(events)
    timeline = SideTimeline(name=side, role=inferred_role)
    for event in events:
        timeline.add(event)
    return timeline


def _load_jsonl(path: str, side: str) -> List[Event]:
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            events.append(
                Event(
                    side=side,
                    wall=record.get("wall"),
                    mono=record.get("mono"),
                    direction=record.get("dir", "trace"),
                    opcode=record.get("opcode"),
                    block=record.get("block"),
                    size=record.get("size"),
                    note=record.get("note"),
                    raw=line,
                )
            )
    return events


_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})([,.]?\d+)?"
)

# (message regex, direction, opcode or None, block group index, note)
_TEXT_PATTERNS = [
    (re.compile(r"Sending DAT packet (\d+)"), "tx", "DAT", 1, None),
    (re.compile(r"Skipping DAT packet (\d+) for testing"),
     "tx", "DAT", 1, "local-drop-test-hook"),
    (re.compile(r"Handling DAT packet - block (\d+)"), "rx", "DAT", 1, None),
    (re.compile(r"Dropping duplicate block (\d+)"),
     "rx", "DAT", 1, "duplicate"),
    (re.compile(r"Whoa! Received future block (\d+) but expected (\d+)"),
     "rx", "DAT", 1, "future-block"),
    (re.compile(r"Sending ack to block (\d+)"), "tx", "ACK", 1, None),
    (re.compile(r"Received ACK for packet (\d+)"), "rx", "ACK", 1, None),
    (re.compile(r"Received duplicate ACK for block (\d+)"),
     "rx", "ACK", 1, "duplicate"),
    (re.compile(r"Resending packet DAT packet: block (\d+)"),
     "tx", "DAT", 1, "retransmit"),
    (re.compile(r"Resending packet ACK packet: block (\d+)"),
     "tx", "ACK", 1, "retransmit"),
    (re.compile(r"Timeout waiting for traffic"), "trace", None, None, "timeout"),
    (re.compile(r"Timeout waiting for ACK for block (\d+)"),
     "trace", None, 1, "timeout-expect-ack"),
    (re.compile(r"hit max retries, giving up"),
     "trace", None, None, "retries-exhausted"),
    (re.compile(r"Received ERR packet from peer"), "rx", "ERR", None, None),
    (re.compile(r"Fatal exception thrown from session [^:]+: (.*)"),
     "trace", None, None, "fatal"),
    (re.compile(r"Successful transfer\."),
     "trace", None, None, "session-complete"),
    (re.compile(r"Received ACK to final DAT"),
     "trace", None, None, "transfer-complete"),
    (re.compile(r"End of file detected"), "trace", None, None, "eof"),
    (re.compile(r"but (?:we're|we are) connected to .* Discarding"),
     "rx", None, None, "discarded-wrong-tid"),
    (re.compile(r"expected host .* Discarding"),
     "rx", None, None, "discarded-wrong-host"),
    (re.compile(r"Can't find the owner for this packet\. Discarding"),
     "rx", None, None, "discarded-no-owner"),
]


def _parse_timestamp(line: str) -> Optional[float]:
    match = _TIMESTAMP_RE.search(line)
    if not match:
        return None
    date_part, clock_part, frac_part = match.groups()
    stamp = f"{date_part} {clock_part}"
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            parsed = datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        value = parsed.timestamp()
        if frac_part and frac_part not in (".", ","):
            value += float(frac_part.replace(",", "."))
        return value
    return None


def _load_text_log(path: str, side: str) -> List[Event]:
    """Best-effort adapter for the stock tftpy text log format."""
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            wall = _parse_timestamp(line)
            for pattern, direction, opcode, group_idx, note in _TEXT_PATTERNS:
                found = pattern.search(line)
                if not found:
                    continue
                block = None
                if group_idx is not None:
                    block = int(found.group(group_idx))
                events.append(
                    Event(
                        side=side,
                        wall=wall,
                        mono=None,
                        direction=direction,
                        opcode=opcode,
                        block=block,
                        note=note,
                        raw=line,
                    )
                )
                break
    return events


def _infer_role(events: List[Event]) -> str:
    dat_tx = sum(1 for e in events if e.opcode == "DAT" and e.direction == "tx")
    dat_rx = sum(1 for e in events if e.opcode == "DAT" and e.direction == "rx")
    if dat_tx >= dat_rx:
        return "sender"
    return "receiver"


def load_storage(path: Optional[str], size: Optional[int],
                 blksize: int) -> StorageDigest:
    """Build a storage digest from a JSON file or explicit CLI numbers."""
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        blocks = data.get("blocks_present")
        return StorageDigest(
            path=data["path"],
            size=int(data["size"]),
            blksize=int(data.get("blksize", blksize)),
            expected_size=data.get("expected_size"),
            blocks_present=set(blocks) if blocks is not None else None,
            final_block=data.get("final_block"),
            digest_sha256=data.get("sha256"),
            taken_at=data.get("taken_at"),
            derived=blocks is None,
        )
    if size is None:
        raise ValueError("storage digest file or --storage-size is required")
    return StorageDigest(
        path="<cli>", size=size, blksize=blksize, derived=True
    )


def build_storage_digest(path: str, blksize: int,
                         expected_size: Optional[int] = None) -> StorageDigest:
    """Compute a storage digest directly from the receiver's output file.

    This is the simplest offline entry when the output file is available: the
    size drives the contiguous block inference and the sha256 plus collection
    timestamp let later runs prove whether the file changed between digests.
    """
    import hashlib
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    return StorageDigest(
        path=path,
        size=size,
        blksize=blksize,
        expected_size=expected_size,
        blocks_present=None,
        digest_sha256=hasher.hexdigest(),
        taken_at=time.time(),
        derived=True,
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def to_ranges(blocks: List[int]) -> List[Tuple[int, int]]:
    """Compress a sorted block list into inclusive contiguous ranges."""
    if not blocks:
        return []
    ordered = sorted(blocks)
    ranges = []
    start = prev = ordered[0]
    for block in ordered[1:]:
        if block == prev + 1:
            prev = block
            continue
        ranges.append((start, prev))
        start = prev = block
    ranges.append((start, prev))
    return ranges


def format_ranges(blocks: List[int]) -> str:
    ranges = to_ranges(blocks)
    if not ranges:
        return "-"
    return ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in ranges
    )


@dataclass
class Hypothesis:
    stage: str
    verdict: str          # supported / possible / ruled-out
    evidence: str
    instrument: str


@dataclass
class Category:
    key: str
    title: str
    blocks: List[int]
    hypotheses: List[Hypothesis]
    evidence_notes: List[str]


class TransferAnalysis:
    """Aligns both timelines plus storage under a clock skew bound."""

    def __init__(self, sender: SideTimeline, receiver: SideTimeline,
                 storage: StorageDigest, clock_skew: float,
                 network_jitter: float = 0.0):
        self.sender = sender
        self.receiver = receiver
        self.storage = storage
        self.skew = float(clock_skew)
        self.jitter = float(network_jitter)
        self.sent = set(sender.dat_tx)
        self.seen = set(receiver.dat_rx)
        # ACK block 0 acknowledges the WRQ/OACK handshake, not data; only
        # block numbers >= 1 represent confirmed data blocks.
        self.acked = {block for block in sender.ack_rx if block >= 1}
        self.stored = storage.stored_blocks()
        # Terminal DAT blocks carry no file bytes (4-byte header only) and are
        # legitimately absent from storage, so never flag them in category 3.
        self.empty_dat = {
            block for block, events in sender.dat_tx.items()
            if all((event.size or 0) <= 4 for event in events)
        }

    def order_confidence(self, t_first: Optional[float],
                         t_second: Optional[float]) -> Tuple[str, float]:
        """Classify ordering of two cross-side wall timestamps.

        Returns a verdict and the raw delta. Ordering is only asserted outside
        the skew band; everything inside it is indeterminate by construction.
        """
        if t_first is None or t_second is None:
            return "no-timestamp", 0.0
        delta = t_second - t_first
        band = self.skew + self.jitter
        if abs(delta) <= band:
            return "indeterminate-in-skew-band", delta
        if delta < 0:
            return "inconsistent-check-skew", delta
        return "consistent", delta

    def classify(self) -> List[Category]:
        cat1_blocks = sorted(self.sent - self.seen)
        cat2_blocks = sorted(self.seen - self.acked)
        cat3_blocks = sorted((self.acked - self.stored) - self.empty_dat)
        return [
            Category(
                "SENT_NOT_SEEN", "已发未见 (sent, never observed at receiver)",
                cat1_blocks, self._hypotheses_sent_not_seen(cat1_blocks),
                self._evidence_sent_not_seen(cat1_blocks),
            ),
            Category(
                "SEEN_NOT_ACKED", "已见未获确认 (seen at receiver, no ACK at sender)",
                cat2_blocks, self._hypotheses_seen_not_acked(cat2_blocks),
                self._evidence_seen_not_acked(cat2_blocks),
            ),
            Category(
                "ACKED_NOT_STORED", "已确认但存档无法对上 (acked, missing from storage)",
                cat3_blocks, self._hypotheses_acked_not_stored(cat3_blocks),
                self._evidence_acked_not_stored(cat3_blocks),
            ),
        ]

    # -- evidence gathering -------------------------------------------------

    def _evidence_sent_not_seen(self, blocks: List[int]) -> List[str]:
        notes = []
        for block in blocks[:5]:
            attempts = self.sender.dat_tx.get(block, [])
            times = ", ".join(
                f"attempt {idx + 1}@{e.wall:.3f}" if e.wall is not None
                else f"attempt {idx + 1}@no-ts"
                for idx, e in enumerate(attempts)
            )
            notes.append(f"block {block}: sender DAT tx {len(attempts)}x ({times})")
        if len(blocks) > 5:
            notes.append(f"... {len(blocks) - 5} more blocks omitted")
        if self.receiver.last_wall is not None and blocks:
            first_missing = self.sender.dat_tx[blocks[0]][0].wall
            if first_missing is not None:
                verdict, delta = self.order_confidence(
                    self.receiver.last_wall, first_missing
                )
                notes.append(
                    f"receiver last event vs first missing DAT: "
                    f"{verdict} (delta={delta:+.3f}s, skew band={self.skew}s)"
                )
        return notes

    def _evidence_seen_not_acked(self, blocks: List[int]) -> List[str]:
        notes = []
        for block in blocks[:5]:
            rx_count = len(self.receiver.dat_rx.get(block, []))
            ack_tx = self.receiver.ack_tx.get(block, [])
            ack_rx = self.sender.ack_rx.get(block, [])
            notes.append(
                f"block {block}: receiver DAT rx {rx_count}x, "
                f"receiver ACK tx {len(ack_tx)}x, sender ACK rx {len(ack_rx)}x"
            )
        if len(blocks) > 5:
            notes.append(f"... {len(blocks) - 5} more blocks omitted")
        return notes

    def _evidence_acked_not_stored(self, blocks: List[int]) -> List[str]:
        notes = [
            f"storage accounts for {len(self.stored)} contiguous blocks "
            f"({format_ranges(sorted(self.stored))}); "
            f"storage block map derived={self.storage.derived}"
        ]
        in_process_stored = set(self.receiver.dat_stored)
        if in_process_stored:
            notes.append(
                f"receiver process logged 'stored-block' for "
                f"{len(in_process_stored)} blocks "
                f"({format_ranges(sorted(in_process_stored))})"
            )
            written_but_missing = sorted(
                (set(blocks) & in_process_stored) - self.stored
            )
            if written_but_missing:
                notes.append(
                    "blocks written in-process per trace but absent from the "
                    f"on-disk digest: {format_ranges(written_but_missing)} - "
                    "points at flush/fsync/truncation or digest timing rather "
                    "than the network"
                )
        for block in blocks[:5]:
            ack_events = self.sender.ack_rx.get(block, [])
            rx_events = self.receiver.dat_rx.get(block, [])
            store_events = self.receiver.dat_stored.get(block, [])
            notes.append(
                f"block {block}: sender ACK rx {len(ack_events)}x, "
                f"receiver DAT rx {len(rx_events)}x, "
                f"receiver store {len(store_events)}x"
            )
        if len(blocks) > 5:
            notes.append(f"... {len(blocks) - 5} more blocks omitted")
        return notes

    # -- hypothesis engines -------------------------------------------------

    def _hypotheses_sent_not_seen(self, blocks: List[int]) -> List[Hypothesis]:
        if not blocks:
            return []
        hypotheses = [
            Hypothesis(
                STAGE_NET_DAT, "possible",
                "DAT transmitted at sender with no receive event at receiver; "
                "an in-flight loss remains consistent with every observation.",
                "tftpy/TftpStates.py::TftpState.sendDAT - record block, wire "
                "size, attempt ordinal and sendto() result for every DAT tx",
            ),
            Hypothesis(
                STAGE_RCVR_SOCKET, "possible",
                "Receiver may have dropped the datagram in its UDP receive "
                "buffer or never got scheduled; no application-level trace "
                "exists for kernel-level drops.",
                "tftpy/TftpServer.py::TftpServer.listen - log select() readiness "
                "and recvfrom() size per session socket; capture SO_RCVBUF drops "
                "outside the process alongside it",
            ),
        ]
        local_drops = [
            e for b in blocks for e in self.sender.dat_tx.get(b, [])
            if e.note == "local-drop-test-hook"
        ]
        if local_drops:
            hypotheses.append(Hypothesis(
                STAGE_SENDER_TX, "supported",
                f"{len(local_drops)} DAT sends were skipped inside the sender "
                "process (test hook or equivalent local drop).",
                "tftpy/TftpStates.py::TftpState.sendDAT - keep the skip/drop "
                "branch logging the block number and reason",
            ))
        if self.receiver.has_note("discarded-wrong-tid") or \
                self.receiver.has_note("discarded-wrong-host") or \
                self.receiver.has_note("discarded-no-owner"):
            hypotheses.append(Hypothesis(
                STAGE_RCVR_DEMUX, "supported",
                "Receiver log contains TID/host/owner discard events; datagrams "
                "arrived but were rejected before reaching the session state.",
                "tftpy/TftpContexts.py::TftpContext.cycle and "
                "tftpy/TftpServer.py::TftpServer.listen - log the rejected "
                "peer ip:port, expected tid and first payload bytes",
            ))
        else:
            hypotheses.append(Hypothesis(
                STAGE_RCVR_DEMUX, "possible",
                "No discard event was logged, but demux rejection at the TID "
                "check cannot be excluded without per-datagram peer logging.",
                "tftpy/TftpContexts.py::TftpContext.cycle - add a trace event on "
                "every tidport/address mismatch with block number when parsable",
            ))
        receiver_stopped_early = False
        if self.receiver.last_wall is not None and blocks:
            first_gap_tx = self.sender.dat_tx[blocks[0]][0].wall
            if first_gap_tx is not None:
                verdict, _ = self.order_confidence(
                    self.receiver.last_wall, first_gap_tx
                )
                receiver_stopped_early = verdict == "consistent"
        if receiver_stopped_early:
            hypotheses.append(Hypothesis(
                STAGE_RCVR_CRASH, "supported",
                "Receiver's last logged event precedes the first missing DAT by "
                "more than the clock skew band; the receiver was no longer "
                "processing the session when the gap blocks were sent.",
                "tftpy/TftpContexts.py::TftpContextServer.end and the listen() "
                "deletion path - log session key, last block, exit reason",
            ))
        else:
            hypotheses.append(Hypothesis(
                STAGE_RCVR_CRASH, "possible",
                "Receiver stop time cannot be placed relative to the gap inside "
                "the clock skew band; a stop during the window is not excluded.",
                "tftpy/TftpServer.py::TftpServer.listen - emit a session-stop "
                "trace event with wall time before context.end()",
            ))
        if self.sender.has_note("retries-exhausted"):
            hypotheses.append(Hypothesis(
                STAGE_SENDER_RX, "ruled-out",
                "Sender retried until the retry limit instead of stopping "
                "early; the sender timeout loop behaved as designed.",
                "no change needed; retain timeout/retry trace events",
            ))
        else:
            hypotheses.append(Hypothesis(
                STAGE_SENDER_RX, "possible",
                "No retries-exhausted marker found; an unlogged sender-side exit "
                "or sendto failure could truncate the send timeline.",
                "tftpy/TftpContexts.py::TftpContextClientUpload.start - log "
                "retry_count, next_block and exception type on loop exit; "
                "wrap sendto() in sendDAT/resendLast to capture OSError",
            ))
        return hypotheses

    def _hypotheses_seen_not_acked(self, blocks: List[int]) -> List[Hypothesis]:
        if not blocks:
            return []
        receiver_acked = any(self.receiver.ack_tx.get(b) for b in blocks)
        hypotheses = []
        if receiver_acked:
            hypotheses.append(Hypothesis(
                STAGE_NET_ACK, "possible",
                "Receiver logged ACK transmission but the sender has no ACK "
                "receive; an in-flight ACK loss fits all observations.",
                "tftpy/TftpStates.py::TftpState.sendACK - record ACK tx block "
                "and sendto() result; correlate against sender cycle() rx events",
            ))
            hypotheses.append(Hypothesis(
                STAGE_SENDER_RX, "possible",
                "The ACK may instead have reached the sender host and been "
                "discarded at the socket/TID layer; existing logs cannot "
                "separate this from network loss.",
                "tftpy/TftpContexts.py::TftpContext.cycle - trace every "
                "recvfrom() with peer ip:port and the TID discard branches",
            ))
        else:
            hypotheses.append(Hypothesis(
                STAGE_RCVR_CRASH, "possible",
                "Receiver has a DAT receive but no ACK transmit event; it may "
                "have stopped between receive and sendACK(). Note handleDat() "
                "calls sendACK() before fileobj.write().",
                "tftpy/TftpStates.py::TftpState.handleDat - trace at entry, "
                "after sendACK() and after fileobj.write() separately",
            ))
            hypotheses.append(Hypothesis(
                STAGE_NET_ACK, "possible",
                "ACK transmission may be absent from the log simply because "
                "debug logging was disabled; network ACK loss stays possible.",
                "enable the JSONL tracer (bin/tftpy_server.py --trace-dir) so "
                "ACK tx is captured independently of log level",
            ))
        if any(len(self.receiver.dat_rx.get(b, [])) > 1 for b in blocks):
            hypotheses.append(Hypothesis(
                STAGE_NET_ACK, "supported",
                "Receiver logged duplicate DAT blocks: it re-received data it "
                "had already handled, which only happens when its earlier ACK "
                "never advanced the sender state.",
                "tftpy/TftpStates.py::TftpState.handleDat - keep the duplicate "
                "branch recording block and expected next_block",
            ))
        if self.sender.has_note("timeout") or self.sender.has_note(
                "timeout-expect-ack"):
            hypotheses.append(Hypothesis(
                STAGE_SENDER_RX, "supported",
                "Sender logged timeouts while awaiting these ACKs; the loss "
                "manifests on the ACK return path, but network vs local socket "
                "cannot be split without per-datagram rx tracing.",
                "tftpy/TftpContexts.py::TftpContext.cycle - add rx/discard "
                "events so timeout windows can be reconciled with arrivals",
            ))
        return hypotheses

    def _hypotheses_acked_not_stored(self, blocks: List[int]) -> List[Hypothesis]:
        if not blocks:
            return []
        hypotheses = [
            Hypothesis(
                STAGE_RCVR_CRASH, "possible",
                "handleDat() runs sendACK() before fileobj.write(); a stop in "
                "that window leaves the sender holding an ACK for data never "
                "written. This ordering makes the category structurally possible.",
                "tftpy/TftpStates.py::TftpState.handleDat - emit separate trace "
                "events around sendACK(), fileobj.write(), flush and close",
            ),
            Hypothesis(
                STAGE_RCVR_WRITE, "possible",
                "A short/failed write, missing flush/fsync, disk full or "
                "external truncation would leave storage short despite ACKs.",
                "tftpy/TftpStates.py::TftpState.handleDat - log write offset and "
                "byte count; tftpy/TftpContexts.py::TftpContextServer.end - log "
                "final file size, flush/fsync result and ERR DiskFull",
            ),
            Hypothesis(
                STAGE_DIGEST, "possible",
                "The digest may have been taken before the file was closed or "
                "after external truncation; re-take it against the closed file.",
                "record taken_at in the digest and compare with the receiver "
                "session-end trace event; include sha256 of the closed file",
            ),
        ]
        if any(e.note and "disk" in e.note.lower() for e in
               self.receiver.notes + self.receiver.err_events) or \
                any(e.opcode == "ERR" for e in self.receiver.err_events):
            hypotheses.append(Hypothesis(
                STAGE_RCVR_WRITE, "supported",
                "Receiver logged an ERR/disk condition during the transfer.",
                "tftpy/TftpStates.py::TftpState.sendError - trace errorcode and "
                "the triggering write/flush failure with errno",
            ))
        if self.receiver.has_note("session-complete") or \
                self.receiver.has_note("transfer-complete"):
            hypotheses.append(Hypothesis(
                STAGE_RCVR_CRASH, "ruled-out",
                "Receiver logged a clean completion marker, arguing against a "
                "mid-transfer crash; focus on write/fsync path or digest timing.",
                "no change needed; retain completion marker in the tracer",
            ))
        return hypotheses

    # -- merged timeline ----------------------------------------------------

    def timeline_rows(self, only_anomalous: bool = True,
                      context: int = 2) -> List[dict]:
        blocks = sorted(self.sent | self.seen | self.acked | self.stored)
        anomalous = set()
        for category in self.classify():
            anomalous.update(category.blocks)
        rows = []
        for index, block in enumerate(blocks):
            if only_anomalous and not any(
                abs(block - gap) <= context for gap in anomalous
            ):
                continue
            tx_events = self.sender.dat_tx.get(block, [])
            rx_events = self.receiver.dat_rx.get(block, [])
            ack_tx_events = self.receiver.ack_tx.get(block, [])
            ack_rx_events = self.sender.ack_rx.get(block, [])
            t_tx = tx_events[0].wall if tx_events else None
            t_rx = rx_events[0].wall if rx_events else None
            confidence, delta = self.order_confidence(t_tx, t_rx)
            rows.append({
                "block": block,
                "sender_dat_tx": len(tx_events),
                "sender_dat_tx_wall": t_tx,
                "receiver_dat_rx": len(rx_events),
                "receiver_dat_rx_wall": t_rx,
                "rx_minus_tx_s": round(delta, 3) if t_tx and t_rx else None,
                "order_confidence": confidence,
                "receiver_ack_tx": len(ack_tx_events),
                "sender_ack_rx": len(ack_rx_events),
                "stored": block in self.stored,
            })
        return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

INSTRUMENTATION_TABLE = [
    ("sender", "tftpy/TftpStates.py::TftpState.sendDAT (DAT tx path)",
     "block, wire size, attempt ordinal, sendto() success/OSError"),
    ("sender", "tftpy/TftpStates.py::TftpState.resendLast (retransmission)",
     "block, retry_count, reason (timeout), last_dat_time"),
    ("sender", "tftpy/TftpContexts.py::TftpContext.cycle (receive path)",
     "recvfrom peer ip:port, parsed opcode/block, TID/host discard branch"),
    ("sender", "tftpy/TftpContexts.py::TftpContextClientUpload.start (loop exit)",
     "next_block, retry_count, exception type at termination"),
    ("receiver", "tftpy/TftpServer.py::TftpServer.listen (demux loop)",
     "socket readiness, session key match, no-owner/TID discards"),
    ("receiver", "tftpy/TftpStates.py::TftpState.handleDat (DAT rx + write)",
     "entry block/expected block, sendACK done, write offset/bytes, "
     "duplicate/future branches"),
    ("receiver", "tftpy/TftpStates.py::TftpState.sendACK (ACK tx path)",
     "block, sendto() result"),
    ("receiver", "tftpy/TftpContexts.py::TftpContextServer.end (close)",
     "final file size, flush/fsync result, duration, dup/error counters"),
]


def _fmt_time(value: Optional[float]) -> str:
    return f"{value:.3f}" if value is not None else "no-ts"


def render_text(analysis: TransferAnalysis) -> str:
    sender = analysis.sender
    receiver = analysis.receiver
    storage = analysis.storage
    lines = []
    lines.append("=" * 78)
    lines.append("TFTP offline transfer alignment report")
    lines.append("=" * 78)
    lines.append("[inputs]")
    lines.append(f"  sender   : {sender.name} role={sender.role} "
                 f"events={len(sender.events)} "
                 f"window=({_fmt_time(sender.first_wall)},"
                 f"{_fmt_time(sender.last_wall)})")
    lines.append(f"  receiver : {receiver.name} role={receiver.role} "
                 f"events={len(receiver.events)} "
                 f"window=({_fmt_time(receiver.first_wall)},"
                 f"{_fmt_time(receiver.last_wall)})")
    lines.append(f"  storage  : path={storage.path} size={storage.size} "
                 f"blksize={storage.blksize} derived={storage.derived}")
    if storage.expected_size is not None:
        lines.append(f"             expected_size={storage.expected_size} "
                     f"delta={storage.size - storage.expected_size}")
    lines.append(f"  clock skew bound: {analysis.skew}s "
                 f"(network jitter allowance: {analysis.jitter}s)")
    if sender.rollover_seen() or receiver.rollover_seen():
        lines.append("  WARNING: block number 0 observed - 16-bit rollover in "
                     "play; ranges are numeric and wrap at 65536")
    lines.append("")
    lines.append("[coverage]")
    lines.append(f"  DAT blocks sent      : {len(analysis.sent)} "
                 f"({format_ranges(sorted(analysis.sent))})")
    lines.append(f"  DAT blocks observed  : {len(analysis.seen)} "
                 f"({format_ranges(sorted(analysis.seen))})")
    lines.append(f"  blocks ACKed at sender: {len(analysis.acked)} "
                 f"({format_ranges(sorted(analysis.acked))})")
    lines.append(f"  blocks in storage    : {len(analysis.stored)} "
                 f"({format_ranges(sorted(analysis.stored))})")
    lines.append("")

    for category in analysis.classify():
        lines.append("-" * 78)
        lines.append(f"[{category.key}] {category.title}")
        if not category.blocks:
            lines.append("  no blocks in this category")
            lines.append("")
            continue
        lines.append(f"  blocks ({len(category.blocks)}): "
                     f"{format_ranges(category.blocks)}")
        lines.append("  evidence:")
        for note in category.evidence_notes:
            lines.append(f"    - {note}")
        lines.append("  candidate responsibility stages "
                     "(multiple retained while evidence cannot narrow):")
        for hypothesis in category.hypotheses:
            lines.append(f"    [{hypothesis.verdict.upper():9s}] "
                         f"{hypothesis.stage}")
            lines.append(f"        why : {hypothesis.evidence}")
            lines.append(f"        log : {hypothesis.instrument}")
        lines.append("")

    lines.append("-" * 78)
    lines.append("[merged timeline around gaps] "
                 "(ordering inside the skew band is NOT inferred)")
    header = (f"  {'block':>6} {'s_tx':>4} {'r_rx':>4} {'rx-tx':>8} "
              f"{'confidence':>24} {'r_acktx':>7} {'s_ackrx':>7} stored")
    lines.append(header)
    for row in analysis.timeline_rows():
        lines.append(
            f"  {row['block']:>6} {row['sender_dat_tx']:>4} "
            f"{row['receiver_dat_rx']:>4} "
            f"{(str(row['rx_minus_tx_s']) if row['rx_minus_tx_s'] is not None else '-'):>8} "
            f"{row['order_confidence']:>24} {row['receiver_ack_tx']:>7} "
            f"{row['sender_ack_rx']:>7} {'yes' if row['stored'] else 'NO'}"
        )
    lines.append("")
    lines.append("[canonical instrumentation to enable on both sides]")
    for side, location, fields in INSTRUMENTATION_TABLE:
        lines.append(f"  {side:8s} {location}")
        lines.append(f"             fields: {fields}")
    lines.append("")
    lines.append("Capture with the built-in tracer (independent of log level):")
    lines.append("  bin/tftpy_client.py --trace client.jsonl ...")
    lines.append("  bin/tftpy_server.py --trace-dir /var/log/tftp/ ...")
    lines.append("Then re-run: python -m tftpy.offline_diag --clock-skew S "
                 "--sender-trace client.jsonl --receiver-trace server.jsonl "
                 "--storage-size N --blksize B")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_json(analysis: TransferAnalysis) -> str:
    payload = {
        "clock_skew": analysis.skew,
        "network_jitter": analysis.jitter,
        "storage": dataclasses.asdict(analysis.storage),
        "coverage": {
            "sent": sorted(analysis.sent),
            "seen": sorted(analysis.seen),
            "acked": sorted(analysis.acked),
            "stored": sorted(analysis.stored),
        },
        "categories": [
            {
                "key": c.key,
                "title": c.title,
                "blocks": c.blocks,
                "ranges": [list(r) for r in to_ranges(c.blocks)],
                "evidence_notes": c.evidence_notes,
                "hypotheses": [
                    dataclasses.asdict(h) for h in c.hypotheses
                ],
            }
            for c in analysis.classify()
        ],
        "timeline": analysis.timeline_rows(),
    }
    payload["storage"]["blocks_present"] = (
        sorted(analysis.storage.blocks_present)
        if analysis.storage.blocks_present is not None else None
    )
    return json.dumps(payload, indent=2, ensure_ascii=False)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline alignment of sender/receiver TFTP traces with a "
                    "receiver storage digest, under a bounded clock skew."
    )
    parser.add_argument("--sender-trace", required=True,
                        help="sender JSONL tracer file or tftpy text log")
    parser.add_argument("--receiver-trace", required=True,
                        help="receiver JSONL tracer file or tftpy text log")
    parser.add_argument("--clock-skew", required=True, type=float,
                        help="maximum absolute clock difference between the two "
                             "hosts, in seconds (include NTP/zone uncertainty)")
    parser.add_argument("--network-jitter", type=float, default=0.0,
                        help="extra one-way timing allowance in seconds added "
                             "to the skew band for ordering decisions")
    parser.add_argument("--storage", help="storage digest JSON file")
    parser.add_argument("--storage-file",
                        help="receiver output file to build a digest from "
                             "directly (size, sha256 and taken_at)")
    parser.add_argument("--storage-size", type=int,
                        help="receiver output file size in bytes")
    parser.add_argument("--expected-size", type=int,
                        help="expected complete file size in bytes, if known")
    parser.add_argument("--blksize", type=int, default=512,
                        help="negotiated blocksize for size->block derivation")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", help="write the report here instead of "
                                         "stdout")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if sum((bool(args.storage), bool(args.storage_file),
            args.storage_size is not None)) != 1:
        raise SystemExit(
            "provide exactly one of --storage, --storage-file or "
            "--storage-size")
    sender = load_events(args.sender_trace, "sender", "sender")
    receiver = load_events(args.receiver_trace, "receiver", "receiver")
    if args.storage_file:
        storage = build_storage_digest(
            args.storage_file, args.blksize, args.expected_size)
    else:
        storage = load_storage(args.storage, args.storage_size, args.blksize)
        if args.expected_size is not None:
            storage.expected_size = args.expected_size
    analysis = TransferAnalysis(
        sender, receiver, storage, args.clock_skew, args.network_jitter
    )
    report = render_json(analysis) if args.format == "json" \
        else render_text(analysis)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
