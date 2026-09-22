"""本地开发环境控制面 —— 服务入口。

运行：uv run uvicorn main:app --reload
文档：启动后访问 /docs
"""

from __future__ import annotations

import os

from controlplane.http_api import create_app
from controlplane.service import ControlPlane
from controlplane.storage import JsonStateStore

_STATE_FILE = os.environ.get("CONTROL_PLANE_STATE_FILE", "control_plane_state.json")

_control_plane = ControlPlane(
    global_limit=int(os.environ.get("CONTROL_PLANE_GLOBAL_LIMIT", "5")),
    default_lease_ttl_seconds=float(
        os.environ.get("CONTROL_PLANE_DEFAULT_LEASE_TTL", "60")
    ),
    store=JsonStateStore(_STATE_FILE),
)

app = create_app(_control_plane)
