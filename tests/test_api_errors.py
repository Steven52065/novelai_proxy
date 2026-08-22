from __future__ import annotations

from app.api_errors import (
    APIError,
    AuthError,
    ConcurrentGenerationError,
    DataSerializationError,
    NovelAIProxyError,
    api_error_status_code,
)


def test_api_error_preserves_sdk_compatible_contract():
    request = {"url": "/ai/generate-image"}
    response = {"message": "upstream failed"}
    exc = APIError("upstream failed", request, response, "429")

    assert isinstance(exc, NovelAIProxyError)
    assert exc.message == "upstream failed"
    assert exc.request is request
    assert exc.response is response
    assert exc.code == "429"
    assert exc.args == ("upstream failed", request, response, "429")
    assert exc.__dict__ == {
        "message": "upstream failed",
        "request": request,
        "response": response,
        "code": "429",
    }


def test_api_error_subtypes_and_status_mapping():
    for error_type in (AuthError, DataSerializationError, ConcurrentGenerationError):
        assert isinstance(error_type("failed", {}, {}, None), APIError)

    assert api_error_status_code(APIError("failed", {}, {}, "403")) == 403
    assert api_error_status_code(APIError("failed", {}, {}, "unknown")) == 502
    assert api_error_status_code(DataSerializationError("invalid payload", {}, {}, "201")) == 502


def test_api_error_never_reports_a_non_error_status_code():
    """上游 2xx + 非白名单 Content-Type 也会抛 APIError，不能把失败报成 2xx。"""
    for code in (200, 201, "201", 302, 399):
        assert api_error_status_code(APIError("failed", {}, {}, code)) == 502

    assert api_error_status_code(APIError("failed", {}, {}, 400)) == 400
    assert api_error_status_code(APIError("failed", {}, {}, 429)) == 429
    assert api_error_status_code(APIError("failed", {}, {}, 500)) == 500
