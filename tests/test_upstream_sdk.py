from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest

from app.api_errors import APIError, AuthError, DataSerializationError
from app.novelai_models import AugmentImageRequest, UpscaleRequest
from app.upstream import UpstreamClient


class FakeResponse:
    def __init__(self, *, status_code=201, content=b"zip-bytes", content_type="application/zip", json_body=None):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type}
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("not json")
        return self._json_body


class RecordingSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, data):
        self.posts.append((url, data))
        return self.response

    async def get(self, url):
        self.gets.append(url)
        return self.response


class FakeCredential:
    def __init__(self, session: RecordingSession):
        self.session = session

    async def get_session(self):
        return self.session


def _client_with_response(monkeypatch, response: FakeResponse) -> tuple[UpstreamClient, RecordingSession]:
    client = UpstreamClient("secret-token")
    session = RecordingSession(response)
    monkeypatch.setattr(client, "_credential", lambda: FakeCredential(session))
    return client, session


def _zip_payload(*files: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for filename, content in files:
            archive.writestr(filename, content)
    return buffer.getvalue()


def test_upstream_uses_api_token_session_factory_for_pst_token():
    credential = UpstreamClient("pst-secret-token")._credential()

    assert credential.token_kind == "api"
    assert credential.token == "pst-secret-token"


def test_upstream_uses_jwt_token_session_factory_for_jwt_token():
    credential = UpstreamClient("eyJhbGciOiJIUzI1NiJ9.token.signature")._credential()

    assert credential.token_kind == "jwt"
    assert credential.token == "eyJhbGciOiJIUzI1NiJ9.token.signature"


def test_upstream_reuses_api_key_credential_instance():
    client = UpstreamClient("pst-secret-token")

    assert client._credential() is client._credential()


def test_upstream_config_uses_jwt_credential_for_ey_token():
    client = UpstreamClient("eyJhbGciOiJIUzI1NiJ9.payload.signature")

    credential = client._credential()

    assert client.api_key == "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    assert credential.token_kind == "jwt"


def test_session_factory_builds_sdk_compatible_headers_and_options(monkeypatch):
    captured = {}

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("app.upstream.AsyncSession", FakeAsyncSession)
    factory = UpstreamClient("pst-secret-token")._credential()
    session = asyncio.run(factory.get_session(timeout=42, update_headers={"x-extra": "yes"}))

    assert isinstance(session, FakeAsyncSession)
    assert captured["timeout"] == 42
    assert captured["impersonate"] == "chrome136"
    assert captured["headers"]["Authorization"] == "Bearer pst-secret-token"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["x-correlation-id"] == factory.x_correlation_id
    assert len(factory.x_correlation_id) == 6
    assert captured["headers"]["x-extra"] == "yes"
    assert captured["headers"]["x-initiated-at"].endswith("Z")


@pytest.mark.parametrize(
    "content_type",
    [
        "application/zip",
        "binary/octet-stream",
        "application/binary",
        "application/octet-stream",
        "application/x-zip-compressed",
    ],
)
def test_upstream_binary_post_accepts_supported_binary_content_types(monkeypatch, content_type: str):
    client, _session = _client_with_response(monkeypatch, FakeResponse(content_type=content_type))

    assert asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"})) == b"zip-bytes"


def test_generate_image_payload_posts_official_url_and_json_body(monkeypatch):
    client, session = _client_with_response(monkeypatch, FakeResponse())
    payload = {"input": "1girl", "parameters": {"steps": 1}}

    result = asyncio.run(client.generate_image_payload_zip(payload))

    assert result == b"zip-bytes"
    assert session.posts == [
        ("https://image.novelai.net/ai/generate-image", json.dumps(payload).encode("utf-8")),
    ]


def test_encode_vibe_posts_official_url_and_json_body(monkeypatch):
    client, session = _client_with_response(monkeypatch, FakeResponse(content_type="application/binary", content=b"vibe"))
    payload = {"image": "base64", "model": "nai-diffusion-4-5-full"}

    result = asyncio.run(client.encode_vibe_binary(payload))

    assert result == b"vibe"
    assert session.posts == [
        ("https://image.novelai.net/ai/encode-vibe", json.dumps(payload).encode("utf-8")),
    ]


def test_upscale_posts_official_url_and_preserves_complete_zip(monkeypatch):
    zip_payload = _zip_payload(("first.png", b"first"), ("second.png", b"second"))
    client, session = _client_with_response(monkeypatch, FakeResponse(content=zip_payload))
    request = UpscaleRequest(image="aW1n", width=64, height=32, scale=2)

    result = asyncio.run(client.upscale_zip(request))

    assert result == zip_payload
    assert session.posts == [
        (
            "https://api.novelai.net/ai/upscale",
            json.dumps(request.model_dump(mode="json", exclude_none=True)).encode("utf-8"),
        )
    ]


def test_augment_posts_official_url_and_json_body(monkeypatch):
    zip_payload = _zip_payload(("image.png", b"image"))
    client, session = _client_with_response(monkeypatch, FakeResponse(content=zip_payload))
    request = AugmentImageRequest(
        req_type="sketch",
        width=64,
        height=32,
        image="aW1n",
        defry=0,
    )

    result = asyncio.run(client.augment_image_zip(request))

    assert result == zip_payload
    assert session.posts == [
        (
            "https://image.novelai.net/ai/augment-image",
            json.dumps(request.model_dump(mode="json", exclude_none=True)).encode("utf-8"),
        )
    ]


@pytest.mark.parametrize("method", ["upscale_zip", "augment_image_zip"])
def test_tool_zip_response_rejects_invalid_zip(monkeypatch, method: str):
    client, _session = _client_with_response(monkeypatch, FakeResponse(content=b"not-a-zip"))
    request = (
        UpscaleRequest(image="aW1n", width=64, height=32, scale=2)
        if method == "upscale_zip"
        else AugmentImageRequest(req_type="sketch", width=64, height=32, image="aW1n", defry=0)
    )

    with pytest.raises(DataSerializationError, match="ZIP 文件无效"):
        asyncio.run(getattr(client, method)(request))


@pytest.mark.parametrize("method", ["upscale_zip", "augment_image_zip"])
def test_tool_zip_response_rejects_empty_zip(monkeypatch, method: str):
    client, _session = _client_with_response(monkeypatch, FakeResponse(content=_zip_payload()))
    request = (
        UpscaleRequest(image="aW1n", width=64, height=32, scale=2)
        if method == "upscale_zip"
        else AugmentImageRequest(req_type="sketch", width=64, height=32, image="aW1n", defry=0)
    )

    with pytest.raises(DataSerializationError, match="不包含任何文件"):
        asyncio.run(getattr(client, method)(request))


def test_suggest_tags_uses_url_encoded_query_and_returns_json(monkeypatch):
    body = {"tags": [{"tag": "red hair", "count": 3, "confidence": 0.9}]}
    client, session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=200, json_body=body, content_type="application/json"),
    )

    result = asyncio.run(
        client.suggest_tags("nai-diffusion-3", "red hair & blue eyes", "en")
    )

    assert result == body
    assert session.gets == [
        "https://image.novelai.net/ai/generate-image/suggest-tags?"
        "model=nai-diffusion-3&prompt=red+hair+%26+blue+eyes&lang=en"
    ]


def test_upstream_500_is_not_retried(monkeypatch):
    """上游 500 是故障信号，必须原样上报，不得重发放大上游负载或重复扣费。"""
    client, session = _client_with_response(
        monkeypatch,
        FakeResponse(
            status_code=500,
            json_body={"statusCode": 500, "message": "Internal Server Error"},
            content_type="application/json",
        ),
    )

    with pytest.raises(APIError) as exc_info:
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))

    assert str(exc_info.value.code) == "500"
    assert len(session.posts) == 1


def test_upstream_suggest_tags_500_is_not_retried(monkeypatch):
    client, session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=500, json_body={"message": "boom"}, content_type="application/json"),
    )

    with pytest.raises(APIError):
        asyncio.run(client.suggest_tags("nai-diffusion-3", "red hair", "en"))

    assert len(session.gets) == 1


def test_upstream_binary_post_maps_auth_status_to_auth_error(monkeypatch):
    client, _session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=401, content=b'{"message":"bad token"}', json_body={"message": "bad token"}),
    )

    with pytest.raises(AuthError) as exc_info:
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))

    assert exc_info.value.message == "bad token"
    assert str(exc_info.value.code) == "401"


def test_upstream_binary_post_maps_429_to_api_error_without_requiring_json(monkeypatch):
    client, _session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=429, content=b"Too many requests", content_type="text/plain"),
    )

    with pytest.raises(APIError) as exc_info:
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))

    assert exc_info.value.message == "Too many requests"
    assert str(exc_info.value.code) == "429"


def test_upstream_binary_post_rejects_empty_success_response(monkeypatch):
    client, _session = _client_with_response(monkeypatch, FakeResponse(content=b""))

    with pytest.raises(DataSerializationError):
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))


def test_upstream_long_non_json_error_body_is_preserved_not_discarded(monkeypatch):
    """网关 500 常以大段 HTML 返回；整体丢弃会让 usage_logs 失去排查价值。"""
    body = ("<html><title>502 Bad Gateway</title>" + "x" * 3000 + "</html>").encode("utf-8")
    client, _session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=500, content=body, content_type="text/html"),
    )

    with pytest.raises(APIError) as exc_info:
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))

    message = exc_info.value.message
    assert message.startswith("<html><title>502 Bad Gateway</title>")
    assert "truncated" in message
    assert str(len(body)) in message


def test_upstream_short_non_json_error_body_is_kept_verbatim(monkeypatch):
    client, _session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=500, content=b"upstream exploded", content_type="text/plain"),
    )

    with pytest.raises(APIError) as exc_info:
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))

    assert exc_info.value.message == "upstream exploded"


@pytest.mark.parametrize(
    "json_body,expected",
    [
        ({"statusCode": 429, "message": None}, "上游请求失败"),
        ({"statusCode": 429, "message": ""}, "上游请求失败"),
        ({"statusCode": 429, "message": {"nested": "detail"}}, "{'nested': 'detail'}"),
        ({"statusCode": 429, "message": "Concurrent generation is locked"}, "Concurrent generation is locked"),
    ],
)
def test_upstream_error_message_is_normalized_to_text(json_body, expected, monkeypatch):
    """APIError.message 必须始终是字符串。

    error.get("message", 默认值) 的默认值只在键缺失时生效，上游返回
    {"message": null} 或非字符串结构时会把 None/dict 带进 APIError.message，
    随后 usage_logs.mark_failed 的 error_message[:500] 会抛 TypeError/KeyError，
    结算中断、日志行停在 running 非终态，并成为 /account「最近调用」的展示内容。
    """
    client, _session = _client_with_response(
        monkeypatch,
        FakeResponse(status_code=429, json_body=json_body, content_type="application/json"),
    )

    with pytest.raises(APIError) as exc_info:
        asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {"input": "1girl"}))

    assert exc_info.value.message == expected
    assert isinstance(exc_info.value.message, str)
    # 结算路径真正做的事：切片不能抛。
    assert exc_info.value.message[:500] == expected
