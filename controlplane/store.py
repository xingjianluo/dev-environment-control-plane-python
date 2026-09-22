"""JSON-file persistence with atomic writes and backward-compatible reads.

On-disk layout::

    {
      "version": 2,
      "environments": [ {record fields...}, ... ],
      "request_index": {request_id: env_id}
    }

Older records may lack fields introduced later; such fields are filled with
deterministic defaults (see FIELD_DEFAULTS and normalize_record).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from .models import EnvironmentRecord

SCHEMA_VERSION = 2

# Deterministic defaults applied when loading records written by older versions
# that did not yet contain these fields.
FIELD_DEFAULTS: dict[str, Any] = {
    "lease_expires_at": None,
    "occupied_at": None,
    "released_at": None,
    "reclaimed_at": None,
    "release_count": 0,
    "reclaim_count": 0,
    "renew_count": 0,
    "occupy_count": 0,
    "lease_seconds": 60,
    "updated_at": None,  # falls back to created_at in normalize_record
}

REQUIRED_FIELDS = ("env_id", "owner", "request_id", "state", "created_at")


def normalize_record(raw: dict[str, Any]) -> EnvironmentRecord:
    """Fill missing fields with deterministic defaults instead of failing.

    Compatibility rule for timestamps: a missing ``updated_at`` inherits
    ``created_at``. A missing ``lease_expires_at`` stays ``None``; the manager
    treats an ACTIVE record whose lease has no expiry as already expired
    (deterministic "legacy lease" semantics, see README).
    """
    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ValueError(f"record missing required fields: {missing}")
    data = dict(raw)
    for field_name, default in FIELD_DEFAULTS.items():
        if field_name not in data:
            data[field_name] = default
    if data.get("updated_at") is None:
        data["updated_at"] = data["created_at"]
    return EnvironmentRecord(
        env_id=data["env_id"],
        owner=data["owner"],
        request_id=data["request_id"],
        state=data["state"],
        lease_seconds=int(data["lease_seconds"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        lease_expires_at=data.get("lease_expires_at"),
        occupied_at=data.get("occupied_at"),
        released_at=data.get("released_at"),
        reclaimed_at=data.get("reclaimed_at"),
        release_count=int(data.get("release_count", 0)),
        reclaim_count=int(data.get("reclaim_count", 0)),
        renew_count=int(data.get("renew_count", 0)),
        occupy_count=int(data.get("occupy_count", 0)),
    )


class Store:
    """Holds all state in memory and persists it atomically to a JSON file."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.environments: dict[str, EnvironmentRecord] = {}
        self.request_index: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for raw in payload.get("environments", []):
            record = normalize_record(raw)
            self.environments[record.env_id] = record
        self.request_index = dict(payload.get("request_index", {}))

    def save(self) -> None:
        """Atomically persist current state (write temp file then os.replace)."""
        with self.lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "version": SCHEMA_VERSION,
                            "environments": [r.to_dict() for r in self.environments.values()],
                            "request_index": self.request_index,
                        },
                        fh,
                        ensure_ascii=False,
                        indent=2,
                    )
                os.replace(tmp_path, self.path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

    def quota_snapshot(self) -> dict[str, Any]:
        """Used by tests/logging: raw view of current state."""
        with self.lock:
            return {
                "environments": {k: v.to_dict() for k, v in self.environments.items()},
                "request_index": dict(self.request_index),
            }
