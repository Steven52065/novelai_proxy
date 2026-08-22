from __future__ import annotations

from anlas_sync import download


def test_fetch_uses_curl_cffi_with_chrome_impersonation(monkeypatch):
    """novelai.net 会拦截默认 TLS 指纹，下载必须用 curl_cffi 模拟 Chrome。"""
    calls: dict[str, object] = {}

    class FakeResponse:
        content = b"downloaded"

        def raise_for_status(self) -> None:
            calls["raise_for_status"] = True

    def fake_get(url: str, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(download.requests, "get", fake_get)

    assert download.fetch("https://example.test/chunk.js", timeout=17) == b"downloaded"
    assert calls == {
        "url": "https://example.test/chunk.js",
        "kwargs": {
            "headers": {"User-Agent": download.UA},
            "timeout": 17,
            "allow_redirects": True,
            "impersonate": "chrome136",
        },
        "raise_for_status": True,
    }
