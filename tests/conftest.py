import pytest


@pytest.fixture
def make_exchange():
    def build(
        path: str = "/",
        status: int = 200,
        body: bytes = b"",
        *,
        method: str = "GET",
        headers: str = "",
        response_headers: str = "",
        include_content_length: bool = True,
    ) -> tuple[bytes, bytes]:
        """Build raw request and response bytes for parser tests."""

        request = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: example.test\r\n"
            f"{headers}\r\n"
        ).encode("ascii")
        if include_content_length:
            declared = f"Content-Length: {len(body)}\r\n"
        else:
            declared = ""
        response = (
            f"HTTP/1.1 {status} Test\r\n"
            f"{response_headers}"
            f"{declared}"
            "\r\n"
        ).encode("ascii") + body
        return request, response

    return build
