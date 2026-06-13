from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import Database
from ..templating import templates
from .auth import require_admin, require_admin_page_session
from .common import format_bytes, format_display_time, row_to_dict


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_page_session)])


class CleanupLogsRequest(BaseModel):
    older_than_days: int = Field(default=30, ge=0, le=3650)
    statuses: list[str] = Field(default_factory=list)


class ClearPayloadsRequest(BaseModel):
    older_than_days: int = Field(default=7, ge=0, le=3650)
    min_payload_kb: int = Field(default=128, ge=0, le=1024 * 1024)
    clear_output_files: bool = False
    clear_image_urls: bool = False


@api_router.get("/database/stats", dependencies=[Depends(require_admin)])
async def database_stats(request: Request):
    return _database_stats(request)


@api_router.post("/database/cleanup-logs", dependencies=[Depends(require_admin)])
async def cleanup_logs(payload: CleanupLogsRequest, request: Request):
    return _cleanup_logs(request, payload.older_than_days, payload.statuses)


@api_router.post("/database/clear-payloads", dependencies=[Depends(require_admin)])
async def clear_payloads(payload: ClearPayloadsRequest, request: Request):
    return _clear_payloads(
        request,
        older_than_days=payload.older_than_days,
        min_payload_kb=payload.min_payload_kb,
        clear_output_files=payload.clear_output_files,
        clear_image_urls=payload.clear_image_urls,
    )


@api_router.post("/database/vacuum", dependencies=[Depends(require_admin)])
async def vacuum_database(request: Request):
    return _vacuum_database(request)


@web_router.get("/database", response_class=HTMLResponse)
async def database_page(request: Request, message: str | None = None):
    return templates.TemplateResponse(
        request,
        "database.html",
        {
            "active": "database",
            "stats": _database_stats(request),
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
    result = _cleanup_logs(request, older_than_days, statuses or [])
    return RedirectResponse(f"/admin/database?message=已删除 {result['deleted_logs']} 条日志记录", status_code=303)


@web_router.post("/database/clear-payloads")
async def clear_payloads_form(
    request: Request,
    older_than_days: int = Form(7),
    min_payload_kb: int = Form(128),
    clear_output_files: str | None = Form(None),
    clear_image_urls: str | None = Form(None),
):
    result = _clear_payloads(
        request,
        older_than_days=older_than_days,
        min_payload_kb=min_payload_kb,
        clear_output_files=clear_output_files == "on",
        clear_image_urls=clear_image_urls == "on",
    )
    return RedirectResponse(f"/admin/database?message=已清空 {result['updated_logs']} 条日志的大字段", status_code=303)


@web_router.post("/database/vacuum")
async def vacuum_database_form(request: Request):
    result = _vacuum_database(request)
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
        """
        SELECT COUNT(*) AS total_logs,
               COALESCE(SUM(LENGTH(request_payload)), 0) AS request_payload_bytes,
               COALESCE(SUM(LENGTH(output_files)), 0) AS output_files_bytes,
               COALESCE(SUM(LENGTH(image_urls)), 0) AS image_urls_bytes,
               SUM(CASE WHEN request_payload IS NOT NULL AND request_payload != '' THEN 1 ELSE 0 END) AS logs_with_payload
        FROM usage_logs
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
        """
        SELECT l.id, l.request_id, l.action, l.status, l.created_at, u.name AS user_name,
               COALESCE(LENGTH(l.request_payload), 0) AS request_payload_bytes,
               COALESCE(LENGTH(l.output_files), 0) AS output_files_bytes,
               COALESCE(LENGTH(l.image_urls), 0) AS image_urls_bytes
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        ORDER BY request_payload_bytes DESC, output_files_bytes DESC, image_urls_bytes DESC
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
            "output_files_bytes": int(usage["output_files_bytes"] or 0),
            "image_urls_bytes": int(usage["image_urls_bytes"] or 0),
            "payload_bytes": total_payload_bytes,
        },
        "status_counts": [row_to_dict(row) for row in status_rows],
        "largest_logs": [_usage_log_size_to_dict(row) for row in largest_logs],
    }


def _cleanup_logs(request: Request, older_than_days: int, statuses: list[str]) -> dict:
    db: Database = request.app.state.db
    cutoff = _cutoff_time(older_than_days)
    valid_statuses = _valid_statuses(statuses)
    where = ["created_at < ?"]
    params: list[object] = [cutoff]
    if valid_statuses:
        where.append(f"status IN ({','.join('?' for _ in valid_statuses)})")
        params.extend(valid_statuses)
    cursor = db.execute(f"DELETE FROM usage_logs WHERE {' AND '.join(where)}", tuple(params))
    return {"ok": True, "deleted_logs": int(cursor.rowcount), "cutoff": cutoff, "statuses": valid_statuses}


def _clear_payloads(
    request: Request,
    *,
    older_than_days: int,
    min_payload_kb: int,
    clear_output_files: bool,
    clear_image_urls: bool,
) -> dict:
    db: Database = request.app.state.db
    cutoff = _cutoff_time(older_than_days)
    min_payload_bytes = min_payload_kb * 1024
    set_clause = "request_payload = NULL"
    if clear_output_files:
        set_clause += ", output_files = NULL"
    if clear_image_urls:
        set_clause += ", image_urls = NULL"
    size_terms = ["COALESCE(LENGTH(request_payload), 0)"]
    if clear_output_files:
        size_terms.append("COALESCE(LENGTH(output_files), 0)")
    if clear_image_urls:
        size_terms.append("COALESCE(LENGTH(image_urls), 0)")
    size_filter = " + ".join(size_terms)
    cursor = db.execute(
        f"""
        UPDATE usage_logs
        SET {set_clause}
        WHERE created_at < ?
          AND ({size_filter}) >= ?
        """,
        (cutoff, min_payload_bytes),
    )
    return {
        "ok": True,
        "updated_logs": int(cursor.rowcount),
        "cutoff": cutoff,
        "min_payload_bytes": min_payload_bytes,
        "clear_output_files": clear_output_files,
        "clear_image_urls": clear_image_urls,
    }


def _vacuum_database(request: Request) -> dict:
    db: Database = request.app.state.db
    before = _database_total_size(db.path)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = _database_total_size(db.path)
    return {"ok": True, "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(before - after, 0)}


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


def _database_total_size(path: Path) -> int:
    return sum(file_info["bytes"] for file_info in _database_file_sizes(path).values())


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
