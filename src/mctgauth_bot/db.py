"""SQLite 数据层。

单个共享 aiosqlite 连接 + asyncio.Lock 串行化所有访问，开启 WAL。
1:1 绑定由 UNIQUE 约束（tg_user_id、mc_uuid）保证；同一 mc_uuid 至多一条
pending 登录请求由部分唯一索引保证。方法均返回普通 dict（sqlite3.Row 转换）。
"""

import asyncio
import time

import aiosqlite

from .tokens import generate_token

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
  tg_user_id INTEGER NOT NULL UNIQUE,
  mc_uuid    TEXT    NOT NULL UNIQUE,
  mc_name    TEXT    NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_tokens (
  token      TEXT PRIMARY KEY,
  mc_uuid    TEXT NOT NULL UNIQUE,
  mc_name    TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_requests (
  id            TEXT PRIMARY KEY,
  mc_uuid       TEXT NOT NULL,
  mc_name       TEXT NOT NULL,
  ip            TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  tg_chat_id    INTEGER,
  tg_message_id INTEGER,
  created_at    INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_login_pending
  ON login_requests(mc_uuid) WHERE status='pending';
"""


def _row_to_dict(row: aiosqlite.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Database:
    """封装唯一连接的异步数据访问对象。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        """打开连接、设置 WAL、建表建索引。"""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- bindings ----

    async def get_binding_by_uuid(self, mc_uuid: str) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM bindings WHERE mc_uuid=?", (mc_uuid,)
            )
            return _row_to_dict(await cur.fetchone())

    async def get_binding_by_tg(self, tg_user_id: int) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM bindings WHERE tg_user_id=?", (tg_user_id,)
            )
            return _row_to_dict(await cur.fetchone())

    async def get_binding_by_name(self, mc_name: str) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM bindings WHERE mc_name=?", (mc_name,)
            )
            return _row_to_dict(await cur.fetchone())

    async def create_binding(
        self, tg_user_id: int, mc_uuid: str, mc_name: str, created_at: int | None = None
    ) -> None:
        """插入绑定；tg_user_id 或 mc_uuid 冲突会抛 IntegrityError。"""
        if created_at is None:
            created_at = int(time.time())
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO bindings (tg_user_id, mc_uuid, mc_name, created_at) VALUES (?, ?, ?, ?)",
                (tg_user_id, mc_uuid, mc_name, created_at),
            )
            await self._conn.commit()

    async def list_bindings(self, limit: int, offset: int) -> list[dict]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM bindings ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def count_bindings(self) -> int:
        async with self._lock:
            cur = await self._conn.execute("SELECT COUNT(*) AS c FROM bindings")
            row = await cur.fetchone()
            return int(row["c"])

    async def delete_binding_by_uuid_or_name(self, key: str) -> dict | None:
        """按 mc_uuid 或 mc_name 删除一条绑定，返回被删的行；无匹配返回 None。

        同时清除该 mc_uuid 的 pending 令牌，并把其 pending 登录请求置为
        cancelled（返回值供调用方去编辑对应 TG 消息）。
        """
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM bindings WHERE mc_uuid=? OR mc_name=?", (key, key)
            )
            binding = _row_to_dict(await cur.fetchone())
            if binding is None:
                return None
            mc_uuid = binding["mc_uuid"]
            # 收集需要编辑 TG 消息的 pending 登录请求。
            cur = await self._conn.execute(
                "SELECT * FROM login_requests WHERE mc_uuid=? AND status='pending'",
                (mc_uuid,),
            )
            pending_requests = [dict(r) for r in await cur.fetchall()]
            await self._conn.execute("DELETE FROM bindings WHERE mc_uuid=?", (mc_uuid,))
            await self._conn.execute(
                "DELETE FROM pending_tokens WHERE mc_uuid=?", (mc_uuid,)
            )
            await self._conn.execute(
                "UPDATE login_requests SET status='cancelled' WHERE mc_uuid=? AND status='pending'",
                (mc_uuid,),
            )
            await self._conn.commit()
            binding["cancelled_requests"] = pending_requests
            return binding

    # ---- pending_tokens ----

    async def issue_token(
        self, mc_uuid: str, mc_name: str, now: int, expires_at: int
    ) -> dict:
        """原子地复用未过期令牌或签发新令牌，并刷新 mc_name。"""
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await self._conn.execute(
                    "SELECT * FROM pending_tokens WHERE mc_uuid=? AND expires_at>?",
                    (mc_uuid, now),
                )
                token = _row_to_dict(await cur.fetchone())
                if token is not None:
                    await self._conn.execute(
                        "UPDATE pending_tokens SET mc_name=? WHERE token=?",
                        (mc_name, token["token"]),
                    )
                    token["mc_name"] = mc_name
                else:
                    token = {
                        "token": generate_token(),
                        "mc_uuid": mc_uuid,
                        "mc_name": mc_name,
                        "expires_at": expires_at,
                    }
                    await self._conn.execute(
                        "INSERT INTO pending_tokens (token, mc_uuid, mc_name, expires_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(mc_uuid) DO UPDATE SET "
                        "token=excluded.token, mc_name=excluded.mc_name, "
                        "expires_at=excluded.expires_at",
                        (
                            token["token"],
                            token["mc_uuid"],
                            token["mc_name"],
                            token["expires_at"],
                        ),
                    )
                await self._conn.commit()
                return token
            except Exception:
                await self._conn.rollback()
                raise

    async def get_live_token_for_uuid(self, mc_uuid: str, now: int | None = None) -> dict | None:
        """返回该 mc_uuid 未过期的令牌行；无则 None。"""
        if now is None:
            now = int(time.time())
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM pending_tokens WHERE mc_uuid=? AND expires_at>?",
                (mc_uuid, now),
            )
            return _row_to_dict(await cur.fetchone())

    async def upsert_token(
        self, token: str, mc_uuid: str, mc_name: str, expires_at: int
    ) -> None:
        """按 mc_uuid 写入/更新令牌（覆盖旧令牌与 mc_name）。"""
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO pending_tokens (token, mc_uuid, mc_name, expires_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(mc_uuid) DO UPDATE SET "
                "token=excluded.token, mc_name=excluded.mc_name, expires_at=excluded.expires_at",
                (token, mc_uuid, mc_name, expires_at),
            )
            await self._conn.commit()

    async def consume_token(self, token: str, now: int | None = None) -> dict | None:
        """取出并删除一个未过期令牌（一次性）；不存在或已过期返回 None。"""
        if now is None:
            now = int(time.time())
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM pending_tokens WHERE token=? AND expires_at>?",
                (token, now),
            )
            row = _row_to_dict(await cur.fetchone())
            if row is None:
                return None
            await self._conn.execute(
                "DELETE FROM pending_tokens WHERE token=?", (token,)
            )
            await self._conn.commit()
            return row

    async def consume_token_and_create_binding(
        self, tg_user_id: int, token: str, now: int | None = None
    ) -> tuple[str, dict | None]:
        """原子消费令牌并创建绑定，返回结果状态与成功消费的令牌行。"""
        if now is None:
            now = int(time.time())
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await self._conn.execute(
                    "SELECT 1 FROM bindings WHERE tg_user_id=?", (tg_user_id,)
                )
                if await cur.fetchone() is not None:
                    await self._conn.rollback()
                    return "tg_conflict", None

                cur = await self._conn.execute(
                    "SELECT * FROM pending_tokens WHERE token=? AND expires_at>?",
                    (token, now),
                )
                row = _row_to_dict(await cur.fetchone())
                if row is None:
                    await self._conn.rollback()
                    return "token_not_found", None

                cur = await self._conn.execute(
                    "SELECT 1 FROM bindings WHERE mc_uuid=?", (row["mc_uuid"],)
                )
                if await cur.fetchone() is not None:
                    await self._conn.rollback()
                    return "uuid_conflict", None

                await self._conn.execute(
                    "INSERT INTO bindings (tg_user_id, mc_uuid, mc_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tg_user_id, row["mc_uuid"], row["mc_name"], now),
                )
                await self._conn.execute(
                    "DELETE FROM pending_tokens WHERE token=?", (token,)
                )
                await self._conn.commit()
                return "success", row
            except Exception:
                await self._conn.rollback()
                raise

    async def delete_expired_tokens(self, now: int | None = None) -> int:
        if now is None:
            now = int(time.time())
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM pending_tokens WHERE expires_at<=?", (now,)
            )
            await self._conn.commit()
            return cur.rowcount

    # ---- login_requests ----

    async def get_pending_for_uuid(self, mc_uuid: str, now: int | None = None) -> dict | None:
        """返回该 mc_uuid 未过期的 pending 登录请求；无则 None。"""
        if now is None:
            now = int(time.time())
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM login_requests WHERE mc_uuid=? AND status='pending' AND expires_at>?",
                (mc_uuid, now),
            )
            return _row_to_dict(await cur.fetchone())

    async def create_login_request(
        self,
        request_id: str,
        mc_uuid: str,
        mc_name: str,
        ip: str | None,
        created_at: int,
        expires_at: int,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
    ) -> None:
        """插入一条 pending 登录请求；违反部分唯一索引会抛 IntegrityError。"""
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO login_requests "
                "(id, mc_uuid, mc_name, ip, status, tg_chat_id, tg_message_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (request_id, mc_uuid, mc_name, ip, tg_chat_id, tg_message_id, created_at, expires_at),
            )
            await self._conn.commit()

    async def reserve_login_request(
        self,
        request_id: str,
        mc_uuid: str,
        mc_name: str,
        ip: str | None,
        created_at: int,
        expires_at: int,
    ) -> tuple[dict | None, dict | None]:
        """原子地复用未过期请求，或过期旧请求并预留新请求。

        返回 (existing, expired)：existing 非空表示复用；否则已插入一条
        尚无 Telegram 消息 ID 的 pending 请求。
        """
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM login_requests WHERE mc_uuid=? AND status='pending'",
                (mc_uuid,),
            )
            pending = _row_to_dict(await cur.fetchone())
            if pending is not None and pending["expires_at"] > created_at:
                return pending, None

            try:
                if pending is not None:
                    await self._conn.execute(
                        "UPDATE login_requests SET status='expired' WHERE id=?",
                        (pending["id"],),
                    )
                await self._conn.execute(
                    "INSERT INTO login_requests "
                    "(id, mc_uuid, mc_name, ip, status, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (request_id, mc_uuid, mc_name, ip, created_at, expires_at),
                )
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise
            return None, pending

    async def release_login_request(self, request_id: str) -> None:
        """删除尚未发出 Telegram 消息的预留请求。"""
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM login_requests "
                "WHERE id=? AND status='pending' "
                "AND tg_chat_id IS NULL AND tg_message_id IS NULL",
                (request_id,),
            )
            await self._conn.commit()

    async def set_login_tg_message(
        self, request_id: str, tg_chat_id: int, tg_message_id: int
    ) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE login_requests SET tg_chat_id=?, tg_message_id=? WHERE id=?",
                (tg_chat_id, tg_message_id, request_id),
            )
            await self._conn.commit()

    async def get_login_request(self, request_id: str) -> dict | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM login_requests WHERE id=?", (request_id,)
            )
            return _row_to_dict(await cur.fetchone())

    async def set_login_status(self, request_id: str, status: str) -> bool:
        """把请求从 pending 改为新状态；仅当当前为 pending 才生效。

        返回 True 表示成功转移，False 表示当前非 pending（含不存在）。
        """
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE login_requests SET status=? WHERE id=? AND status='pending'",
                (status, request_id),
            )
            await self._conn.commit()
            return cur.rowcount > 0

    async def expire_stale(self, now: int | None = None) -> list[dict]:
        """把已过期的 pending 登录请求置为 expired，返回被置为过期的行。

        返回值供调用方编辑对应 TG 消息（移除按钮、标注过期）。
        """
        if now is None:
            now = int(time.time())
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM login_requests WHERE status='pending' AND expires_at<=?",
                (now,),
            )
            stale = [dict(r) for r in await cur.fetchall()]
            if stale:
                await self._conn.execute(
                    "UPDATE login_requests SET status='expired' WHERE status='pending' AND expires_at<=?",
                    (now,),
                )
                await self._conn.commit()
            return stale
