"""/ping 命令测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from mctgauth_bot.config import DEFAULT_MESSAGES, Config
from mctgauth_bot.handlers.user import handle_ping


async def test_ping_default(config):
    """未覆盖文案时回复内置默认值。"""
    message = SimpleNamespace(answer=AsyncMock())
    await handle_ping(message, config)
    message.answer.assert_awaited_once_with(DEFAULT_MESSAGES["ping"])


async def test_ping_configured(tmp_path):
    """config.toml 覆盖 ping 后回复配置文案。"""
    cfg = Config(
        bot_token="dummy-token",
        api_secret="test-secret",
        db_path=str(tmp_path / "test.db"),
        messages={**DEFAULT_MESSAGES, "ping": "在线，一切正常。"},
    )
    message = SimpleNamespace(answer=AsyncMock())
    await handle_ping(message, cfg)
    message.answer.assert_awaited_once_with("在线，一切正常。")
