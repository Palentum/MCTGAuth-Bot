"""限流器测试：上限与窗口滚动。"""

from mctgauth_bot import ratelimit
from mctgauth_bot.ratelimit import FixedWindowLimiter


def test_allows_up_to_limit():
    lim = FixedWindowLimiter(max_calls=3, window_seconds=60)
    assert lim.allow("k") is True
    assert lim.allow("k") is True
    assert lim.allow("k") is True
    assert lim.allow("k") is False


def test_keys_are_independent():
    lim = FixedWindowLimiter(max_calls=1, window_seconds=60)
    assert lim.allow("a") is True
    assert lim.allow("b") is True
    assert lim.allow("a") is False


def test_window_rollover(monkeypatch):
    fake = {"t": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: fake["t"])

    lim = FixedWindowLimiter(max_calls=2, window_seconds=10)
    assert lim.allow("k") is True
    assert lim.allow("k") is True
    assert lim.allow("k") is False  # 窗口内超限

    # 时间推进到窗口外，计数重置。
    fake["t"] = 1011.0
    assert lim.allow("k") is True
    assert lim.allow("k") is True
    assert lim.allow("k") is False


def test_within_window_stays_blocked(monkeypatch):
    fake = {"t": 0.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: fake["t"])

    lim = FixedWindowLimiter(max_calls=1, window_seconds=10)
    assert lim.allow("k") is True
    fake["t"] = 9.0  # 仍在窗口内
    assert lim.allow("k") is False
