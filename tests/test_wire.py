from gateway_traffic.wire import parse_transaction


def test_final_status_and_received_body_bytes(make_exchange):
    req, resp = make_exchange("/items?id=3", 200, b"hello")
    transaction = parse_transaction(req, resp)
    assert transaction.path == "/items"
    assert transaction.method == b"GET"
    assert transaction.final_status == 200
    assert transaction.body_bytes == 5
    assert transaction.response_complete


def test_interim_responses_do_not_become_final_status(make_exchange):
    req, resp = make_exchange("/continue", 503, b"no service")
    resp = (
        b"HTTP/1.1 100 Continue\r\n\r\n"
        b"HTTP/1.1 103 Early Hints\r\nLink: </s.css>\r\n\r\n"
        + resp
    )
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 503
    assert transaction.informational_statuses == (100, 103)
    assert transaction.body_bytes == len(b"no service")


def test_chunked_body_counts_only_payload_bytes(make_exchange):
    req, _ = make_exchange("/chunked")
    resp = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"4\r\nwiki\r\n5\r\npedia\r\n0\r\n\r\n"
    )
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 200
    assert transaction.body_bytes == len(b"wikipedia")
    assert transaction.response_complete


def test_truncated_content_length_uses_actual_bytes(make_exchange):
    req, resp = make_exchange("/truncated", 200, b"abc", include_content_length=False)
    resp = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nabc"
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 200
    assert transaction.body_bytes == 3
    assert not transaction.response_complete
    assert any("expected 100" in error for error in transaction.errors)


def test_head_response_has_no_body_even_with_content_length(make_exchange):
    req, _ = make_exchange("/head", method="HEAD", include_content_length=False)
    resp = b"HTTP/1.1 200 OK\r\nContent-Length: 1234\r\n\r\n"
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 200
    assert transaction.body_bytes == 0


def test_204_response_ignores_declared_body(make_exchange):
    req, _ = make_exchange("/nocontent")
    resp = b"HTTP/1.1 204 No Content\r\nContent-Length: 99\r\n\r\n"
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 204
    assert transaction.body_bytes == 0


def test_close_delimited_body(make_exchange):
    req, resp = make_exchange("/close", 200, b"body", include_content_length=False)
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 200
    assert transaction.body_bytes == 4
    assert transaction.response_complete


def test_status_101_is_the_final_status_of_an_upgrade(make_exchange):
    req, resp = make_exchange(
        "/upgrade",
        101,
        b"",
        headers="Upgrade: websocket\r\nConnection: Upgrade\r\n",
        include_content_length=False,
        response_headers="Upgrade: websocket\r\nConnection: Upgrade\r\n",
    )
    transaction = parse_transaction(req, resp)
    assert transaction.final_status == 101
    assert transaction.response_protocol_switch
