"""外部值进入 HTML 文案前必须转义的回归测试。

Bot 全局 ParseMode.HTML，模板自带标签需保留，但插入的运行时外部值
（mc_name、ip、mc_uuid）若含 < > & 会注入标签伪造文案或使 Telegram 拒发。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from mctgauth_bot.handlers.admin import handle_list


def test_msg_escapes_injected_values(config):
    """占位符里的 < > & 被转义，模板自带 HTML 标签保留。"""
    payload = '<a href="x">evil</a> & <tg-spoiler>x</tg-spoiler>'
    out = config.msg("login_prompt", mc_name=payload, ip="1.2.3.4")
    # 注入的标签被转义，不再出现原样尖括号。
    assert "<a href" not in out
    assert "<tg-spoiler>" not in out
    assert "&lt;" in out and "&amp;" in out
    # 模板自带的 <b>/<code> 标签仍在。
    assert "<b>" in out and "<code>" in out


def test_msg_without_kwargs_unchanged(config):
    """无占位符的静态文案原样返回，模板 HTML 不受影响。"""
    out = config.msg("panel_unbound")
    assert out == config.messages["panel_unbound"]


async def test_admin_list_escapes_values(db, config):
    """/list 直接拼 HTML 的 mc_name/mc_uuid 也被转义。"""
    await db.create_binding(1, "<b>uuid</b>", "<i>Evil</i>", created_at=1730000000)
    message = SimpleNamespace(answer=AsyncMock())
    command = SimpleNamespace(args=None)

    await handle_list(message, command, db, config)

    sent = message.answer.await_args.args[0]
    assert "<i>Evil</i>" not in sent
    assert "&lt;i&gt;Evil&lt;/i&gt;" in sent
    assert "&lt;b&gt;uuid&lt;/b&gt;" in sent
