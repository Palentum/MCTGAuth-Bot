"""用户面板视图渲染测试。"""

from mctgauth_bot.handlers.user import PanelCb, render_panel


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
