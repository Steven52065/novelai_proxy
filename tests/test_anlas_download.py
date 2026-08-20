from __future__ import annotations

from anlas_sync import download


def test_fetch_uses_declared_httpx_dependency(monkeypatch):
    calls: dict[str, object] = {}

    class FakeResponse:
        content = b"downloaded"

        def raise_for_status(self) -> None:
            calls["raise_for_status"] = True

    def fake_get(url: str, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(download.httpx, "get", fake_get)

    assert download.fetch("https://example.test/chunk.js", timeout=17) == b"downloaded"
    assert calls == {
        "url": "https://example.test/chunk.js",
        "kwargs": {
            "headers": {"User-Agent": download.UA},
            "timeout": 17,
            "follow_redirects": True,
        },
        "raise_for_status": True,
    }
