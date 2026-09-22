"""领域模型：环境状态机与数据载体。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EnvironmentState(str, Enum):
    """环境生命周期状态。

    状态迁移（均为显式操作）：
      ALLOCATED -> ACTIVE        （申请后即激活；内部一步完成）
      ACTIVE    -> RELEASED      持有方释放 / 租约过期被回收
      ACTIVE    -> EXPIRED       租约超时回收（幂等，配额归还）
      EXPIRED   -> ACTIVE        （不允许；过期即终结，需重新申请）
      RELEASED 为终态；EXPIRED 为终态。
    """

    ALLOCATED = "ALLOCATED"
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


# 允许的状态迁移表：from -> {to}
ALLOWED_TRANSITIONS: dict[EnvironmentState, frozenset[EnvironmentState]] = {
    EnvironmentState.ALLOCATED: frozenset({EnvironmentState.ACTIVE, EnvironmentState.RELEASED}),
    EnvironmentState.ACTIVE: frozenset({EnvironmentState.RELEASED, EnvironmentState.EXPIRED}),
    EnvironmentState.RELEASED: frozenset(),
    EnvironmentState.EXPIRED: frozenset(),
}


@dataclass
class Environment:
    """一个被申请出来的开发环境。"""

    env_id: str
    owner: str
    request_id: str
    state: EnvironmentState = EnvironmentState.ACTIVE
    lease_ttl_seconds: float = 60.0
    lease_expires_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    released_at: float | None = None
    expired_at: float | None = None
    renew_count: int = 0

    def is_lease_active(self, now: float) -> bool:
        """租约是否仍有效。仅 ACTIVE 环境可能有效。"""
        if self.state is not EnvironmentState.ACTIVE:
            return False
        if self.lease_expires_at is None:
            return False
        return now < self.lease_expires_at

    def to_dict(self) -> dict:
        return {
            "env_id": self.env_id,
            "owner": self.owner,
            "request_id": self.request_id,
            "state": self.state.value,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "lease_expires_at": self.lease_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "released_at": self.released_at,
            "expired_at": self.expired_at,
            "renew_count": self.renew_count,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Environment":
        """从持久化记录读取。

        兼容约定（历史记录缺少后增字段时按确定缺省补全，不报错）：
          * state 缺失            -> ACTIVE（历史环境默认处于占用中）
          * lease_ttl_seconds 缺失 -> 60.0
          * lease_expires_at 缺失  -> None
          * created_at/updated_at 缺失 -> 0.0
          * released_at/expired_at 缺失 -> None
          * renew_count 缺失       -> 0
        env_id / owner / request_id 为身份字段，缺失视为数据损坏。
        """
        state_raw = raw.get("state")
        if state_raw is None:
            state = EnvironmentState.ACTIVE
        else:
            state = EnvironmentState(state_raw)
        return cls(
            env_id=raw["env_id"],
            owner=raw["owner"],
            request_id=raw["request_id"],
            state=state,
            lease_ttl_seconds=float(raw.get("lease_ttl_seconds", 60.0)),
            lease_expires_at=raw.get("lease_expires_at"),
            created_at=float(raw.get("created_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            released_at=raw.get("released_at"),
            expired_at=raw.get("expired_at"),
            renew_count=int(raw.get("renew_count", 0)),
        )


@dataclass
class RequestRecord:
    """请求幂等记录：同一 request_id 的重复/并发提交只判定一次。

    结果被缓存（成功或确定性拒绝），后续提交原样回放；
    意图不一致的提交返回 REQUEST_CONFLICT。
    """

    request_id: str
    owner: str
    intent: str  # 固定为 "ALLOCATE"
    lease_ttl_seconds: float
    status: str = "PENDING"  # PENDING / SUCCESS / ERROR
    env_id: str | None = None
    error_code: str | None = None
    error_payload: dict | None = None
    created_at: float = 0.0
    completed_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "owner": self.owner,
            "intent": self.intent,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "status": self.status,
            "env_id": self.env_id,
            "error_code": self.error_code,
            "error_payload": self.error_payload,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RequestRecord":
        """兼容读取：intent/status 等后增字段缺失时按确定缺省补全。"""
        return cls(
            request_id=raw["request_id"],
            owner=raw["owner"],
            intent=raw.get("intent", "ALLOCATE"),
            lease_ttl_seconds=float(raw.get("lease_ttl_seconds", 60.0)),
            status=raw.get("status", "PENDING"),
            env_id=raw.get("env_id"),
            error_code=raw.get("error_code"),
            error_payload=raw.get("error_payload"),
            created_at=float(raw.get("created_at", 0.0)),
            completed_at=raw.get("completed_at"),
        )


@dataclass
class AllocationResult:
    """allocate 的返回：幂等重放时 replayed=True。"""

    env: Environment
    replayed: bool = False


@dataclass
class QuotaSnapshot:
    """某一时刻的配额视图。"""

    global_used: int
    global_limit: int
    owner: str | None
    owner_used: int | None
    owner_limit: int | None

    def to_dict(self) -> dict:
        return {
            "global": {"used": self.global_used, "limit": self.global_limit},
            "owner": (
                None
                if self.owner is None
                else {
                    "owner": self.owner,
                    "used": self.owner_used,
                    "limit": self.owner_limit,
                }
            ),
        }
