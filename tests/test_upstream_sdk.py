from __future__ import annotations

import asyncio
import json

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
    zip_payload = b"zip-with-multiple-files"
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
    client, session = _client_with_response(monkeypatch, FakeResponse())
    request = AugmentImageRequest(
        req_type="sketch",
        width=64,
        height=32,
        image="aW1n",
        defry=0,
    )

    result = asyncio.run(client.augment_image_zip(request))

    assert result == b"zip-bytes"
    assert session.posts == [
        (
            "https://image.novelai.net/ai/augment-image",
            json.dumps(request.model_dump(mode="json", exclude_none=True)).encode("utf-8"),
        )
    ]


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
