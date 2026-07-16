"""周期清理任务。

每 30 秒：删除过期令牌；把过期的 pending 登录请求置为 expired，
并编辑其 TG 消息（移除按钮、标注过期）。对异常鲁棒：记录后继续。
"""

import asyncio
import logging

from .api import Notifier
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

SWEEP_INTERVAL = 30


async def sweep_once(db: Database, cfg: Config, notifier: Notifier) -> None:
    """执行一轮清理。"""
    await db.delete_expired_tokens()
    stale = await db.expire_stale()
    for req in stale:
        if req.get("tg_chat_id") is not None and req.get("tg_message_id") is not None:
            try:
                await notifier.close_request_message(
                    req["tg_chat_id"],
                    req["tg_message_id"],
                    cfg.msg("login_expired", mc_name=req["mc_name"]),
                )
            except Exception:
                log.warning(
                    "清理过期登录请求时编辑消息失败：request_id=%s",
                    req.get("id"),
                    exc_info=True,
                )


async def run_sweeper(db: Database, cfg: Config, notifier: Notifier) -> None:
    """清理循环，直到被取消。"""
    while True:
        try:
            await sweep_once(db, cfg, notifier)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("清理任务发生异常，将在下一轮继续")
        await asyncio.sleep(SWEEP_INTERVAL)
