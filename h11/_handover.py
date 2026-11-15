# Records describing the handover from HTTP/1.1 to some other protocol.
#
# An offline capture often contains a single TCP segment whose first part is
# still HTTP and whose remainder already belongs to the upgraded protocol.
# h11 parses the complete HTTP events out of the front of the segment and
# leaves the remainder in its receive buffer. These records make that boundary
# explicit: where it is in the original stream, which events were completed,
# and which raw bytes belong to the new protocol.
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type, Union

from ._events import ConnectionClosed, Event
from ._state import CLIENT, SERVER
from ._util import Sentinel

__all__ = [
    "CLIENT_TO_SERVER",
    "SERVER_TO_CLIENT",
    "DirectionProgress",
    "Handover",
    "RecordedEvent",
    "ReplayResult",
    "HandoverReplay",
    "CaptureReplay",
    "replay_capture",
]

# Directions are identified by the role that sent the bytes.
CLIENT_TO_SERVER = CLIENT
SERVER_TO_CLIENT = SERVER


@dataclass(frozen=True)
class DirectionProgress:
    """How far one direction of a connection has been consumed or produced.

    The fields are absolute byte counts from the beginning of the recorded
    stream in that direction. ``received`` and ``sent`` count everything that
    was handed to h11 or emitted by it; ``consumed`` counts the prefix that has
    been turned into complete HTTP events. Any bytes between ``consumed`` and
    ``received`` are still buffered (and available through
    :attr:`Connection.trailing_data`).
    """

    direction: Type[Sentinel]
    received: int = 0
    consumed: int = 0
    buffered: int = 0
    sent: int = 0
    closed: bool = False

    @property
    def received_bytes(self) -> int:
        return self.received

    @property
    def consumed_bytes(self) -> int:
        return self.consumed

    @property
    def buffered_bytes(self) -> int:
        return self.buffered

    @property
    def sent_bytes(self) -> int:
        return self.sent

    @property
    def produced(self) -> int:
        return self.sent

    @property
    def offset(self) -> int:
        """Alias for :attr:`consumed`: the position of the first buffered byte."""
        return self.consumed

    @property
    def position(self) -> int:
        """Alias for :attr:`consumed`."""
        return self.consumed

    @property
    def received_offset(self) -> int:
        """Alias for :attr:`received`: just past the last received byte."""
        return self.received

    @property
    def remaining(self) -> int:
        return self.buffered

    @property
    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.received, self.consumed, self.buffered, self.sent)


@dataclass
class Handover:
    """The result of draining HTTP events up to a protocol handover.

    The object is a snapshot. ``events`` contains every complete HTTP event
    that h11 produced while draining the receive buffer. ``data`` contains the
    bytes immediately after those events, but only when the protocol switch
    has actually been confirmed; if the peer responded to an upgrade proposal
    with an ordinary response (for example ``200 OK``), ``switched`` is false
    and ``data`` is empty so that the caller keeps reading HTTP.

    ``offset`` is the absolute position of ``data[0]`` in the original stream
    for this direction (equivalently, the number of bytes consumed as HTTP).
    ``received`` is the absolute position just past the last byte handed to
    :meth:`Connection.receive_data`. A direction that was not switched still
    reports the same positions, with empty ``data``. ``buffered`` is the number
    of bytes that have been received but not yet turned into an HTTP event.

    The bytes in ``data`` have been removed from the connection's receive
    buffer. Taking a handover is therefore a one-shot operation: the same
    upgraded bytes cannot accidentally be delivered twice. If more raw bytes
    for the same direction arrive later, they are appended to the same
    handover's ``data`` (and ``received`` is advanced), which keeps a single
    boundary record per direction.
    """

    direction: Optional[Type[Sentinel]]
    events: List[Event] = field(default_factory=list)
    data: bytes = b""
    offset: int = 0
    consumed: int = 0
    received: int = 0
    buffered: int = 0
    closed: bool = False
    switched: bool = False

    @property
    def http_events(self) -> List[Event]:
        return self.events

    @property
    def trailing_data(self) -> bytes:
        return self.data

    @property
    def remainder(self) -> bytes:
        return self.data

    @property
    def remaining_data(self) -> bytes:
        return self.data

    @property
    def raw_data(self) -> bytes:
        return self.data

    @property
    def first_byte(self) -> bytes:
        return self.data[:1]

    @property
    def first_bytes(self) -> bytes:
        return self.data

    @property
    def stream_offset(self) -> int:
        return self.offset

    @property
    def position(self) -> int:
        return self.offset

    @property
    def remainder_offset(self) -> int:
        return self.offset

    @property
    def consumed_offset(self) -> int:
        return self.consumed

    @property
    def consumed_until(self) -> int:
        return self.consumed

    @property
    def received_bytes(self) -> int:
        return self.received

    @property
    def received_offset(self) -> int:
        return self.received

    @property
    def switched_protocol(self) -> bool:
        return self.switched

    @property
    def has_remainder(self) -> bool:
        return bool(self.data)

    @property
    def progress(self) -> DirectionProgress:
        direction = self.direction
        if direction is None:
            direction = CLIENT
        return DirectionProgress(
            direction=direction,
            received=self.received,
            consumed=self.consumed,
            buffered=self.buffered,
            closed=self.closed,
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "direction": self.direction,
            "events": list(self.events),
            "data": self.data,
            "offset": self.offset,
            "consumed": self.consumed,
            "received": self.received,
            "buffered": self.buffered,
            "closed": self.closed,
            "switched": self.switched,
        }

    def __bool__(self) -> bool:
        return bool(self.events or self.data or self.closed or self.switched)


@dataclass(frozen=True)
class RecordedEvent:
    """An HTTP event with the direction and stream position it came from.

    ``start`` is the absolute stream position of the first byte of the event;
    ``offset``/``consumed`` is the position just past its last byte. ``received``
    is the position just past everything supplied for that direction so far.
    """

    direction: Type[Sentinel]
    event: Event
    start: int = 0
    offset: int = 0
    received: int = 0

    @property
    def consumed(self) -> int:
        return self.offset

    @property
    def consumed_to(self) -> int:
        return self.offset

    @property
    def start_offset(self) -> int:
        return self.start

    @property
    def end_offset(self) -> int:
        return self.offset


@dataclass
class ReplayResult:
    """Everything produced by one :meth:`HandoverReplay.feed` call."""

    events: List[RecordedEvent] = field(default_factory=list)
    handovers: List[Handover] = field(default_factory=list)
    progress: Dict[Type[Sentinel], DirectionProgress] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.events or self.handovers)


def _normalize_direction(direction: Union[str, Type[Sentinel]]) -> Type[Sentinel]:
    if direction is CLIENT:
        return CLIENT
    if direction is SERVER:
        return SERVER
    if isinstance(direction, str):
        value = direction.lower().replace("-", "_")
        if value in ("client", "client_to_server", "c2s", "request", "requests"):
            return CLIENT
        if value in ("server", "server_to_client", "s2c", "response", "responses"):
            return SERVER
    raise ValueError(f"unknown replay direction: {direction!r}")


class HandoverReplay:
    """Replay an offline duplex capture through two mirrored h11 connections.

    A capture is a series of byte chunks tagged with the role that sent them.
    Each chunk is fed verbatim to the receiving :class:`Connection`; the
    complete HTTP events it produces are mirrored into the sending side's
    state machine so that both ends agree about upgrade proposals and protocol
    switches. This is what makes a ``101`` or a ``2xx`` CONNECT response
    interpretable even when the capture never contains the bytes of the
    request or response that the local application would have sent.

    Chunks may be split anywhere, including in the middle of the blank line
    that ends the HTTP header block, or the whole capture may be fed in one
    call. The first byte after a confirmed switch is reported exactly once,
    either in :attr:`ReplayResult.handovers` or later through
    :meth:`Connection.trailing_data`, never in both.
    """

    def __init__(
        self,
        client: Optional[object] = None,
        server: Optional[object] = None,
        *,
        max_incomplete_event_size: Optional[int] = None,
    ) -> None:
        # Imported lazily to keep _connection -> _handover import-cycle free.
        from ._connection import Connection

        kwargs = {}
        if max_incomplete_event_size is not None:
            kwargs["max_incomplete_event_size"] = max_incomplete_event_size
        self.client = client if client is not None else Connection(CLIENT, **kwargs)
        self.server = server if server is not None else Connection(SERVER, **kwargs)
        self._handovers: List[Handover] = []
        self._handover_by_direction: Dict[Type[Sentinel], Handover] = {}
        self._pending_events: Dict[Type[Sentinel], List[Event]] = {
            CLIENT: [],
            SERVER: [],
        }
        self._fed: Dict[Type[Sentinel], int] = {CLIENT: 0, SERVER: 0}
        self._closed: Dict[Type[Sentinel], bool] = {CLIENT: False, SERVER: False}

    # -- direction / endpoint helpers -------------------------------------

    @staticmethod
    def _receiver(direction: Type[Sentinel]) -> Type[Sentinel]:
        # Bytes sent by the client are received by the server, and vice versa.
        return SERVER if direction is CLIENT else CLIENT

    def _receiving_conn(self, direction: Type[Sentinel]) -> Any:
        # A chunk sent by one role is parsed by the other role's connection.
        return self.server if direction is CLIENT else self.client

    def _mirror_conn(self, direction: Type[Sentinel]) -> Any:
        # The sender's own connection mirrors the events so its state machine
        # knows what it has "sent".
        return self.client if direction is CLIENT else self.server

    # -- replay -----------------------------------------------------------

    def feed(self, direction: Union[str, Type[Sentinel]], data: bytes) -> ReplayResult:
        """Feed one captured chunk and return the events/handovers it caused."""
        direction = _normalize_direction(direction)
        receiving = self._receiving_conn(direction)
        result = ReplayResult()
        if data:
            self._fed[direction] += len(data)
        else:
            self._closed[direction] = True
        receiving.receive_data(data)
        self._drain(receiving, direction, result)
        self._collect_handovers(result)
        result.progress = self.progress
        return result

    def feed_client_to_server(self, data: bytes) -> ReplayResult:
        return self.feed(CLIENT, data)

    def feed_server_to_client(self, data: bytes) -> ReplayResult:
        return self.feed(SERVER, data)

    # Common aliases used by capture readers.
    feed_c2s = feed_client_to_server
    feed_s2c = feed_server_to_client
    feed_request = feed_client_to_server
    feed_response = feed_server_to_client

    def feed_all(
        self, records: Iterable[Union[Tuple[Union[str, Type[Sentinel]], bytes], object]]
    ) -> List[ReplayResult]:
        """Feed an iterable of ``(direction, bytes)`` or record-like objects."""
        results = []
        for record in records:
            if isinstance(record, tuple):
                direction, data = record
            else:
                direction = getattr(record, "direction")
                data = getattr(record, "data", getattr(record, "bytes", b""))
            results.append(self.feed(direction, data))
        return results

    run = feed_all

    def _drain(
        self, conn: object, direction: Type[Sentinel], result: ReplayResult
    ) -> None:
        from ._connection import NEED_DATA, PAUSED

        while True:
            start = conn.trailing_data_offset  # type: ignore[attr-defined]
            event = conn.next_event()  # type: ignore[attr-defined]
            if event is NEED_DATA or event is PAUSED:
                break
            recorded = RecordedEvent(
                direction=direction,
                event=event,
                start=start,
                offset=conn.trailing_data_offset,  # type: ignore[attr-defined]
                received=conn.received_bytes,  # type: ignore[attr-defined]
            )
            result.events.append(recorded)
            if direction not in self._handover_by_direction:
                self._pending_events[direction].append(event)
            # Mirror the event into the sender's state machine.
            mirror = self._mirror_conn(direction)
            mirror._process_event(direction, event)
            # Mirroring may have confirmed the other direction's switch.
            self._collect_handovers(result)
            if type(event) is ConnectionClosed:
                break

    def _collect_handovers(self, result: ReplayResult) -> None:
        for direction, conn in ((CLIENT, self.server), (SERVER, self.client)):
            if not conn.switched_protocol:  # type: ignore[attr-defined]
                continue
            (
                data,
                offset,
                consumed,
                received,
            ) = conn._take_handover_data()  # type: ignore[attr-defined]
            existing = self._handover_by_direction.get(direction)
            if existing is not None:
                if data:
                    replacement = Handover(
                        direction=existing.direction,
                        events=existing.events,
                        data=existing.data + data,
                        offset=existing.offset,
                        consumed=existing.consumed,
                        received=received,
                        buffered=received - existing.consumed,
                        closed=existing.closed
                        or bool(conn.trailing_data[1]),  # type: ignore[attr-defined]
                        switched=True,
                    )
                    self._handover_by_direction[direction] = replacement
                    index = self._handovers.index(existing)
                    self._handovers[index] = replacement
                    result.handovers.append(replacement)
                continue
            handover = Handover(
                direction=direction,
                events=list(self._pending_events[direction]),
                data=data,
                offset=offset,
                consumed=consumed,
                received=received,
                buffered=received - consumed,
                closed=bool(conn.trailing_data[1]),  # type: ignore[attr-defined]
                switched=True,
            )
            self._pending_events[direction].clear()
            self._handover_by_direction[direction] = handover
            self._handovers.append(handover)
            result.handovers.append(handover)

    # -- observations -----------------------------------------------------

    @property
    def handovers(self) -> List[Handover]:
        return list(self._handovers)

    @property
    def progress(self) -> Dict[Type[Sentinel], DirectionProgress]:
        """Per-direction consumption, keyed by the role that sent the bytes."""
        return {
            CLIENT: DirectionProgress(
                direction=CLIENT,
                received=self.server.received_bytes,  # type: ignore[attr-defined]
                consumed=self.server.trailing_data_offset,  # type: ignore[attr-defined]
                buffered=(
                    self.server.received_bytes  # type: ignore[attr-defined]
                    - self.server.trailing_data_offset  # type: ignore[attr-defined]
                ),
                closed=self._closed[CLIENT],
            ),
            SERVER: DirectionProgress(
                direction=SERVER,
                received=self.client.received_bytes,  # type: ignore[attr-defined]
                consumed=self.client.trailing_data_offset,  # type: ignore[attr-defined]
                buffered=(
                    self.client.received_bytes  # type: ignore[attr-defined]
                    - self.client.trailing_data_offset  # type: ignore[attr-defined]
                ),
                closed=self._closed[SERVER],
            ),
        }

    def progress_for(self, direction: Union[str, Type[Sentinel]]) -> DirectionProgress:
        return self.progress[_normalize_direction(direction)]

    @property
    def positions(self) -> Dict[Type[Sentinel], int]:
        """Absolute supplied-byte counts, keyed by sender role."""
        return dict(self._fed)

    def position_for(self, direction: Union[str, Type[Sentinel]]) -> int:
        return self._fed[_normalize_direction(direction)]

    @property
    def consumed(self) -> Dict[Type[Sentinel], int]:
        return {
            CLIENT: self.server.trailing_data_offset,  # type: ignore[attr-defined]
            SERVER: self.client.trailing_data_offset,  # type: ignore[attr-defined]
        }

    @property
    def closed(self) -> Dict[Type[Sentinel], bool]:
        return dict(self._closed)

    @property
    def server_connection(self) -> object:
        """The connection that parses client-to-server bytes."""
        return self.server

    @property
    def client_connection(self) -> object:
        """The connection that parses server-to-client bytes."""
        return self.client


# Descriptive aliases for callers that think in terms of a capture rather than
# a handover.
CaptureReplay = HandoverReplay


def replay_capture(
    records: Iterable[Union[Tuple[Union[str, Type[Sentinel]], bytes], object]],
    **kwargs: object,
) -> HandoverReplay:
    """Replay *records* and return the populated :class:`HandoverReplay`."""
    replay = HandoverReplay(**kwargs)  # type: ignore[arg-type]
    replay.feed_all(records)
    return replay
