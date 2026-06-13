"""测试 insert_retry_attempt 的重试记录复制行为"""
import sys
sys.path.insert(0, '.')

from app.database import Database
from app.usage_logs import UsageLogRepository, UsageLogCreate


def test_insert_retry_attempt_copies_initial_record():
    """测试从初始记录复制并插入重试记录"""
    db = Database(':memory:')
    db.init_schema()
    repo = UsageLogRepository(db)

    # 插入测试用户
    db.execute('INSERT INTO users (api_key_hash, name, created_at) VALUES (?, ?, ?)',
               ('test_hash', 'test_user', '2026-06-03T00:00:00'))

    log = UsageLogCreate(
        request_id='test-request',
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

    repo.insert_retry_attempt(request_id='test-request', attempt_number=1, upstream_id='upstream-B')

    # 验证记录
    row = db.query_one('SELECT * FROM usage_logs WHERE request_id = ? AND attempt_number = ?',
                       ('test-request', 1))
    assert row is not None
    assert row['status'] == 'running'
    assert row['upstream_id'] == 'upstream-B'
    assert row['user_id'] == 1
    assert row['action'] == 'generate-image'
    assert row['model'] == 'nai-diffusion-3'
    print('[PASS] 复制初始记录插入重试记录成功')


def test_insert_retry_attempt_from_database():
    """测试从数据库复制记录插入重试记录（实际重试场景）"""
    db = Database(':memory:')
    db.init_schema()
    repo = UsageLogRepository(db)

    # 插入测试用户
    db.execute('INSERT INTO users (api_key_hash, name, created_at) VALUES (?, ?, ?)',
               ('test_hash', 'test_user', '2026-06-03T00:00:00'))

    # 1. 插入初始记录（attempt_number=0）
    log = UsageLogCreate(
        request_id='test-request-2',
        user_id=1,
        action='generate-image',
        estimated_anlas_cost=10,
        model='nai-diffusion-3',
        width=832,
        height=1216,
        steps=28,
        n_samples=1,
        request_payload={'prompt': 'test'},
    )
    repo.insert_queued(log, attempt_number=0)
    repo.mark_running('test-request-2', queued_ms=100, upstream_id='upstream-A', attempt_number=0)
    repo.mark_failed('test-request-2', queued_ms=150, error_code='429', error_message='Rate limit', attempt_number=0)

    # 2. 使用 request_id 从数据库复制并插入重试记录（模拟重试）
    repo.insert_retry_attempt(request_id='test-request-2', attempt_number=1, upstream_id='upstream-B')

    # 3. 验证重试记录
    rows = db.query_all('SELECT * FROM usage_logs WHERE request_id = ? ORDER BY attempt_number',
                        ('test-request-2',))
    assert len(rows) == 2, f"应该有 2 条记录，实际有 {len(rows)} 条"

    # 验证第一条记录
    row0 = rows[0]
    assert row0['attempt_number'] == 0
    assert row0['status'] == 'failed'
    assert row0['error_code'] == '429'
    assert row0['upstream_id'] == 'upstream-A'

    # 验证第二条记录（从第一条复制）
    row1 = rows[1]
    assert row1['attempt_number'] == 1
    assert row1['status'] == 'running'
    assert row1['upstream_id'] == 'upstream-B'
    # 验证复制的字段
    assert row1['user_id'] == row0['user_id']
    assert row1['action'] == row0['action']
    assert row1['model'] == row0['model']
    assert row1['width'] == row0['width']
    assert row1['height'] == row0['height']
    assert row1['steps'] == row0['steps']
    assert row1['n_samples'] == row0['n_samples']
    assert row1['estimated_anlas_cost'] == row0['estimated_anlas_cost']
    assert row1['request_payload'] == row0['request_payload']

    print('[PASS] 从数据库复制记录插入重试记录成功')


def test_insert_retry_attempt_validates_parameters():
    """测试参数验证"""
    db = Database(':memory:')
    db.init_schema()
    repo = UsageLogRepository(db)

    try:
        repo.insert_retry_attempt(attempt_number=1)
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "Must provide request_id" in str(e)
        print('[PASS] 参数验证成功')


if __name__ == '__main__':
    test_insert_retry_attempt_copies_initial_record()
    test_insert_retry_attempt_from_database()
    test_insert_retry_attempt_validates_parameters()
    print('\n[ALL PASS] 所有测试通过！')
