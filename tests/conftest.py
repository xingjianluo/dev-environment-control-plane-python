"""测试公共夹具。

每个测试的控制面判定日志（请求标识 / 判定依据 / 配额变化）写入
tests/logs/<测试名>.log，并在测试结束后打印到终端，便于复现与审阅。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

import pytest

from controlplane.clock import Clock
from controlplane.service import ControlPlane
from controlplane.storage import JsonStateStore

LOG_DIR = Path(__file__).parent / "logs"


class FakeClock(Clock):
    """虚拟时钟：时间只随测试显式推进，租约结论完全确定。"""

    def __init__(self, start: float = 1_000_000.0):
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tmp_state_path():
    d = tempfile.mkdtemp(prefix="cp-test-")
    yield Path(d) / "state.json"
    shutil.rmtree(d, ignore_errors=True)


def make_cp(
    clock: FakeClock,
    *,
    global_limit: int = 5,
    owner_limits: dict[str, int] | None = None,
    default_lease_ttl_seconds: float = 60.0,
    max_lease_ttl_seconds: float = 600.0,
    state_path: Path | None = None,
) -> ControlPlane:
    store = JsonStateStore(state_path) if state_path is not None else None
    return ControlPlane(
        global_limit=global_limit,
        owner_limits=owner_limits,
        default_lease_ttl_seconds=default_lease_ttl_seconds,
        max_lease_ttl_seconds=max_lease_ttl_seconds,
        store=store,
        clock=clock,
    )


@pytest.fixture
def make_plane(clock):
    return lambda **kw: make_cp(clock, **kw)


@pytest.fixture(autouse=True)
def _capture_decision_logs(request):
    """把 controlplane logger 的判定日志落到独立文件，测试末尾回显。"""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / (request.node.name + ".log")
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    target = logging.getLogger("controlplane")
    target.setLevel(logging.INFO)
    target.addHandler(handler)
    yield
    target.removeHandler(handler)
    handler.close()
    if request.config.getoption("--show-logs"):
        import sys

        sys.stderr.write(f"\n===== decision logs: {log_path} =====\n")
        sys.stderr.write(log_path.read_text(encoding="utf-8"))
        sys.stderr.flush()


def pytest_addoption(parser):
    parser.addoption(
        "--show-logs",
        action="store_true",
        default=False,
        help="在测试输出中回显判定日志（请求标识/判定依据/配额变化）",
    )
