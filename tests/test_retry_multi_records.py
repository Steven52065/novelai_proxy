"""测试 429 重试后数据库中会有多条记录"""
import sys
sys.path.insert(0, '.')

from app.database import Database
from app.usage_logs import UsageLogRepository, UsageLogCreate


def test_retry_creates_multiple_records():
    """验证 429 重试会创建多条数据库记录，而不是更新同一条记录"""
    # 创建测试数据库
    db = Database(':memory:')
    db.init_schema()
    repo = UsageLogRepository(db)

    # 插入测试用户
    db.execute('INSERT INTO users (api_key_hash, name, created_at) VALUES (?, ?, ?)',
               ('test_hash', 'test_user', '2026-06-03T00:00:00'))

    # 模拟一个请求的完整重试流程
    request_id = 'test-429-retry-request'

    # 1. 初始请求入队（attempt_number=0）
    log = UsageLogCreate(
        request_id=request_id,
        user_id=1,
        action='generate-image',
        estimated_anlas_cost=10,
        model='nai-diffusion-3',
        width=832,
        height=1216,
        steps=28,
        n_samples=1,
    )
    repo.insert_queued(log, attempt_number=0)

    # 2. 第一次尝试（upstream A）- 429 失败
    repo.mark_running(request_id, queued_ms=100, upstream_id='upstream-A', attempt_number=0)
    repo.mark_failed(request_id, queued_ms=150, error_code='429', error_message='Rate limit exceeded', attempt_number=0)

    # 3. 第二次尝试（upstream B）- 插入新记录（attempt_number=1）
    repo.insert_retry_attempt(log, attempt_number=1, upstream_id='upstream-B')
    repo.mark_failed(request_id, queued_ms=200, error_code='429', error_message='Rate limit exceeded', attempt_number=1)

    # 4. 第三次尝试（upstream C）- 插入新记录（attempt_number=2）并成功
    repo.insert_retry_attempt(log, attempt_number=2, upstream_id='upstream-C')
    repo.mark_success(request_id, queued_ms=250, final_cost=10, output_files=[], is_retry_success=True, attempt_number=2)

    # 验证结果
    rows = db.query_all('SELECT request_id, attempt_number, status, error_code, upstream_id, is_retry_success FROM usage_logs ORDER BY attempt_number')

    print(f'\n总记录数: {len(rows)}')
    assert len(rows) == 3, f"应该有 3 条记录，实际有 {len(rows)} 条"

    # 验证第一条记录（attempt_number=0，失败）
    row0 = rows[0]
    print(f'\n记录 1: attempt_number={row0["attempt_number"]}, status={row0["status"]}, error_code={row0["error_code"]}, upstream_id={row0["upstream_id"]}')
    assert row0['request_id'] == request_id
    assert row0['attempt_number'] == 0
    assert row0['status'] == 'failed'
    assert row0['error_code'] == '429'
    assert row0['upstream_id'] == 'upstream-A'

    # 验证第二条记录（attempt_number=1，失败）
    row1 = rows[1]
    print(f'记录 2: attempt_number={row1["attempt_number"]}, status={row1["status"]}, error_code={row1["error_code"]}, upstream_id={row1["upstream_id"]}')
    assert row1['request_id'] == request_id
    assert row1['attempt_number'] == 1
    assert row1['status'] == 'failed'
    assert row1['error_code'] == '429'
    assert row1['upstream_id'] == 'upstream-B'

    # 验证第三条记录（attempt_number=2，成功）
    row2 = rows[2]
    print(f'记录 3: attempt_number={row2["attempt_number"]}, status={row2["status"]}, error_code={row2["error_code"]}, upstream_id={row2["upstream_id"]}, is_retry_success={row2["is_retry_success"]}')
    assert row2['request_id'] == request_id
    assert row2['attempt_number'] == 2
    assert row2['status'] == 'success'
    assert row2['error_code'] is None  # 成功记录应该清空 error_code
    assert row2['upstream_id'] == 'upstream-C'
    assert row2['is_retry_success'] == 1

    # 验证统计查询
    # 请求次数（按不同的 request_id 计数）
    request_count = db.query_one('SELECT COUNT(DISTINCT request_id) as cnt FROM usage_logs')[0]
    print(f'\n请求次数（DISTINCT request_id）: {request_count}')
    assert request_count == 1, "应该只有 1 个不同的 request_id"

    # 失败次数（按记录数计数）
    failed_count = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs WHERE status = "failed"')[0]
    print(f'失败次数（status=failed 的记录数）: {failed_count}')
    assert failed_count == 2, "应该有 2 条失败记录"

    # 成功次数（按记录数计数）
    success_count = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs WHERE status = "success"')[0]
    print(f'成功次数（status=success 的记录数）: {success_count}')
    assert success_count == 1, "应该有 1 条成功记录"

    # 重试成功次数
    retry_success_count = db.query_one('SELECT COUNT(*) as cnt FROM usage_logs WHERE status = "success" AND is_retry_success = 1')[0]
    print(f'重试成功次数（is_retry_success=1）: {retry_success_count}')
    assert retry_success_count == 1, "应该有 1 条重试成功记录"

    print('\n[PASS] 所有测试通过！')


if __name__ == '__main__':
    test_retry_creates_multiple_records()
