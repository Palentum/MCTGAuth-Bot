"""HTTP API 测试：鉴权、各端点契约、限流、通知副作用。"""

import time

from mctgauth_bot.tokens import TOKEN_RE


async def test_auth_missing(client):
    resp = await client.get("/api/v1/health")
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "unauthorized"


async def test_auth_wrong(client):
    resp = await client.get(
        "/api/v1/health", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status == 401


async def test_health(client, auth_headers):
    resp = await client.get("/api/v1/health", headers=auth_headers)
    assert resp.status == 200
    assert await resp.json() == {"ok": True}


async def test_binding_lookup_unbound(client, auth_headers):
    resp = await client.get("/api/v1/binding/uuid-x", headers=auth_headers)
    assert resp.status == 200
    assert await resp.json() == {"bound": False}


async def test_binding_lookup_bound(client, auth_headers, db):
    await db.create_binding(42, "uuid-x", "Steve")
    resp = await client.get("/api/v1/binding/uuid-x", headers=auth_headers)
    assert resp.status == 200
    assert await resp.json() == {"bound": True, "tg_user_id": 42, "mc_name": "Steve"}


async def test_register_token_happy(client, auth_headers):
    resp = await client.post(
        "/api/v1/register-token",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert TOKEN_RE.match(body["token"])
    assert body["bot_username"] == "TestBot"
    assert body["expires_at"] > int(time.time())


async def test_register_token_idempotent(client, auth_headers):
    r1 = await client.post(
        "/api/v1/register-token",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve"},
    )
    t1 = (await r1.json())["token"]
    # 再次请求同 uuid（未过期）→ 同一令牌，并可更新 mc_name。
    r2 = await client.post(
        "/api/v1/register-token",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "SteveRenamed"},
    )
    body2 = await r2.json()
    assert body2["token"] == t1


async def test_register_token_already_bound(client, auth_headers, db):
    await db.create_binding(42, "uuid-x", "Steve")
    resp = await client.post(
        "/api/v1/register-token",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve"},
    )
    assert resp.status == 409
    assert (await resp.json())["error"] == "already_bound"


async def test_register_token_rate_limited(client, auth_headers):
    # 默认 register 限流 5 次/窗口。第 6 次应 429。
    for _ in range(5):
        r = await client.post(
            "/api/v1/register-token",
            headers=auth_headers,
            json={"mc_uuid": "uuid-x", "mc_name": "Steve"},
        )
        assert r.status == 200
    r6 = await client.post(
        "/api/v1/register-token",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve"},
    )
    assert r6.status == 429
    assert (await r6.json())["error"] == "rate_limited"


async def test_login_request_happy(client, auth_headers, db, notifier):
    await db.create_binding(42, "uuid-x", "Steve")
    resp = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["request_id"]
    assert body["expires_at"] > int(time.time())
    # 通知恰好发一次。
    assert len(notifier.send_calls) == 1
    assert notifier.send_calls[0][0] == 42  # tg_user_id


async def test_login_request_reuse_pending(client, auth_headers, db, notifier):
    await db.create_binding(42, "uuid-x", "Steve")
    r1 = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    id1 = (await r1.json())["request_id"]

    r2 = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "5.6.7.8"},
    )
    assert r2.status == 200
    assert (await r2.json())["request_id"] == id1
    # 复用 pending：不再发第二条消息。
    assert len(notifier.send_calls) == 1


async def test_login_request_not_bound(client, auth_headers):
    resp = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-none", "mc_name": "Ghost", "ip": "1.2.3.4"},
    )
    assert resp.status == 404
    assert (await resp.json())["error"] == "not_bound"


async def test_login_request_tg_send_failed(client, auth_headers, db, notifier):
    await db.create_binding(42, "uuid-x", "Steve")
    notifier.fail_send = True
    resp = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    assert resp.status == 502
    assert (await resp.json())["error"] == "tg_send_failed"
    # 发送失败不创建请求。
    assert await db.get_pending_for_uuid("uuid-x") is None


async def test_login_request_rate_limited(client, auth_headers, db):
    await db.create_binding(42, "uuid-x", "Steve")
    # login 限流 10 次/窗口。为避免 pending 复用，每次成功后立即取消。
    for _ in range(10):
        r = await client.post(
            "/api/v1/login-request",
            headers=auth_headers,
            json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
        )
        assert r.status == 201
        rid = (await r.json())["request_id"]
        await client.delete(f"/api/v1/login-request/{rid}", headers=auth_headers)
    r11 = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    assert r11.status == 429


async def test_get_login_request_status(client, auth_headers, db, notifier):
    await db.create_binding(42, "uuid-x", "Steve")
    r = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    rid = (await r.json())["request_id"]
    resp = await client.get(f"/api/v1/login-request/{rid}", headers=auth_headers)
    assert resp.status == 200
    assert (await resp.json())["status"] == "pending"


async def test_get_login_request_unknown(client, auth_headers):
    resp = await client.get("/api/v1/login-request/nope", headers=auth_headers)
    assert resp.status == 404
    assert (await resp.json())["error"] == "not_found"


async def test_delete_login_request_cancel(client, auth_headers, db, notifier):
    await db.create_binding(42, "uuid-x", "Steve")
    r = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    rid = (await r.json())["request_id"]

    resp = await client.delete(f"/api/v1/login-request/{rid}", headers=auth_headers)
    assert resp.status == 200
    assert (await resp.json())["status"] == "cancelled"
    # 取消时编辑了 TG 消息。
    assert len(notifier.close_calls) == 1

    # 状态确实变为 cancelled。
    resp2 = await client.get(f"/api/v1/login-request/{rid}", headers=auth_headers)
    assert (await resp2.json())["status"] == "cancelled"


async def test_delete_login_request_terminal(client, auth_headers, db, notifier):
    await db.create_binding(42, "uuid-x", "Steve")
    r = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    rid = (await r.json())["request_id"]
    await db.set_login_status(rid, "approved")

    resp = await client.delete(f"/api/v1/login-request/{rid}", headers=auth_headers)
    assert resp.status == 200
    # 已终态：返回当前状态，不再编辑消息。
    assert (await resp.json())["status"] == "approved"
    assert len(notifier.close_calls) == 0


async def test_delete_login_request_unknown(client, auth_headers):
    resp = await client.delete("/api/v1/login-request/nope", headers=auth_headers)
    assert resp.status == 404
