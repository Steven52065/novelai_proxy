from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import Database
from ..payload_archive import PayloadArchiveService
from ..templating import templates
from .auth import require_admin_or_session, require_admin_page_session
from .common import format_bytes, format_display_time, row_to_dict


api_router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin_or_session)])
web_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_page_session)])


REQUEST_PAYLOAD_ORIGINAL_BYTES_SQL = """
l.request_payload_available_bytes
"""
REQUEST_PAYLOAD_HOT_COMPRESSED_BYTES_SQL = """
l.request_payload_compressed_bytes
"""
HAS_REQUEST_PAYLOAD_SQL = """
CASE
    WHEN l.request_payload_available_bytes > 0 THEN 1
    ELSE 0
END
"""


class CleanupLogsRequest(BaseModel):
    older_than_days: int = Field(default=30, ge=0, le=3650)
    statuses: list[str] = Field(default_factory=list)


class ClearPayloadsRequest(BaseModel):
    older_than_days: int = Field(default=7, ge=0, le=3650)
    min_payload_kb: int = Field(default=128, ge=0, le=1024 * 1024)
    clear_output_files: bool = False
    clear_image_urls: bool = False


class ArchivePayloadsRequest(BaseModel):
    hot_days: int | None = Field(default=None, ge=0, le=3650)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)


@api_router.get("/database/stats")
async def database_stats(request: Request):
    return await asyncio.to_thread(_database_stats, request)


@api_router.post("/database/cleanup-logs")
async def cleanup_logs(payload: CleanupLogsRequest, request: Request):
    return await asyncio.to_thread(_cleanup_logs, request, payload.older_than_days, payload.statuses)


@api_router.post("/database/clear-payloads")
async def clear_payloads(payload: ClearPayloadsRequest, request: Request):
    return await asyncio.to_thread(
        _clear_payloads,
        request,
        older_than_days=payload.older_than_days,
        min_payload_kb=payload.min_payload_kb,
        clear_output_files=payload.clear_output_files,
        clear_image_urls=payload.clear_image_urls,
    )


@api_router.post("/database/archive-payloads")
async def archive_payloads(payload: ArchivePayloadsRequest, request: Request):
    return await asyncio.to_thread(_archive_payloads, request, hot_days=payload.hot_days, max_rows=payload.max_rows)


@api_router.post("/database/vacuum")
async def vacuum_database(request: Request):
    return await asyncio.to_thread(_vacuum_database, request)


@web_router.get("/database", response_class=HTMLResponse)
async def database_page(request: Request, message: str | None = None):
    stats = await asyncio.to_thread(_database_stats, request)
    return templates.TemplateResponse(
        request,
        "database.html",
        {
            "active": "database",
            "stats": stats,
            "message": message,
            "status_choices": ["success", "failed", "rejected", "queued", "running"],
        },
    )


@web_router.post("/database/cleanup-logs")
async def cleanup_logs_form(
    request: Request,
    older_than_days: int = Form(30),
    statuses: list[str] | None = Form(None),
):
    result = await asyncio.to_thread(_cleanup_logs, request, older_than_days, statuses or [])
    return RedirectResponse(f"/admin/database?message=已删除 {result['deleted_logs']} 条日志记录", status_code=303)


@web_router.post("/database/clear-payloads")
async def clear_payloads_form(
    request: Request,
    older_than_days: int = Form(7),
    min_payload_kb: int = Form(128),
    clear_output_files: str | None = Form(None),
    clear_image_urls: str | None = Form(None),
):
    result = await asyncio.to_thread(
        _clear_payloads,
        request,
        older_than_days=older_than_days,
        min_payload_kb=min_payload_kb,
        clear_output_files=clear_output_files == "on",
        clear_image_urls=clear_image_urls == "on",
    )
    return RedirectResponse(f"/admin/database?message=已清空 {result['updated_logs']} 条日志的大字段", status_code=303)


@web_router.post("/database/archive-payloads")
async def archive_payloads_form(
    request: Request,
    hot_days: int | None = Form(None),
    max_rows: int | None = Form(None),
):
    result = await asyncio.to_thread(_archive_payloads, request, hot_days=hot_days, max_rows=max_rows)
    return RedirectResponse(
        f"/admin/database?message=已归档 {result['archived_payloads']} 条 Payload，生成 {result['archived_parts']} 个压缩块",
        status_code=303,
    )


@web_router.post("/database/vacuum")
async def vacuum_database_form(request: Request):
    result = await asyncio.to_thread(_vacuum_database, request)
    before = format_bytes(result["before_bytes"])
    after = format_bytes(result["after_bytes"])
    return RedirectResponse(f"/admin/database?message=数据库压缩完成：{before} -> {after}", status_code=303)


def _database_stats(request: Request) -> dict:
    db: Database = request.app.state.db
    db_path = db.path
    db_files = _database_file_sizes(db_path)
    page = db.query_one("PRAGMA page_count")
    free = db.query_one("PRAGMA freelist_count")
    page_size = db.query_one("PRAGMA page_size")
    usage = db.query_one(
        f"""
        SELECT COUNT(*) AS total_logs,
               COALESCE(SUM({REQUEST_PAYLOAD_ORIGINAL_BYTES_SQL}), 0) AS request_payload_bytes,
               COALESCE(SUM({REQUEST_PAYLOAD_HOT_COMPRESSED_BYTES_SQL}), 0) AS request_payload_compressed_bytes,
               COALESCE(SUM(l.output_files_bytes), 0) AS output_files_bytes,
               COALESCE(SUM(l.image_urls_bytes), 0) AS image_urls_bytes,
               COALESCE(SUM({HAS_REQUEST_PAYLOAD_SQL}), 0) AS logs_with_payload
        FROM usage_logs l
        """
    )
    status_rows = db.query_all(
        """
        SELECT status, COUNT(*) AS count
        FROM usage_logs
        GROUP BY status
        ORDER BY count DESC, status
        """
    )
    largest_logs = db.query_all(
        f"""
        SELECT l.id, l.request_id, l.action, l.status, l.created_at, u.name AS user_name,
               {REQUEST_PAYLOAD_ORIGINAL_BYTES_SQL} AS request_payload_bytes,
               {REQUEST_PAYLOAD_HOT_COMPRESSED_BYTES_SQL} AS request_payload_compressed_bytes,
               l.output_files_bytes AS output_files_bytes,
               l.image_urls_bytes AS image_urls_bytes
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        ORDER BY l.request_payload_available_bytes DESC, l.output_files_bytes DESC, l.image_urls_bytes DESC, l.id DESC
        LIMIT 10
        """
    )
    total_payload_bytes = (
        int(usage["request_payload_bytes"] or 0)
        + int(usage["output_files_bytes"] or 0)
        + int(usage["image_urls_bytes"] or 0)
    )
    main_bytes = int(db_files["main"]["bytes"])
    wal_bytes = int(db_files["wal"]["bytes"])
    shm_bytes = int(db_files["shm"]["bytes"])
    page_count = int(page[0])
    freelist_count = int(free[0])
    sqlite_bytes = page_count * int(page_size[0])
    reclaimable_bytes = freelist_count * int(page_size[0])
    archive_stats = _payload_archive_stats(request)
    return {
        "database_path": str(db_path),
        "files": db_files,
        "total_file_bytes": main_bytes + wal_bytes + shm_bytes,
        "main_file_bytes": main_bytes,
        "sqlite_bytes": sqlite_bytes,
        "reclaimable_bytes": reclaimable_bytes,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "page_size": int(page_size[0]),
        "usage_logs": {
            "total": int(usage["total_logs"] or 0),
            "logs_with_payload": int(usage["logs_with_payload"] or 0),
            "request_payload_bytes": int(usage["request_payload_bytes"] or 0),
            "request_payload_compressed_bytes": int(usage["request_payload_compressed_bytes"] or 0),
            "hot_payload_compressed_bytes": int(usage["request_payload_compressed_bytes"] or 0),
            "output_files_bytes": int(usage["output_files_bytes"] or 0),
            "image_urls_bytes": int(usage["image_urls_bytes"] or 0),
            "payload_bytes": total_payload_bytes,
        },
        "payload_archives": archive_stats,
        "status_counts": [row_to_dict(row) for row in status_rows],
        "largest_logs": [_usage_log_size_to_dict(row) for row in largest_logs],
    }


def _archive_payloads(request: Request, *, hot_days: int | None, max_rows: int | None) -> dict:
    config = request.app.state.config.database.payload_archive
    service: PayloadArchiveService = request.app.state.payload_archive_service
    return service.archive_due_payloads(
        hot_days=config.hot_days if hot_days is None else hot_days,
        compression=config.compression,
        compression_level=config.compression_level,
        max_payloads_per_part=config.max_payloads_per_part,
        max_raw_bytes_per_part=config.max_raw_bytes_per_part,
        max_rows_per_run=config.max_rows_per_run if max_rows is None else max_rows,
    )


def _cleanup_logs(request: Request, older_than_days: int, statuses: list[str]) -> dict:
    db: Database = request.app.state.db
    payload_archive: PayloadArchiveService = request.app.state.payload_archive_service
    cutoff = _cutoff_time(older_than_days)
    valid_statuses = _valid_statuses(statuses)
    where = ["created_at < ?"]
    params: list[object] = [cutoff]
    if valid_statuses:
        where.append(f"status IN ({','.join('?' for _ in valid_statuses)})")
        params.extend(valid_statuses)
    where_sql = " AND ".join(where)
    archive_ids: set[int] = set()
    deleted_logs = 0
    batch_size = 500
    while True:
        batch_params = (*params, batch_size)
        with db.transaction() as conn:
            conn.execute(
                """
                CREATE TEMP TABLE IF NOT EXISTS temp_log_cleanup_targets (
                    log_id INTEGER PRIMARY KEY
                )
                """
            )
            conn.execute("DELETE FROM temp_log_cleanup_targets")
            conn.execute(
                f"""
                INSERT OR IGNORE INTO temp_log_cleanup_targets (log_id)
                SELECT id
                FROM usage_logs
                WHERE {where_sql}
                ORDER BY id
                LIMIT ?
                """,
                batch_params,
            )
            target_row = conn.execute("SELECT COUNT(*) AS count FROM temp_log_cleanup_targets").fetchone()
            target_count = int(target_row["count"] or 0)
            if target_count <= 0:
                batch_deleted = 0
            else:
                archive_rows = conn.execute(
                    """
                    SELECT DISTINCT r.archive_id
                    FROM usage_log_payload_archive_refs r
                    JOIN temp_log_cleanup_targets t ON t.log_id = r.log_id
                    """
                ).fetchall()
                archive_ids.update(int(row["archive_id"]) for row in archive_rows)
                cursor = conn.execute(
                    """
                    DELETE FROM usage_logs
                    WHERE id IN (SELECT log_id FROM temp_log_cleanup_targets)
                    """
                )
                batch_deleted = int(cursor.rowcount)
            conn.execute("DELETE FROM temp_log_cleanup_targets")
        if batch_deleted <= 0:
            break
        deleted_logs += batch_deleted
    compact_result = payload_archive.compact_archives(archive_ids)
    return {
        "ok": True,
        "deleted_logs": deleted_logs,
        "cutoff": cutoff,
        "statuses": valid_statuses,
        "compacted_archives": compact_result["compacted_archives"],
        "deleted_archives": compact_result["deleted_archives"],
    }


def _clear_payloads(
    request: Request,
    *,
    older_than_days: int,
    min_payload_kb: int,
    clear_output_files: bool,
    clear_image_urls: bool,
) -> dict:
    db: Database = request.app.state.db
    payload_archive: PayloadArchiveService = request.app.state.payload_archive_service
    cutoff = _cutoff_time(older_than_days)
    min_payload_bytes = min_payload_kb * 1024
    set_clause = (
        "request_payload = NULL, "
        "request_payload_encoding = 'json', "
        "request_payload_blob = NULL, "
        "request_payload_bytes = 0, "
        "request_payload_available_bytes = 0, "
        "request_payload_compressed_bytes = 0"
    )
    if clear_output_files:
        set_clause += ", output_files = NULL, output_files_bytes = 0"
    if clear_image_urls:
        set_clause += ", image_urls = NULL, image_urls_bytes = 0"
    size_terms = [REQUEST_PAYLOAD_ORIGINAL_BYTES_SQL]
    if clear_output_files:
        size_terms.append("l.output_files_bytes")
    if clear_image_urls:
        size_terms.append("l.image_urls_bytes")
    size_filter = " + ".join(size_terms)
    with db.transaction() as conn:
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS temp_payload_clear_targets (
                log_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute("DELETE FROM temp_payload_clear_targets")
        conn.execute(
            f"""
            INSERT OR IGNORE INTO temp_payload_clear_targets (log_id)
            SELECT l.id
            FROM usage_logs l
            WHERE l.created_at < ?
              AND ({size_filter}) >= ?
            """,
            (cutoff, min_payload_bytes),
        )
        target_row = conn.execute("SELECT COUNT(*) AS count FROM temp_payload_clear_targets").fetchone()
        updated_logs = int(target_row["count"] or 0)
        archived_row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM usage_log_payload_archive_refs r
            JOIN temp_payload_clear_targets t ON t.log_id = r.log_id
            """
        ).fetchone()
        purged_archived_payloads = int(archived_row["count"] or 0)
        archive_rows = conn.execute(
            """
            SELECT DISTINCT r.archive_id
            FROM usage_log_payload_archive_refs r
            JOIN temp_payload_clear_targets t ON t.log_id = r.log_id
            """
        ).fetchall()
        archive_ids = [int(row["archive_id"]) for row in archive_rows]
        if updated_logs:
            conn.execute(
                f"""
                UPDATE usage_logs
                SET {set_clause}
                WHERE id IN (SELECT log_id FROM temp_payload_clear_targets)
                """
            )
            conn.execute(
                """
                DELETE FROM usage_log_payload_archive_refs
                WHERE log_id IN (SELECT log_id FROM temp_payload_clear_targets)
                """
            )
        conn.execute("DELETE FROM temp_payload_clear_targets")
    compact_result = payload_archive.compact_archives(archive_ids)
    return {
        "ok": True,
        "updated_logs": updated_logs,
        "cutoff": cutoff,
        "min_payload_bytes": min_payload_bytes,
        "clear_output_files": clear_output_files,
        "clear_image_urls": clear_image_urls,
        "purged_archived_payloads": purged_archived_payloads,
        "compacted_archives": compact_result["compacted_archives"],
        "deleted_archives": compact_result["deleted_archives"],
    }


def _vacuum_database(request: Request) -> dict:
    db: Database = request.app.state.db
    return db.vacuum()


def _payload_archive_stats(request: Request) -> dict:
    db: Database = request.app.state.db
    config = request.app.state.config.database.payload_archive
    service: PayloadArchiveService = request.app.state.payload_archive_service
    row = db.query_one(
        """
        SELECT COUNT(*) AS archive_count,
               COALESCE(SUM(payload_count), 0) AS payload_count,
               COALESCE(SUM(raw_bytes), 0) AS raw_bytes,
               COALESCE(SUM(compressed_bytes), 0) AS compressed_bytes
        FROM usage_log_payload_archives
        """
    )
    archive_count = int(row["archive_count"] or 0)
    payload_count = int(row["payload_count"] or 0)
    raw_bytes = int(row["raw_bytes"] or 0)
    compressed_bytes = int(row["compressed_bytes"] or 0)
    compression_ratio = (compressed_bytes / raw_bytes) if raw_bytes else None
    return {
        "enabled": bool(config.enabled),
        "hot_days": int(config.hot_days),
        "archive_count": archive_count,
        "payload_count": payload_count,
        "raw_bytes": raw_bytes,
        "compressed_bytes": compressed_bytes,
        "raw_display": format_bytes(raw_bytes),
        "compressed_display": format_bytes(compressed_bytes),
        "compression_ratio": compression_ratio,
        "compression_ratio_display": "-" if compression_ratio is None else f"{compression_ratio:.1%}",
        "candidate_count": service.count_archive_candidates(hot_days=config.hot_days),
    }


def _cutoff_time(older_than_days: int) -> str:
    days = max(0, min(int(older_than_days), 3650))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _valid_statuses(statuses: list[str]) -> list[str]:
    allowed = {"success", "failed", "rejected", "queued", "running"}
    valid = []
    for status in statuses:
        if status in allowed and status not in valid:
            valid.append(status)
    return valid


def _database_file_sizes(path: Path) -> dict[str, dict]:
    files = {
        "main": path,
        "wal": Path(f"{path}-wal"),
        "shm": Path(f"{path}-shm"),
    }
    return {
        key: {
            "path": str(file_path),
            "bytes": file_path.stat().st_size if file_path.exists() else 0,
            "display": format_bytes(file_path.stat().st_size if file_path.exists() else 0),
        }
        for key, file_path in files.items()
    }


def _usage_log_size_to_dict(row):
    data = row_to_dict(row)
    request_payload_bytes = int(data["request_payload_bytes"] or 0)
    output_files_bytes = int(data["output_files_bytes"] or 0)
    image_urls_bytes = int(data["image_urls_bytes"] or 0)
    data["created_at_display"] = format_display_time(data.get("created_at"))
    data["request_payload_display"] = format_bytes(request_payload_bytes)
    data["output_files_display"] = format_bytes(output_files_bytes)
    data["image_urls_display"] = format_bytes(image_urls_bytes)
    data["total_bytes"] = request_payload_bytes + output_files_bytes + image_urls_bytes
    data["total_display"] = format_bytes(data["total_bytes"])
    return data
