# mctgauth-bot

Minecraft 离线模式服务器的 Telegram 登录认证系统——**Bot 端**。

## 项目简介

离线模式（offline-mode）的 Minecraft 服务器无法通过正版验证确认玩家身份，任何人只要知道用户名即可登入。本系统用 Telegram 账号为离线服务器补上一层身份认证：

- **本仓库（Bot 端）**：持有唯一的 SQLite 数据库（绑定关系、绑定令牌、登录请求），运行 aiogram long-polling 处理 Telegram 交互，并对外暴露一套 Bearer 鉴权的 HTTP API。
- **模组端（Fabric mod，独立仓库）**：玩家进入服务器时，模组调用本服务发起登录请求，然后**轮询**本服务查询审批结果。模组从不被本服务反向连接。

数据与消息流向：

```
玩家加入服务器
      │
      ▼
Fabric 模组 ──HTTP POST /login-request──▶ Bot 服务 ──发送审批消息──▶ Telegram 用户
      │                                        │                         │
      │◀────轮询 GET /login-request/{id}───────┤◀──点击 同意/拒绝 按钮────┘
      ▼
放行 / 踢出
```

**Bot 端是数据的唯一拥有者**；模组端不持久化任何认证状态，只按 request_id 轮询。

配套 Fabric 模组位于**独立仓库**，本仓库不引用其路径。

## 快速开始

1. 用 [BotFather](https://t.me/BotFather) 创建 Bot，拿到 Token。
2. 安装依赖：

   ```bash
   uv sync
   ```

3. 配置密钥（二选一，环境变量优先）：

   - 环境变量 / `.env`（推荐）：

     ```bash
     cp .env.example .env
     # 编辑 .env，填入 BOT_TOKEN 与 API_SECRET
     # API_SECRET 生成：openssl rand -hex 32
     ```

   - 或 `config.toml`：

     ```bash
     cp config.example.toml config.toml
     # 编辑 config.toml
     ```

4. 启动：

   ```bash
   uv run mctgauth-bot                 # 使用 ./config.toml
   uv run mctgauth-bot --config /path/to/config.toml
   ```

   `.env` 中的变量需自行导出到环境，或用支持 `.env` 的进程管理器加载。缺少 `BOT_TOKEN` 或 `API_SECRET` 时会以中文错误信息退出。

## 配置参考

配置来源优先级（低 → 高）：内置默认 < `config.toml` < 环境变量。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `bot_token` | string | 无 | Telegram Bot Token。可用环境变量 `BOT_TOKEN` 覆盖。必填。 |
| `api_secret` | string | 无 | HTTP API 的 Bearer 密钥。可用环境变量 `API_SECRET` 覆盖。必填。 |
| `listen_host` | string | `127.0.0.1` | HTTP API 监听地址。 |
| `listen_port` | int | `8632` | HTTP API 监听端口。 |
| `admin_ids` | int[] | `[]` | 管理员 Telegram 用户 ID。 |
| `token_ttl` | int | `300` | 绑定令牌有效期（秒）。 |
| `login_ttl` | int | `300` | 登录请求有效期（秒）。 |
| `register_max_calls` | int | `5` | register-token 限流次数。 |
| `register_window` | int | `600` | register-token 限流窗口（秒）。 |
| `login_max_calls` | int | `10` | login-request 限流次数。 |
| `login_window` | int | `600` | login-request 限流窗口（秒）。 |
| `db_path` | string | `mctgauth.db` | SQLite 文件路径。 |
| `[messages]` | table | 见 `config.py` | 用户可见文案覆盖，未列出的键用内置中文默认。 |

## Telegram 命令

**普通用户：**

- `/start` — 无参数时打开用户面板：显示绑定状态（角色名、绑定时间），内联按钮可 **刷新** 状态或切换到 **帮助** 视图（消息原地编辑，不刷屏）。
- `/start <令牌>` — 通过 deep-link 携带令牌自动绑定。
- `/help` — 显示使用帮助（仅用户命令，不含管理员命令）。
- `/address` — 回复配置的服务器地址文案（`config.toml` 的 `[messages]` 中 `address` 键，支持 HTML）。
- `/ping` — 回复存活确认文案（`[messages]` 中 `ping` 键），用于检测 Bot 是否在线。
- 直接发送 8 位令牌文本 — 完成绑定。
- 登录审批消息上的 **同意 / 拒绝** 按钮 — 批准或拒绝一次登录。

**管理员（仅 `admin_ids` 中的用户，其余人静默忽略）：**

- `/list [页码]` — 分页查看所有绑定（每页 10 条）。
- `/whois <mc_name|mc_uuid|tg_id>` — 查询单条绑定。
- `/unbind <mc_name|mc_uuid>` — 解除绑定，并清理其令牌、取消其 pending 登录请求。
- `/help` — 列出管理员命令（普通用户的 `/help` 只显示用户帮助）。

## HTTP API 契约（v1，权威版本）

**本节是 API 契约的权威定义**，模组端必须与此一致。

- 基础路径：`/api/v1`
- 鉴权：每个请求都需 `Authorization: Bearer <api_secret>`；缺失或错误 → `401`。服务端用 `hmac.compare_digest` 常数时间比较。
- 请求与响应体均为 UTF-8 JSON。
- 错误体统一形状：`{"error":"<机器码>","message":"<中文说明>"}`。
- 请求体非法（畸形 JSON、非对象、必填字段缺失或类型不符）→ `400` `bad_request`。

### `GET /api/v1/health`

健康检查。

- `200` → `{"ok":true}`

### `GET /api/v1/binding/{mc_uuid}`

查询某 mc_uuid 的绑定情况。

- 已绑定 → `200` `{"bound":true,"tg_user_id":123,"mc_name":"Steve"}`
- 未绑定 → `200` `{"bound":false}`

### `POST /api/v1/register-token`

为玩家签发绑定令牌。**幂等**：若该 mc_uuid 已有未过期令牌，返回**同一令牌**（并刷新存储的 mc_name）。

请求体：

```json
{"mc_uuid": "…", "mc_name": "Steve"}
```

- `200` → `{"token":"AB2CD3EF","expires_at":1730000000,"bot_username":"YourBot"}`
- `409` `already_bound` — 该 mc_uuid 已完成绑定。
- `429` `rate_limited` — 触发限流。

`token` 字母表为 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（无 `I`、`O`、`0`、`1`），长度 8。玩家可发送 `https://t.me/<bot_username>?start=<token>` 或直接把令牌发给 Bot 完成绑定。

### `POST /api/v1/login-request`

发起一次登录审批。**副作用**：向绑定用户发送中文提示「`<mc_name> 正在从 IP <ip> 请求登录服务器`」并附「同意 / 拒绝」按钮。

请求体：

```json
{"mc_uuid": "…", "mc_name": "Steve", "ip": "1.2.3.4"}
```

- `201` → `{"request_id":"<uuid4>","expires_at":1730000000}` — 新建成功。
- `200` → `{"request_id":"<已存在id>","expires_at":…}` — 该 mc_uuid 已有未过期 pending 请求，返回既有 request_id，**不发第二条消息**。
- `404` `not_bound` — 该 mc_uuid 未绑定。
- `429` `rate_limited` — 触发限流（mc_uuid 与 ip 分别计，任一超限即拒）。
- `502` `tg_send_failed` — Telegram 发送失败（如用户拉黑了 Bot）。此时**不创建请求**。

### `GET /api/v1/login-request/{id}`

轮询登录请求状态。

- `200` → `{"status":"pending"|"approved"|"denied"|"expired"|"cancelled"}`
- `404` `not_found` — 未知 id。

### `DELETE /api/v1/login-request/{id}`

取消一次登录请求（玩家断线/超时时由模组调用）。

- 若为 `pending`：置为 `cancelled`，编辑对应 TG 消息（移除按钮、标注取消），返回 `200` `{"status":"cancelled"}`。
- 若已是终态：返回 `200`，`status` 为当前值。
- `404` `not_found` — 未知 id。

## 安全说明

- **默认仅监听 `127.0.0.1`**。当模组与本服务在同一台机器时无需额外配置。
- 若模组运行在另一台机器，**不要**把监听地址改成 `0.0.0.0` 直接暴露。请改用：
  - SSH 隧道：`ssh -L 8632:127.0.0.1:8632 user@bot-host`，模组连本地转发端口；或
  - 前置反向代理（Nginx/Caddy）并启用 TLS，代理到本地回环端口。
- `api_secret` 用 `openssl rand -hex 32` 生成，务必足够随机；泄露等于放行任意登录审批。
- `config.toml`、`.env`、`*.db` 均已在 `.gitignore` 中，切勿提交。

## 数据库

单个 SQLite 文件（默认 `mctgauth.db`，WAL 模式）。表结构：

- `bindings` — TG 账号 ↔ 游戏角色的 1:1 绑定（`tg_user_id`、`mc_uuid` 双向 `UNIQUE`）。
- `pending_tokens` — 未消费的绑定令牌，`mc_uuid` 唯一，带 `expires_at`。
- `login_requests` — 登录请求及其状态与关联 TG 消息，部分唯一索引保证同一 `mc_uuid` 至多一条 `pending`。

备份：直接复制 `.db` 文件即可（WAL 模式下建议连同 `-wal`、`-shm` 一并复制，或在服务停止时复制）。

## 开发

```bash
uv sync
uv run pytest -q
```
