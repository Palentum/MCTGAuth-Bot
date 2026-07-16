"""管理员路由：查看与解除绑定。

路由级过滤 admin_ids；非管理员的消息被静默忽略（不做任何回复）。
"""

import logging
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..config import Config
from ..db import Database

log = logging.getLogger(__name__)

router = Router(name="admin")

PAGE_SIZE = 10


class AdminFilter:
    """仅放行 admin_ids 中的用户。"""

    def __init__(self, admin_ids: set[int]):
        self._admin_ids = admin_ids

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in self._admin_ids


def setup_admin_router(cfg: Config) -> Router:
    """按当前配置的 admin_ids 绑定过滤器并返回路由。"""
    admin_filter = AdminFilter(cfg.admin_ids)
    router.message.filter(admin_filter)
    return router


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@router.message(Command("help"))
async def handle_help(message: Message, cfg: Config) -> None:
    await message.answer(cfg.msg("admin_help"))


@router.message(Command("list"))
async def handle_list(
    message: Message, command: CommandObject, db: Database, cfg: Config
) -> None:
    """/list [页码]：分页显示所有绑定，每页 10 条。"""
    page = 1
    if command.args:
        try:
            page = max(1, int(command.args.strip()))
        except ValueError:
            page = 1

    total = await db.count_bindings()
    if total == 0:
        await message.answer(cfg.msg("admin_list_empty"))
        return

    offset = (page - 1) * PAGE_SIZE
    rows = await db.list_bindings(PAGE_SIZE, offset)
    lines = [cfg.msg("admin_list_header", page=page, total=total)]
    for r in rows:
        lines.append(
            f"<code>{r['tg_user_id']}</code> — <b>{r['mc_name']}</b> "
            f"(<code>{r['mc_uuid']}</code>)"
        )
    await message.answer("\n".join(lines))


@router.message(Command("whois"))
async def handle_whois(
    message: Message, command: CommandObject, db: Database, cfg: Config
) -> None:
    """/whois <mc_name|mc_uuid|tg_id>：查询单条绑定。"""
    if not command.args:
        await message.answer(cfg.msg("admin_whois_usage"))
        return
    key = command.args.strip()

    binding = None
    # 纯数字优先按 tg_id 查。
    if key.isdigit():
        binding = await db.get_binding_by_tg(int(key))
    if binding is None:
        binding = await db.get_binding_by_uuid(key)
    if binding is None:
        binding = await db.get_binding_by_name(key)

    if binding is None:
        await message.answer(cfg.msg("admin_whois_not_found"))
        return

    await message.answer(
        cfg.msg(
            "admin_whois_result",
            tg_user_id=binding["tg_user_id"],
            mc_name=binding["mc_name"],
            mc_uuid=binding["mc_uuid"],
            created_at=_fmt_ts(binding["created_at"]),
        )
    )


@router.message(Command("unbind"))
async def handle_unbind(
    message: Message, command: CommandObject, db: Database, cfg: Config
) -> None:
    """/unbind <mc_name|mc_uuid>：解除绑定并清理其令牌与 pending 登录请求。"""
    if not command.args:
        await message.answer(cfg.msg("admin_unbind_usage"))
        return
    key = command.args.strip()

    binding = await db.delete_binding_by_uuid_or_name(key)
    if binding is None:
        await message.answer(cfg.msg("admin_unbind_not_found"))
        return

    # 编辑被取消的登录请求消息，移除按钮。
    cancelled = binding.get("cancelled_requests", [])
    for req in cancelled:
        if req.get("tg_chat_id") is not None and req.get("tg_message_id") is not None:
            try:
                await message.bot.edit_message_text(
                    text=cfg.msg("login_cancelled", mc_name=req["mc_name"]),
                    chat_id=req["tg_chat_id"],
                    message_id=req["tg_message_id"],
                )
            except Exception:
                log.warning(
                    "解绑时编辑登录消息失败：request_id=%s", req.get("id"), exc_info=True
                )

    await message.answer(
        cfg.msg(
            "admin_unbind_success",
            mc_name=binding["mc_name"],
            mc_uuid=binding["mc_uuid"],
        )
    )
