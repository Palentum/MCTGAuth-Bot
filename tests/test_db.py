"""数据层测试：1:1 约束、令牌一次性、状态机、部分唯一索引。"""

import time

import pytest
from aiosqlite import IntegrityError


async def test_binding_unique_both_directions(db):
    await db.create_binding(1, "uuid-a", "Alice")

    # 同一 tg_user_id 再绑另一个 uuid → 拒绝。
    with pytest.raises(IntegrityError):
        await db.create_binding(1, "uuid-b", "Alice2")

    # 同一 mc_uuid 被另一个 tg 账号绑 → 拒绝。
    with pytest.raises(IntegrityError):
        await db.create_binding(2, "uuid-a", "Bob")

    assert (await db.get_binding_by_uuid("uuid-a"))["tg_user_id"] == 1
    assert (await db.get_binding_by_tg(1))["mc_uuid"] == "uuid-a"


async def test_consume_token_one_shot(db):
    now = int(time.time())
    await db.upsert_token("TOKENAAA", "uuid-a", "Alice", now + 300)

    first = await db.consume_token("TOKENAAA")
    assert first is not None
    assert first["mc_uuid"] == "uuid-a"

    # 第二次消费同一令牌 → None。
    assert await db.consume_token("TOKENAAA") is None


async def test_consume_expired_token(db):
    now = int(time.time())
    await db.upsert_token("TOKENAAA", "uuid-a", "Alice", now - 1)
    assert await db.consume_token("TOKENAAA") is None


async def test_upsert_token_idempotent_by_uuid(db):
    now = int(time.time())
    await db.upsert_token("TOKEN111", "uuid-a", "Alice", now + 300)
    # 同 uuid 覆盖：换令牌与 mc_name。
    await db.upsert_token("TOKEN222", "uuid-a", "AliceRenamed", now + 300)

    live = await db.get_live_token_for_uuid("uuid-a", now)
    assert live["token"] == "TOKEN222"
    assert live["mc_name"] == "AliceRenamed"
    # 旧令牌不应再可消费。
    assert await db.consume_token("TOKEN111") is None


async def test_set_login_status_only_from_pending(db):
    now = int(time.time())
    await db.create_login_request("req-1", "uuid-a", "Alice", "1.2.3.4", now, now + 300)

    # pending → approved 成功。
    assert await db.set_login_status("req-1", "approved") is True
    # 再次转移（已非 pending）失败。
    assert await db.set_login_status("req-1", "denied") is False
    assert (await db.get_login_request("req-1"))["status"] == "approved"


async def test_partial_unique_index_blocks_second_pending(db):
    now = int(time.time())
    await db.create_login_request("req-1", "uuid-a", "Alice", None, now, now + 300)

    # 同 uuid 第二条 pending → 违反部分唯一索引。
    with pytest.raises(IntegrityError):
        await db.create_login_request("req-2", "uuid-a", "Alice", None, now, now + 300)


async def test_partial_unique_allows_after_terminal(db):
    now = int(time.time())
    await db.create_login_request("req-1", "uuid-a", "Alice", None, now, now + 300)
    await db.set_login_status("req-1", "denied")

    # 第一条转终态后，可再建一条 pending。
    await db.create_login_request("req-2", "uuid-a", "Alice", None, now, now + 300)
    assert (await db.get_login_request("req-2"))["status"] == "pending"


async def test_get_pending_for_uuid_respects_expiry(db):
    now = int(time.time())
    await db.create_login_request("req-1", "uuid-a", "Alice", None, now, now - 1)
    # 已过期，不算 live pending。
    assert await db.get_pending_for_uuid("uuid-a", now) is None


async def test_expire_stale_returns_and_transitions(db):
    now = int(time.time())
    await db.create_login_request("req-1", "uuid-a", "Alice", None, now, now - 1)
    await db.create_login_request("req-2", "uuid-b", "Bob", None, now, now + 300)

    stale = await db.expire_stale(now)
    assert [r["id"] for r in stale] == ["req-1"]
    assert (await db.get_login_request("req-1"))["status"] == "expired"
    assert (await db.get_login_request("req-2"))["status"] == "pending"


async def test_delete_binding_cleans_up(db):
    now = int(time.time())
    await db.create_binding(1, "uuid-a", "Alice")
    await db.upsert_token("TOKENAAA", "uuid-a", "Alice", now + 300)
    await db.create_login_request(
        "req-1", "uuid-a", "Alice", None, now, now + 300, tg_chat_id=1, tg_message_id=99
    )

    deleted = await db.delete_binding_by_uuid_or_name("Alice")
    assert deleted is not None
    assert deleted["mc_uuid"] == "uuid-a"
    # 返回被取消的 pending 请求供编辑 TG 消息。
    assert len(deleted["cancelled_requests"]) == 1
    assert deleted["cancelled_requests"][0]["id"] == "req-1"

    assert await db.get_binding_by_uuid("uuid-a") is None
    assert await db.get_live_token_for_uuid("uuid-a", now) is None
    assert (await db.get_login_request("req-1"))["status"] == "cancelled"


async def test_delete_binding_not_found(db):
    assert await db.delete_binding_by_uuid_or_name("nope") is None


async def test_list_bindings_pagination(db):
    for i in range(15):
        await db.create_binding(i, f"uuid-{i}", f"P{i}", created_at=1000 + i)
    page1 = await db.list_bindings(10, 0)
    page2 = await db.list_bindings(10, 10)
    assert len(page1) == 10
    assert len(page2) == 5
    assert await db.count_bindings() == 15


async def test_delete_expired_tokens(db):
    now = int(time.time())
    await db.upsert_token("TOKENAAA", "uuid-a", "Alice", now - 1)
    await db.upsert_token("TOKENBBB", "uuid-b", "Bob", now + 300)
    removed = await db.delete_expired_tokens(now)
    assert removed == 1
    assert await db.get_live_token_for_uuid("uuid-b", now) is not None
