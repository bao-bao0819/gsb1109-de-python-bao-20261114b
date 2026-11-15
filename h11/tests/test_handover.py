from typing import List, Tuple

import pytest

from .._connection import Connection, NEED_DATA, PAUSED
from .._events import Data, EndOfMessage, InformationalResponse, Request, Response
from .._handover import CLIENT_TO_SERVER, HandoverReplay, SERVER_TO_CLIENT
from .._state import CLIENT, SERVER, SWITCHED_PROTOCOL

REQUEST = (
    b"GET /chat HTTP/1.1\r\n"
    b"Host: example.com\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"\r\n"
)
ACCEPT_101 = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"\r\n"
)
RESPONSE_200 = b"HTTP/1.1 200 OK\r\n" b"Content-Length: 5\r\n" b"\r\n" b"hello"
FIRST_FRAME = b"\x81\x05hello"


def _splits(blob: bytes) -> List[List[bytes]]:
    """Every two-way split, plus three-way splits that cut the blank line."""
    out: List[List[bytes]] = [[blob]]
    for i in range(1, len(blob)):
        out.append([blob[:i], blob[i:]])
    blank_line_start = max(0, len(blob) - 4)
    for i in range(blank_line_start, len(blob)):
        for j in range(i + 1, len(blob)):
            out.append([blob[:i], blob[i:j], blob[j:]])
    return out


def _assert_handover_events(handover: object) -> None:
    assert type(handover.events[0]) is Request  # type: ignore[attr-defined]
    assert type(handover.events[-1]) is EndOfMessage  # type: ignore[attr-defined]


def test_connection_positions_are_absolute() -> None:
    c = Connection(SERVER)
    assert c.received_bytes == 0
    assert c.trailing_data_offset == 0

    c.receive_data(REQUEST[:7])
    assert c.received_bytes == 7
    assert c.trailing_data_offset == 0
    assert c.remainder == REQUEST[:7]

    c.receive_data(REQUEST[7:])
    assert c.received_bytes == len(REQUEST)
    # Parsing has not happened yet, so all supplied bytes are still buffered.
    assert c.trailing_data_offset == 0

    while True:
        event = c.next_event()
        if event is NEED_DATA or event is PAUSED:
            break
    assert c.trailing_data_offset == len(REQUEST)
    assert c.trailing_data == (b"", False)


def test_handover_yields_events_and_absolute_offset() -> None:
    c = Connection(SERVER)
    c.receive_data(REQUEST)
    handover = c.handover()
    _assert_handover_events(handover)
    # The client proposed a switch, but the server has not answered yet, so
    # no bytes belong to the new protocol.
    assert handover.switched is False
    assert handover.data == b""

    # Once the server accepts, later bytes from the client are raw protocol
    # data and the connection hands them over with their original position.
    c.send(InformationalResponse(status_code=101, headers=[("Upgrade", "x")]))
    c.receive_data(FIRST_FRAME)
    handover = c.handover()
    assert handover.switched is True
    assert handover.data == FIRST_FRAME
    assert handover.offset == len(REQUEST)
    assert handover.consumed == len(REQUEST)
    assert handover.received == len(REQUEST) + len(FIRST_FRAME)


def test_confirmed_switch_hands_over_remainder_once() -> None:
    # The peer's 101 arrives back-to-back with its first frames.
    c2 = Connection(CLIENT)
    c2.send(
        Request(method="GET", target="/", headers=[("Host", "x"), ("Upgrade", "x")])
    )
    c2.send(EndOfMessage())
    c2.receive_data(ACCEPT_101 + FIRST_FRAME)
    handover = c2.handover()
    assert type(handover.events[0]) is InformationalResponse
    assert handover.switched is True
    assert handover.data == FIRST_FRAME
    assert handover.offset == len(ACCEPT_101)
    # Taking the handover empties the buffer: no duplicate on a second call.
    assert c2.trailing_data == (b"", False)
    assert c2.handover().data == b""


@pytest.mark.parametrize("chunks", _splits(ACCEPT_101 + FIRST_FRAME))
def test_replay_101_all_split_points(chunks: List[bytes]) -> None:
    replay = HandoverReplay()
    replay.feed_client_to_server(REQUEST)
    for chunk in chunks:
        replay.feed_server_to_client(chunk)

    handovers = {h.direction: h for h in replay.handovers}
    handover = handovers[SERVER_TO_CLIENT]
    assert handover.data == FIRST_FRAME
    assert handover.offset == len(ACCEPT_101)
    assert handover.consumed == len(ACCEPT_101)
    assert handover.received == len(ACCEPT_101) + len(FIRST_FRAME)
    # The client never hands over anything that is still HTTP request data.
    client_handover = handovers[CLIENT_TO_SERVER]
    assert client_handover.data == b""


@pytest.mark.parametrize("chunks", _splits(RESPONSE_200))
def test_200_after_upgrade_stays_http(chunks: List[bytes]) -> None:
    replay = HandoverReplay()
    replay.feed_client_to_server(REQUEST)
    events = []
    for chunk in chunks:
        events.extend(e.event for e in replay.feed_server_to_client(chunk).events)

    assert type(events[0]) is Response
    assert type(events[-1]) is EndOfMessage
    assert all(type(event) in (Data, EndOfMessage) for event in events[1:])
    assert h11_data(events) == b"hello"
    assert replay.handovers == []
    progress = replay.progress_for(SERVER_TO_CLIENT)
    assert progress.received == len(RESPONSE_200)
    # Nothing was handed over, so all of it was consumed as HTTP.
    assert progress.consumed == len(RESPONSE_200)
    assert progress.buffered == 0


def h11_data(events: List[object]) -> bytes:
    out = b""
    for event in events:
        if type(event) is Data:
            out += event.data  # type: ignore[attr-defined]
    return out


@pytest.mark.parametrize("chunks", _splits(REQUEST + FIRST_FRAME))
def test_server_sees_request_and_remainder_across_splits(chunks: List[bytes]) -> None:
    replay = HandoverReplay()
    for chunk in chunks:
        replay.feed_client_to_server(chunk)
    # Server accepted on its own; mirror the 101 so both sides switch.
    replay.feed_server_to_client(ACCEPT_101)

    client_handover = {h.direction: h for h in replay.handovers}[CLIENT_TO_SERVER]
    # The request event boundaries are reported in absolute stream positions.
    request_start = client_handover.events[0]
    assert getattr(request_start, "method", None) == b"GET"
    assert client_handover.offset == len(REQUEST)


def test_replay_duplex_capture_and_progress() -> None:
    replay = HandoverReplay()
    result = replay.feed_all(
        [
            (CLIENT_TO_SERVER, REQUEST),
            (SERVER_TO_CLIENT, ACCEPT_101 + FIRST_FRAME),
        ]
    )
    assert len(result) == 2
    handovers = {h.direction: h for h in replay.handovers}
    assert handovers[CLIENT_TO_SERVER].data == b""
    assert handovers[SERVER_TO_CLIENT].data == FIRST_FRAME
    c2s, s2c = replay.progress_for("client"), replay.progress_for("server")
    assert c2s.received == c2s.consumed == len(REQUEST)
    assert s2c.received == len(ACCEPT_101) + len(FIRST_FRAME)
    assert s2c.consumed == len(ACCEPT_101)
    assert s2c.buffered == len(FIRST_FRAME)


def test_late_tail_appends_to_same_handover() -> None:
    replay = HandoverReplay()
    replay.feed_client_to_server(REQUEST)
    first = replay.feed_server_to_client(ACCEPT_101)
    second = replay.feed_server_to_client(FIRST_FRAME[:1])
    third = replay.feed_server_to_client(FIRST_FRAME[1:])

    assert {h.direction: h for h in first.handovers}[SERVER_TO_CLIENT].data == b""
    assert {h.direction: h for h in second.handovers}[SERVER_TO_CLIENT].data == (
        FIRST_FRAME[:1]
    )
    assert {h.direction: h for h in third.handovers}[SERVER_TO_CLIENT].data == (
        FIRST_FRAME
    )
    assert len(replay.handovers) == 2
    server_handover = {h.direction: h for h in replay.handovers}[SERVER_TO_CLIENT]
    assert server_handover.data == FIRST_FRAME
    assert server_handover.received == len(ACCEPT_101) + len(FIRST_FRAME)


def test_empty_capture_chunks_are_end_of_stream() -> None:
    replay = HandoverReplay()
    result = replay.feed_client_to_server(b"")
    assert [type(e.event).__name__ for e in result.events] == ["ConnectionClosed"]
    assert replay.closed[CLIENT] is True


@pytest.mark.parametrize("chunks", _splits(REQUEST + FIRST_FRAME))
def test_server_handover_after_accept_arrives(chunks: List[bytes]) -> None:
    # The client's request and its first frames may share a chunk, but the
    # server has not accepted yet. Only after the 101 appears does the
    # buffered remainder become upgraded-protocol data.
    replay = HandoverReplay()
    for chunk in chunks:
        replay.feed_client_to_server(chunk)
    assert replay.handovers == []

    replay.feed_server_to_client(ACCEPT_101)
    handover = {h.direction: h for h in replay.handovers}[CLIENT_TO_SERVER]
    assert handover.switched is True
    assert handover.data == FIRST_FRAME
    assert handover.offset == len(REQUEST)
    assert handover.consumed == len(REQUEST)
    assert handover.received == len(REQUEST) + len(FIRST_FRAME)


def test_denied_upgrade_keeps_reading_http_body() -> None:
    replay = HandoverReplay()
    replay.feed_client_to_server(REQUEST)
    results = [
        replay.feed_server_to_client(chunk)
        for chunk in [RESPONSE_200[:20], RESPONSE_200[20:] + b"extra"]
    ]
    assert replay.handovers == []
    # "extra" remains unparsed HTTP body data for a following cycle; it is not
    # handed over as a new protocol.
    handover = {h.direction: h for h in replay.handovers}
    assert handover == {}
    assert replay.progress_for("server").consumed == len(RESPONSE_200)
    assert replay.progress_for("server").buffered == len(b"extra")


CONNECT_REQUEST = (
    b"CONNECT example.com:443 HTTP/1.1\r\n"
    b"Host: example.com\r\n"
    b"\r\n"
)
CONNECT_RESPONSE = b"HTTP/1.1 200 Connection established\r\n" b"\r\n"


def test_connect_2xx_hands_over_remainder() -> None:
    replay = HandoverReplay()
    replay.feed_client_to_server(CONNECT_REQUEST)
    replay.feed_server_to_client(CONNECT_RESPONSE)
    replay.feed_server_to_client(FIRST_FRAME)
    handover = {h.direction: h for h in replay.handovers}[SERVER_TO_CLIENT]
    assert handover.switched is True
    assert handover.data == FIRST_FRAME
    assert handover.offset == len(CONNECT_RESPONSE)


def test_connect_4xx_stays_http() -> None:
    replay = HandoverReplay()
    replay.feed_client_to_server(CONNECT_REQUEST)
    replay.feed_server_to_client(
        b"HTTP/1.1 403 Forbidden\r\n" b"Content-Length: 0\r\n" b"\r\n"
    )
    assert replay.handovers == []
