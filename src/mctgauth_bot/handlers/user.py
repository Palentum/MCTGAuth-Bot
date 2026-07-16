"""普通用户路由：账号绑定与登录审批。

绑定入口：/start 携带 deep-link 令牌，或直接发送 8 位令牌文本。
审批入口：登录提示消息上的同意/拒绝按钮（LoginCb 回调）。
"""

import logging
import time

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiosqlite import IntegrityError

from ..config import Config
from ..db import Database
from ..tokens import TOKEN_RE

log = logging.getLogger(__name__)

router = Router(name="user")


class LoginCb(CallbackData, prefix="lg"):
    """登录审批按钮的回调数据。"""

    action: str  # "approve" | "deny"
    req_id: str


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
    """执行绑定流程：校验令牌与占用，写入绑定，回复结果。"""
    tg_user_id = message.from_user.id

    # 发送者若已绑定，直接拒绝。
    if await db.get_binding_by_tg(tg_user_id) is not None:
        await message.answer(cfg.msg("bind_already_bound_tg"))
        return

    row = await db.consume_token(token)
    if row is None:
        await message.answer(cfg.msg("bind_token_not_found"))
        return

    mc_uuid = row["mc_uuid"]
    mc_name = row["mc_name"]
    try:
        await db.create_binding(tg_user_id, mc_uuid, mc_name)
    except IntegrityError:
        # 竞态：令牌消费后、写绑定前，该 mc_uuid（或本 tg 账号）已被占用。
        await message.answer(cfg.msg("bind_already_bound_uuid"))
        return

    await message.answer(cfg.msg("bind_success", mc_name=mc_name))


@router.message(CommandStart(deep_link=True))
async def handle_start_deeplink(
    message: Message, command: CommandObject, db: Database, cfg: Config
) -> None:
    """/start <payload>：payload 是合法令牌则走绑定，否则提示。"""
    payload = (command.args or "").strip()
    if TOKEN_RE.match(payload):
        await _do_bind(message, payload, db, cfg)
    else:
        await message.answer(cfg.msg("start_no_payload"))


@router.message(CommandStart())
async def handle_start_plain(message: Message, cfg: Config) -> None:
    """无 payload 的 /start：引导用户发送令牌。"""
    await message.answer(cfg.msg("start_no_payload"))


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
        await db.set_login_status(request_id, "expired")
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
