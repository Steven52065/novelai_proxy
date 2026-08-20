from __future__ import annotations

from app.api_errors import APIError

from app.queue_errors import UpstreamExecutionTimeout
from app.upstream_queue import ProxyQueue, _format_upstream_500_error_message


def test_format_upstream_500_includes_message_and_details():
    exc = APIError(
        "Internal Server Error",
        request={},
        response={
            "statusCode": 500,
            "message": "Internal Server Error",
            "details": {"reason": "invalid width"},
        },
        code=500,
    )
    assert _format_upstream_500_error_message(exc) == (
        "Internal Server Error | details={'reason': 'invalid width'}"
    )


def test_format_upstream_500_without_details_keeps_message():
    exc = APIError(
        "Internal Server Error",
        request={},
        response={"statusCode": 500, "message": "Internal Server Error"},
        code="500",
    )
    assert _format_upstream_500_error_message(exc) == "Internal Server Error"


def test_format_upstream_500_accepts_list_message():
    # NovelAI 校验错误常见形态：message 是 list，不能对 list 调 strip。
    exc = APIError(
        ["width invalid", "height invalid"],
        request={},
        response={
            "statusCode": 500,
            "message": ["width invalid", "height invalid"],
            "details": {"field": "width"},
        },
        code=500,
    )
    assert _format_upstream_500_error_message(exc) == (
        "['width invalid', 'height invalid'] | details={'field': 'width'}"
    )
    code, message = ProxyQueue._error_details(exc)
    assert code == "500"
    assert message == "['width invalid', 'height invalid'] | details={'field': 'width'}"


def test_error_details_only_enriches_500():
    exc_500 = APIError(
        "Internal Server Error",
        request={},
        response={
            "statusCode": 500,
            "message": "Internal Server Error",
            "details": "boom",
        },
        code=500,
    )
    code, message = ProxyQueue._error_details(exc_500)
    assert code == "500"
    assert message == "Internal Server Error | details='boom'"

    exc_429 = APIError(
        "Too many requests",
        request={},
        response={"statusCode": 429, "message": "Too many requests", "details": "ignored"},
        code=429,
    )
    code, message = ProxyQueue._error_details(exc_429)
    assert code == "429"
    assert message == "Too many requests"

    code, message = ProxyQueue._error_details(UpstreamExecutionTimeout(12.0))
    assert code == "upstream_timeout"
    assert "12" in message
