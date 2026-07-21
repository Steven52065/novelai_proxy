from __future__ import annotations

import pytest

from app.config import HotPayloadConfig
from app.database import Database
from app.payload_archive import PayloadNotFoundError, PayloadArchiveService
from app.usage_logs import UsageLogCreate, UsageLogRepository


def test_skip_request_payload_stores_no_payload():
    db = _db_with_user()
    repo = UsageLogRepository(db, skip_request_payload=True)

    repo.insert_queued(_log({"input": "1girl, masterpiece", "model": "nai-diffusion-3"}))

    row = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("skip-payload",))
    assert row["request_payload"] is None
    assert row["request_payload_encoding"] == "json"
    assert row["request_payload_blob"] is None
    assert row["request_payload_bytes"] == 0
    assert row["request_payload_available_bytes"] == 0
    assert row["request_payload_compressed_bytes"] == 0

    # 汇总信息仍然记录，只有 Payload 被丢弃。
    assert row["model"] == "nai-diffusion-3"

    # 管理后台据此判断为不可重放、无可用 Payload。
    source = repo.get_by_id(row["id"])
    assert source["has_request_payload"] == 0
    assert source["request_payload"] is None

    with pytest.raises(PayloadNotFoundError):
        PayloadArchiveService(db).get_payload_text(row["id"])
    db.close()


def test_skip_request_payload_overrides_hot_payload_compression():
    db = _db_with_user()
    repo = UsageLogRepository(
        db,
        hot_payload_config=HotPayloadConfig(enabled=True, min_bytes=100, min_savings_ratio=0.10),
        skip_request_payload=True,
    )
    payload = {"input": "compressible text " * 500, "model": "nai-diffusion-3"}

    repo.insert_queued(_log(payload))

    row = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("skip-payload",))
    assert row["request_payload"] is None
    assert row["request_payload_encoding"] == "json"
    assert row["request_payload_blob"] is None
    assert row["request_payload_bytes"] == 0
    db.close()


def test_request_payload_recorded_when_switch_off():
    db = _db_with_user()
    repo = UsageLogRepository(db)

    repo.insert_queued(_log({"input": "1girl", "model": "nai-diffusion-3"}))

    row = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("skip-payload",))
    assert row["request_payload"] is not None
    assert row["request_payload_bytes"] > 0

    source = repo.get_by_id(row["id"])
    assert source["has_request_payload"] == 1
    db.close()


def _db_with_user() -> Database:
    db = Database(":memory:")
    db.init_schema()
    db.execute(
        "INSERT INTO users (api_key_hash, name, created_at) VALUES (?, ?, ?)",
        ("hash", "user", "2026-06-03T00:00:00+00:00"),
    )
    return db


def _log(payload: dict) -> UsageLogCreate:
    return UsageLogCreate(
        request_id="skip-payload",
        user_id=1,
        action="generate",
        estimated_anlas_cost=0,
        model=payload.get("model"),
        request_payload=payload,
    )
