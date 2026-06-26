from __future__ import annotations

from datetime import datetime, timezone

from app.config import HotPayloadConfig
from app.database import Database
from app.payload_archive import PayloadArchiveService
from app.usage_logs import UsageLogCreate, UsageLogRepository


def test_hot_payload_keeps_small_payload_as_minified_json():
    db = _db_with_user()
    repo = UsageLogRepository(
        db,
        hot_payload_config=HotPayloadConfig(enabled=True, min_bytes=4096),
    )
    payload = {"b": "x", "a": 1}

    repo.insert_queued(_log(payload))

    row = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("hot-payload",))
    assert row["request_payload"] == '{"a":1,"b":"x"}'
    assert row["request_payload_encoding"] == "json"
    assert row["request_payload_blob"] is None
    assert row["request_payload_bytes"] == len(row["request_payload"].encode("utf-8"))
    assert row["request_payload_available_bytes"] == row["request_payload_bytes"]
    assert row["request_payload_compressed_bytes"] == 0

    decoded = PayloadArchiveService(db).get_payload_dict(row["id"])
    assert decoded == payload
    db.close()


def test_hot_payload_compresses_large_payload_when_savings_threshold_is_met():
    db = _db_with_user()
    repo = UsageLogRepository(
        db,
        hot_payload_config=HotPayloadConfig(enabled=True, min_bytes=100, min_savings_ratio=0.10),
    )
    payload = _large_payload("compressible text " * 500)

    repo.insert_queued(_log(payload))

    row = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("hot-payload",))
    assert row["request_payload"] is None
    assert row["request_payload_encoding"] == "zlib"
    assert row["request_payload_blob"]
    assert row["request_payload_bytes"] > row["request_payload_compressed_bytes"]
    assert row["request_payload_available_bytes"] == row["request_payload_bytes"]
    assert row["request_payload_compressed_bytes"] == len(row["request_payload_blob"])

    source = repo.get_by_id(row["id"])
    assert source["request_payload"] is None
    assert source["has_request_payload"] == 1
    assert source["request_payload_bytes"] == row["request_payload_bytes"]
    assert PayloadArchiveService(db).get_payload_dict(row["id"]) == payload
    db.close()


def test_hot_payload_keeps_json_when_savings_threshold_is_not_met():
    db = _db_with_user()
    repo = UsageLogRepository(
        db,
        hot_payload_config=HotPayloadConfig(enabled=True, min_bytes=100, min_savings_ratio=0.99),
    )
    payload = _large_payload("still compressible " * 500)

    repo.insert_queued(_log(payload))

    row = db.query_one("SELECT request_payload, request_payload_encoding, request_payload_blob FROM usage_logs")
    assert row["request_payload"] is not None
    assert row["request_payload_encoding"] == "json"
    assert row["request_payload_blob"] is None
    db.close()


def test_insert_retry_attempt_copies_hot_compressed_payload_fields():
    db = _db_with_user()
    repo = UsageLogRepository(
        db,
        hot_payload_config=HotPayloadConfig(enabled=True, min_bytes=100, min_savings_ratio=0.10),
    )
    payload = _large_payload("retry copy " * 700)

    repo.insert_queued(_log(payload))
    repo.insert_retry_attempt(request_id="hot-payload", attempt_number=1, upstream_id="upstream-b")

    rows = db.query_all("SELECT * FROM usage_logs WHERE request_id = ? ORDER BY attempt_number", ("hot-payload",))
    assert rows[0]["request_payload_encoding"] == "zlib"
    assert rows[1]["request_payload_encoding"] == "zlib"
    assert rows[1]["request_payload_blob"] == rows[0]["request_payload_blob"]
    assert rows[1]["request_payload_bytes"] == rows[0]["request_payload_bytes"]
    assert rows[1]["request_payload_available_bytes"] == rows[0]["request_payload_available_bytes"]
    assert rows[1]["request_payload_compressed_bytes"] == rows[0]["request_payload_compressed_bytes"]
    assert PayloadArchiveService(db).get_payload_dict(rows[1]["id"]) == payload
    db.close()


def test_payload_archive_decodes_hot_compressed_payload_before_cold_archive():
    db = _db_with_user()
    repo = UsageLogRepository(
        db,
        hot_payload_config=HotPayloadConfig(enabled=True, min_bytes=100, min_savings_ratio=0.10),
    )
    payload = _large_payload("cold archive " * 700)
    repo.insert_queued(_log(payload))
    db.execute(
        "UPDATE usage_logs SET created_at = ? WHERE request_id = ?",
        ("2026-05-10T00:00:00+00:00", "hot-payload"),
    )

    service = PayloadArchiveService(db)
    result = service.archive_due_payloads(now=datetime(2026, 5, 22, 12, tzinfo=timezone.utc), hot_days=7)

    assert result["archived_payloads"] == 1
    row = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("hot-payload",))
    assert row["request_payload"] is None
    assert row["request_payload_encoding"] == "json"
    assert row["request_payload_blob"] is None
    assert row["request_payload_bytes"] == 0
    assert row["request_payload_compressed_bytes"] == 0
    ref = db.query_one("SELECT payload_bytes FROM usage_log_payload_archive_refs WHERE log_id = ?", (row["id"],))
    assert ref["payload_bytes"] > 0
    assert row["request_payload_available_bytes"] == ref["payload_bytes"]
    assert service.get_payload_dict(row["id"]) == payload
    db.close()


def test_success_and_image_url_updates_maintain_precomputed_bytes():
    db = _db_with_user()
    repo = UsageLogRepository(db)
    repo.insert_queued(_log({"prompt": "bytes"}))

    output_files = [{"filename": "图像.png"}]
    initial_urls = [{"url": "https://files.example/初始.png"}]
    repo.mark_success(
        "hot-payload",
        queued_ms=1,
        final_cost=0,
        output_files=output_files,
        image_urls=initial_urls,
    )
    row = db.query_one("SELECT output_files, output_files_bytes, image_urls, image_urls_bytes FROM usage_logs")
    assert row["output_files_bytes"] == len(row["output_files"].encode("utf-8"))
    assert row["image_urls_bytes"] == len(row["image_urls"].encode("utf-8"))

    updated_urls = [{"url": "https://files.example/更新.png"}]
    repo.update_image_urls("hot-payload", updated_urls)
    row = db.query_one("SELECT image_urls, image_urls_bytes FROM usage_logs")
    assert row["image_urls_bytes"] == len(row["image_urls"].encode("utf-8"))
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
        request_id="hot-payload",
        user_id=1,
        action="generate",
        estimated_anlas_cost=0,
        request_payload=payload,
    )


def _large_payload(text: str) -> dict:
    return {
        "input": text,
        "model": "nai-diffusion-3",
        "parameters": {
            "width": 512,
            "height": 768,
            "steps": 1,
            "n_samples": 1,
        },
    }
