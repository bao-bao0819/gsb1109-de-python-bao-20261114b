import pytest

from .._latency import (
    Boundary,
    ByteChunk,
    LatencyAnalyzer,
    LatencyRange,
    ObservationWindow,
    analyze_latency,
)


REQ = b"GET /a HTTP/1.1\r\nHost: example.com\r\n\r\n"
REQ2 = b"GET /b HTTP/1.1\r\nHost: example.com\r\n\r\n"


def resp(body: bytes = b"") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


def test_observation_window_validation() -> None:
    assert ObservationWindow(1, 2).duration == 1
    with pytest.raises(ValueError):
        ObservationWindow(2, 1)


def test_byte_chunk_normalization() -> None:
    chunk = ByteChunk(bytearray(b"abc"), 1, 2)
    assert chunk.data == b"abc"
    assert chunk.window == ObservationWindow(1, 2)
    n1 = ByteChunk(b"x", 1, 2)
    n2 = ByteChunk(b"x", 1, 2)
    assert n1 == n2
    assert ByteChunk(b"x", window_start=1, window_end=2) == n1
    with pytest.raises(ValueError):
        ByteChunk(b"x", 2, 1)
    with pytest.raises(TypeError):
        ByteChunk(b"x")


def test_callback_time_is_not_treated_as_first_byte() -> None:
    # The callback merely says "the bytes arrived before now", so a chunk's
    # window begins at the previous callback, not at its own timestamp.
    chunk = ByteChunk.observed_at(b"abc", 10.05, previous_observed_at=10.00)
    assert chunk.window == ObservationWindow(10.00, 10.05)
    # For the very first chunk there is no previous callback, so the caller
    # must supply the start of collection; the callback is only the end.
    first = ByteChunk.observed_at(b"abc", 10.05, collection_start=9.90)
    assert first.window == ObservationWindow(9.90, 10.05)
    with pytest.raises(ValueError):
        ByteChunk.observed_at(b"abc", 10.05)


def test_basic_bounds() -> None:
    [result] = analyze_latency(
        [ByteChunk(REQ, 1.0, 1.1)],
        [ByteChunk(resp(b"hi"), 2.0, 2.2)],
    )
    assert result.request_end.observed
    assert result.response_start.observed
    assert result.request_end.offset == len(REQ) - 1
    # earliest response byte minus latest request byte; latest response byte
    # minus earliest request byte.
    assert result.latency == LatencyRange(2.0 - 1.1, 2.2 - 1.0)
    assert not result.missing_sides
    assert not result.inconsistent


def test_one_buffer_with_previous_tail_does_not_steal_next_start() -> None:
    # The response buffer begins with the *tail* of response 1's body, then
    # contains response 2.  Response 2's first byte must be found by parsing,
    # not by taking the chunk's earliest byte.
    reqs = [ByteChunk(REQ + REQ2, 1.0, 1.2)]
    r1 = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc"
    r2 = b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n"
    # Note: the chunk deliberately starts in the middle of response 1's body.
    responses = [
        ByteChunk(r1[:30], 2.0, 2.1),
        ByteChunk(r1[30:] + r2, 2.2, 2.4),
    ]
    first, second = analyze_latency(reqs, responses)
    assert first.response_start.offset == 0
    assert second.response_start.offset == len(r1)
    # The second response's window is the second chunk, not the first one.
    assert second.response_start.window == ObservationWindow(2.2, 2.4)
    assert second.latency == LatencyRange(2.2 - 1.2, 2.4 - 1.0)


def test_next_response_start_is_not_the_previous_body_tail() -> None:
    # The second response chunk opens in the middle of response 1's body, so
    # its earliest byte is a body tail byte.  Response 2's true start is at
    # offset len(r1) inside that chunk.  The two chunks have very different
    # windows, so mistakenly using the chunk's earliest byte would also
    # produce a visibly wrong *time*.
    r1 = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
    r2 = b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n"
    assert r1.index(b"hello") == 38
    responses = [
        ByteChunk(r1[:40], 2.0, 2.1),
        ByteChunk(r1[40:] + r2, 9.0, 9.1),
    ]
    first, second = analyze_latency([ByteChunk(REQ + REQ2, 1.0, 1.0)], responses)
    assert first.response_start.offset == 0
    assert first.response_start.window == ObservationWindow(2.0, 2.1)
    assert second.response_start.offset == len(r1)
    assert second.response_start.window == ObservationWindow(9.0, 9.1)
    # If the tail byte at offset 40 had been used, response 2's start would
    # still land in the second chunk; the offset check above is the real
    # discriminator, and the latency differs too.
    assert second.latency == LatencyRange(8.0, 8.1)


def test_request_tail_and_next_request_share_a_chunk() -> None:
    body = b"hello"
    first = (
        b"POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n" + body
    )
    reqs = [
        ByteChunk(first[:40], 1.0, 1.1),
        ByteChunk(first[40:] + REQ2, 1.2, 1.3),
    ]
    responses = [ByteChunk(resp() + resp(), 2.0, 2.2)]
    latencies = analyze_latency(reqs, responses)
    assert len(latencies) == 2
    assert latencies[0].request_end.offset == len(first) - 1
    assert latencies[1].request_end.offset == len(first) + len(REQ2) - 1


def test_interim_response_is_listed_separately() -> None:
    data = (
        b"HTTP/1.1 100 Continue\r\n\r\n"
        b"HTTP/1.1 103 Early Hints\r\nLink: </a>\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    )
    [result] = analyze_latency(
        [ByteChunk(REQ, 1.0, 1.1)],
        [ByteChunk(data, 2.0, 2.3)],
    )
    assert result.response_start.offset == data.index(b"HTTP/1.1 200")
    assert [i.status_code for i in result.interim_responses] == [100, 103]
    assert [i.first_byte.offset for i in result.interim_responses] == [
        0,
        data.index(b"HTTP/1.1 103"),
    ]
    # Interim latencies are separate; the headline latency is still measured
    # to the final response start.
    interim_latency = result.interim_responses[0].latency_after_request_end
    assert interim_latency is not None
    assert interim_latency.lower == pytest.approx(0.9)
    assert interim_latency.upper == pytest.approx(1.3)
    assert result.latency.lower == pytest.approx(0.9)
    assert result.latency.upper == pytest.approx(1.3)


def test_interim_only_reports_missing_final_boundary() -> None:
    [result] = analyze_latency(
        [ByteChunk(REQ, 1.0, 1.0)],
        [ByteChunk(b"HTTP/1.1 100 Continue\r\n\r\n", 2.0, 2.0)],
    )
    assert result.missing_sides == ("response_start",)
    assert result.interim_responses[0].status_code == 100
    assert not result.latency.observed


def test_missing_response_start_says_which_side() -> None:
    [result] = analyze_latency([ByteChunk(REQ, 1.0, 1.0)], [])
    assert result.request_end.observed
    assert result.missing_sides == ("response_start",)
    assert result.missing_boundary == "response_start"
    assert "response_start" in result.explain()
    assert not result.latency.observed


def test_missing_request_end_says_which_side() -> None:
    truncated = b"GET /a HTTP/1.1\r\nHost: example.com\r\n"
    results = analyze_latency(
        [ByteChunk(truncated, 1.0, 1.0)],
        [ByteChunk(resp(), 2.0, 2.0)],
    )
    # A truncated request cannot be turned into a complete Request event, so
    # the analyzer does not invent a request or a latency for it.
    assert results == []


def test_truncated_body_is_not_reported_as_complete_request() -> None:
    partial = b"POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nabc"
    results = analyze_latency(
        [ByteChunk(partial, 1.0, 1.0)],
        [ByteChunk(resp(), 2.0, 2.0)],
    )
    assert len(results) == 1
    assert results[0].missing_sides == ("request_end",)
    assert results[0].request_end.reason


def test_chunked_request_end_includes_terminating_chunk() -> None:
    req = (
        b"POST /a HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n0\r\nX-Trailer: y\r\n\r\n"
    )
    [result] = analyze_latency(
        [ByteChunk(req, 1.0, 1.0)], [ByteChunk(resp(), 2.0, 2.0)]
    )
    assert result.request_end.offset == len(req) - 1


def test_head_response_has_no_body() -> None:
    req = b"HEAD /a HTTP/1.1\r\nHost: x\r\n\r\n"
    data = b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n"
    [result] = analyze_latency([ByteChunk(req, 1, 1)], [ByteChunk(data, 2, 2)])
    assert result.response_start.observed


def test_pipelined_requests_match_in_order() -> None:
    responses = resp(b"1") + resp(b"22")
    results = analyze_latency(
        [ByteChunk(REQ + REQ2, 1.0, 1.0)],
        [ByteChunk(responses, 2.0, 2.0)],
    )
    assert [r.request.target for r in results if r.request is not None] == [
        b"/a",
        b"/b",
    ]
    assert results[0].response_start.offset == 0
    assert results[1].response_start.offset == len(resp(b"1"))


def test_response_exhaustion_marks_later_requests() -> None:
    results = analyze_latency(
        [ByteChunk(REQ + REQ2, 1.0, 1.0)],
        [ByteChunk(resp(), 2.0, 2.0)],
    )
    assert len(results) == 2
    assert results[0].response_start.observed
    assert results[1].missing_sides == ("response_start",)


def test_incomplete_final_response_does_not_expose_next_start() -> None:
    # Response 1 declares a 5-byte body but only 2 bytes were observed, so
    # response 2's start cannot be located safely.  We must not guess that it
    # is in the same chunk.
    headers = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nab"
    results = analyze_latency(
        [ByteChunk(REQ + REQ2, 1.0, 1.0)],
        [ByteChunk(headers, 2.0, 2.0)],
    )
    assert results[0].response_start.observed
    assert results[1].missing_sides == ("response_start",)


def test_same_chunk_inconsistent_windows_are_flagged() -> None:
    # A response batch that ends before a request batch starts would imply a
    # negative latency; the range is clamped to zero and flagged.
    [result] = analyze_latency(
        [ByteChunk(REQ, 5.0, 6.0)],
        [ByteChunk(resp(), 1.0, 2.0)],
    )
    assert result.inconsistent
    assert result.latency == LatencyRange(0.0, 0.0)


def test_analyzer_and_unified_input() -> None:
    analyzer = LatencyAnalyzer()
    analyzer.add("request", (REQ, 1.0, 1.0))
    analyzer.add("response", (resp(), 2.0, 2.0))
    [via_analyzer] = analyzer.analyze()

    [via_helper] = analyze_latency(
        [("request", REQ, 1.0, 1.0), ("response", resp(), 2.0, 2.0)]
    )
    assert via_analyzer.latency == via_helper.latency

    with pytest.raises(ValueError):
        analyzer.add("sideways", (REQ, 1.0, 1.0))


def test_result_serialization() -> None:
    [result] = analyze_latency(
        [ByteChunk(REQ, 1.0, 1.0)],
        [ByteChunk(b"HTTP/1.1 100 Continue\r\n\r\n" + resp(), 2.0, 2.0)],
    )
    data = result.as_dict()
    assert data["request_end"]["observed"] is True
    assert data["response_start"]["offset"] > 0
    assert data["latency"] == {"lower": 1.0, "upper": 1.0}
    assert data["interim_responses"][0]["status_code"] == 100
    assert data["missing_sides"] == []


def test_boundary_missing_helper() -> None:
    boundary = Boundary.missing("request_end", "not captured")
    assert not boundary.observed
    assert "not captured" in boundary.explain()


def test_100_continue_may_precede_request_body_end() -> None:
    # With Expect: 100-continue the interim response legitimately arrives
    # before the request body has finished, so its range may be negative; the
    # final response still gets a non-negative range.
    headers = (
        b"POST /u HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
        b"Expect: 100-continue\r\n\r\n"
    )
    final = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    [result] = analyze_latency(
        [ByteChunk(headers, 1.0, 1.1), ByteChunk(b"hello", 5.0, 5.1)],
        [ByteChunk(b"HTTP/1.1 100 Continue\r\n\r\n" + final, 1.2, 1.3)],
    )
    interim = result.interim_responses[0].latency_after_request_end
    assert interim is not None
    assert interim.upper < 0
    assert result.latency.observed
    assert result.latency.lower >= 0


def test_101_switch_is_terminal_not_interim() -> None:
    req = (
        b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\n\r\n"
    )
    resp = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
    [result] = analyze_latency(
        [ByteChunk(req, 1.0, 1.0)], [ByteChunk(resp, 2.0, 2.0)]
    )
    assert result.final_status_code == 101
    assert result.response_start.observed
    assert result.interim_responses == ()


def test_protocol_error_in_response_stream_is_not_raised() -> None:
    # A malformed response should make the boundary unobservable, not blow up
    # the whole report.
    results = analyze_latency(
        [ByteChunk(REQ, 1.0, 1.0)],
        [ByteChunk(b"garbage\r\n\r\n", 2.0, 2.0)],
    )
    assert results[0].missing_sides == ("response_start",)


def test_chunk_alias_properties() -> None:
    chunk = ByteChunk(b"abc", 1.0, 2.0)
    assert chunk.window_start == 1.0
    assert chunk.window_end == 2.0
    assert len(chunk) == 3


def test_dict_and_sequence_chunk_forms() -> None:
    from .._latency import _coerce_chunk

    assert _coerce_chunk({"data": b"a", "start": 1, "end": 2}) == ByteChunk(
        b"a", 1, 2
    )
    assert _coerce_chunk(
        {"bytes": b"a", "window_start": 1, "window_end": 2}
    ) == ByteChunk(b"a", 1, 2)
    with pytest.raises(TypeError):
        _coerce_chunk({"data": b"a"})
    with pytest.raises(TypeError):
        _coerce_chunk((b"a", 1))
    with pytest.raises(TypeError):
        _coerce_chunk(object())


def test_unified_dict_input_variants() -> None:
    rd = {"direction": "request", "data": REQ, "start": 1, "end": 1}
    sd = {"direction": "response", "data": resp(), "start": 2, "end": 2}
    [result] = analyze_latency([rd, sd])
    assert result.latency.observed
    # The caller's dicts are left untouched.
    assert rd["direction"] == "request"

    with pytest.raises(TypeError):
        analyze_latency([b"not an observation"])
    with pytest.raises(ValueError):
        analyze_latency([("up", REQ, 1, 1)])


def test_range_helpers() -> None:
    observed = LatencyRange(1.0, 2.0)
    assert observed.observed
    assert observed.min == 1.0
    assert observed.max == 2.0
    assert observed.width == pytest.approx(1.0)
    assert 1.5 in observed
    assert 3.0 not in observed
    missing = LatencyRange(None, None)
    assert not missing.observed
    assert missing.width is None
    assert str(missing) == "unobserved"
    assert 1.0 not in missing


def test_result_explain_without_missing_sides() -> None:
    [result] = analyze_latency(
        [ByteChunk(REQ, 1.0, 1.0)], [ByteChunk(resp(), 2.0, 3.0)]
    )
    text = result.explain()
    assert "latency in" in text
    assert result.final_response_start is result.response_start
    assert result.request_end_observed
    assert result.response_start_observed


def test_analyzer_record_and_calculate() -> None:
    analyzer = LatencyAnalyzer()
    analyzer.record("request", REQ, 1.0, 1.0)
    analyzer.record("response", resp(), 2.0, 2.0)
    [result] = analyzer.calculate()
    assert result.latency == LatencyRange(1.0, 1.0)


def test_offset_outside_any_chunk_is_unobserved() -> None:
    from .._latency import _ChunkIndex

    index = _ChunkIndex([ByteChunk(b"ab", 1, 1), ByteChunk(b"", 2, 2)])
    assert index.locate(-1) is None
    assert index.locate(2) is None
    assert index.locate(0) == (0, ObservationWindow(1, 1))

