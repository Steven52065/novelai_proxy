"""仪表盘小时统计（dashboard_hourly_*）的触发器/重建 SQL 生成与小时桶格式契约。

dashboard_hourly_stats / dashboard_hourly_request_refs 是 usage_logs 的派生缓存，
由触发器随日志的 INSERT/UPDATE/DELETE 同步增减：

- 每个小时桶按 {__all__ 汇总, 按上游} 两个维度各记一行；
- 同一 request_id 在同一桶内的多条日志（重试），依靠 refs 表的引用计数
  只计一次 request_count；
- 删除日志会同步回退统计——清理历史日志即缩减仪表盘历史曲线（有意保留的语义），
  必要时可用重建脚本从现存日志全量重算两表。

SQL 触发器与 Python 查询侧（admin/dashboard.py）共享 BUCKET_HOUR_FORMAT /
hour_bucket()，保证两侧产出的 bucket_hour 逐字符一致。
"""

from __future__ import annotations

from datetime import datetime

from .timezones import DISPLAY_TIMEZONE, TZ_OFFSET_HOURS

# 汇总维度的保留上游 id，真实上游不得使用该名字。
ALL_UPSTREAMS = "__all__"

# bucket_hour 列的格式契约。仅使用 %Y %m %d %H 指令，Python strftime 与
# SQLite strftime 对它们的行为一致，可直接共用同一格式串。
BUCKET_HOUR_FORMAT = f"%Y-%m-%dT%H:00:00+{TZ_OFFSET_HOURS:02d}:00"

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

# request_count 需按 request_id 去重（重试不重复计数），与按日志行直接累加的
# 状态列分开处理。
_STATUS_COLUMNS = (
    "success_count",
    "failed_count",
    "rejected_count",
    "retry_success_count",
    "anlas_cost",
)
_COUNTER_COLUMNS = ("request_count", *_STATUS_COLUMNS)
_STATS_INSERT_COLUMNS = f"bucket_hour, upstream_id, {', '.join(_COUNTER_COLUMNS)}, updated_at"


def hour_bucket(value: datetime) -> str:
    """Python 侧生成小时桶字符串，与触发器写入的 bucket_hour 逐字符一致。"""
    local = value.astimezone(DISPLAY_TIMEZONE).replace(minute=0, second=0, microsecond=0)
    return local.strftime(BUCKET_HOUR_FORMAT)


def sql_hour_bucket(column_expr: str) -> str:
    """SQL 侧生成小时桶表达式，结果与 hour_bucket() 逐字符一致。"""
    return f"strftime('{BUCKET_HOUR_FORMAT}', datetime({column_expr}, '{TZ_OFFSET_HOURS:+d} hours'))"


def dashboard_triggers_script() -> str:
    """生成 usage_logs → dashboard_hourly_* 的同步触发器脚本（先 DROP 再 CREATE，可重复执行）。"""
    apply_blocks = "".join(_apply_block(upstream, guard) for upstream, guard in _dimensions("NEW."))
    retract_blocks = "".join(_retract_block(upstream, guard) for upstream, guard in _dimensions("OLD."))
    return f"""
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_insert;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update_old;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update_new;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_delete;

CREATE TRIGGER trg_usage_logs_dashboard_insert
AFTER INSERT ON usage_logs
BEGIN
{apply_blocks}
END;

CREATE TRIGGER trg_usage_logs_dashboard_update
AFTER UPDATE OF created_at, request_id, upstream_id, status, is_retry_success, final_anlas_cost ON usage_logs
BEGIN
{retract_blocks}
{apply_blocks}
END;

CREATE TRIGGER trg_usage_logs_dashboard_delete
AFTER DELETE ON usage_logs
BEGIN
{retract_blocks}
END;
"""


def rebuild_dashboard_hourly_stats_script() -> str:
    """生成从 usage_logs 全量重建 dashboard_hourly_* 两表的脚本。"""
    bucket = sql_hour_bucket("created_at")
    deltas = _stat_deltas("")
    sums = ",\n       ".join(f"SUM({deltas[column]})" for column in _STATUS_COLUMNS)
    upstream_present = "upstream_id IS NOT NULL AND upstream_id != ''"
    return f"""
DELETE FROM dashboard_hourly_stats;
DELETE FROM dashboard_hourly_request_refs;

INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
SELECT {bucket}, '{ALL_UPSTREAMS}', request_id, COUNT(*)
FROM usage_logs
GROUP BY {bucket}, request_id;

INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
SELECT {bucket}, upstream_id, request_id, COUNT(*)
FROM usage_logs
WHERE {upstream_present}
GROUP BY {bucket}, upstream_id, request_id;

INSERT INTO dashboard_hourly_stats ({_STATS_INSERT_COLUMNS})
SELECT {bucket},
       '{ALL_UPSTREAMS}',
       COUNT(DISTINCT request_id),
       {sums},
       {_NOW_UTC}
FROM usage_logs
GROUP BY {bucket};

INSERT INTO dashboard_hourly_stats ({_STATS_INSERT_COLUMNS})
SELECT {bucket},
       upstream_id,
       COUNT(DISTINCT request_id),
       {sums},
       {_NOW_UTC}
FROM usage_logs
WHERE {upstream_present}
GROUP BY {bucket}, upstream_id;
"""


def _dimensions(row: str) -> tuple[tuple[str, str | None], ...]:
    """统计的两个维度：(upstream_id 表达式, 行是否记入该维度的条件)。

    row 为触发器内的行引用前缀（"NEW." / "OLD."）。__all__ 汇总维度无条件记入；
    按上游维度仅在该行的 upstream_id 非空时记入。
    """
    return (
        (f"'{ALL_UPSTREAMS}'", None),
        (f"{row}upstream_id", f"{row}upstream_id IS NOT NULL AND {row}upstream_id != ''"),
    )


def _stat_deltas(row: str) -> dict[str, str]:
    """单条日志对各状态统计列的贡献值（row 为行引用前缀，重建脚本中为空串）。"""
    status = f"lower({row}status)"
    return {
        "success_count": f"CASE WHEN {status} = 'success' THEN 1 ELSE 0 END",
        "failed_count": f"CASE WHEN {status} = 'failed' THEN 1 ELSE 0 END",
        "rejected_count": f"CASE WHEN {status} = 'rejected' THEN 1 ELSE 0 END",
        "retry_success_count": f"CASE WHEN {status} = 'success' AND {row}is_retry_success = 1 THEN 1 ELSE 0 END",
        "anlas_cost": f"CASE WHEN {status} = 'success' THEN COALESCE({row}final_anlas_cost, 0) ELSE 0 END",
    }


def _apply_block(upstream: str, guard: str | None) -> str:
    """把 NEW 行记入指定维度：refs 引用计数 +1，stats 行 upsert 累加。"""
    bucket = sql_hour_bucket("NEW.created_at")
    deltas = _stat_deltas("NEW.")
    first_ref = f"""CASE WHEN (
            SELECT ref_count
            FROM dashboard_hourly_request_refs
            WHERE bucket_hour = {bucket}
              AND upstream_id = {upstream}
              AND request_id = NEW.request_id
        ) = 1 THEN 1 ELSE 0 END"""
    row_values = ",\n        ".join(
        [bucket, upstream, first_ref]
        + [deltas[column] for column in _STATUS_COLUMNS]
        + [_NOW_UTC]
    )
    # 带条件的维度用 INSERT ... SELECT ... WHERE 实现条件写入；无条件维度用
    # VALUES（SQLite 的 INSERT ... SELECT 搭配 ON CONFLICT 时必须带 WHERE 子句）。
    if guard is None:
        refs_source = f"VALUES ({bucket}, {upstream}, NEW.request_id, 1)"
        stats_source = f"VALUES (\n        {row_values}\n    )"
    else:
        refs_source = f"SELECT {bucket}, {upstream}, NEW.request_id, 1\n    WHERE {guard}"
        stats_source = f"SELECT\n        {row_values}\n    WHERE {guard}"
    accumulate = ",\n        ".join(
        [f"{column} = {column} + excluded.{column}" for column in _COUNTER_COLUMNS]
        + ["updated_at = excluded.updated_at"]
    )
    return f"""
    INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
    {refs_source}
    ON CONFLICT(bucket_hour, upstream_id, request_id)
    DO UPDATE SET ref_count = ref_count + 1;

    INSERT INTO dashboard_hourly_stats ({_STATS_INSERT_COLUMNS})
    {stats_source}
    ON CONFLICT(bucket_hour, upstream_id)
    DO UPDATE SET
        {accumulate};
"""


def _retract_block(upstream: str, guard: str | None) -> str:
    """把 OLD 行从指定维度回退：状态列扣减，refs 引用计数 -1，
    引用归零时 request_count -1 并清掉 refs 行。"""
    bucket = sql_hour_bucket("OLD.created_at")
    deltas = _stat_deltas("OLD.")
    guard_clause = f"\n      AND {guard}" if guard else ""
    decrements = ",\n        ".join(
        f"{column} = {column} - {deltas[column]}" for column in _STATUS_COLUMNS
    )
    return f"""
    UPDATE dashboard_hourly_stats
    SET {decrements},
        updated_at = {_NOW_UTC}
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}{guard_clause};

    UPDATE dashboard_hourly_request_refs
    SET ref_count = ref_count - 1
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}
      AND request_id = OLD.request_id{guard_clause};

    UPDATE dashboard_hourly_stats
    SET request_count = request_count - 1,
        updated_at = {_NOW_UTC}
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}{guard_clause}
      AND (
          SELECT ref_count
          FROM dashboard_hourly_request_refs
          WHERE bucket_hour = {bucket}
            AND upstream_id = {upstream}
            AND request_id = OLD.request_id
      ) = 0;

    DELETE FROM dashboard_hourly_request_refs
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}
      AND request_id = OLD.request_id{guard_clause}
      AND ref_count <= 0;
"""
