"""进程入口。

启动顺序：init db → 建 Bot/Dispatcher/routers → get_me 缓存用户名 →
起 aiohttp（AppRunner+TCPSite）→ 起 sweeper → dp.start_polling。
退出时反向清理。
"""

import argparse
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiohttp import web

from .api import Notifier, build_app
from .config import Config, ConfigError, load_config
from .db import Database
from .handlers import admin as admin_handlers
from .handlers import user as user_handlers
from .sweeper import run_sweeper

log = logging.getLogger(__name__)


class BotNotifier(Notifier):
    """基于 aiogram Bot 的生产通知实现。"""

    def __init__(self, bot: Bot, cfg: Config):
        self._bot = bot
        self._cfg = cfg

    async def send_login_prompt(
        self, tg_user_id: int, mc_name: str, ip: str, request_id: str
    ) -> tuple[int, int]:
        keyboard = user_handlers.build_login_keyboard(self._cfg, request_id)
        try:
            msg = await self._bot.send_message(
                chat_id=tg_user_id,
                text=self._cfg.msg("login_prompt", mc_name=mc_name, ip=ip),
                reply_markup=keyboard,
            )
        except TelegramForbiddenError:
            # 用户拉黑了 Bot：向上抛，由 API 层转成 502 tg_send_failed。
            raise
        return msg.chat.id, msg.message_id

    async def close_request_message(
        self, chat_id: int, message_id: int, text: str
    ) -> None:
        await self._bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id
        )


async def run(cfg: Config) -> None:
    """组装并运行整个服务，直到轮询停止。"""
    db = Database(cfg.db_path)
    await db.init()

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notifier = BotNotifier(bot, cfg)

    dp = Dispatcher()
    # 通过 workflow_data 注入依赖，供 handler 参数按名取用。
    dp["db"] = db
    dp["cfg"] = cfg
    # ping 路由最先注册：/ping 在任意 chat 类型中均可响应；
    # admin / user 路由自带仅私聊过滤，群组内其余命令静默忽略。
    dp.include_router(user_handlers.ping_router)
    dp.include_router(admin_handlers.setup_admin_router(cfg))
    dp.include_router(user_handlers.router)

    # 缓存 bot 用户名，供 register-token 响应拼 deep-link。
    me = await bot.get_me()
    bot_username = me.username

    app = build_app(db, cfg, notifier)
    app["bot_username"] = bot_username

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.listen_host, cfg.listen_port)
    await site.start()
    log.info("HTTP API 监听于 %s:%s", cfg.listen_host, cfg.listen_port)

    sweeper_task = asyncio.create_task(run_sweeper(db, cfg, notifier))

    try:
        await dp.start_polling(bot)
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except asyncio.CancelledError:
            pass
        await runner.cleanup()
        await bot.session.close()
        await db.close()


def cli() -> None:
    """同步入口：解析 --config，加载配置后运行。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Minecraft Telegram 登录认证 Bot 服务")
    parser.add_argument(
        "--config",
        default="config.toml",
        help="配置文件路径（默认 ./config.toml，缺失则全部使用默认值）",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        raise SystemExit(f"配置错误：{e}")

    asyncio.run(run(cfg))
