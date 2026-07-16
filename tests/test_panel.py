"""用户面板视图渲染测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from mctgauth_bot.handlers.user import (
    LoginCb,
    PanelCb,
    handle_login_callback,
    render_panel,
)


async def test_panel_main_unbound(db, config):
    """未绑定：显示引导文案，主视图带 刷新/帮助 两个按钮。"""
    text, keyboard = await render_panel(111, "main", db, config)
    assert "未绑定" in text
    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == 2
    assert buttons[0].callback_data == PanelCb(view="main").pack()
    assert buttons[1].callback_data == PanelCb(view="help").pack()


async def test_panel_main_bound(db, config):
    """已绑定：显示角色名与绑定时间。"""
    await db.create_binding(222, "uuid-1", "Steve", created_at=1730000000)
    text, _ = await render_panel(222, "main", db, config)
    assert "Steve" in text
    assert "2024-10-27" in text and "UTC" in text


async def test_panel_help_view(db, config):
    """帮助视图：显示用户帮助，带返回按钮，且不含管理员命令。"""
    text, keyboard = await render_panel(111, "help", db, config)
    assert "/start" in text
    for admin_cmd in ("/list", "/whois", "/unbind"):
        assert admin_cmd not in text
    buttons = keyboard.inline_keyboard[0]
    assert len(buttons) == 1
    assert buttons[0].callback_data == PanelCb(view="main").pack()


async def test_expired_login_callback_loses_status_race(db, config, monkeypatch):
    await db.create_binding(42, "uuid-race", "Steve")
    await db.create_login_request("request-race", "uuid-race", "Steve", None, 0, 1)
    original_set_status = db.set_login_status

    async def approve_before_expire(request_id, status):
        assert await original_set_status(request_id, "approved")
        return await original_set_status(request_id, status)

    monkeypatch.setattr(db, "set_login_status", approve_before_expire)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    await handle_login_callback(
        callback,
        LoginCb(action="approve", req_id="request-race"),
        db,
        config,
    )

    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once_with(
        config.msg("login_cb_not_pending"), show_alert=True
    )
    assert (await db.get_login_request("request-race"))["status"] == "approved"
