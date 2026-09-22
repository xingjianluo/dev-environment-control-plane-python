"""时钟抽象：测试中可注入虚拟时钟以确定性地验证租约超时。"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()
