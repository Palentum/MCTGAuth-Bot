"""清理任务测试：过期令牌删除、过期请求置 expired 并编辑消息。"""

import time

from mctgauth_bot.sweeper import sweep_once

from .conftest import StubNotifier


async def test_sweep_deletes_expired_token(db, config):
    now = int(time.time())
    await db.upsert_token("TOKENAAA", "uuid-a", "Alice", now - 1)
    await db.upsert_token("TOKENBBB", "uuid-b", "Bob", now + 300)

    await sweep_once(db, config, StubNotifier())

    assert await db.get_live_token_for_uuid("uuid-a", now) is None
    assert await db.get_live_token_for_uuid("uuid-b", now) is not None


async def test_sweep_expires_pending_and_edits_message(db, config):
    now = int(time.time())
    await db.create_login_request(
        "req-1", "uuid-a", "Alice", None, now, now - 1, tg_chat_id=42, tg_message_id=99
    )
    notifier = StubNotifier()

    await sweep_once(db, config, notifier)

    assert (await db.get_login_request("req-1"))["status"] == "expired"
    # 编辑了对应 TG 消息。
    assert len(notifier.close_calls) == 1
    chat_id, message_id, _text = notifier.close_calls[0]
    assert chat_id == 42
    assert message_id == 99


async def test_sweep_ignores_live_pending(db, config):
    now = int(time.time())
    await db.create_login_request(
        "req-1", "uuid-a", "Alice", None, now, now + 300, tg_chat_id=42, tg_message_id=99
    )
    notifier = StubNotifier()

    await sweep_once(db, config, notifier)

    assert (await db.get_login_request("req-1"))["status"] == "pending"
    assert len(notifier.close_calls) == 0
