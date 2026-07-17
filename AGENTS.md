# Repository Guidelines

## Project Overview

`mctgauth-bot` 是 Minecraft 离线模式服务器的 Telegram 登录认证 **Bot 端**。本服务持有唯一 SQLite 数据库，负责账号绑定、一次性绑定令牌和登录审批；Fabric 模组位于独立仓库，只通过 Bearer HTTP API 发起请求并轮询结果。

## Architecture & Data Flow

- `src/mctgauth_bot/main.py` 是组合根：加载配置，初始化 SQLite、aiogram `Bot`/`Dispatcher`、aiohttp API 和后台 sweeper，并在退出时反向清理。
- 登录主链路：Fabric `POST /api/v1/login-request` → `api.py` 查绑定、复用 pending 请求并限流 → `Notifier` 发送 Telegram 审批消息 → `db.py` 写入请求 → `handlers/user.py` 将状态原子更新为 `approved`/`denied` → Fabric 轮询 `GET /api/v1/login-request/{id}`。断线可 `DELETE` 为 `cancelled`，超时由 `sweeper.py` 置为 `expired`。
- 持久状态在 SQLite：`bindings`、`pending_tokens`、`login_requests`。`Database` 使用单个 aiosqlite 连接和 `asyncio.Lock`；唯一约束及 pending 部分唯一索引维护并发不变量。
- 依赖方向为 `main` → `api`/`handlers`/`sweeper` → `db`。aiogram 通过 Dispatcher workflow data 按参数名注入 `db`/`cfg`；aiohttp 通过 `app[...]` 注入依赖；Telegram 副作用统一经 `Notifier`，测试使用桩实现。
- 限流器 `FixedWindowLimiter` 仅驻留内存，进程重启后清空；业务状态不得放入 handler 局部或模组端持久化。

## Key Directories

- `src/mctgauth_bot/`：应用包；包含进程入口、配置、HTTP API、数据库、令牌、限流和清理任务。
- `src/mctgauth_bot/handlers/`：aiogram 路由；`user.py` 处理绑定、面板和审批，`admin.py` 处理管理命令。
- `tests/`：pytest 测试；按模块拆为 API、DB、panel、ratelimit、sweeper、tokens。
- `docs/`：变更记录和已实施任务说明；API 契约以 `README.md` 的 `/api/v1` 章节为准。
- 仓库没有 `scripts/`、`bin/` 或 `tools/`；不要假设存在额外脚本入口。

## Development Commands

```bash
uv sync
cp .env.example .env                  # 填写并自行导出 BOT_TOKEN、API_SECRET
cp config.example.toml config.toml    # 或使用 TOML 配置
uv run mctgauth-bot
uv run mctgauth-bot --config /path/to/config.toml
uv run pytest -q
uv run pytest -q tests/test_api.py
uv run pytest -q tests/test_api.py::test_auth_missing
```

仓库只配置了 Hatchling 构建后端，没有项目专用的 build、lint、format 或 type-check 命令，也未配置 Ruff、Black、Mypy 或 pre-commit；不要虚构质量门禁。

## Code Conventions & Common Patterns

- 使用 `snake_case` 函数/变量、`PascalCase` 类和 CallbackData、`UPPER_CASE` 常量；保留现有类型注解和中文 docstring/用户消息风格。
- I/O 全程使用 `async`/`await`。后台任务用 `asyncio.create_task`，取消时捕获 `asyncio.CancelledError`，资源在 `finally` 中关闭；不要在事件循环里加入阻塞 I/O。
- 数据访问集中在 `Database`。新增读写沿用 `async with self._lock`；写操作显式 `commit()`，行转换为普通 `dict`。优先用数据库约束和带 `status='pending'` 的条件更新保护并发状态转换。
- HTTP 错误沿用 `_json_error`，响应形状为 `{"error":"<code>","message":"<中文说明>"}`；保持 `/api/v1` Bearer 鉴权和既有状态码契约。
- Telegram 通知失败在 API 边界转为 `502 tg_send_failed`，且失败时不落库；非关键消息编辑失败记录 warning 后继续。配置错误抛 `ConfigError`，入口转为中文退出信息。
- 配置通过 `Config` dataclass 传递；用户可见文案经 `cfg.msg(...)` 获取，新增可覆盖文案时同步 `DEFAULT_MESSAGES` 与 `config.example.toml`。
- 测试或新边界代码复用现有注入点；不要在 `api.py` 直接构造真实 Telegram Bot，也不要增加第二套状态管理或配置模式。

## Important Files

| Path | Purpose |
| --- | --- |
| `pyproject.toml` | Python 版本、依赖、console entry point、Hatchling 和 pytest 配置 |
| `uv.lock` | uv 锁定依赖版本；应随依赖变更同步 |
| `src/mctgauth_bot/main.py` | `mctgauth-bot` 入口和服务生命周期 |
| `src/mctgauth_bot/config.py` | 默认值、TOML/环境变量合并及用户文案 |
| `src/mctgauth_bot/api.py` | `/api/v1` 路由、鉴权、限流和 `Notifier` 接口 |
| `src/mctgauth_bot/db.py` | SQLite schema、约束和全部数据访问 |
| `src/mctgauth_bot/handlers/user.py` | 绑定、用户面板、登录审批状态转换 |
| `src/mctgauth_bot/handlers/admin.py` | 管理员过滤器及查询/解绑命令 |
| `src/mctgauth_bot/sweeper.py` | 过期令牌和登录请求清理 |
| `tests/conftest.py` | 临时数据库、aiohttp client、鉴权头和 `StubNotifier` |
| `README.md` | 启动说明、安全约束和权威 HTTP API 契约 |

## Runtime/Tooling Preferences

- 要求 CPython `>=3.12`；使用 `uv` 和已提交的 `uv.lock`，命令优先写成 `uv run ...`，不要改用全局 pip 环境。
- 主要运行依赖为 aiogram 3.x 与 aiosqlite；HTTP 层由 aiogram 依赖链中的 aiohttp 提供。打包使用 Hatchling，源码布局为 `src/`。
- 配置优先级：内置默认 < `config.toml` < `BOT_TOKEN`/`API_SECRET` 环境变量。`.env` 不会由应用自动加载，必须先导出或交给进程管理器加载。
- `config.toml`、`.env`、`*.db`、WAL/SHM 文件和 `.venv/` 均为本地生成或敏感文件，不得提交。默认 API 只监听 `127.0.0.1:8632`。

## Testing & QA

- 测试栈：pytest、pytest-asyncio、pytest-aiohttp；`asyncio_mode = "auto"`，异步测试无需额外 `@pytest.mark.asyncio`。
- `tests/conftest.py` 使用 `tmp_path` 创建隔离 SQLite，`aiohttp_client` 验证 HTTP 行为，`StubNotifier` 记录 Telegram 副作用并模拟发送失败。沿用这些 fixture，避免访问真实 Telegram 或持久数据库。
- 最小验证：API 改动跑 `tests/test_api.py`，DB 改动跑 `tests/test_db.py`，其他模块跑同名测试文件；跨模块或契约改动跑 `uv run pytest -q`。
- 测试应验证可观察契约：HTTP 状态/JSON、幂等、数据库唯一性和状态转换、限流窗口、过期清理及通知副作用。仓库当前没有覆盖率工具或最低覆盖率门槛。
