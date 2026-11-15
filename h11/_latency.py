# Bounds on request/response latency, given batched observations of the wire.
#
# A gateway that polls every few tens of milliseconds cannot say exactly when
# an individual byte arrived.  All it can say is that every byte in one
# observation batch arrived somewhere inside that batch's time window.  This
# module turns those windows into provable bounds for the time between the end
# of a request and the start of its final response, using h11 to find the
# relevant byte positions.
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ._connection import Connection, NEED_DATA, PAUSED
from ._events import (
    Data,
    EndOfMessage,
    InformationalResponse,
    Request,
    Response,
)
from ._state import CLIENT, SERVER
from ._util import ProtocolError

__all__ = [
    "ByteChunk",
    "ObservationWindow",
    "Boundary",
    "LatencyRange",
    "InterimResponse",
    "RequestLatency",
    "LatencyAnalyzer",
    "analyze_latency",
]

REQUEST = "request"
RESPONSE = "response"

# Direction spellings accepted by the analyzer.  Gateways tend to use either
# the wire direction or the side of the conversation, so accept both.
_REQUEST_DIRECTIONS = frozenset(("request", "requests", "send", "sent", "client"))
_RESPONSE_DIRECTIONS = frozenset(
    ("response", "responses", "recv", "received", "server")
)

# A chunk can be supplied as a ByteChunk, a (data, start, end) triple, or a
# dict with the same information.  Being liberal here makes it easy to feed
# data straight from a gateway's JSON/CSV logs.
ChunkLike = Union["ByteChunk", Sequence[Any], Dict[str, Any]]


@dataclass(frozen=True)
class ObservationWindow:
    """The interval of time during which a batch of bytes was observed.

    ``start`` and ``end`` must be on the same clock (typically a monotonic
    clock) and ``start <= end``.  A byte in this batch could have arrived at
    any point in the interval; the interval says nothing about where inside the
    batch it was.
    """

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(
                "observation window end ({!r}) is before its start ({!r})".format(
                    self.end, self.start
                )
            )

    @property
    def lower(self) -> float:
        return self.start

    @property
    def upper(self) -> float:
        return self.end

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, init=False)
class ByteChunk:
    """A batch of bytes together with the window when it was observed.

    ``start``/``end`` are accepted both positionally and as keywords.  The
    aliases ``window_start``/``window_end`` are provided for callers that name
    the bound explicitly.
    """

    data: bytes
    start: float
    end: float

    def __init__(
        self,
        data: Union[bytes, bytearray, memoryview],
        start: Optional[float] = None,
        end: Optional[float] = None,
        *,
        window: Optional[ObservationWindow] = None,
        window_start: Optional[float] = None,
        window_end: Optional[float] = None,
    ) -> None:
        if window is not None:
            if start is None:
                start = window.start
            elif start != window.start:
                raise TypeError("both start and window were supplied")
            if end is None:
                end = window.end
            elif end != window.end:
                raise TypeError("both end and window were supplied")
        if start is None:
            start = window_start
        elif window_start is not None and start != window_start:
            raise TypeError("both start and window_start were supplied")
        if end is None:
            end = window_end
        elif window_end is not None and end != window_end:
            raise TypeError("both end and window_end were supplied")
        if start is None or end is None:
            raise TypeError("ByteChunk requires a start and an end time")
        if end < start:
            raise ValueError(
                "chunk end ({!r}) is before its start ({!r})".format(end, start)
            )
        object.__setattr__(self, "data", bytes(data))
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def window_start(self) -> float:
        return self.start

    @property
    def window_end(self) -> float:
        return self.end

    @property
    def window(self) -> ObservationWindow:
        return ObservationWindow(self.start, self.end)

    @classmethod
    def observed_at(
        cls,
        data: Union[bytes, bytearray, memoryview],
        observed_at: float,
        previous_observed_at: Optional[float] = None,
        *,
        collection_start: Optional[float] = None,
    ) -> "ByteChunk":
        """Build a chunk from callback times.

        The callback time is an upper bound on arrival, never a lower one.
        For every chunk after the first, the window starts at the previous
        callback.  The first chunk has no previous callback, so its start must
        be supplied explicitly via ``collection_start``; using the callback
        time as the start would make the lower latency bound unprovable.
        """
        if previous_observed_at is None:
            if collection_start is None:
                raise ValueError(
                    "the first chunk needs collection_start; its callback time "
                    "is an upper bound, not the start of its window"
                )
            start = collection_start
        else:
            start = previous_observed_at
        return cls(data, start, observed_at)

    def __len__(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class Boundary:
    """One end of a latency interval, located in the observed byte stream.

    ``offset`` is the index of the relevant byte in that direction's
    concatenated stream.  For a request end this is the last byte of the
    request (including its body/terminating chunk); for a response it is the
    first byte of the status line.  ``chunk_index`` says which observation
    batch contained that byte, and ``window`` is that batch's time window.

    If ``observed`` is false then the byte was never seen; ``reason`` explains
    which boundary is missing and why.
    """

    side: str
    observed: bool
    offset: Optional[int] = None
    chunk_index: Optional[int] = None
    window: Optional[ObservationWindow] = None
    reason: Optional[str] = None

    @classmethod
    def missing(cls, side: str, reason: str) -> "Boundary":
        return cls(side=side, observed=False, reason=reason)

    def explain(self) -> str:
        if not self.observed:
            return "{} was not observed ({})".format(self.side, self.reason)
        assert self.window is not None and self.chunk_index is not None
        return "{} at byte {} in chunk {} during [{!r}, {!r}]".format(
            self.side, self.offset, self.chunk_index, self.window.start, self.window.end
        )


@dataclass(frozen=True)
class LatencyRange:
    """A provable interval for a latency, or an unbounded/missing result."""

    lower: Optional[float]
    upper: Optional[float]

    @property
    def observed(self) -> bool:
        return self.lower is not None and self.upper is not None

    @property
    def min(self) -> Optional[float]:
        return self.lower

    @property
    def max(self) -> Optional[float]:
        return self.upper

    @property
    def width(self) -> Optional[float]:
        if self.lower is None or self.upper is None:
            return None
        return self.upper - self.lower

    def __contains__(self, value: float) -> bool:
        if self.lower is None or self.upper is None:
            return False
        return self.lower <= value <= self.upper

    def __str__(self) -> str:
        if self.lower is None or self.upper is None:
            return "unobserved"
        return "[{!r}, {!r}]".format(self.lower, self.upper)


@dataclass(frozen=True)
class InterimResponse:
    """The first byte of a 1xx response, kept separate from the final one."""

    status_code: int
    first_byte: Boundary
    # Request-end -> this 1xx first byte.  None when the request end was not
    # observed.  This is deliberately separate from RequestLatency.latency,
    # which always describes the final (non-1xx) response.
    latency_after_request_end: Optional[LatencyRange] = None


@dataclass(frozen=True)
class RequestLatency:
    """Latency information for one request/response exchange."""

    index: int
    request: Optional[Request]
    request_end: Boundary
    response_start: Boundary
    latency: LatencyRange
    interim_responses: Tuple[InterimResponse, ...] = ()
    inconsistent: bool = False
    final_status_code: Optional[int] = None

    @property
    def final_response_start(self) -> Boundary:
        return self.response_start

    @property
    def request_end_observed(self) -> bool:
        return self.request_end.observed

    @property
    def response_start_observed(self) -> bool:
        return self.response_start.observed

    @property
    def missing_sides(self) -> Tuple[str, ...]:
        """The boundaries that were not observed, e.g. ``("request_end",)``."""
        missing = []
        if not self.request_end.observed:
            missing.append("request_end")
        if not self.response_start.observed:
            missing.append("response_start")
        return tuple(missing)

    @property
    def missing_boundary(self) -> Optional[str]:
        """A human-readable summary of the missing side(s), or ``None``."""
        missing = self.missing_sides
        if not missing:
            return None
        return ", ".join(missing)

    def explain(self) -> str:
        if self.missing_sides:
            details = []
            if not self.request_end.observed:
                details.append(self.request_end.explain())
            if not self.response_start.observed:
                details.append(self.response_start.explain())
            return "request {}: latency unobserved; {}".format(
                self.index, "; ".join(details)
            )
        text = "request {}: latency in {}".format(self.index, self.latency)
        text += " (request_end {}, final response start {})".format(
            self.request_end.window, self.response_start.window
        )
        if self.inconsistent:
            text += "; observation windows are inconsistent with causality"
        if self.interim_responses:
            text += "; {} interim response(s) listed separately".format(
                len(self.interim_responses)
            )
        return text

    def as_dict(self) -> Dict[str, Any]:
        def boundary_dict(boundary: Boundary) -> Dict[str, Any]:
            result: Dict[str, Any] = {
                "observed": boundary.observed,
                "offset": boundary.offset,
                "chunk_index": boundary.chunk_index,
                "reason": boundary.reason,
            }
            if boundary.window is not None:
                result["window_start"] = boundary.window.start
                result["window_end"] = boundary.window.end
            return result

        return {
            "index": self.index,
            "method": None if self.request is None else self.request.method.decode(
                "ascii", "replace"
            ),
            "target": None if self.request is None else self.request.target.decode(
                "ascii", "replace"
            ),
            "final_status_code": self.final_status_code,
            "request_end": boundary_dict(self.request_end),
            "response_start": boundary_dict(self.response_start),
            "latency": {
                "lower": self.latency.lower,
                "upper": self.latency.upper,
            },
            "interim_responses": [
                {
                    "status_code": interim.status_code,
                    "first_byte": boundary_dict(interim.first_byte),
                    "latency": None
                    if interim.latency_after_request_end is None
                    else {
                        "lower": interim.latency_after_request_end.lower,
                        "upper": interim.latency_after_request_end.upper,
                    },
                }
                for interim in self.interim_responses
            ],
            "missing_sides": list(self.missing_sides),
            "inconsistent": self.inconsistent,
        }


@dataclass
class _RequestRecord:
    request: Optional[Request]
    start: int
    end: Optional[int]  # exclusive offset after the request's EndOfMessage
    complete: bool


@dataclass
class _ResponseRecord:
    final_start: Optional[int]
    final_status: Optional[int]
    interim_starts: List[Tuple[int, int]]  # (status_code, byte offset)
    end: Optional[int]  # exclusive offset after the final EndOfMessage
    complete: bool


class _ChunkIndex:
    def __init__(self, chunks: Sequence[ByteChunk]) -> None:
        self.chunks = list(chunks)
        self.offsets: List[Tuple[int, int]] = []
        self._starts: List[int] = []
        position = 0
        for chunk in self.chunks:
            next_position = position + len(chunk.data)
            self.offsets.append((position, next_position))
            self._starts.append(position)
            position = next_position
        self.total = position

    def data(self) -> bytes:
        return b"".join(chunk.data for chunk in self.chunks)

    def locate(self, offset: int) -> Optional[Tuple[int, ObservationWindow]]:
        if not 0 <= offset < self.total:
            return None
        # Find the last chunk whose start is <= offset.  Zero-length chunks
        # are skipped by advancing past any empty ranges.
        index = bisect_right(self._starts, offset) - 1
        while index >= 0:
            start, end = self.offsets[index]
            if start <= offset < end:
                return index, self.chunks[index].window
            index -= 1
        return None


def _coerce_chunk(chunk: ChunkLike) -> ByteChunk:
    if isinstance(chunk, ByteChunk):
        return chunk
    if isinstance(chunk, dict):
        data = chunk.get("data")
        if data is None:
            data = chunk.get("bytes")
        start = chunk.get("start", chunk.get("window_start"))
        end = chunk.get("end", chunk.get("window_end"))
        if data is None or start is None or end is None:
            raise TypeError("chunk dict needs data/start/end")
        return ByteChunk(data, start, end)
    if isinstance(chunk, (tuple, list)):
        if len(chunk) < 3:
            raise TypeError("chunk sequence needs at least (data, start, end)")
        return ByteChunk(chunk[0], chunk[1], chunk[2])
    raise TypeError("unsupported chunk type: {!r}".format(type(chunk).__name__))


def _parse_requests(stream: bytes) -> List[_RequestRecord]:
    records: List[_RequestRecord] = []
    base = 0
    total = len(stream)
    while base < total:
        conn = Connection(SERVER)
        conn.receive_data(stream[base:])
        cursor = base
        request: Optional[Request] = None
        eom_end: Optional[int] = None
        complete = False
        try:
            while True:
                before = len(conn._receive_buffer)
                event = conn.next_event()
                after = len(conn._receive_buffer)
                consumed = before - after
                event_end = cursor + consumed
                cursor = event_end
                if isinstance(event, Request):
                    request = event
                if isinstance(event, EndOfMessage):
                    eom_end = event_end
                    complete = True
                    break
                if event is NEED_DATA or event is PAUSED:
                    break
        except ProtocolError:
            # Keep whatever request line/headers we managed to see.  Its end
            # is simply not observable.
            pass

        if request is None:
            # There is no request in this tail of the stream.  We cannot
            # invent one, and further parsing would just repeat this failure.
            break
        records.append(
            _RequestRecord(
                request=request,
                start=base,
                end=eom_end,
                complete=complete,
            )
        )
        if not complete or eom_end is None or eom_end <= base:
            # A truncated request: we cannot know where the next one starts.
            break
        base = eom_end
    return records


def _replay_request(conn: Connection, request: Request) -> None:
    # The response reader only needs the request's method (for HEAD/CONNECT
    # body framing) and the connection state.  Sending the request object
    # gives it both without needing the request body.
    try:
        conn.send(request)
    except ProtocolError:
        # If the request itself cannot be replayed, the best we can do is
        # leave the connection in its initial state and parse the response
        # with a default method.
        pass


def _parse_response(
    stream: bytes, base: int, request: Optional[Request]
) -> _ResponseRecord:
    conn = Connection(CLIENT)
    if request is not None:
        _replay_request(conn, request)
    remaining = stream[base:]
    conn.receive_data(remaining)
    conn.receive_data(b"")  # allows HTTP/1.0-style responses to terminate

    cursor = base
    final_start: Optional[int] = None
    final_status: Optional[int] = None
    interim_starts: List[Tuple[int, int]] = []
    switch_start: Optional[int] = None
    end: Optional[int] = None
    complete = False
    try:
        while True:
            before = len(conn._receive_buffer)
            event = conn.next_event()
            after = len(conn._receive_buffer)
            consumed = before - after
            event_start = cursor
            event_end = cursor + consumed
            cursor = event_end

            if isinstance(event, InformationalResponse):
                if event.status_code == 101:
                    # 101 is the last HTTP/1.1 response before the protocol
                    # switch.  It is a terminal response for latency purposes,
                    # not an interim one.
                    switch_start = event_start
                else:
                    interim_starts.append((event.status_code, event_start))
                continue
            if isinstance(event, Response):
                final_start = event_start
                final_status = event.status_code
                continue
            if isinstance(event, EndOfMessage) and final_start is not None:
                end = event_end
                complete = True
                break
            if event is NEED_DATA or event is PAUSED:
                break
            if isinstance(event, Data):
                continue
            # ConnectionClosed, a protocol switch, or anything else: stop.
            if final_start is not None and end is None:
                end = event_end
            break
    except ProtocolError:
        pass

    if final_start is None and switch_start is not None:
        final_start = switch_start
        final_status = 101

    return _ResponseRecord(
        final_start=final_start,
        final_status=final_status,
        interim_starts=interim_starts,
        end=end,
        complete=complete,
    )


def _boundary(
    side: str,
    offset: int,
    index: _ChunkIndex,
    missing_reason: str,
) -> Boundary:
    located = index.locate(offset)
    if located is None:
        return Boundary.missing(side, missing_reason)
    chunk_index, window = located
    return Boundary(
        side=side,
        observed=True,
        offset=offset,
        chunk_index=chunk_index,
        window=window,
    )


def _latency_between(
    request_end: Boundary, response_start: Boundary
) -> Tuple[LatencyRange, bool]:
    if not request_end.observed or not response_start.observed:
        return LatencyRange(None, None), False
    assert request_end.window is not None
    assert response_start.window is not None
    request_min = request_end.window.start
    request_max = request_end.window.end
    response_min = response_start.window.start
    response_max = response_start.window.end

    # The final response cannot causally precede the request end, so the
    # interval is intersected with [0, inf).
    raw_lower = response_min - request_max
    raw_upper = response_max - request_min
    inconsistent = raw_upper < 0
    lower = max(0.0, raw_lower)
    upper = max(0.0, raw_upper)
    return LatencyRange(lower, upper), inconsistent


def _interim_latency_between(
    request_end: Boundary, interim_start: Boundary
) -> Tuple[LatencyRange, bool]:
    # An interim response may legitimately precede the request end: a server
    # can answer "100 Continue" before the client has finished uploading, and
    # early hints can arrive just as early.  Unlike the final response, this
    # interval is therefore not clamped to [0, inf).
    if not request_end.observed or not interim_start.observed:
        return LatencyRange(None, None), False
    assert request_end.window is not None
    assert interim_start.window is not None
    lower = interim_start.window.start - request_end.window.end
    upper = interim_start.window.end - request_end.window.start
    return LatencyRange(lower, upper), upper < 0


class LatencyAnalyzer:
    """Collect request/response byte batches and compute latency bounds.

    Example::

        analyzer = LatencyAnalyzer()
        analyzer.add_request_chunk(ByteChunk(request_bytes, 10.00, 10.05))
        analyzer.add_response_chunk(ByteChunk(response_bytes, 10.06, 10.10))
        [result] = analyzer.analyze()
        print(result.latency)       # LatencyRange(lower=0.01, upper=0.1)

    The two directions have independent observation streams.  Every batch
    must be added in wire order.  Times may be any float on a common clock
    (e.g. ``time.monotonic()``).
    """

    def __init__(self) -> None:
        self._request_chunks: List[ByteChunk] = []
        self._response_chunks: List[ByteChunk] = []

    def add_request_chunk(self, chunk: ChunkLike) -> "LatencyAnalyzer":
        self._request_chunks.append(_coerce_chunk(chunk))
        return self

    def add_response_chunk(self, chunk: ChunkLike) -> "LatencyAnalyzer":
        self._response_chunks.append(_coerce_chunk(chunk))
        return self

    def add_chunk(
        self,
        direction: str,
        chunk: ChunkLike,
    ) -> "LatencyAnalyzer":
        normalized = direction.strip().lower()
        if normalized in _REQUEST_DIRECTIONS:
            return self.add_request_chunk(chunk)
        if normalized in _RESPONSE_DIRECTIONS:
            return self.add_response_chunk(chunk)
        raise ValueError(
            "direction must be 'request' or 'response', not {!r}".format(direction)
        )

    def add(self, direction: str, chunk: ChunkLike) -> "LatencyAnalyzer":
        return self.add_chunk(direction, chunk)

    def record(
        self,
        direction: str,
        data: Union[bytes, bytearray, memoryview],
        start: float,
        end: float,
    ) -> "LatencyAnalyzer":
        """Convenience wrapper around :meth:`add_chunk`."""
        return self.add_chunk(direction, ByteChunk(data, start, end))

    def analyze(self) -> List[RequestLatency]:
        request_index = _ChunkIndex(self._request_chunks)
        response_index = _ChunkIndex(self._response_chunks)
        requests = _parse_requests(request_index.data())
        response_stream = response_index.data()

        results: List[RequestLatency] = []
        response_base = 0
        response_total = response_index.total
        response_observed = True

        for index, record in enumerate(requests):
            if record.complete and record.end is not None and record.end > record.start:
                request_end = _boundary(
                    "request_end",
                    record.end - 1,
                    request_index,
                    "request end byte was not present in any request chunk",
                )
            else:
                request_end = Boundary.missing(
                    "request_end",
                    "request was truncated before its EndOfMessage",
                )

            response: Optional[_ResponseRecord] = None
            if response_observed and response_base < response_total:
                response = _parse_response(
                    response_stream, response_base, record.request
                )
                if response.final_start is not None:
                    response_start = _boundary(
                        "response_start",
                        response.final_start,
                        response_index,
                        "final response start byte was not present in any "
                        "response chunk",
                    )
                else:
                    response_start = Boundary.missing(
                        "response_start",
                        "no final (non-1xx) response was observed",
                    )

                interim: List[InterimResponse] = []
                for status_code, offset in response.interim_starts:
                    interim_start = _boundary(
                        "interim_response_start",
                        offset,
                        response_index,
                        "interim response start byte was not present in any "
                        "response chunk",
                    )
                    interim_latency, _ = _interim_latency_between(
                        request_end, interim_start
                    )
                    interim.append(
                        InterimResponse(
                            status_code=status_code,
                            first_byte=interim_start,
                            latency_after_request_end=interim_latency
                            if interim_latency.observed
                            else None,
                        )
                    )

                latency, inconsistent = _latency_between(request_end, response_start)
                results.append(
                    RequestLatency(
                        index=index,
                        request=record.request,
                        request_end=request_end,
                        response_start=response_start,
                        latency=latency,
                        interim_responses=tuple(interim),
                        inconsistent=inconsistent,
                        final_status_code=response.final_status,
                    )
                )

                # Match the next request to the next response.  If this
                # response was incomplete we cannot safely find that next
                # response's start, so mark the remaining ones unobserved.
                if response.complete and response.end is not None:
                    response_base = response.end
                else:
                    response_observed = False
            else:
                response_start = Boundary.missing(
                    "response_start",
                    "response stream was exhausted before this request's "
                    "final response",
                )
                latency = LatencyRange(None, None)
                results.append(
                    RequestLatency(
                        index=index,
                        request=record.request,
                        request_end=request_end,
                        response_start=response_start,
                        latency=latency,
                    )
                )

        return results

    def calculate(self) -> List[RequestLatency]:
        return self.analyze()


def _unified_chunks(chunks: Iterable[Any]) -> Tuple[List[ByteChunk], List[ByteChunk]]:
    request_chunks: List[ByteChunk] = []
    response_chunks: List[ByteChunk] = []
    for item in chunks:
        if isinstance(item, (tuple, list)) and len(item) == 4:
            direction = str(item[0])
            chunk = _coerce_chunk(item[1:])
        elif isinstance(item, dict) and "direction" in item:
            direction = str(item["direction"])
            payload = {key: value for key, value in item.items() if key != "direction"}
            chunk = _coerce_chunk(payload)
        else:
            raise TypeError(
                "unified observations must be (direction, data, start, end) "
                "or a dict with a direction"
            )
        if direction.lower() in _REQUEST_DIRECTIONS:
            request_chunks.append(chunk)
        elif direction.lower() in _RESPONSE_DIRECTIONS:
            response_chunks.append(chunk)
        else:
            raise ValueError("unknown direction: {!r}".format(direction))
    return request_chunks, response_chunks


def analyze_latency(
    request_chunks: Iterable[ChunkLike],
    response_chunks: Optional[Iterable[ChunkLike]] = None,
) -> List[RequestLatency]:
    """Compute latency bounds for every parsed request.

    ``request_chunks`` and ``response_chunks`` are iterables of
    :class:`ByteChunk` objects, ``(data, start, end)`` triples, or dicts with
    those keys.  As a convenience, if ``response_chunks`` is omitted then
    ``request_chunks`` may instead be a single interleaved iterable of
    ``(direction, data, start, end)`` items.
    """
    analyzer = LatencyAnalyzer()
    if response_chunks is None:
        request_items, response_items = _unified_chunks(request_chunks)
        for request_item in request_items:
            analyzer.add_request_chunk(request_item)
        for response_item in response_items:
            analyzer.add_response_chunk(response_item)
    else:
        for chunk in request_chunks:
            analyzer.add_request_chunk(chunk)
        for chunk in response_chunks:
            analyzer.add_response_chunk(chunk)
    return analyzer.analyze()


# Friendly aliases.  The main names above are the documented API; these make
# the module a little easier to discover and to adapt to existing call sites.
LatencyTracker = LatencyAnalyzer
LatencyCalculator = LatencyAnalyzer
ObservedChunk = ByteChunk
Chunk = ByteChunk
TimeWindow = ObservationWindow
RequestLatencyEstimate = RequestLatency
compute_latency_bounds = analyze_latency
