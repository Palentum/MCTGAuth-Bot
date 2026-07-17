"""/address 命令测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from mctgauth_bot.config import DEFAULT_MESSAGES, Config
from mctgauth_bot.handlers.user import handle_address


async def test_address_default(config):
    """未覆盖文案时回复内置默认值。"""
    message = SimpleNamespace(answer=AsyncMock())
    await handle_address(message, config)
    message.answer.assert_awaited_once_with(DEFAULT_MESSAGES["address"])


async def test_address_configured(tmp_path):
    """config.toml 覆盖 address 后回复配置文案。"""
    cfg = Config(
        bot_token="dummy-token",
        api_secret="test-secret",
        db_path=str(tmp_path / "test.db"),
        messages={**DEFAULT_MESSAGES, "address": "服务器地址：<code>mc.example.com</code>"},
    )
    message = SimpleNamespace(answer=AsyncMock())
    await handle_address(message, cfg)
    message.answer.assert_awaited_once_with("服务器地址：<code>mc.example.com</code>")
