from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlite3 import Row
from typing import Any

from .database import Database, utc_now_iso
from .logging_utils import json_dumps, logger
from .timezones import DISPLAY_TIMEZONE


class PayloadArchiveError(RuntimeError):
    """Raised when an archived payload cannot be read safely."""


class PayloadNotFoundError(LookupError):
    """Raised when a log does not have a hot or archived request payload."""


@dataclass(frozen=True)
class PayloadArchiveSettings:
    hot_days: int = 7
    compression: str = "zlib"
    compression_level: int = 6
    max_payloads_per_part: int = 1000
    max_raw_bytes_per_part: int = 33554432
    max_rows_per_run: int = 10000


@dataclass(frozen=True)
class ArchiveCandidate:
    log_id: int
    created_at: str
    request_payload: str
    archive_date: str
    payload_bytes: int


class PayloadArchiveService:
    def __init__(self, db: Database):
        self.db = db

    def archive_due_payloads(
        self,
        *,
        now: datetime | None = None,
        hot_days: int = 7,
        compression: str = "zlib",
        compression_level: int = 6,
        max_payloads_per_part: int = 1000,
        max_raw_bytes_per_part: int = 33554432,
        max_rows_per_run: int = 10000,
    ) -> dict[str, Any]:
        settings = PayloadArchiveSettings(
            hot_days=max(0, int(hot_days)),
            compression=compression,
            compression_level=max(0, min(int(compression_level), 9)),
            max_payloads_per_part=max(1, int(max_payloads_per_part)),
            max_raw_bytes_per_part=max(1024, int(max_raw_bytes_per_part)),
            max_rows_per_run=max(1, int(max_rows_per_run)),
        )
        if settings.compression != "zlib":
            raise ValueError(f"Unsupported payload archive compression: {settings.compression}")

        cutoff = self.archive_cutoff_utc(now=now, hot_days=settings.hot_days)
        candidates = self._load_candidates(cutoff, settings.max_rows_per_run)
        parts = self._build_parts(candidates, settings)

        archived_parts = 0
        archived_payloads = 0
        raw_bytes = 0
        compressed_bytes = 0
        archive_ids: list[int] = []
        for part in parts:
            result = self._archive_part(part, settings)
            if result["payload_count"] == 0:
                continue
            archived_parts += 1
            archived_payloads += int(result["payload_count"])
            raw_bytes += int(result["raw_bytes"])
            compressed_bytes += int(result["compressed_bytes"])
            archive_ids.append(int(result["archive_id"]))

        return {
            "ok": True,
            "cutoff": cutoff,
            "archived_parts": archived_parts,
            "archived_payloads": archived_payloads,
            "raw_bytes": raw_bytes,
            "compressed_bytes": compressed_bytes,
            "archive_ids": archive_ids,
        }

    def get_payload_text(self, log_id: int) -> str:
        row = self.db.query_one(
            """
            SELECT l.request_payload,
                   l.request_payload_encoding,
                   l.request_payload_blob,
                   l.request_payload_bytes,
                   l.request_payload_compressed_bytes,
                   r.payload_key,
                   a.id AS archive_id,
                   a.compression,
                   a.payload_sha256,
                   a.payload_blob
            FROM usage_logs l
            LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
            LEFT JOIN usage_log_payload_archives a ON a.id = r.archive_id
            WHERE l.id = ?
            """,
            (int(log_id),),
        )
        if row is None:
            raise PayloadNotFoundError("Log not found")
        hot_payload = self._decode_hot_payload(row)
        if hot_payload is not None:
            return hot_payload
        if row["archive_id"] is None:
            raise PayloadNotFoundError("This log has no request payload")
        payloads = self._decode_archive_payloads(row)
        payload_key = str(row["payload_key"])
        payload = payloads.get(payload_key)
        if payload is None:
            raise PayloadArchiveError(f"Archived payload key is missing: {payload_key}")
        if not isinstance(payload, str):
            raise PayloadArchiveError("Archived payload is not a string")
        return payload

    def get_payload_dict(self, log_id: int) -> dict[str, Any]:
        text = self.get_payload_text(log_id)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("This log has no replayable request payload") from exc
        if not isinstance(payload, dict):
            raise ValueError("This log has no replayable request payload")
        return payload

    def compact_archives(self, archive_ids: list[int] | set[int] | tuple[int, ...]) -> dict[str, Any]:
        unique_ids = sorted({int(archive_id) for archive_id in archive_ids if int(archive_id) > 0})
        compacted = 0
        deleted = 0
        raw_bytes = 0
        compressed_bytes = 0
        for archive_id in unique_ids:
            result = self._compact_archive(archive_id)
            compacted += int(result["compacted"])
            deleted += int(result["deleted"])
            raw_bytes += int(result["raw_bytes"])
            compressed_bytes += int(result["compressed_bytes"])
        return {
            "ok": True,
            "compacted_archives": compacted,
            "deleted_archives": deleted,
            "raw_bytes": raw_bytes,
            "compressed_bytes": compressed_bytes,
        }

    def count_archive_candidates(self, *, now: datetime | None = None, hot_days: int = 7) -> int:
        cutoff = self.archive_cutoff_utc(now=now, hot_days=hot_days)
        row = self.db.query_one(
            """
            SELECT COUNT(*) AS count
            FROM usage_logs l
            LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
            WHERE l.created_at < ?
              AND l.request_payload_bytes > 0
              AND r.log_id IS NULL
            """,
            (cutoff,),
        )
        return int(row["count"] or 0) if row is not None else 0

    @staticmethod
    def archive_cutoff_utc(*, now: datetime | None = None, hot_days: int = 7) -> str:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff_date = (now.astimezone(DISPLAY_TIMEZONE) - timedelta(days=max(0, int(hot_days)))).date()
        cutoff_local = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=DISPLAY_TIMEZONE)
        return cutoff_local.astimezone(timezone.utc).isoformat()

    def _load_candidates(self, cutoff: str, max_rows: int) -> list[ArchiveCandidate]:
        rows = self.db.query_all(
            """
            SELECT l.id, l.created_at, l.request_payload, l.request_payload_encoding,
                   l.request_payload_blob, l.request_payload_bytes, l.request_payload_compressed_bytes
            FROM usage_logs l
            LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
            WHERE l.created_at < ?
              AND l.request_payload_bytes > 0
              AND r.log_id IS NULL
            ORDER BY l.created_at ASC, l.id ASC
            LIMIT ?
            """,
            (cutoff, max_rows),
        )
        candidates: list[ArchiveCandidate] = []
        for row in rows:
            archive_date = self._archive_date_for_created_at(str(row["created_at"]))
            if archive_date is None:
                logger.warning("skip payload archive candidate with invalid created_at log_id=%s", row["id"])
                continue
            payload = self._decode_hot_payload(row)
            if payload is None:
                continue
            candidates.append(
                ArchiveCandidate(
                    log_id=int(row["id"]),
                    created_at=str(row["created_at"]),
                    request_payload=payload,
                    archive_date=archive_date,
                    payload_bytes=len(payload.encode("utf-8")),
                )
            )
        return candidates

    def _build_parts(
        self,
        candidates: list[ArchiveCandidate],
        settings: PayloadArchiveSettings,
    ) -> list[list[ArchiveCandidate]]:
        parts: list[list[ArchiveCandidate]] = []
        current: list[ArchiveCandidate] = []
        current_date: str | None = None
        current_bytes = 0

        for candidate in candidates:
            must_flush = False
            if current and candidate.archive_date != current_date:
                must_flush = True
            if current and len(current) >= settings.max_payloads_per_part:
                must_flush = True
            if current and current_bytes + candidate.payload_bytes > settings.max_raw_bytes_per_part:
                must_flush = True
            if must_flush:
                parts.append(current)
                current = []
                current_bytes = 0

            current.append(candidate)
            current_date = candidate.archive_date
            current_bytes += candidate.payload_bytes

        if current:
            parts.append(current)
        return parts

    def _archive_part(self, part: list[ArchiveCandidate], settings: PayloadArchiveSettings) -> dict[str, int]:
        ids = [candidate.log_id for candidate in part]
        if not ids:
            return {"archive_id": 0, "payload_count": 0, "raw_bytes": 0, "compressed_bytes": 0}
        placeholders = ",".join("?" for _ in ids)
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT l.id, l.request_payload, l.request_payload_encoding,
                       l.request_payload_blob, l.request_payload_bytes, l.request_payload_compressed_bytes
                FROM usage_logs l
                LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
                WHERE l.id IN ({placeholders})
                  AND l.request_payload_bytes > 0
                  AND r.log_id IS NULL
                ORDER BY l.created_at ASC, l.id ASC
                """,
                tuple(ids),
            ).fetchall()
            if not rows:
                return {"archive_id": 0, "payload_count": 0, "raw_bytes": 0, "compressed_bytes": 0}

            source_by_id = {candidate.log_id: candidate for candidate in part}
            decoded_rows: list[tuple[int, str]] = []
            for row in rows:
                payload = self._decode_hot_payload(row)
                if payload is not None:
                    decoded_rows.append((int(row["id"]), payload))
            if not decoded_rows:
                return {"archive_id": 0, "payload_count": 0, "raw_bytes": 0, "compressed_bytes": 0}

            archive_date = source_by_id[decoded_rows[0][0]].archive_date
            payloads = {str(log_id): payload for log_id, payload in decoded_rows}
            raw = json_dumps({"version": 1, "payloads": payloads}).encode("utf-8")
            compressed = zlib.compress(raw, settings.compression_level)
            checksum = hashlib.sha256(raw).hexdigest()
            next_part_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(part_number), 0) + 1 FROM usage_log_payload_archives WHERE archive_date = ?",
                    (archive_date,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO usage_log_payload_archives (
                    archive_date, part_number, compression, compression_level, payload_count,
                    raw_bytes, compressed_bytes, payload_sha256, payload_blob, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive_date,
                    next_part_number,
                    settings.compression,
                    settings.compression_level,
                    len(payloads),
                    len(raw),
                    len(compressed),
                    checksum,
                    compressed,
                    utc_now_iso(),
                ),
            )
            archive_id = int(cursor.lastrowid)
            ref_rows = [
                (log_id, archive_id, str(log_id), len(payload.encode("utf-8")))
                for log_id, payload in decoded_rows
            ]
            conn.executemany(
                """
                INSERT INTO usage_log_payload_archive_refs (log_id, archive_id, payload_key, payload_bytes)
                VALUES (?, ?, ?, ?)
                """,
                ref_rows,
            )
            row_ids = tuple(log_id for log_id, _payload in decoded_rows)
            row_placeholders = ",".join("?" for _ in row_ids)
            conn.execute(
                f"""
                UPDATE usage_logs
                SET request_payload = NULL,
                    request_payload_encoding = 'json',
                    request_payload_blob = NULL,
                    request_payload_bytes = 0,
                    request_payload_compressed_bytes = 0
                WHERE id IN ({row_placeholders})
                """,
                row_ids,
            )
            return {
                "archive_id": archive_id,
                "payload_count": len(payloads),
                "raw_bytes": len(raw),
                "compressed_bytes": len(compressed),
            }

    def _compact_archive(self, archive_id: int) -> dict[str, int]:
        with self.db.transaction() as conn:
            archive = conn.execute(
                "SELECT * FROM usage_log_payload_archives WHERE id = ?",
                (archive_id,),
            ).fetchone()
            if archive is None:
                return {"compacted": 0, "deleted": 0, "raw_bytes": 0, "compressed_bytes": 0}
            refs = conn.execute(
                """
                SELECT log_id, payload_key, payload_bytes
                FROM usage_log_payload_archive_refs
                WHERE archive_id = ?
                ORDER BY log_id
                """,
                (archive_id,),
            ).fetchall()
            if not refs:
                conn.execute("DELETE FROM usage_log_payload_archives WHERE id = ?", (archive_id,))
                return {"compacted": 0, "deleted": 1, "raw_bytes": 0, "compressed_bytes": 0}

            payloads = self._decode_archive_payloads(archive)
            kept: dict[str, str] = {}
            for ref in refs:
                key = str(ref["payload_key"])
                payload = payloads.get(key)
                if not isinstance(payload, str):
                    raise PayloadArchiveError(f"Archived payload key is missing during compact: {key}")
                kept[key] = payload

            raw = json_dumps({"version": 1, "payloads": kept}).encode("utf-8")
            compressed = zlib.compress(raw, int(archive["compression_level"]))
            conn.execute(
                """
                UPDATE usage_log_payload_archives
                SET payload_count = ?,
                    raw_bytes = ?,
                    compressed_bytes = ?,
                    payload_sha256 = ?,
                    payload_blob = ?
                WHERE id = ?
                """,
                (len(kept), len(raw), len(compressed), hashlib.sha256(raw).hexdigest(), compressed, archive_id),
            )
            return {"compacted": 1, "deleted": 0, "raw_bytes": len(raw), "compressed_bytes": len(compressed)}

    def _decode_hot_payload(self, row: Row) -> str | None:
        encoding = str(row["request_payload_encoding"] or "json")
        request_payload = row["request_payload"]
        blob = row["request_payload_blob"]
        has_text = request_payload is not None and str(request_payload) != ""
        has_blob = blob is not None and len(bytes(blob)) > 0

        if encoding == "json":
            if has_text:
                return str(request_payload)
            if has_blob:
                raise PayloadArchiveError("Hot payload blob is marked as json")
            return None
        if encoding == "zlib":
            if has_text:
                raise PayloadArchiveError("Hot payload has zlib encoding but text storage is populated")
            if not has_blob:
                return None
            try:
                raw = zlib.decompress(bytes(blob))
            except zlib.error as exc:
                raise PayloadArchiveError("Hot payload blob cannot be decompressed") from exc
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PayloadArchiveError("Hot payload blob is not valid UTF-8") from exc
        raise PayloadArchiveError(f"Unsupported hot payload encoding: {encoding}")

    def _decode_archive_payloads(self, archive: Row) -> dict[str, Any]:
        if archive["compression"] != "zlib":
            raise PayloadArchiveError(f"Unsupported archived payload compression: {archive['compression']}")
        try:
            raw = zlib.decompress(bytes(archive["payload_blob"]))
        except zlib.error as exc:
            raise PayloadArchiveError("Archived payload blob cannot be decompressed") from exc
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != archive["payload_sha256"]:
            raise PayloadArchiveError("Archived payload checksum mismatch")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PayloadArchiveError("Archived payload blob is not valid JSON") from exc
        if not isinstance(decoded, dict) or decoded.get("version") != 1:
            raise PayloadArchiveError("Unsupported archived payload format")
        payloads = decoded.get("payloads")
        if not isinstance(payloads, dict):
            raise PayloadArchiveError("Archived payloads map is missing")
        return payloads

    @staticmethod
    def _archive_date_for_created_at(value: str) -> str | None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(DISPLAY_TIMEZONE).date().isoformat()
