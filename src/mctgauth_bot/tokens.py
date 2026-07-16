"""绑定令牌的生成与校验。

令牌用于把游戏内玩家（mc_uuid）与 Telegram 账号关联：
服务器为玩家签发一个短令牌，玩家在 Telegram 里发给 Bot 完成绑定。
"""

import re
import secrets

# 令牌字母表：A-Z 去掉易混淆的 I、O，数字保留 2-9（去掉 0、1）。
# 目的是让玩家手动抄写/输入时不易出错。
TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# 令牌长度固定为 8 位。
TOKEN_LENGTH = 8

# 精确匹配上述字母表、长度为 8 的完整字符串。
TOKEN_RE = re.compile(r"^[" + TOKEN_ALPHABET + r"]{" + str(TOKEN_LENGTH) + r"}$")


def generate_token() -> str:
    """生成一个 8 位的随机令牌，字符取自 TOKEN_ALPHABET。"""
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
