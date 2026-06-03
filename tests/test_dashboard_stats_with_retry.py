"""测试仪表盘统计在有重试记录时的正确性"""
import sys
sys.path.insert(0, '.')

from datetime import datetime, timedelta
from app.database import Database
from app.usage_logs import UsageLogRepository, UsageLogCreate
from app.admin.common import local_day_range, to_utc_iso, DISPLAY_TIMEZONE


def test_dashboard_stats_count_distinct_requests():
    """验证仪表盘统计使用 COUNT(DISTINCT request_id)，不会重复计数重试记录"""
    db = Database(':memory:')
    db.init_schema()
    repo = UsageLogRepository(db)

    # 插入测试用户
    db.execute('INSERT INTO users (api_key_hash, name, created_at) VALUES (?, ?, ?)',
               ('test_hash', 'test_user', '2026-06-03T00:00:00'))

    # 场景1：一个请求，一次成功（无重试）
    log1 = UsageLogCreate(
        request_id='req-001',
        user_id=1,
        action='generate-image',
        estimated_anlas_cost=10,
        model='nai-diffusion-3',
        width=832,
        height=1216,
        steps=28,
        n_samples=1,
    )
    repo.insert_queued(log1, attempt_number=0)
    repo.mark_running('req-001', queued_ms=50, upstream_id='upstream-A', attempt_number=0)
    repo.mark_success('req-001', queued_ms=100, final_cost=10, output_files=[], is_retry_success=False, attempt_number=0)

    # 场景2：一个请求，重试2次后成功（3条记录）
    log2 = UsageLogCreate(
        request_id='req-002',
        user_id=1,
        action='generate-image',
        estimated_anlas_cost=10,
        model='nai-diffusion-3',
        width=832,
        height=1216,
        steps=28,
        n_samples=1,
    )
    # 第一次尝试 - 429失败
    repo.insert_queued(log2, attempt_number=0)
    repo.mark_running('req-002', queued_ms=50, upstream_id='upstream-A', attempt_number=0)
    repo.mark_failed('req-002', queued_ms=100, error_code='429', error_message='Rate limit', attempt_number=0)

    # 第二次尝试 - 429失败
    repo.insert_retry_attempt(request_id='req-002', attempt_number=1, upstream_id='upstream-B')
    repo.mark_failed('req-002', queued_ms=150, error_code='429', error_message='Rate limit', attempt_number=1)

    # 第三次尝试 - 成功
    repo.insert_retry_attempt(request_id='req-002', attempt_number=2, upstream_id='upstream-C')
    repo.mark_success('req-002', queued_ms=200, final_cost=10, output_files=[], is_retry_success=True, attempt_number=2)

    # 验证数据库记录数
    total_records = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs')['cnt']
    print(f'\n总数据库记录数: {total_records}')
    assert total_records == 4, f"应该有4条记录（1个无重试 + 3个有重试），实际有 {total_records} 条"

    # 模拟仪表盘的今日请求数统计（使用 COUNT(DISTINCT request_id)）
    today_start, today_end = local_day_range(datetime.now(DISPLAY_TIMEZONE))
    today_requests = db.query_one(
        """
        SELECT COUNT(DISTINCT request_id) AS c
        FROM usage_logs
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        """,
        (to_utc_iso(today_start), to_utc_iso(today_end)),
    )["c"]

    print(f'今日请求数（COUNT DISTINCT request_id）: {today_requests}')
    assert today_requests == 2, f"应该统计为2个请求，实际为 {today_requests} 个"

    # 验证失败次数（应该是2次，因为req-002有2次失败尝试）
    failed_count = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs WHERE status = "failed"')['cnt']
    print(f'失败次数（COUNT *）: {failed_count}')
    assert failed_count == 2, f"应该有2次失败，实际有 {failed_count} 次"

    # 验证成功次数（应该是2次，因为有2个请求最终成功）
    success_count = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs WHERE status = "success"')['cnt']
    print(f'成功次数（COUNT *）: {success_count}')
    assert success_count == 2, f"应该有2次成功，实际有 {success_count} 次"

    # 验证重试成功标记
    retry_success_count = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs WHERE status = "success" AND is_retry_success = 1')['cnt']
    print(f'重试成功次数（is_retry_success=1）: {retry_success_count}')
    assert retry_success_count == 1, f"应该有1次重试成功，实际有 {retry_success_count} 次"

    # 模拟趋势图统计（按小时分组）
    trend_stats = db.query_all(
        """
        SELECT CAST(strftime('%H', datetime(created_at, '+8 hours')) AS INTEGER) AS bucket,
               COUNT(DISTINCT request_id) AS requests,
               SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END) AS rejected
        FROM usage_logs
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        GROUP BY bucket
        """,
        (to_utc_iso(today_start), to_utc_iso(today_end)),
    )

    print(f'\n趋势图统计: {trend_stats}')
    if trend_stats:
        total_requests_in_trend = sum(row['requests'] for row in trend_stats)
        total_failed_in_trend = sum(row['failed'] for row in trend_stats)
        print(f'趋势图中的总请求数: {total_requests_in_trend}')
        print(f'趋势图中的总失败数: {total_failed_in_trend}')
        assert total_requests_in_trend == 2, f"趋势图应该显示2个请求，实际显示 {total_requests_in_trend} 个"
        assert total_failed_in_trend == 2, f"趋势图应该显示2次失败，实际显示 {total_failed_in_trend} 次"

    print('\n[PASS] 所有仪表盘统计测试通过！')


if __name__ == '__main__':
    test_dashboard_stats_count_distinct_requests()
