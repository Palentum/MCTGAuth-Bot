"""配置加载。

优先级（从低到高）：内置默认 < config.toml < 环境变量（BOT_TOKEN、API_SECRET）。
config.toml 缺失时全部走默认。bot_token 或 api_secret 为空则立即报错。
"""

import html
import os
import tomllib
from dataclasses import dataclass, field


class ConfigError(Exception):
    """配置无效时抛出，消息为面向用户的中文说明。"""


# 所有对用户可见的中文文案的默认值。
# 支持占位符的文案用 str.format 传参（占位符名见各处调用）。
DEFAULT_MESSAGES: dict[str, str] = {
    # 绑定流程
    "bind_success": "绑定成功！你的 Telegram 账号已关联游戏角色 <b>{mc_name}</b>。\n请回到游戏中执行 <code>/account login</code> 完成登录。",
    "bind_token_not_found": "令牌无效或已过期，请回到游戏重新获取绑定令牌。",
    "bind_already_bound_tg": "你的 Telegram 账号已绑定过一个游戏角色，无法重复绑定。",
    "bind_already_bound_uuid": "该游戏角色已被其他 Telegram 账号绑定。",
    # 用户面板
    "panel_bound": (
        "<b>用户面板</b>\n\n"
        "已绑定角色：<b>{mc_name}</b>\n"
        "绑定时间：{created_at}\n\n"
        "登录服务器时，我会向你发送审批消息。"
    ),
    "panel_unbound": (
        "<b>用户面板</b>\n\n"
        "当前未绑定任何游戏角色。\n"
        "请把游戏中获得的 8 位绑定令牌直接发送给我完成绑定。"
    ),
    "panel_help": (
        "<b>使用帮助</b>\n\n"
        "1. 进入游戏服务器，执行 <code>/account register</code> 获取 8 位绑定令牌；\n"
        "2. 把令牌直接发送给我，完成账号绑定；\n"
        "3. 回到游戏执行 <code>/account login</code>，然后点击我发来的审批消息中的「同意」即可进入。\n\n"
        "命令：\n"
        "<code>/start</code> — 打开用户面板\n"
        "<code>/help</code> — 显示本帮助"
    ),
    "panel_btn_refresh": "🔄 刷新",
    "panel_btn_help": "❓ 帮助",
    "panel_btn_back": "⬅️ 返回",
    # 登录审批
    "login_prompt": "<b>{mc_name}</b> 正在从 IP <code>{ip}</code> 请求登录服务器。",
    "login_btn_approve": "同意",
    "login_btn_deny": "拒绝",
    "login_approved": "已同意 <b>{mc_name}</b> 的登录请求。",
    "login_denied": "已拒绝 <b>{mc_name}</b> 的登录请求。",
    "login_expired": "<b>{mc_name}</b> 的登录请求已过期。",
    "login_cancelled": "<b>{mc_name}</b> 的登录请求已被服务器取消。",
    "login_cb_expired": "该登录请求已过期。",
    "login_cb_not_pending": "该登录请求已被处理。",
    "login_cb_not_owner": "你无权处理此登录请求。",
    "login_cb_approved": "已同意登录。",
    "login_cb_denied": "已拒绝登录。",
    # 管理命令
    "admin_help": (
        "管理员命令：\n"
        "<code>/list [页码]</code> — 分页查看所有绑定\n"
        "<code>/whois &lt;mc_name|mc_uuid|tg_id&gt;</code> — 查询单条绑定\n"
        "<code>/unbind &lt;mc_name|mc_uuid&gt;</code> — 解除绑定"
    ),
    "admin_list_empty": "当前没有任何绑定记录。",
    "admin_list_header": "绑定列表（第 {page} 页，共 {total} 条）：",
    "admin_whois_usage": "用法：<code>/whois &lt;mc_name|mc_uuid|tg_id&gt;</code>",
    "admin_whois_not_found": "未找到匹配的绑定记录。",
    "admin_whois_result": "TG 用户：<code>{tg_user_id}</code>\n角色名：<b>{mc_name}</b>\nUUID：<code>{mc_uuid}</code>\n绑定时间：{created_at}",
    "admin_unbind_usage": "用法：<code>/unbind &lt;mc_name|mc_uuid&gt;</code>",
    "admin_unbind_not_found": "未找到匹配的绑定记录。",
    "admin_unbind_success": "已解除绑定：<b>{mc_name}</b>（UUID <code>{mc_uuid}</code>）。",
}


@dataclass
class Config:
    """运行期配置。字段默认值即缺省行为。"""

    bot_token: str
    api_secret: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8632
    admin_ids: set[int] = field(default_factory=set)
    token_ttl: int = 300
    login_ttl: int = 300
    # 限流：register 每 window 秒 5 次，login 每 window 秒 10 次。
    register_max_calls: int = 5
    register_window: int = 600
    login_max_calls: int = 10
    login_window: int = 600
    db_path: str = "mctgauth.db"
    messages: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MESSAGES))

    def msg(self, key: str, **kwargs: object) -> str:
        """取一条文案并按需 format；未配置的 key 回退到内置默认。

        文案在 HTML 解析模式下发送，模板自带的 HTML 标签需保留，但占位符插入的
        是运行时外部值（mc_name、ip、mc_uuid 等，来源可能是被攻陷的模组），必须
        转义以防注入 HTML 标签伪造文案或使 Telegram 拒发。
        """
        template = self.messages.get(key, DEFAULT_MESSAGES[key])
        if not kwargs:
            return template
        escaped = {k: html.escape(str(v), quote=False) for k, v in kwargs.items()}
        return template.format(**escaped)


def load_config(config_path: str = "config.toml") -> Config:
    """从 toml 文件（可缺失）加载配置，并叠加环境变量覆盖。"""
    data: dict = {}
    if os.path.exists(config_path):
        with open(config_path, "rb") as f:
            data = tomllib.load(f)

    # messages 段：内置默认与用户覆盖合并（用户键优先）。
    messages = dict(DEFAULT_MESSAGES)
    user_messages = data.get("messages")
    if isinstance(user_messages, dict):
        messages.update({str(k): str(v) for k, v in user_messages.items()})

    admin_ids = {int(x) for x in data.get("admin_ids", [])}

    # 环境变量对 token / secret 的覆盖优先级最高。
    bot_token = os.environ.get("BOT_TOKEN") or str(data.get("bot_token", "")).strip()
    api_secret = os.environ.get("API_SECRET") or str(data.get("api_secret", "")).strip()

    if not bot_token:
        raise ConfigError(
            "缺少 bot_token：请在 config.toml 设置 bot_token，或设置环境变量 BOT_TOKEN。"
        )
    if not api_secret:
        raise ConfigError(
            "缺少 api_secret：请在 config.toml 设置 api_secret，或设置环境变量 API_SECRET。"
        )

    return Config(
        bot_token=bot_token,
        api_secret=api_secret,
        listen_host=str(data.get("listen_host", "127.0.0.1")),
        listen_port=int(data.get("listen_port", 8632)),
        admin_ids=admin_ids,
        token_ttl=int(data.get("token_ttl", 300)),
        login_ttl=int(data.get("login_ttl", 300)),
        register_max_calls=int(data.get("register_max_calls", 5)),
        register_window=int(data.get("register_window", 600)),
        login_max_calls=int(data.get("login_max_calls", 10)),
        login_window=int(data.get("login_window", 600)),
        db_path=str(data.get("db_path", "mctgauth.db")),
        messages=messages,
    )
