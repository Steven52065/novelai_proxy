from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeImageHosting, FakeUpstream, write_test_config, _wait_for_log_image_urls


def test_generate_uploads_images_to_configured_image_host(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        release_upload = threading.Event()
        fake_hosting = FakeImageHosting(release_upload)
        app.state.upstream = FakeUpstream()
        app.state.proxy_queue.image_hosting = fake_hosting
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "image-host-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert fake_hosting.uploaded_request_ids == [success_log["request_id"]]
        assert success_log["image_urls"] == []

        release_upload.set()
        success_log = _wait_for_log_image_urls(client, success_log["request_id"])
        assert success_log["image_urls"] == [
            {
                "provider": "catbox",
                "url": "https://files.catbox.moe/fake-image.png",
                "filename": "image.png",
                "bytes": 10,
                "index": 1,
            }
        ]

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        page = client.get("/admin/logs")
        assert page.status_code == 200
        assert "https://files.catbox.moe/fake-image.png" in page.text
        assert "图床图片" in page.text

def test_generate_skips_image_host_upload_when_pending_limit_reached(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        release_upload = threading.Event()
        fake_hosting = FakeImageHosting(release_upload, max_pending_uploads=1)
        app.state.upstream = FakeUpstream()
        app.state.proxy_queue.image_hosting = fake_hosting
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "image-host-limit-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        try:
            first = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)
            second = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

            assert first.status_code == 201
            assert second.status_code == 201
            assert len(fake_hosting.uploaded_request_ids) == 1
        finally:
            release_upload.set()

        _wait_for_log_image_urls(client, fake_hosting.uploaded_request_ids[0])
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        skipped_log = next(row for row in logs if row["request_id"] not in fake_hosting.uploaded_request_ids)
        assert skipped_log["status"] == "success"
        assert skipped_log["image_urls"] == []

def test_image_host_upload_pending_limit_zero_allows_unlimited_tasks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        release_upload = threading.Event()
        fake_hosting = FakeImageHosting(release_upload, max_pending_uploads=0)
        app.state.upstream = FakeUpstream()
        app.state.proxy_queue.image_hosting = fake_hosting
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "image-host-unlimited-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        try:
            first = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)
            second = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

            assert first.status_code == 201
            assert second.status_code == 201
            assert len(fake_hosting.uploaded_request_ids) == 2
        finally:
            release_upload.set()

        for request_id in fake_hosting.uploaded_request_ids:
            success_log = _wait_for_log_image_urls(client, request_id)
            assert success_log["status"] == "success"
