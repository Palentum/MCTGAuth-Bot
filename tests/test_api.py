"""HTTP API 测试：鉴权、各端点契约、限流、通知副作用。"""

import asyncio
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


async def test_register_token_idempotent_when_concurrent(client, auth_headers, db):
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/register-token",
                headers=auth_headers,
                json={"mc_uuid": "uuid-x", "mc_name": "Steve"},
            )
            for _ in range(2)
        )
    )
    bodies = [await response.json() for response in responses]

    assert [response.status for response in responses] == [200, 200]
    assert bodies[0]["token"] == bodies[1]["token"]
    assert (await db.get_live_token_for_uuid("uuid-x"))["token"] == bodies[0]["token"]


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


async def test_register_token_malformed_json(client, auth_headers):
    resp = await client.post(
        "/api/v1/register-token", headers=auth_headers, data="{not json"
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_request"


async def test_register_token_missing_field(client, auth_headers):
    resp = await client.post(
        "/api/v1/register-token", headers=auth_headers, json={"mc_uuid": "uuid-x"}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_request"


async def test_register_token_body_not_object(client, auth_headers):
    resp = await client.post(
        "/api/v1/register-token", headers=auth_headers, json=["mc_uuid", "mc_name"]
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_request"


async def test_login_request_missing_field(client, auth_headers):
    resp = await client.post(
        "/api/v1/login-request", headers=auth_headers, json={"mc_uuid": "uuid-x"}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_request"


async def test_login_request_ip_not_string(client, auth_headers, db):
    await db.create_binding(42, "uuid-x", "Steve")
    resp = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": 123},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_request"


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


async def test_login_request_concurrent_reuses_reservation(
    client, auth_headers, db, notifier
):
    await db.create_binding(42, "uuid-x", "Steve")
    first_send_started = asyncio.Event()
    second_send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def blocking_send(tg_user_id, mc_name, ip, request_id):
        notifier.send_calls.append((tg_user_id, mc_name, ip, request_id))
        (first_send_started if len(notifier.send_calls) == 1 else second_send_started).set()
        await release_send.wait()
        return tg_user_id, 1000 + len(notifier.send_calls)

    notifier.send_login_prompt = blocking_send
    payload = {"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"}
    first = asyncio.create_task(
        client.post("/api/v1/login-request", headers=auth_headers, json=payload)
    )
    await first_send_started.wait()
    second = asyncio.create_task(
        client.post("/api/v1/login-request", headers=auth_headers, json=payload)
    )
    second_send = asyncio.create_task(second_send_started.wait())
    await asyncio.wait({second, second_send}, return_when=asyncio.FIRST_COMPLETED)
    release_send.set()
    responses = await asyncio.gather(first, second)
    second_send.cancel()

    assert sorted(response.status for response in responses) == [200, 201]
    assert len(notifier.send_calls) == 1
    assert len({(await response.json())["request_id"] for response in responses}) == 1


async def test_login_request_concurrent_first_send_fails(
    client, auth_headers, db, notifier
):
    # 首个请求在发送悬停期间被第二个请求复用预留，随后首个发送失败：
    # 复用方拿到的 request_id 不能变成 404（回归：曾因物理删除预留而 404）。
    await db.create_binding(42, "uuid-x", "Steve")
    first_send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def blocking_failing_send(tg_user_id, mc_name, ip, request_id):
        notifier.send_calls.append((tg_user_id, mc_name, ip, request_id))
        first_send_started.set()
        await release_send.wait()
        raise RuntimeError("模拟发送失败")

    notifier.send_login_prompt = blocking_failing_send
    payload = {"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"}
    first = asyncio.create_task(
        client.post("/api/v1/login-request", headers=auth_headers, json=payload)
    )
    await first_send_started.wait()
    # 第二个请求在首个发送悬停期间复用预留，立即得到 200 + 既有 request_id。
    second = await client.post(
        "/api/v1/login-request", headers=auth_headers, json=payload
    )
    assert second.status == 200
    reused_id = (await second.json())["request_id"]

    # 放行首个发送使其失败。
    release_send.set()
    first_resp = await first
    assert first_resp.status == 502
    # 只发一条（被复用），复用方的 request_id 仍可查到终态 expired，而非 404。
    assert len(notifier.send_calls) == 1
    polled = await client.get(
        f"/api/v1/login-request/{reused_id}", headers=auth_headers
    )
    assert polled.status == 200
    assert (await polled.json())["status"] == "expired"


async def test_login_request_replaces_expired_pending(
    client, auth_headers, db, notifier
):
    await db.create_binding(42, "uuid-x", "Steve")
    now = int(time.time())
    await db.create_login_request(
        "expired-request",
        "uuid-x",
        "Steve",
        "1.2.3.4",
        now - 60,
        now - 1,
        tg_chat_id=42,
        tg_message_id=999,
    )

    response = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )

    assert response.status == 201
    assert len(notifier.send_calls) == 1
    assert notifier.close_calls[0][:2] == (42, 999)
    assert (await db.get_login_request("expired-request"))["status"] == "expired"


async def test_login_request_rate_limited_still_closes_expired_message(
    client, auth_headers, db, notifier
):
    await db.create_binding(42, "uuid-x", "Steve")
    now = int(time.time())
    await db.create_login_request(
        "expired-request",
        "uuid-x",
        "Steve",
        "1.2.3.4",
        now - 60,
        now - 1,
        tg_chat_id=42,
        tg_message_id=999,
    )
    # 耗尽 mc_uuid 维度限流，令新请求必命中 429。
    limiter = client.app["login_limiter"]
    for _ in range(client.app["cfg"].login_max_calls):
        assert limiter.allow("uuid-x")

    response = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-x", "mc_name": "Steve", "ip": "1.2.3.4"},
    )

    assert response.status == 429
    # 旧过期请求的消息仍被关闭，且未发新提示、预留行已回滚。
    assert notifier.close_calls[0][:2] == (42, 999)
    assert notifier.send_calls == []
    assert await db.get_pending_for_uuid("uuid-x") is None
    assert (await db.get_login_request("expired-request"))["status"] == "expired"


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
    # 发送失败不留下 pending；预留行改置终态 expired（供并发复用方轮询到终态而非 404）。
    assert await db.get_pending_for_uuid("uuid-x") is None
    assert (await db.get_login_request(notifier.send_calls[0][3]))["status"] == "expired"


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


async def test_get_login_request_reports_expired_before_sweeper(
    client, auth_headers, db
):
    now = int(time.time())
    await db.create_login_request(
        "expired-request", "uuid-x", "Steve", None, now - 60, now - 1
    )

    response = await client.get(
        "/api/v1/login-request/expired-request", headers=auth_headers
    )

    assert response.status == 200
    assert (await response.json())["status"] == "expired"


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


async def test_delete_login_request_loses_cancel_race(
    client, auth_headers, db, notifier, monkeypatch
):
    await db.create_binding(42, "uuid-race", "Steve")
    response = await client.post(
        "/api/v1/login-request",
        headers=auth_headers,
        json={"mc_uuid": "uuid-race", "mc_name": "Steve", "ip": "1.2.3.4"},
    )
    request_id = (await response.json())["request_id"]
    original_set_status = db.set_login_status

    async def approve_before_cancel(request_id, status):
        assert await original_set_status(request_id, "approved")
        return await original_set_status(request_id, status)

    monkeypatch.setattr(db, "set_login_status", approve_before_cancel)

    response = await client.delete(
        f"/api/v1/login-request/{request_id}", headers=auth_headers
    )

    assert await response.json() == {"status": "approved"}
    assert notifier.close_calls == []
    assert (await db.get_login_request(request_id))["status"] == "approved"


async def test_delete_login_request_unknown(client, auth_headers):
    resp = await client.delete("/api/v1/login-request/nope", headers=auth_headers)
    assert resp.status == 404
