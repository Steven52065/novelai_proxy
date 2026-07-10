from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.database import utc_now_iso


def _quota_row(client: TestClient, user_id: int) -> dict:
    row = client.app.state.db.query_one(
        """
        SELECT total, used, reserved, reset_period, reset_day, last_reset_at
        FROM user_anlas_quota
        WHERE user_id = ?
        """,
        (user_id,),
    )
    assert row is not None
    return dict(row)


def test_bulk_reset_anlas_clears_usage_without_touching_schedule(client, create_user, admin_auth):
    first_id = create_user(client, "bulk-anlas-1")
    second_id = create_user(client, "bulk-anlas-2")
    untouched_id = create_user(client, "bulk-anlas-untouched")

    marker_reset_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    for user_id, used, reserved in ((first_id, 30, 10), (second_id, 99, 1), (untouched_id, 7, 3)):
        client.app.state.db.execute(
            "UPDATE user_anlas_quota SET used = ?, reserved = ?, last_reset_at = ? WHERE user_id = ?",
            (used, reserved, marker_reset_at, user_id),
        )

    resp = client.post(
        "/admin/api/users/bulk-reset-anlas",
        auth=admin_auth,
        json={"user_ids": [first_id, second_id, first_id]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "user_ids": [first_id, second_id]}

    for user_id in (first_id, second_id):
        quota = _quota_row(client, user_id)
        assert quota["used"] == 0
        assert quota["reserved"] == 0
        assert quota["total"] == 100
        assert quota["reset_period"] == "month"
        assert quota["reset_day"] == 5
        assert quota["last_reset_at"] == marker_reset_at

    untouched = _quota_row(client, untouched_id)
    assert untouched["used"] == 7
    assert untouched["reserved"] == 3


def test_bulk_reset_free_small_daily_clears_current_window_only(client, create_user, admin_auth):
    user_id = create_user(
        client,
        "bulk-daily-user",
        anlas_total=0,
        free_small_daily_limit_enabled=True,
        free_small_daily_limit=5,
    )

    manager = client.app.state.free_small_daily_limit_manager
    confirmed = manager.reserve(user_id, 2)
    manager.confirm(confirmed)
    manager.reserve(user_id, 1)
    before = manager.get_snapshot(user_id)
    assert (before.used, before.reserved) == (2, 1)

    old_window_start = "2000-01-01T00:00:00+08:00"
    now = utc_now_iso()
    client.app.state.db.execute(
        """
        INSERT INTO free_small_daily_usage (user_id, window_start, used, reserved, created_at, updated_at)
        VALUES (?, ?, 4, 0, ?, ?)
        """,
        (user_id, old_window_start, now, now),
    )

    resp = client.post(
        "/admin/api/users/bulk-reset-free-small-daily",
        auth=admin_auth,
        json={"user_ids": [user_id]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "user_ids": [user_id]}

    after = manager.get_snapshot(user_id)
    assert after.used == 0
    assert after.reserved == 0
    assert after.available == 5
    assert after.window_start == before.window_start

    old_row = client.app.state.db.query_one(
        "SELECT used, reserved FROM free_small_daily_usage WHERE user_id = ? AND window_start = ?",
        (user_id, old_window_start),
    )
    assert dict(old_row) == {"used": 4, "reserved": 0}


def test_bulk_reset_rejects_empty_selection_and_unknown_user(client, create_user, admin_auth):
    user_id = create_user(client, "bulk-validate-user")
    client.app.state.db.execute(
        "UPDATE user_anlas_quota SET used = 12, reserved = 0 WHERE user_id = ?",
        (user_id,),
    )

    for path in ("/admin/api/users/bulk-reset-anlas", "/admin/api/users/bulk-reset-free-small-daily"):
        empty_resp = client.post(path, auth=admin_auth, json={"user_ids": []})
        # 本项目将请求验证错误统一转换为 400。
        assert empty_resp.status_code == 400

        missing_resp = client.post(path, auth=admin_auth, json={"user_ids": [user_id, 999]})
        assert missing_resp.status_code == 404
        assert missing_resp.json()["message"] == "User not found"

    # 校验失败时不应重置任何用户。
    assert _quota_row(client, user_id)["used"] == 12


def test_bulk_reset_web_forms_and_page_controls(client, create_user):
    user_id = create_user(
        client,
        "bulk-web-user",
        free_small_daily_limit_enabled=True,
        free_small_daily_limit=5,
    )
    client.app.state.db.execute(
        "UPDATE user_anlas_quota SET used = 40, reserved = 5 WHERE user_id = ?",
        (user_id,),
    )
    manager = client.app.state.free_small_daily_limit_manager
    manager.confirm(manager.reserve(user_id, 3))

    anonymous = client.post(
        "/admin/users/bulk-reset-anlas",
        data={"user_ids": str(user_id)},
        follow_redirects=False,
    )
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/admin/login"
    assert _quota_row(client, user_id)["used"] == 40

    login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies.get("novelai_proxy_admin_csrf")

    page = client.get("/admin/users")
    assert page.status_code == 200
    for marker in (
        "user-remark-search",
        "搜索备注名",
        "user-select-checkbox",
        'data-user-name="bulk-web-user"',
        "select-all-users",
        "user-select",
        "bulk-reset-anlas-btn",
        "bulk-reset-daily-btn",
        "重置Anlas点数",
        "重置日小图",
    ):
        assert marker in page.text

    anlas_resp = client.post(
        "/admin/users/bulk-reset-anlas",
        data={"user_ids": str(user_id)},
        follow_redirects=False,
    )
    assert anlas_resp.status_code == 303
    assert anlas_resp.headers["location"] == "/admin/users"
    quota = _quota_row(client, user_id)
    assert quota["used"] == 0
    assert quota["reserved"] == 0

    daily_resp = client.post(
        "/admin/users/bulk-reset-free-small-daily",
        data={"user_ids": str(user_id)},
        follow_redirects=False,
    )
    assert daily_resp.status_code == 303
    assert daily_resp.headers["location"] == "/admin/users"
    snapshot = manager.get_snapshot(user_id)
    assert snapshot.used == 0
    assert snapshot.reserved == 0

    no_selection = client.post("/admin/users/bulk-reset-anlas", data={}, follow_redirects=False)
    assert no_selection.status_code == 400
    assert no_selection.json()["message"] == "No users selected"
