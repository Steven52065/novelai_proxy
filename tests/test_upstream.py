from __future__ import annotations

import asyncio

from app.upstream import UpstreamClient


class FakeBinaryResponse:
    status_code = 201
    content = b"zip-bytes"
    headers = {"Content-Type": "application/zip"}

class FakeBinarySession:
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, data):
        return FakeBinaryResponse()

class FakeBinaryCredential:
    async def get_session(self):
        return FakeBinarySession()

def test_upstream_accepts_official_application_zip_content_type(monkeypatch):
    client = UpstreamClient("token")
    monkeypatch.setattr(client, "_credential", lambda: FakeBinaryCredential())

    import asyncio

    assert asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {})) == b"zip-bytes"
