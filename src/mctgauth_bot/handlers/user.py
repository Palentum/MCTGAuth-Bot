"""普通用户路由：账号绑定、登录审批与用户面板。

绑定入口：/start 携带 deep-link 令牌，或直接发送 8 位令牌文本。
审批入口：登录提示消息上的同意/拒绝按钮（LoginCb 回调）。
面板入口：/start（无令牌）或 /help，按钮切换视图（PanelCb 回调）。
"""

import logging
import time
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Config
from ..db import Database
from ..tokens import TOKEN_RE

log = logging.getLogger(__name__)

router = Router(name="user")


class LoginCb(CallbackData, prefix="lg"):
    """登录审批按钮的回调数据。"""

    action: str  # "approve" | "deny"
    req_id: str


class PanelCb(CallbackData, prefix="pn"):
    """用户面板视图切换按钮的回调数据。"""

    view: str  # "main" | "help"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def render_panel(
    tg_user_id: int, view: str, db: Database, cfg: Config
) -> tuple[str, InlineKeyboardMarkup]:
    """渲染面板指定视图，返回 (文本, 内联键盘)。"""
    if view == "help":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=cfg.msg("panel_btn_back"),
                        callback_data=PanelCb(view="main").pack(),
                    )
                ]
            ]
        )
        return cfg.msg("panel_help"), keyboard

    binding = await db.get_binding_by_tg(tg_user_id)
    if binding is None:
        text = cfg.msg("panel_unbound")
    else:
        text = cfg.msg(
            "panel_bound",
            mc_name=binding["mc_name"],
            created_at=_fmt_ts(binding["created_at"]),
        )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=cfg.msg("panel_btn_refresh"),
                    callback_data=PanelCb(view="main").pack(),
                ),
                InlineKeyboardButton(
                    text=cfg.msg("panel_btn_help"),
                    callback_data=PanelCb(view="help").pack(),
                ),
            ]
        ]
    )
    return text, keyboard


def build_login_keyboard(cfg: Config, request_id: str) -> InlineKeyboardMarkup:
    """构造同意/拒绝内联键盘。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=cfg.msg("login_btn_approve"),
                    callback_data=LoginCb(action="approve", req_id=request_id).pack(),
                ),
                InlineKeyboardButton(
                    text=cfg.msg("login_btn_deny"),
                    callback_data=LoginCb(action="deny", req_id=request_id).pack(),
                ),
            ]
        ]
    )


async def _do_bind(message: Message, token: str, db: Database, cfg: Config) -> None:
    """原子消费令牌并创建绑定，回复对应结果。"""
    status, row = await db.consume_token_and_create_binding(message.from_user.id, token)
    if row is None:
        message_key = {
            "tg_conflict": "bind_already_bound_tg",
            "uuid_conflict": "bind_already_bound_uuid",
            "token_not_found": "bind_token_not_found",
        }[status]
        await message.answer(cfg.msg(message_key))
        return

    await message.answer(cfg.msg("bind_success", mc_name=row["mc_name"]))


@router.message(CommandStart(deep_link=True))
async def handle_start_deeplink(
    message: Message, command: CommandObject, db: Database, cfg: Config
) -> None:
    """/start <payload>：payload 是合法令牌则走绑定，否则显示面板。"""
    payload = (command.args or "").strip()
    if TOKEN_RE.match(payload):
        await _do_bind(message, payload, db, cfg)
    else:
        text, keyboard = await render_panel(message.from_user.id, "main", db, cfg)
        await message.answer(text, reply_markup=keyboard)


@router.message(CommandStart())
async def handle_start_plain(message: Message, db: Database, cfg: Config) -> None:
    """无 payload 的 /start：显示用户面板。"""
    text, keyboard = await render_panel(message.from_user.id, "main", db, cfg)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("help"))
async def handle_help(message: Message, db: Database, cfg: Config) -> None:
    """普通用户 /help：显示帮助视图（管理员的 /help 由 admin 路由先行处理）。"""
    text, keyboard = await render_panel(message.from_user.id, "help", db, cfg)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("address"))
async def handle_address(message: Message, cfg: Config) -> None:
    """/address：回复配置中的服务器地址文案。"""
    await message.answer(cfg.msg("address"))


@router.callback_query(PanelCb.filter())
async def handle_panel_callback(
    callback: CallbackQuery, callback_data: PanelCb, db: Database, cfg: Config
) -> None:
    """面板按钮：编辑原消息切换视图 / 刷新状态。"""
    text, keyboard = await render_panel(
        callback.from_user.id, callback_data.view, db, cfg
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        # 刷新后内容未变化时 Telegram 拒绝编辑，静默忽略即可。
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.message(F.text.regexp(TOKEN_RE.pattern))
async def handle_token_text(message: Message, db: Database, cfg: Config) -> None:
    """纯令牌文本消息：走绑定流程。"""
    await _do_bind(message, message.text.strip(), db, cfg)


@router.callback_query(LoginCb.filter())
async def handle_login_callback(
    callback: CallbackQuery, callback_data: LoginCb, db: Database, cfg: Config
) -> None:
    """处理同意/拒绝按钮。"""
    request_id = callback_data.req_id
    row = await db.get_login_request(request_id)

    if row is None:
        await callback.answer(cfg.msg("login_cb_not_pending"), show_alert=True)
        return

    # 校验点击者确为该 mc_uuid 的绑定用户。
    binding = await db.get_binding_by_uuid(row["mc_uuid"])
    if binding is None or binding["tg_user_id"] != callback.from_user.id:
        await callback.answer(cfg.msg("login_cb_not_owner"), show_alert=True)
        return

    now = int(time.time())
    # 已过期：置为 expired 并编辑消息。
    if row["status"] == "pending" and row["expires_at"] <= now:
        if not await db.set_login_status(request_id, "expired"):
            await callback.answer(cfg.msg("login_cb_not_pending"), show_alert=True)
            return
        await callback.message.edit_text(
            cfg.msg("login_expired", mc_name=row["mc_name"])
        )
        await callback.answer(cfg.msg("login_cb_expired"), show_alert=True)
        return

    if row["status"] != "pending":
        await callback.answer(cfg.msg("login_cb_not_pending"), show_alert=True)
        return

    new_status = "approved" if callback_data.action == "approve" else "denied"
    # set_login_status 带 pending 守卫，防止并发重复处理。
    if not await db.set_login_status(request_id, new_status):
        await callback.answer(cfg.msg("login_cb_not_pending"), show_alert=True)
        return

    if new_status == "approved":
        await callback.message.edit_text(
            cfg.msg("login_approved", mc_name=row["mc_name"])
        )
        await callback.answer(cfg.msg("login_cb_approved"))
    else:
        await callback.message.edit_text(
            cfg.msg("login_denied", mc_name=row["mc_name"])
        )
        await callback.answer(cfg.msg("login_cb_denied"))
