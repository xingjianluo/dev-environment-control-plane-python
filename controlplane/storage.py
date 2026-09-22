"""持久化：JSON 文件原子写 + 历史记录缺省字段兼容读取。

进程重启后由该存储把 environments / requests / 配额视图恢复，
保证重启后对外视图一致。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Environment, RequestRecord

SCHEMA_VERSION = 1


class JsonStateStore:
    """以单个 JSON 文件保存全部状态，写盘采用临时文件 + os.replace 原子替换。"""

    def __init__(self, path: str | os.PathLike[str] | None):
        self.path = Path(path) if path else None

    def exists(self) -> bool:
        return self.path is not None and self.path.exists()

    def save(
        self,
        environments: dict[str, Environment],
        requests: dict[str, RequestRecord],
        *,
        global_limit: int,
        owner_limits: dict[str, int],
        default_lease_ttl_seconds: float,
        max_lease_ttl_seconds: float,
        version: int = SCHEMA_VERSION,
    ) -> None:
        if self.path is None:
            return
        payload = {
            "version": version,
            "config": {
                "global_limit": global_limit,
                "owner_limits": dict(owner_limits),
                "default_lease_ttl_seconds": default_lease_ttl_seconds,
                "max_lease_ttl_seconds": max_lease_ttl_seconds,
            },
            "environments": [e.to_dict() for e in environments.values()],
            "requests": [r.to_dict() for r in requests.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + os.replace：崩溃时要么旧文件、要么新文件，不会出现半截 JSON。
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def load(self) -> dict:
        """返回原始状态字典；文件不存在时返回空骨架。

        返回结构：
        {
          "version": int,
          "config": {...},
          "environments": [dict, ...],
          "requests": [dict, ...],
        }
        """
        if self.path is None or not self.path.exists():
            return {
                "version": SCHEMA_VERSION,
                "config": {},
                "environments": [],
                "requests": [],
            }
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 顶层缺省同样按确定语义补全，容忍更老的快照。
        return {
            "version": int(data.get("version", 1)),
            "config": data.get("config", {}) or {},
            "environments": data.get("environments", []) or [],
            "requests": data.get("requests", []) or [],
        }
