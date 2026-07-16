"""内存固定窗口限流器。

每个端点各持一个实例；调用方按 mc_uuid、ip 等维度分别 allow()，
两者都通过才放行。进程重启即清空，不做持久化。
"""

import time


class FixedWindowLimiter:
    """固定窗口计数限流：在 window_seconds 内至多 max_calls 次。"""

    def __init__(self, max_calls: int, window_seconds: int):
        self._max_calls = max_calls
        self._window = window_seconds
        # key -> (窗口起始时间戳, 该窗口内已计数)
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> bool:
        """记录一次针对 key 的调用；未超限返回 True，超限返回 False。"""
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= self._window:
            # 窗口已过期，开启新窗口。
            start, count = now, 0
        if count >= self._max_calls:
            # 已达上限，且仍在窗口内：拒绝，且不再累加。
            self._buckets[key] = (start, count)
            return False
        self._buckets[key] = (start, count + 1)
        return True
