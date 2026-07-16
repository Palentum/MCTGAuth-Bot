"""测试共享 fixture。"""

import pytest

from mctgauth_bot.api import Notifier, build_app
from mctgauth_bot.config import Config
from mctgauth_bot.db import Database


class StubNotifier(Notifier):
    """记录调用的桩通知器；可配置为发送失败。"""

    def __init__(self):
        self.send_calls = []
        self.close_calls = []
        self.fail_send = False
        self._next_message_id = 1000

    async def send_login_prompt(self, tg_user_id, mc_name, ip, request_id):
        self.send_calls.append((tg_user_id, mc_name, ip, request_id))
        if self.fail_send:
            raise RuntimeError("模拟发送失败")
        self._next_message_id += 1
        # chat_id 用 tg_user_id 模拟私聊。
        return tg_user_id, self._next_message_id

    async def close_request_message(self, chat_id, message_id, text):
        self.close_calls.append((chat_id, message_id, text))


@pytest.fixture
def api_secret():
    return "test-secret"


@pytest.fixture
def config(api_secret, tmp_path):
    return Config(
        bot_token="dummy-token",
        api_secret=api_secret,
        db_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
async def db(config):
    database = Database(config.db_path)
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def notifier():
    return StubNotifier()


@pytest.fixture
async def client(aiohttp_client, db, config, notifier):
    app = build_app(db, config, notifier)
    app["bot_username"] = "TestBot"
    return await aiohttp_client(app)


@pytest.fixture
def auth_headers(api_secret):
    return {"Authorization": f"Bearer {api_secret}"}
