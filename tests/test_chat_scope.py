"""命令的 chat 作用域与 /help 分角色帮助测试。

走 Dispatcher.feed_update 全链路，覆盖路由级过滤器：
- /ping 在群组与私聊中均回复；
- 其余命令仅私聊回复，群组内静默忽略；
- 普通用户 /help 只见用户帮助，管理员 /help 见用户 + 管理帮助。
"""

import importlib
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from mctgauth_bot.config import DEFAULT_MESSAGES, Config

ADMIN_ID = 900
USER_ID = 100
GROUP_ID = -200


class RecordingSession(BaseSession):
    """拦截 Bot API 调用的桩会话，记录发出的消息文本。"""

    def __init__(self):
        super().__init__()
        self.sent_texts: list[str] = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        assert isinstance(method, SendMessage), f"未预期的 API 调用：{type(method).__name__}"
        self.sent_texts.append(method.text)
        return Message(
            message_id=999,
            date=datetime.now(timezone.utc),
            chat=Chat(id=method.chat_id, type="private"),
            text=method.text,
        )

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


@pytest.fixture
def config(tmp_path):
    return Config(
        bot_token="42:TEST",
        api_secret="test-secret",
        db_path=str(tmp_path / "test.db"),
        admin_ids={ADMIN_ID},
    )


@pytest.fixture
def session():
    return RecordingSession()


@pytest.fixture
def bot(session, config):
    return Bot(token=config.bot_token, session=session)


@pytest.fixture
def dp(db, config):
    # 模块级 router 单例只能挂到一个 Dispatcher，reload 使每个测试拿到全新实例。
    from mctgauth_bot.handlers import admin as admin_handlers
    from mctgauth_bot.handlers import user as user_handlers

    importlib.reload(user_handlers)
    importlib.reload(admin_handlers)

    dispatcher = Dispatcher()
    dispatcher["db"] = db
    dispatcher["cfg"] = config
    dispatcher.include_router(user_handlers.ping_router)
    dispatcher.include_router(admin_handlers.setup_admin_router(config))
    dispatcher.include_router(user_handlers.router)
    return dispatcher


def make_update(text: str, chat_type: str, user_id: int) -> Update:
    chat_id = user_id if chat_type == "private" else GROUP_ID
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="tester"),
        text=text,
    )
    return Update(update_id=1, message=message)


async def test_ping_replies_in_group_and_private(dp, bot, session):
    """/ping 在群组与私聊中均回复。"""
    await dp.feed_update(bot, make_update("/ping", "group", USER_ID))
    await dp.feed_update(bot, make_update("/ping", "supergroup", ADMIN_ID))
    await dp.feed_update(bot, make_update("/ping", "private", USER_ID))
    assert session.sent_texts == [DEFAULT_MESSAGES["ping"]] * 3


async def test_other_commands_silent_in_group(dp, bot, session):
    """除 /ping 外的命令在群组内静默忽略（普通用户与管理员均如此）。"""
    for text in ("/help", "/address", "/start"):
        await dp.feed_update(bot, make_update(text, "group", USER_ID))
    for text in ("/help", "/list", "/whois abc"):
        await dp.feed_update(bot, make_update(text, "group", ADMIN_ID))
    assert session.sent_texts == []


async def test_user_help_in_private(dp, bot, session):
    """普通用户私聊 /help：只见用户帮助，不含管理命令。"""
    await dp.feed_update(bot, make_update("/help", "private", USER_ID))
    assert len(session.sent_texts) == 1
    assert session.sent_texts[0] == DEFAULT_MESSAGES["panel_help"]
    assert DEFAULT_MESSAGES["admin_help"] not in session.sent_texts[0]


async def test_admin_commands_blocked_for_non_admin(dp, bot, session):
    """非管理员私聊发管理命令：被 AdminFilter 拦截，静默忽略。"""
    for text in ("/list", "/whois abc", "/unbind abc"):
        await dp.feed_update(bot, make_update(text, "private", USER_ID))
    assert session.sent_texts == []


async def test_admin_help_in_private(dp, bot, session):
    """管理员私聊 /help：同时包含用户帮助与管理命令帮助。"""
    await dp.feed_update(bot, make_update("/help", "private", ADMIN_ID))
    assert len(session.sent_texts) == 1
    assert DEFAULT_MESSAGES["panel_help"] in session.sent_texts[0]
    assert DEFAULT_MESSAGES["admin_help"] in session.sent_texts[0]
