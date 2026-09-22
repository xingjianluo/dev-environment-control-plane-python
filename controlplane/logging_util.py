"""Structured decision logging.

Every state-changing operation emits one JSON log line containing at least:
  - request_id : idempotency key of the request
  - action     : APPLY / OCCUPY / RENEW / RELEASE / RECLAIM
  - basis      : why the decision was made (the decision rule that fired)
  - quota_before / quota_after : global/owner quota counters around the action
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("controlplane")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    return logger


def _snapshot(quota: dict[str, Any] | None) -> dict[str, Any] | None:
    if quota is None:
        return None
    out: dict[str, Any] = {"global": dict(quota.get("global", {}))}
    owners = quota.get("owners")
    out["owners"] = {k: dict(v) for k, v in owners.items()} if owners else {}
    return out


def log_decision(
    logger: logging.Logger,
    *,
    request_id: str,
    action: str,
    accepted: bool,
    basis: str,
    quota_before: dict[str, Any] | None = None,
    quota_after: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "event": "decision",
        "request_id": request_id,
        "action": action,
        "accepted": accepted,
        "basis": basis,
        "quota_before": _snapshot(quota_before),
        "quota_after": _snapshot(quota_after),
    }
    payload.update(extra)
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
