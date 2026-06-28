from __future__ import annotations

import asyncio
import io
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from novelai_python._exceptions import APIError


PAYLOAD = {
    "input": "1girl",
    "model": "nai-diffusion-3",
    "action": "generate",
    "parameters": {
        "width": 832,
        "height": 1216,
        "scale": 5.0,
        "sampler": "k_euler_ancestral",
        "steps": 23,
        "n_samples": 1,
        "ucPreset": 0,
        "qualityToggle": False,
        "sm": False,
        "sm_dyn": False,
    },
}


class FakeUpstream:
    def __init__(self):
        self.last_generate_payload = None
        self.last_post_binary_url = None
        self.last_post_binary_payload = None
        self.generate_started_at = []
        self.suggest_tags_calls = []

    async def generate_image_zip(self, req):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as zip_file:
            zip_file.writestr("image.png", b"fake-image")
        return buffer.getvalue()

    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(time.monotonic())
        self.last_generate_payload = payload
        return await self.generate_image_zip(payload)

    async def encode_vibe_binary(self, payload):
        return b"fake-vibe"

    async def post_binary(self, url, payload):
        self.last_post_binary_url = url
        self.last_post_binary_payload = payload
        return await self.generate_image_zip(payload)

    async def upscale_zip(self, req):
        return b"fake-upscale-zip"

    async def augment_image_zip(self, req):
        return b"fake-augment-zip"

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        self.suggest_tags_calls.append({"model": model, "prompt": prompt, "lang": lang})
        return {"tags": []}


class BlockingFakeUpstream(FakeUpstream):
    def __init__(self, release_event: threading.Event):
        super().__init__()
        self.release_event = release_event

    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(time.monotonic())
        self.last_generate_payload = payload
        await asyncio.to_thread(self.release_event.wait)
        return await self.generate_image_zip(payload)


class FailingThenSuccessfulUpstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(time.monotonic())
        if len(self.generate_started_at) == 1:
            raise APIError(
                "Too many requests",
                request=payload,
                response={"message": "Too many requests"},
                code="429",
            )
        self.last_generate_payload = payload
        return await self.generate_image_zip(payload)


class FakeQueueSnapshot:
    def qsize(self):
        return 1

    def snapshot(self):
        return {
            "queue_size": 1,
            "running": {
                "request_id": "running-request",
                "user_id": 1,
                "action": "generate",
                "tier": "normal",
                "estimated_anlas_cost": 0,
                "priority": 10,
                "position": 0,
                "status": "running",
                "queued_seconds": 3,
                "running_seconds": 1,
            },
            "queued": [
                {
                    "request_id": "queued-request",
                    "user_id": 1,
                    "action": "generate",
                    "tier": "vip",
                    "estimated_anlas_cost": 5,
                    "priority": 0,
                    "position": 1,
                    "status": "queued",
                    "queued_seconds": 8,
                }
            ],
        }


class FakeImageHosting:
    def __init__(self, release_event: threading.Event | None = None, max_pending_uploads: int = 50):
        self.release_event = release_event
        self.max_pending_uploads = max_pending_uploads
        self.uploaded_request_ids = []

    async def upload_zip_images(self, *, zip_payload: bytes, request_id: str):
        assert b"fake-image" in zip_payload
        self.uploaded_request_ids.append(request_id)
        if self.release_event is not None:
            await asyncio.to_thread(self.release_event.wait)
        return [
            {
                "provider": "catbox",
                "url": "https://files.catbox.moe/fake-image.png",
                "filename": "image.png",
                "bytes": 10,
                "index": 1,
            }
        ]


def write_test_config(
    tmp_path: Path,
    upstream_interval_min_seconds: float = 0,
    upstream_interval_max_seconds: float | None = None,
    upstream_error_extra_delay_seconds: float = 0,
    upstream_execution_timeout_seconds: float = 60,
    hot_payload_enabled: bool = False,
    hot_payload_min_bytes: int = 4096,
    hot_payload_min_savings_ratio: float = 0.10,
) -> Path:
    if upstream_interval_max_seconds is None:
        upstream_interval_max_seconds = upstream_interval_min_seconds
    db_path = tmp_path / "test.db"
    _seed_novelai_db(db_path, ["default"])
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
admin:
  username: admin
  password: admin123
server:
  host: 127.0.0.1
  port: 8080
queue:
  max_queue_size: 2
  upstream_interval_min_seconds: {upstream_interval_min_seconds}
  upstream_interval_max_seconds: {upstream_interval_max_seconds}
  upstream_error_extra_delay_seconds: {upstream_error_extra_delay_seconds}
  upstream_execution_timeout_seconds: {upstream_execution_timeout_seconds}
database:
  path: "{db_path.as_posix()}"
  hot_payload:
    enabled: {str(hot_payload_enabled).lower()}
    compression: zlib
    compression_level: 6
    min_bytes: {hot_payload_min_bytes}
    min_savings_ratio: {hot_payload_min_savings_ratio}
logging:
  level: DEBUG
  directory: "{(tmp_path / "logs").as_posix()}"
cors:
  enabled: true
  allow_origins:
    - "https://client.example"
  allow_methods:
    - "*"
  allow_headers:
    - "*"
  expose_headers:
    - Content-Disposition
""",
        encoding="utf-8",
    )
    return config_path


def write_test_config_with_upstreams(
    tmp_path: Path,
    upstream_ids: list[str],
    max_queue_size: int = 2,
    dispatch_max_queue_size: int | None = None,
    routing_strategy: str = "round_robin",
    upstream_interval_min_seconds: float = 0,
    upstream_execution_timeout_seconds: float = 60,
    retry_429_max_attempts: int = 5,
    routing_adaptive_yaml: str = "",
) -> Path:
    db_path = tmp_path / "test.db"
    _seed_novelai_db(db_path, upstream_ids)
    config_path = tmp_path / "config.yaml"
    dispatch_max_queue_size_yaml = "" if dispatch_max_queue_size is None else f"  dispatch_max_queue_size: {dispatch_max_queue_size}\n"
    config_path.write_text(
        f"""
admin:
  username: admin
  password: admin123
server:
  host: 127.0.0.1
  port: 8080
queue:
  max_queue_size: {max_queue_size}
{dispatch_max_queue_size_yaml.rstrip()}
  upstream_interval_min_seconds: {upstream_interval_min_seconds}
  upstream_interval_max_seconds: {upstream_interval_min_seconds}
  upstream_error_extra_delay_seconds: 0
  upstream_execution_timeout_seconds: {upstream_execution_timeout_seconds}
  retry_429_max_attempts: {retry_429_max_attempts}
routing:
  strategy: {routing_strategy}
{routing_adaptive_yaml.rstrip()}
database:
  path: "{db_path.as_posix()}"
logging:
  level: DEBUG
  directory: "{(tmp_path / "logs").as_posix()}"
cors:
  enabled: true
  allow_origins:
    - "https://client.example"
  allow_methods:
    - "*"
  allow_headers:
    - "*"
  expose_headers:
    - Content-Disposition
""",
        encoding="utf-8",
    )
    return config_path


def _seed_novelai_db(db_path: Path, upstream_ids: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS novelai_upstreams (
                id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS novelai_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                account_tier INTEGER NOT NULL DEFAULT 3,
                upscale_anlas_cost INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO novelai_settings(id, account_tier, upscale_anlas_cost, created_at)
            VALUES (1, 3, 0, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET account_tier = excluded.account_tier
            """
        )
        for upstream_id in upstream_ids:
            conn.execute(
                """
                INSERT INTO novelai_upstreams(id, api_key, enabled, created_at)
                VALUES (?, ?, 1, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET enabled = 1
                """,
                (upstream_id, f"pst-test-token-{upstream_id}"),
            )
        conn.commit()
    finally:
        conn.close()


def write_test_config_with_image_format_policy(
    tmp_path: Path,
    mode: str = "request",
    image_format: str = "webp",
) -> Path:
    config_path = write_test_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            f"""
image_format:
  mode: {mode}
  format: {image_format}
"""
        )
    return config_path


def _wait_for_log_image_urls(client: TestClient, request_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        log = next(row for row in logs if row["request_id"] == request_id)
        if log["image_urls"]:
            return log
        time.sleep(0.05)
    raise AssertionError("timed out waiting for image host upload")
