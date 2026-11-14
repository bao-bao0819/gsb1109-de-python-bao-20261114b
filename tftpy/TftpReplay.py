# vim: ts=4 sw=4 et ai:
"""Offline, deterministic event replay for TFTP option negotiation and transfer
teardown.

This module never opens a network socket. Instead it:

* feeds scripted datagrams through tftpy's real context/state objects via a
  fake socket (FakeSock), exercising the production code paths;
* maintains two *independent* completion verdicts straight from the observed
  event streams:

  - ReceiverCompletionVerdict is driven by the DAT (data) sequence. The
    receiver only holds the complete byte stream once it sees end-of-file
    evidence: a short block, or the zero-length tail block that is required
    when a file is an exact multiple of the block size.
  - SenderCompletionVerdict is driven by the ACK sequence. The sender only has
    a final acknowledgement once the final DAT has been sent *and* the ACK
    matching that block arrives. A lost final ACK and the resulting
    retransmissions never complete the transfer on their own.

The effective block size used for short-block decisions follows the
negotiation: an accepted OACK updates it, while a rejected proposal (the peer
starts transmitting data instead of returning an OACK) resets it to the
default block size. A rejected proposal must not keep being used to judge
short blocks.

Both verdicts only flip their ``complete`` flag when the concrete evidence
for that side has been observed, so replaying the same event transcript always
produces the same result.
"""

import socket
from io import BytesIO

from .TftpContexts import TftpContextClientDownload, TftpContextServer
from .TftpPacketFactory import TftpPacketFactory
from .TftpPacketTypes import *
from .TftpShared import *

log = logging.getLogger("tftpy.TftpReplay")


class _Timeout:
    """Sentinel marking a FakeSock recvfrom that simulates a dropped packet."""


# Singleton timeout marker used in replay scripts.
TIMEOUT = _Timeout()


class _BlocksizeTracker:
    """Tracks the block size actually in effect for a session.

    The proposed block size is only used after an OACK accepts it. If the
    proposal is rejected, transmission proceeds with the default block size.
    """

    def __init__(self, blksize=DEF_BLKSIZE):
        self.effective_blksize = int(blksize)
        self.proposed_blksize = None
        self.negotiated = False

    def propose_blksize(self, blksize):
        """Record a blksize offered in an RRQ/WRQ, not yet accepted."""
        self.proposed_blksize = int(blksize)

    def oack_accepted(self, options):
        """Apply the options returned in an accepted OACK."""
        if options and "blksize" in options:
            self.effective_blksize = int(options["blksize"])
            self.negotiated = True
            self.proposed_blksize = None

    def negotiation_rejected(self):
        """The peer ignored the proposal and started transmitting with the
        default block size. Short-block judgements must use that default."""
        self.effective_blksize = DEF_BLKSIZE
        self.negotiated = False
        self.proposed_blksize = None


class ReceiverCompletionVerdict(_BlocksizeTracker):
    """Decides, from the DAT sequence alone, whether the receiver has the
    complete byte stream.

    Completion is evidence-gated: it flips to True only when an in-sequence
    DAT carries fewer bytes than the effective block size (a short block, or
    the zero-length tail block for an exact-multiple file).
    """

    def __init__(self, blksize=DEF_BLKSIZE):
        super().__init__(blksize)
        self.next_block = 1
        self.complete = False
        # (blocknumber, data length, outcome) for every observed DAT.
        self.evidence = []

    def dat_received(self, blocknumber, data):
        """Feed one observed DAT. Returns the outcome without raising for
        duplicate or out-of-order blocks."""
        size = len(data)
        if self.complete:
            return "already-complete"
        if blocknumber < self.next_block:
            return "duplicate"
        if blocknumber != self.next_block:
            return "future"
        self.next_block += 1
        if size < self.effective_blksize:
            self.complete = True
            reason = "zero-tail" if size == 0 else "short-block"
            self.evidence.append((blocknumber, size, reason))
            return "eof"
        self.evidence.append((blocknumber, size, "full"))
        return "full"


class SenderCompletionVerdict(_BlocksizeTracker):
    """Decides, from the ACK sequence plus the sender's own DATs, whether the
    sender has obtained the final acknowledgement.

    Completion is evidence-gated: the final DAT is remembered when it is sent,
    but the verdict only flips to True once an ACK for that exact block
    arrives. Retransmissions of the final DAT and stale ACKs do not complete
    the transfer.
    """

    def __init__(self, blksize=DEF_BLKSIZE):
        super().__init__(blksize)
        self.next_block = 1
        self.final_block = None
        self.complete = False
        # (blocknumber, data length, outcome) for sends and the final ACK.
        self.evidence = []

    def dat_sent(self, blocknumber, data):
        """Feed one DAT the sender transmitted, including retransmissions."""
        size = len(data)
        if size < self.effective_blksize:
            if self.final_block is None:
                self.final_block = blocknumber
                self.evidence.append((blocknumber, size, "final-dat-sent"))
            else:
                self.evidence.append((blocknumber, size, "final-dat-resent"))
            return "final"
        self.evidence.append((blocknumber, size, "dat-sent"))
        return "full"

    def ack_received(self, blocknumber):
        """Feed one observed ACK."""
        if self.complete:
            return "already-complete"
        if self.final_block is None:
            return "ack"
        if blocknumber == self.final_block:
            self.complete = True
            self.evidence.append((blocknumber, 0, "final-ack"))
            return "final-ack"
        if blocknumber < self.final_block:
            return "duplicate-ack"
        return "future-ack"


class FakeSock:
    """A socket replacement recording outbound datagrams and replaying a
    scripted inbound stream. No real network IO happens."""

    def __init__(self, incoming=()):
        # Each item is either a raw datagram or the TIMEOUT sentinel.
        self._incoming = list(incoming)
        # Recorded (buffer, address) pairs for everything sent.
        self.sent = []
        self.closed = False
        self.timeout = None

    def bind(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, buffer, address):
        self.sent.append((bytes(buffer), address))

    def recvfrom(self, buffersize):
        if not self._incoming:
            raise socket.timeout("no more scripted packets")
        item = self._incoming.pop(0)
        if isinstance(item, _Timeout):
            raise socket.timeout("simulated packet loss")
        return item, ("127.0.0.1", 6969)

    def close(self):
        self.closed = True

    def fileno(self):
        return -1


def parse_wire(buffer):
    """Decode a raw datagram back into a tftpy packet object."""
    return TftpPacketFactory().parse(buffer)


def sent_packets(fake):
    """Decode every datagram captured by a FakeSock, in transmission order."""
    return [parse_wire(buffer) for buffer, _address in fake.sent]


def new_server_context(root, retries=DEF_TIMEOUT_RETRIES):
    """Build an offline TftpContextServer with its socket replaced by a
    FakeSock. The real socket created by the constructor is closed."""
    context = TftpContextServer(
        "127.0.0.1", 6969, timeout=5, root=root, retries=retries
    )
    fake = FakeSock()
    context.sock.close()
    context.sock = fake
    return context, fake


def start_rrq(context, filename, options):
    """Replay an initial RRQ through a server context."""
    rrq = TftpPacketRRQ()
    rrq.filename = filename
    rrq.mode = "octet"
    rrq.options = dict(options)
    context.start(rrq.encode().buffer)


def start_wrq(context, filename, options):
    """Replay an initial WRQ through a server context."""
    wrq = TftpPacketWRQ()
    wrq.filename = filename
    wrq.mode = "octet"
    wrq.options = dict(options)
    context.start(wrq.encode().buffer)


def drive_ack(context, blocknumber):
    """Deliver an ACK to a server context, mirroring what cycle() does after
    recvfrom(). The state transition may send the next DAT into the FakeSock."""
    ack = TftpPacketACK()
    ack.blocknumber = blocknumber
    context.state = context.state.handle(ack, "127.0.0.1", 6969)


def drive_dat(context, blocknumber, data):
    """Deliver a DAT to a server context (the receiving side of an upload).
    Returns the new state, which is None once end-of-file has been seen."""
    dat = TftpPacketDAT()
    dat.blocknumber = blocknumber
    dat.data = data
    context.state = context.state.handle(dat, "127.0.0.1", 6969)
    return context.state


def drive_timeout(context):
    """Simulate one timeout on a server context. Mirrors TftpServer.listen:
    retry_count is incremented and, while retries remain, the last packet is
    retransmitted. Returns the new retry_count."""
    context.retry_count += 1
    if context.retry_count >= context.retries:
        raise TftpTimeout("replay exhausted retries")
    context.state.resendLast()
    return context.retry_count


def replay_client_download(incoming, options, output=None):
    """Run a real TftpContextClientDownload offline.

    ``incoming`` is the scripted server traffic (raw datagrams and/or TIMEOUT
    markers) replayed through a FakeSock. Returns the context, fake socket and
    the BytesIO the transfer wrote to.
    """
    if output is None:
        output = BytesIO()
    context = TftpContextClientDownload(
        "127.0.0.1",
        6969,
        "replay.bin",
        output,
        dict(options),
        None,
        timeout=1,
    )
    fake = FakeSock(incoming)
    context.sock.close()
    context.sock = fake
    context.start()
    return context, fake, output
