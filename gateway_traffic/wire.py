"""Parse one complete HTTP/1.1 transaction with h11.

The functions here deliberately trust h11's framing decisions rather than
Content-Length or Transfer-Encoding metadata.  In particular, ``body_bytes``
is the sum of the lengths of the :class:`h11.Data` events that h11 actually
produced.  A transaction whose wire bytes stop in the middle of a declared
body therefore reports the bytes that were really received, not the declared
length.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import h11


@dataclass
class ParsedTransaction:
    """The parts of a transaction that matter for traffic accounting."""

    request: Optional[h11.Request]
    path: Optional[str]
    final_status: Optional[int]
    informational_statuses: Tuple[int, ...]
    body_bytes: int
    response_wire_bytes: int
    response_complete: bool
    response_saw_end_of_message: bool
    response_protocol_switch: bool
    request_trailing_bytes: bytes = b""
    response_trailing_bytes: bytes = b""
    errors: List[str] = field(default_factory=list)

    @property
    def method(self) -> Optional[bytes]:
        if self.request is None:
            return None
        return self.request.method

    @property
    def status_class(self) -> Optional[str]:
        if self.final_status is None:
            return None
        return f"{self.final_status // 100}xx"


def _drain(conn: h11.Connection, errors: List[str]) -> List[h11.Event]:
    events: List[h11.Event] = []
    while True:
        try:
            event = conn.next_event()
        except h11.ProtocolError as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            break
        if event is h11.NEED_DATA or event is h11.PAUSED:
            break
        events.append(event)
        if isinstance(event, h11.ConnectionClosed):
            break
    return events


def _parse_request(data: bytes, errors: List[str]) -> Tuple[List[h11.Event], bytes]:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(data)
    events = _drain(conn, errors)
    trailing, _ = conn.trailing_data
    return events, bytes(trailing)


def _send_request_events(
    conn: h11.Connection, request_events: List[h11.Event], errors: List[str]
) -> None:
    """Move a client h11.Connection into the response-reading state.

    The event objects came from h11 itself, so re-sending them is only a
    state-machine bookkeeping step; the regenerated request bytes are
    discarded.
    """

    for event in request_events:
        if not isinstance(
            event, (h11.Request, h11.Data, h11.EndOfMessage)
        ):
            continue
        try:
            conn.send(event)
        except h11.ProtocolError as exc:
            errors.append(f"request replay: {type(exc).__name__}: {exc}")
            break


def _request_path(target: bytes) -> str:
    """Extract the request path while preserving percent escapes."""

    text = target.decode("latin-1")
    # absolute-form targets (proxy requests) and origin-form targets both end
    # up with the path in the same place after urlsplit.
    try:
        from urllib.parse import urlsplit

        split = urlsplit(text)
        if split.scheme and split.netloc:
            return split.path or "/"
    except ValueError:
        pass
    if text.startswith("/"):
        return text.split("?", 1)[0]
    # CONNECT authority-form and asterisk-form have no path component.
    return text


def parse_transaction(
    request_wire: bytes, response_wire: bytes, *, signal_eof: bool = True
) -> ParsedTransaction:
    """Parse one request and its response.

    ``request_wire`` must contain exactly one request (headers plus any body)
    and ``response_wire`` must contain the response bytes belonging to that
    transaction.  ``signal_eof`` tells h11 that no more response bytes will
    arrive; this is needed for close-delimited HTTP/1.0-style bodies, and lets
    us distinguish a complete chunked body from a truncated one.
    """

    errors: List[str] = []
    request_events, request_trailing = _parse_request(request_wire, errors)

    request: Optional[h11.Request] = None
    for event in request_events:
        if isinstance(event, h11.Request):
            request = event
            break

    informational_statuses: List[int] = []
    final_status: Optional[int] = None
    body_bytes = 0
    saw_eom = False
    protocol_switch = False

    if request is not None:
        client = h11.Connection(h11.CLIENT)
        _send_request_events(client, request_events, errors)
        client.receive_data(response_wire)
        response_events = _drain(client, errors)
        if signal_eof:
            client.receive_data(b"")
            response_events.extend(_drain(client, errors))

        response_seen = False
        last_informational: Optional[int] = None
        for event in response_events:
            if isinstance(event, h11.InformationalResponse):
                informational_statuses.append(event.status_code)
                last_informational = event.status_code
                response_seen = True
                if event.status_code == 101:
                    protocol_switch = True
            elif isinstance(event, h11.Response):
                final_status = event.status_code
                response_seen = True
            elif isinstance(event, h11.Data):
                body_bytes += len(event.data)
            elif isinstance(event, h11.EndOfMessage):
                saw_eom = True
            elif isinstance(event, h11.ConnectionClosed):
                # A close-delimited response reaches EOF without an explicit
                # EndOfMessage; that is still a complete transaction.
                if response_seen:
                    saw_eom = True

        if final_status is None and protocol_switch and last_informational is not None:
            # h11 classifies 101 as informational, but for an upgraded
            # connection it is the final response status.
            final_status = last_informational

        trailing, _ = client.trailing_data
    else:
        trailing = b""

    return ParsedTransaction(
        request=request,
        path=_request_path(request.target) if request is not None else None,
        final_status=final_status,
        informational_statuses=tuple(informational_statuses),
        body_bytes=body_bytes,
        response_wire_bytes=len(response_wire),
        response_complete=saw_eom,
        response_saw_end_of_message=saw_eom,
        response_protocol_switch=protocol_switch,
        request_trailing_bytes=request_trailing,
        response_trailing_bytes=bytes(trailing),
        errors=errors,
    )
