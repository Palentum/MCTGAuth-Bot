"""HTTP API（aiohttp）。

供 Fabric 模组轮询调用。全部端点需 Bearer 鉴权（常数时间比较）。
Telegram 相关副作用通过注入的 notifier 完成，便于测试时用桩替换。
"""

import hmac
import logging
import time
import uuid

from aiohttp import web

from .config import Config
from .db import Database
from .ratelimit import FixedWindowLimiter

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


class Notifier:
    """Telegram 通知抽象。生产实现包一层 aiogram Bot，测试注入桩。"""

    async def send_login_prompt(
        self, tg_user_id: int, mc_name: str, ip: str, request_id: str
    ) -> tuple[int, int]:
        """向绑定用户发送带同意/拒绝按钮的登录提示，返回 (chat_id, message_id)。"""
        raise NotImplementedError

    async def close_request_message(
        self, chat_id: int, message_id: int, text: str
    ) -> None:
        """编辑指定消息：替换文本并移除按钮。"""
        raise NotImplementedError


def _json_error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


@web.middleware
async def _bearer_middleware(request: web.Request, handler):
    """校验 Authorization: Bearer <api_secret>，常数时间比较。"""
    cfg: Config = request.app["cfg"]
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {cfg.api_secret}"
    if not hmac.compare_digest(auth, expected):
        return _json_error("unauthorized", "缺少或错误的鉴权凭据。", 401)
    return await handler(request)


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _handle_get_binding(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    mc_uuid = request.match_info["mc_uuid"]
    binding = await db.get_binding_by_uuid(mc_uuid)
    if binding is None:
        return web.json_response({"bound": False})
    return web.json_response(
        {
            "bound": True,
            "tg_user_id": binding["tg_user_id"],
            "mc_name": binding["mc_name"],
        }
    )


async def _handle_register_token(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    reg_limiter: FixedWindowLimiter = request.app["reg_limiter"]

    body = await request.json()
    mc_uuid = body["mc_uuid"]
    mc_name = body["mc_name"]

    # 已绑定则不再签发令牌。
    if await db.get_binding_by_uuid(mc_uuid) is not None:
        return _json_error("already_bound", "该角色已完成绑定。", 409)

    if not reg_limiter.allow(mc_uuid):
        return _json_error("rate_limited", "请求过于频繁，请稍后再试。", 429)

    now = int(time.time())
    token = await db.issue_token(mc_uuid, mc_name, now, now + cfg.token_ttl)

    return web.json_response(
        {
            "token": token["token"],
            "expires_at": token["expires_at"],
            "bot_username": request.app["bot_username"],
        }
    )


async def _handle_login_request(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    notifier: Notifier = request.app["notifier"]
    login_limiter: FixedWindowLimiter = request.app["login_limiter"]

    body = await request.json()
    mc_uuid = body["mc_uuid"]
    mc_name = body["mc_name"]
    ip = body.get("ip")

    binding = await db.get_binding_by_uuid(mc_uuid)
    if binding is None:
        return _json_error("not_bound", "该角色尚未绑定 Telegram 账号。", 404)

    now = int(time.time())
    request_id = str(uuid.uuid4())
    expires_at = now + cfg.login_ttl
    existing, expired = await db.reserve_login_request(
        request_id=request_id,
        mc_uuid=mc_uuid,
        mc_name=mc_name,
        ip=ip,
        created_at=now,
        expires_at=expires_at,
    )
    if existing is not None:
        return web.json_response(
            {"request_id": existing["id"], "expires_at": existing["expires_at"]},
            status=200,
        )

    # 限流：mc_uuid 与 ip 两个维度都要通过。
    allowed = login_limiter.allow(mc_uuid)
    if allowed and ip is not None:
        allowed = login_limiter.allow(f"ip:{ip}")
    if not allowed:
        await db.release_login_request(request_id)
        return _json_error("rate_limited", "登录请求过于频繁，请稍后再试。", 429)

    if (
        expired is not None
        and expired["tg_chat_id"] is not None
        and expired["tg_message_id"] is not None
    ):
        try:
            await notifier.close_request_message(
                expired["tg_chat_id"],
                expired["tg_message_id"],
                cfg.msg("login_expired", mc_name=expired["mc_name"]),
            )
        except Exception:
            log.warning(
                "编辑过期登录消息失败：request_id=%s", expired["id"], exc_info=True
            )

    try:
        chat_id, message_id = await notifier.send_login_prompt(
            binding["tg_user_id"], mc_name, ip or "", request_id
        )
    except Exception:
        await db.release_login_request(request_id)
        log.warning("向用户 %s 发送登录提示失败", binding["tg_user_id"], exc_info=True)
        return _json_error("tg_send_failed", "无法向绑定用户发送 Telegram 消息。", 502)

    await db.set_login_tg_message(request_id, chat_id, message_id)
    return web.json_response(
        {"request_id": request_id, "expires_at": expires_at}, status=201
    )


async def _handle_get_login_request(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    request_id = request.match_info["id"]
    row = await db.get_login_request(request_id)
    if row is None:
        return _json_error("not_found", "登录请求不存在。", 404)
    status = (
        "expired"
        if row["status"] == "pending" and row["expires_at"] <= int(time.time())
        else row["status"]
    )
    return web.json_response({"status": status})


async def _handle_delete_login_request(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    notifier: Notifier = request.app["notifier"]
    request_id = request.match_info["id"]

    row = await db.get_login_request(request_id)
    if row is None:
        return _json_error("not_found", "登录请求不存在。", 404)

    if row["status"] != "pending":
        # 已是终态：原样返回当前状态。
        return web.json_response({"status": row["status"]})

    # pending → cancelled，并编辑 TG 消息移除按钮。
    if not await db.set_login_status(request_id, "cancelled"):
        row = await db.get_login_request(request_id)
        if row is None:
            return _json_error("not_found", "登录请求不存在。", 404)
        return web.json_response({"status": row["status"]})
    if row["tg_chat_id"] is not None and row["tg_message_id"] is not None:
        try:
            await notifier.close_request_message(
                row["tg_chat_id"],
                row["tg_message_id"],
                cfg.msg("login_cancelled", mc_name=row["mc_name"]),
            )
        except Exception:
            log.warning("编辑取消消息失败：request_id=%s", request_id, exc_info=True)
    return web.json_response({"status": "cancelled"})


def build_app(db: Database, cfg: Config, notifier: Notifier) -> web.Application:
    """构造 aiohttp 应用。bot_username 先占位，main 拿到后再回填。"""
    app = web.Application(middlewares=[_bearer_middleware])
    app["db"] = db
    app["cfg"] = cfg
    app["notifier"] = notifier
    app["bot_username"] = ""
    app["reg_limiter"] = FixedWindowLimiter(cfg.register_max_calls, cfg.register_window)
    app["login_limiter"] = FixedWindowLimiter(cfg.login_max_calls, cfg.login_window)

    app.router.add_get(f"{API_PREFIX}/health", _handle_health)
    app.router.add_get(f"{API_PREFIX}/binding/{{mc_uuid}}", _handle_get_binding)
    app.router.add_post(f"{API_PREFIX}/register-token", _handle_register_token)
    app.router.add_post(f"{API_PREFIX}/login-request", _handle_login_request)
    app.router.add_get(f"{API_PREFIX}/login-request/{{id}}", _handle_get_login_request)
    app.router.add_delete(f"{API_PREFIX}/login-request/{{id}}", _handle_delete_login_request)
    return app
