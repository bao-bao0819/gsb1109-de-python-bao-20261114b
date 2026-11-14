# vim: ts=4 sw=4 et ai:
"""Offline analysis of timestamped TFTP observations.

This module works from an observation log such as the one captured for a
device daily report, rather than from a live session. Each observation is a
timestamped TFTP packet, for example::

    100.000 RRQ
    100.050 DAT block=1 len=512
    100.080 ACK block=1
    100.200 DAT block=2 len=512
    100.450 DAT block=2 len=512

It answers four accounting questions about the DAT traffic:

* unique payload      - payload delivered for the first time (the bytes that
                        actually landed in the receiving end's store);
* first delivery      - when each unique block first arrived, as an exact
                        time when it was observed, or as a bracketing time
                        interval when the first copy was missed;
* retransmit overhead - payload observed again for an already known block;
* undeterminable      - payload whose first delivery happened outside the
                        observation window, so it cannot be proven unique.

The log is allowed to cover only the middle of a transfer. A block is only
accepted as a first delivery when its predecessor block (or the session
handshake for block 1) was also observed; otherwise it is reported as
undeterminable instead of being optimistically counted as unique.

When two arrivals of the same block are on record but the real first
reception was missed, the first delivery is bracketed by the predecessor's
delivery and the first observed arrival. The second arrival is never used as
a first-delivery time. Effective payload rate is therefore returned as
bounds in that case.

Accounting always reconciles exactly::

    observed DAT bytes == unique payload + retransmit + undeterminable
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

# TFTP block numbers are 16-bit and roll over after 65535.
BLOCK_MODULUS = 2 ** 16
LAST_BLOCK = BLOCK_MODULUS - 1

_OPCODE_RE = re.compile(r"\b(DAT|DATA|ACK|RRQ|WRQ|OACK|ERR)\b", re.IGNORECASE)
_BLOCK_RE = re.compile(r"\bblock\s*[:=]?\s*(\d+)", re.IGNORECASE)
_LEN_RE = re.compile(r"\b(?:len|length|bytes|data_bytes)\s*[:=]?\s*(\d+)",
                     re.IGNORECASE)
_TOKEN_RE = re.compile(r"[^\s]+")

# Classifications stored on each block record.
CLASS_UNIQUE = "unique"
CLASS_UNDETERMINABLE = "undeterminable"


@dataclass
class Observation:
    """One observed TFTP packet at a point in time."""

    time: float
    opcode: str
    block: int = None
    data_bytes: int = 0


@dataclass
class TimeInterval:
    """A half-open knowledge range for an event time.

    ``lower`` is the strict lower bound (the event happened after it) and
    ``upper`` is the inclusive upper bound (no later than it). A ``None``
    lower bound means the log does not provide one.
    """

    lower: float = None
    upper: float = None

    def to_dict(self):
        return {"lower": self.lower, "upper": self.upper}


@dataclass
class BlockDelivery:
    """What the log proves about a single block's delivery."""

    block: int
    generation: int
    size: int
    classification: str
    arrivals: list = field(default_factory=list)
    first_delivery: object = None

    @property
    def duplicate_arrivals(self):
        """Arrivals beyond the first one on record."""
        return max(0, len(self.arrivals) - 1)

    def to_dict(self):
        if isinstance(self.first_delivery, TimeInterval):
            first = self.first_delivery.to_dict()
        else:
            first = self.first_delivery
        return {
            "block": self.block,
            "generation": self.generation,
            "size": self.size,
            "classification": self.classification,
            "first_delivery": first,
            "arrivals": list(self.arrivals),
            "duplicate_arrivals": self.duplicate_arrivals,
        }


@dataclass
class RateBounds:
    """Effective payload rate in bits per second.

    When every first delivery is known exactly the two bounds coincide and
    ``exact`` is true. A ``None`` bound means the log does not constrain it.
    """

    lower_bps: float = None
    upper_bps: float = None
    exact: bool = False

    def to_dict(self):
        return {
            "lower_bps": self.lower_bps,
            "upper_bps": self.upper_bps,
            "exact": self.exact,
        }


@dataclass
class TftpObservationAnalysis:
    """Reconciled accounting of one observed transfer."""

    observed_dat_bytes: int = 0
    observed_dat_packets: int = 0
    unique_payload_bytes: int = 0
    retransmit_bytes: int = 0
    undeterminable_bytes: int = 0
    unique_blocks: int = 0
    retransmit_packets: int = 0
    undeterminable_packets: int = 0
    control_packets: int = 0
    handshake_observed: bool = False
    duration_note: str = ""
    blocks: list = field(default_factory=list)
    rate: RateBounds = field(default_factory=RateBounds)

    def verify_accounting(self):
        """Assert the byte- and packet-level ledgers reconcile."""
        assert (
            self.unique_payload_bytes
            + self.retransmit_bytes
            + self.undeterminable_bytes
            == self.observed_dat_bytes
        )
        assert (
            self.unique_blocks
            + self.retransmit_packets
            + self.undeterminable_packets
            == self.observed_dat_packets
        )

    def to_dict(self):
        return {
            "observed_dat_bytes": self.observed_dat_bytes,
            "observed_dat_packets": self.observed_dat_packets,
            "unique_payload_bytes": self.unique_payload_bytes,
            "retransmit_bytes": self.retransmit_bytes,
            "undeterminable_bytes": self.undeterminable_bytes,
            "unique_blocks": self.unique_blocks,
            "retransmit_packets": self.retransmit_packets,
            "undeterminable_packets": self.undeterminable_packets,
            "control_packets": self.control_packets,
            "handshake_observed": self.handshake_observed,
            "duration_note": self.duration_note,
            "rate": self.rate.to_dict(),
            "blocks": [record.to_dict() for record in self.blocks],
        }


def _parse_time(token):
    """Parse a leading timestamp token, accepting epoch seconds or ISO 8601."""
    try:
        return float(token)
    except ValueError:
        pass
    text = token.rstrip(",")
    if "T" in text or "t" in text:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.timestamp()
        return parsed.timestamp()
    raise ValueError("unparseable timestamp: %s" % token)


def parse_observation_line(line, strict=True):
    """Parse one log line into an :class:`Observation`.

    Lines without a TFTP opcode token are ignored and return ``None``. A DAT
    line without a payload length cannot be accounted for and raises
    ``ValueError`` when ``strict`` is true.
    """
    if not line or not line.strip():
        return None
    opcode_match = _OPCODE_RE.search(line)
    if opcode_match is None:
        return None
    opcode = opcode_match.group(1).upper()
    if opcode == "DATA":
        opcode = "DAT"

    tokens = _TOKEN_RE.findall(line)
    time_value = _parse_time(tokens[0])

    block_match = _BLOCK_RE.search(line)
    block = int(block_match.group(1)) if block_match else None

    data_bytes = 0
    if opcode == "DAT":
        len_match = _LEN_RE.search(line)
        if len_match is None:
            if strict:
                raise ValueError("DAT observation without payload length: %s"
                                 % line.strip())
            return None
        data_bytes = int(len_match.group(1))
        if block is None or not 0 <= block <= LAST_BLOCK:
            if strict:
                raise ValueError(
                    "DAT observation with invalid block number: %s"
                    % line.strip())
            return None

    return Observation(
        time=time_value,
        opcode=opcode,
        block=block,
        data_bytes=data_bytes,
    )


def parse_observations(lines, strict=True):
    """Parse an iterable of log lines into a list of observations."""
    observations = []
    for line in lines:
        observation = parse_observation_line(line, strict=strict)
        if observation is not None:
            observations.append(observation)
    return observations


def load_observations(path, strict=True):
    """Read observations from a log file."""
    with open(path, "r", encoding="utf-8") as logfile:
        return parse_observations(logfile, strict=strict)


def _is_handshake(observation):
    """Whether a control packet proves the transfer start was observed."""
    if observation.opcode in ("RRQ", "WRQ", "OACK"):
        return True
    return observation.opcode == "ACK" and observation.block == 0


def _is_rollover(known_blocks, block):
    """Detect a 16-bit block-number rollover starting a new generation."""
    if not known_blocks or block not in (0, 1):
        return False
    return LAST_BLOCK in known_blocks


def _predecessor_record(generations, generation_index, block):
    """Look up the record for the preceding block in the same generation."""
    predecessor = (block - 1) % BLOCK_MODULUS
    if block == 0 and generation_index > 0:
        return generations[generation_index - 1].get(predecessor)
    return generations[generation_index].get(predecessor)


def _first_delivery_upper(record):
    """The latest time at which a block's first delivery could have happened."""
    delivery = record.first_delivery
    if isinstance(delivery, TimeInterval):
        return delivery.upper
    return delivery


def _classify_block(generations, generation_index, observation, handshake):
    """Build the delivery record for a block seen for the first time.

    A first delivery is only proven when an unbroken in-order chain connects
    it to an observed handshake. Block 1 needs the handshake itself; every
    later block needs a proven-unique predecessor. A retransmit of a block
    delivered before the log started is indistinguishable from a live first
    delivery, so anything off the anchored chain is undeterminable and its
    first delivery is bracketed by the predecessor's upper bound.
    """
    block = observation.block
    known = generations[generation_index]
    predecessor = _predecessor_record(
        generations, generation_index, block)

    first_block_of_transfer = block == 1 and predecessor is None
    anchored = first_block_of_transfer and handshake
    if predecessor is not None and predecessor.classification == CLASS_UNIQUE:
        anchored = True

    if anchored:
        classification = CLASS_UNIQUE
        first_delivery = observation.time
    else:
        classification = CLASS_UNDETERMINABLE
        lower = _first_delivery_upper(predecessor) if predecessor else None
        # The missed first copy arrived strictly before the first sighting;
        # the first observed arrival is the inclusive upper bound. A later
        # duplicate never moves this bound.
        first_delivery = TimeInterval(
            lower=lower,
            upper=observation.time,
        )

    record = BlockDelivery(
        block=block,
        generation=generation_index,
        size=observation.data_bytes,
        classification=classification,
        arrivals=[observation.time],
        first_delivery=first_delivery,
    )
    known[block] = record
    return record


def _delivery_points(analysis):
    """Collect (lower, upper) first-delivery times for every block."""
    points = []
    for record in analysis.blocks:
        delivery = record.first_delivery
        if isinstance(delivery, TimeInterval):
            points.append((delivery.lower, delivery.upper))
        else:
            points.append((delivery, delivery))
    return points


def _compute_rate(analysis, delivered_bytes):
    """Derive effective payload rate, bracketed when timings are intervals.

    The numerator is distinct payload delivered within the observation:
    proven-unique payload plus the first on-record arrival of blocks whose
    true first delivery was missed. Retransmits are overhead and excluded.

    For a delivery bracketed by ``(L, U]``, interval arithmetic on
    ``last_delivery - first_delivery`` gives:

    * widest span  = max(U) - min(L) -> slowest (lower) rate bound;
    * narrowest span = max(L) - min(U) -> fastest (upper) rate bound.

    A missing lower bound means the corresponding rate end is unbounded.
    """
    points = _delivery_points(analysis)
    if not points or delivered_bytes == 0:
        return

    finite_lowers = [lower for lower, _ in points if lower is not None]
    uppers = [upper for _, upper in points]

    start_upper = min(uppers)
    end_upper = max(uppers)
    # The widest span needs the earliest possible start; a single missing
    # lower bound lets the window extend back indefinitely, so the slow
    # rate bound is lost. The narrowest span only needs the latest proven
    # lower bound, which survives as long as any block has a predecessor.
    start_lower = (
        min(finite_lowers) if len(finite_lowers) == len(points) else None)
    end_lower = max(finite_lowers) if finite_lowers else None

    exact = all(
        not isinstance(record.first_delivery, TimeInterval)
        for record in analysis.blocks
    )

    if exact:
        span = end_upper - start_upper
        if span <= 0:
            # Match the live-session convention for instantaneous captures.
            analysis.duration_note = (
                "non-positive delivery span; treated as 1 second")
            span = 1.0
        rate = delivered_bytes * 8.0 / span
        analysis.rate = RateBounds(
            lower_bps=rate, upper_bps=rate, exact=True)
        return

    # Widest plausible span yields the slowest defensible rate.
    span_upper = (
        end_upper - start_lower if start_lower is not None else None)
    # Narrowest plausible span yields the fastest defensible rate.
    span_lower = (
        end_lower - start_upper if end_lower is not None else None)

    lower_rate = (
        delivered_bytes * 8.0 / span_upper
        if span_upper is not None and span_upper > 0
        else None
    )
    upper_rate = (
        delivered_bytes * 8.0 / span_lower
        if span_lower is not None and span_lower > 0
        else None
    )
    analysis.rate = RateBounds(
        lower_bps=lower_rate,
        upper_bps=upper_rate,
        exact=False,
    )


def analyze_observations(observations):
    """Analyze timestamped observations and return reconciled accounting."""
    analysis = TftpObservationAnalysis()
    generations = [{}]
    generation_high = [-1]
    handshake = False

    for observation in observations:
        if observation.opcode != "DAT":
            analysis.control_packets += 1
            if _is_handshake(observation):
                handshake = True
                analysis.handshake_observed = True
            continue

        analysis.observed_dat_packets += 1
        analysis.observed_dat_bytes += observation.data_bytes

        rolled_over = False
        if _is_rollover(generations[-1], observation.block):
            generations.append({})
            generation_high.append(-1)
            rolled_over = True
        generation_index = len(generations) - 1
        known = generations[generation_index]
        block = observation.block

        record = known.get(block)
        if record is None and generation_index > 0 and not rolled_over:
            # The block is new to the current generation. It is only a
            # genuine new block when it is the next one in sequence; a
            # retransmit can lag behind by a whole rollover. Anything off
            # the in-order position resolves against an earlier generation.
            in_order_next = (
                block == (generation_high[generation_index] + 1)
                % BLOCK_MODULUS
            )
            if not in_order_next:
                for earlier in range(generation_index - 1, -1, -1):
                    if block in generations[earlier]:
                        record = generations[earlier][block]
                        break

        if record is None:
            record = _classify_block(
                generations, generation_index, observation, handshake)
            analysis.blocks.append(record)
            if block > generation_high[generation_index]:
                generation_high[generation_index] = block
            if record.classification == CLASS_UNIQUE:
                analysis.unique_blocks += 1
                analysis.unique_payload_bytes += record.size
            else:
                analysis.undeterminable_packets += 1
                analysis.undeterminable_bytes += record.size
        else:
            # Every later arrival of an on-record block is a definite
            # retransmit, even when the block's true first delivery was
            # missed: the earlier observed arrival already accounted it.
            record.arrivals.append(observation.time)
            analysis.retransmit_packets += 1
            analysis.retransmit_bytes += observation.data_bytes

    # Distinct payload whose delivery time is known (exactly or bracketed).
    delivered_bytes = (
        analysis.unique_payload_bytes + analysis.undeterminable_bytes)
    _compute_rate(analysis, delivered_bytes)
    analysis.verify_accounting()
    return analysis


def _format_rate(rate):
    if rate is None:
        return "unbounded"
    return "%.2f bps" % rate


def render_report(analysis):
    """Render a human-readable daily-report section from an analysis."""
    lines = [
        "TFTP observation analysis",
        "  observed DAT bytes:      %d" % analysis.observed_dat_bytes,
        "  unique payload bytes:    %d" % analysis.unique_payload_bytes,
        "  retransmit bytes:        %d" % analysis.retransmit_bytes,
        "  undeterminable bytes:    %d" % analysis.undeterminable_bytes,
        "  DAT packets:             %d" % analysis.observed_dat_packets,
        "  unique blocks:           %d" % analysis.unique_blocks,
        "  retransmit packets:      %d" % analysis.retransmit_packets,
        "  undeterminable packets:  %d"
        % analysis.undeterminable_packets,
        "  control packets:         %d" % analysis.control_packets,
        "  handshake observed:      %s" % analysis.handshake_observed,
    ]

    rate = analysis.rate
    if rate.exact:
        lines.append("  effective rate:          %s"
                     % _format_rate(rate.upper_bps))
    elif rate.lower_bps is not None or rate.upper_bps is not None:
        lines.append("  effective rate bounds:   %s .. %s"
                     % (_format_rate(rate.lower_bps),
                        _format_rate(rate.upper_bps)))
    if analysis.duration_note:
        lines.append("  note:                    %s" % analysis.duration_note)

    lines.append("  block deliveries:")
    for record in analysis.blocks:
        if isinstance(record.first_delivery, TimeInterval):
            interval = record.first_delivery
            timing = "first delivery in (%s, %s]" % (
                interval.lower, interval.upper)
        else:
            timing = "first delivery at %s" % record.first_delivery
        lines.append(
            "    block %d gen %d: %d bytes, %s, arrivals=%d [%s]"
            % (
                record.block,
                record.generation,
                record.size,
                record.classification,
                len(record.arrivals),
                timing,
            )
        )

    lines.append(
        "  reconciliation: %d + %d + %d = %d observed DAT bytes"
        % (
            analysis.unique_payload_bytes,
            analysis.retransmit_bytes,
            analysis.undeterminable_bytes,
            analysis.observed_dat_bytes,
        )
    )
    return "\n".join(lines)


def analysis_to_json(analysis):
    """Serialize an analysis to a JSON string."""
    return json.dumps(analysis.to_dict(), indent=2, sort_keys=True)
