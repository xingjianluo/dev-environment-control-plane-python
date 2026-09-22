"""控制面核心服务：申请 / 续约 / 释放 / 回收 / 查询。

并发模型：进程内所有判定与变更在同一把可重入锁内完成（判定+变更原子），
请求记录（request_id）提供跨调用的幂等与并发折叠；
并发的同一 request_id 后来者在锁上等待并回放首个请求的结论。
"""

from __future__ import annotations

import logging
import threading
import uuid

from .clock import Clock, SystemClock
from .errors import (
    ControlPlaneError,
    EnvironmentNotFoundError,
    InvalidRequestError,
    InvalidTransitionError,
    LeaseExpiredError,
    OwnerMismatchError,
    QuotaExhaustedError,
    RequestConflictError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    AllocationResult,
    Environment,
    EnvironmentState,
    QuotaSnapshot,
    RequestRecord,
)
from .storage import JsonStateStore

logger = logging.getLogger("controlplane")

# 错误码 -> 异常类，用于幂等回放时重建确定性拒绝。
_ERROR_CODE_MAP: dict[str, type[ControlPlaneError]] = {
    cls.error_code: cls
    for cls in (
        InvalidRequestError,
        RequestConflictError,
        QuotaExhaustedError,
        InvalidTransitionError,
        OwnerMismatchError,
        LeaseExpiredError,
        EnvironmentNotFoundError,
    )
}


class ControlPlane:
    def __init__(
        self,
        *,
        global_limit: int = 5,
        owner_limits: dict[str, int] | None = None,
        default_lease_ttl_seconds: float = 60.0,
        max_lease_ttl_seconds: float = 600.0,
        store: JsonStateStore | None = None,
        clock: Clock | None = None,
    ):
        self._lock = threading.RLock()
        self._envs: dict[str, Environment] = {}
        self._requests: dict[str, RequestRecord] = {}
        self.global_limit = global_limit
        self.owner_limits = dict(owner_limits or {})
        self.default_lease_ttl_seconds = float(default_lease_ttl_seconds)
        self.max_lease_ttl_seconds = float(max_lease_ttl_seconds)
        self._store = store
        self._clock = clock or SystemClock()
        self._load()

    # ==================== 生命周期 ====================
    def allocate(
        self,
        *,
        request_id: str,
        owner: str,
        lease_ttl_seconds: float | None = None,
    ) -> AllocationResult:
        """申请环境（幂等：同一 request_id 重复/并发提交折叠为一次）。"""
        request_id = self._require_id(request_id, "request_id")
        owner = self._require_id(owner, "owner")
        ttl = self._resolve_ttl(lease_ttl_seconds)
        with self._lock:
            now = self._clock.now()
            existing = self._requests.get(request_id)
            if existing is not None:
                return self._replay_allocation(existing, owner, ttl, now)

            # 先落 PENDING 记录：并发的同 request_id 后来者只会看到已完成结论
            # （进程内单锁串行化，后来者必然在首个请求完成之后进入）。
            record = RequestRecord(
                request_id=request_id,
                owner=owner,
                intent="ALLOCATE",
                lease_ttl_seconds=ttl,
                status="PENDING",
                created_at=now,
            )
            self._requests[request_id] = record

            # 判定前先回收过期租约，释放配额，保证结论随时钟确定。
            self._sweep_expired_locked(now)

            g_before, per_owner_before = self._usage_locked()
            owner_limit = self._owner_limit(owner)
            if g_before >= self.global_limit:
                self._deny_quota(
                    record,
                    scope="global",
                    owner=owner,
                    used=g_before,
                    limit=self.global_limit,
                    now=now,
                )
            owner_used = per_owner_before.get(owner, 0)
            if owner_used >= owner_limit:
                self._deny_quota(
                    record,
                    scope="owner",
                    owner=owner,
                    used=owner_used,
                    limit=owner_limit,
                    now=now,
                )

            env = Environment(
                env_id="env-" + uuid.uuid4().hex[:12],
                owner=owner,
                request_id=request_id,
                state=EnvironmentState.ACTIVE,
                lease_ttl_seconds=ttl,
                lease_expires_at=now + ttl,
                created_at=now,
                updated_at=now,
            )
            self._envs[env.env_id] = env
            record.status = "SUCCESS"
            record.env_id = env.env_id
            record.completed_at = now
            self._persist_locked()
            g_after, per_owner_after = self._usage_locked()
            logger.info(
                "allocate decision request_id=%s owner=%s env_id=%s basis=QUOTA_GRANTED "
                "replayed=False quota global %d/%d -> %d/%d owner %d/%d -> %d/%d",
                request_id, owner, env.env_id,
                g_before, self.global_limit, g_after, self.global_limit,
                owner_used, owner_limit, per_owner_after.get(owner, 0), owner_limit,
            )
            return AllocationResult(env=env, replayed=False)

    def renew(
        self,
        *,
        request_id: str,
        env_id: str,
        owner: str,
        lease_ttl_seconds: float | None = None,
    ) -> Environment:
        """续约：仅持有方在租约未过期时可续约，租约顺延一个 TTL。"""
        request_id = self._require_id(request_id, "request_id")
        owner = self._require_id(owner, "owner")
        env_id = self._require_id(env_id, "env_id")
        ttl = self._resolve_ttl(lease_ttl_seconds)
        with self._lock:
            now = self._clock.now()
            self._sweep_expired_locked(now)
            env = self._require_env(env_id)
            self._require_owner(env, owner, request_id)

            if env.state is EnvironmentState.EXPIRED:
                logger.info(
                    "renew decision request_id=%s owner=%s env_id=%s basis=LEASE_EXPIRED",
                    request_id, owner, env_id,
                )
                raise LeaseExpiredError(
                    f"environment {env_id} lease expired and was reclaimed",
                    request_id=request_id,
                    env_id=env_id,
                    lease_expires_at=env.lease_expires_at,
                    now=now,
                )
            if env.state is not EnvironmentState.ACTIVE:
                logger.info(
                    "renew decision request_id=%s owner=%s env_id=%s from=%s basis=INVALID_TRANSITION",
                    request_id, owner, env_id, env.state.value,
                )
                raise InvalidTransitionError(
                    f"cannot renew environment in state {env.state.value}",
                    request_id=request_id,
                    env_id=env_id,
                    from_state=env.state.value,
                    to_state=EnvironmentState.ACTIVE.value,
                )

            old_expiry = env.lease_expires_at
            env.lease_expires_at = now + ttl
            env.lease_ttl_seconds = ttl
            env.renew_count += 1
            env.updated_at = now
            self._persist_locked()
            logger.info(
                "renew decision request_id=%s owner=%s env_id=%s basis=RENEW_GRANTED "
                "lease_expires_at %.3f -> %.3f renew_count=%d quota global=%d/%d owner=%d/%d",
                request_id, owner, env_id, old_expiry, env.lease_expires_at,
                env.renew_count, *self._usage_for_log(owner),
            )
            return env

    def release(self, *, request_id: str, env_id: str, owner: str) -> Environment:
        """释放：ACTIVE -> RELEASED，归还配额；对终态重复操作幂等。"""
        request_id = self._require_id(request_id, "request_id")
        owner = self._require_id(owner, "owner")
        env_id = self._require_id(env_id, "env_id")
        with self._lock:
            now = self._clock.now()
            self._sweep_expired_locked(now)
            env = self._require_env(env_id)
            self._require_owner(env, owner, request_id)

            if env.state is EnvironmentState.RELEASED:
                logger.info(
                    "release decision request_id=%s owner=%s env_id=%s basis=ALREADY_RELEASED "
                    "quota global=%d/%d owner=%d/%d",
                    request_id, owner, env_id, *self._usage_for_log(owner),
                )
                return env
            if env.state is EnvironmentState.EXPIRED:
                # 已被回收即视为终态，重复释放幂等返回当前视图。
                logger.info(
                    "release decision request_id=%s owner=%s env_id=%s basis=ALREADY_EXPIRED "
                    "quota global=%d/%d owner=%d/%d",
                    request_id, owner, env_id, *self._usage_for_log(owner),
                )
                return env

            g_before, per_owner_before = self._usage_locked()
            self._transition_locked(env, EnvironmentState.RELEASED, now)
            env.released_at = now
            self._persist_locked()
            g_after, per_owner_after = self._usage_locked()
            owner_limit = self._owner_limit(owner)
            logger.info(
                "release decision request_id=%s owner=%s env_id=%s basis=RELEASE_GRANTED "
                "quota global %d/%d -> %d/%d owner %d/%d -> %d/%d",
                request_id, owner, env_id,
                g_before, self.global_limit, g_after, self.global_limit,
                per_owner_before.get(owner, 0), owner_limit,
                per_owner_after.get(owner, 0), owner_limit,
            )
            return env

    def reclaim_expired(self, *, now: float | None = None) -> list[str]:
        """回收所有租约过期环境（ACTIVE -> EXPIRED），归还配额。幂等。"""
        with self._lock:
            swept = self._sweep_expired_locked(
                self._clock.now() if now is None else now
            )
            if swept:
                self._persist_locked()
            return swept

    # ==================== 查询 ====================
    def get_environment(self, env_id: str) -> Environment:
        with self._lock:
            return self._require_env(env_id)

    def get_request(self, request_id: str) -> RequestRecord:
        with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                raise EnvironmentNotFoundError(
                    f"request {request_id} not found", request_id=request_id
                )
            return record

    def quota(self, owner: str | None = None) -> QuotaSnapshot:
        with self._lock:
            self._sweep_expired_locked(self._clock.now())
            g_used, per_owner = self._usage_locked()
            if owner is None:
                return QuotaSnapshot(
                    global_used=g_used,
                    global_limit=self.global_limit,
                    owner=None,
                    owner_used=None,
                    owner_limit=None,
                )
            limit = self._owner_limit(owner)
            return QuotaSnapshot(
                global_used=g_used,
                global_limit=self.global_limit,
                owner=owner,
                owner_used=per_owner.get(owner, 0),
                owner_limit=limit,
            )

    def list_environments(self, *, include_terminal: bool = False) -> list[Environment]:
        with self._lock:
            self._sweep_expired_locked(self._clock.now())
            return [
                e
                for e in self._envs.values()
                if include_terminal or e.state is EnvironmentState.ACTIVE
            ]

    # ==================== 内部实现（调用方须持锁） ====================
    def _replay_allocation(
        self, record: RequestRecord, owner: str, ttl: float, now: float
    ) -> AllocationResult:
        # 同一 request_id 但意图/参数不一致：拒绝而非折叠。
        if (
            record.owner != owner
            or record.intent != "ALLOCATE"
            or record.lease_ttl_seconds != ttl
        ):
            basis = "REQUEST_CONFLICT"
            logger.info(
                "allocate decision request_id=%s owner=%s basis=%s "
                "existing_owner=%s existing_ttl=%s quota global=%d/%d",
                record.request_id, owner, basis,
                record.owner, record.lease_ttl_seconds,
                self._usage_locked()[0], self.global_limit,
            )
            raise RequestConflictError(
                f"request_id {record.request_id} already submitted with different intent",
                request_id=record.request_id,
            )
        if record.status == "SUCCESS":
            env = self._envs[record.env_id]  # type: ignore[index]
            logger.info(
                "allocate decision request_id=%s owner=%s env_id=%s basis=REQUEST_REPLAY "
                "replayed=True state=%s quota global=%d/%d",
                record.request_id, owner, env.env_id, env.state.value,
                self._usage_locked()[0], self.global_limit,
            )
            return AllocationResult(env=env, replayed=True)
        # 已缓存的确定性拒绝（配额耗尽等）原样回放，结论稳定。
        payload = record.error_payload or {"error": record.error_code}
        logger.info(
            "allocate decision request_id=%s owner=%s basis=REQUEST_REPLAY_ERROR "
            "replayed=True error=%s quota global=%d/%d",
            record.request_id, owner, payload.get("error"),
            self._usage_locked()[0], self.global_limit,
        )
        self._raise_recorded_error(payload)

    def _deny_quota(
        self,
        record: RequestRecord,
        *,
        scope: str,
        owner: str,
        used: int,
        limit: int,
        now: float,
    ) -> None:
        if scope == "global":
            message = f"global quota exhausted: {used}/{limit} environments in use"
        else:
            message = (
                f"owner quota exhausted for {owner}: {used}/{limit} environments in use"
            )
        err = QuotaExhaustedError(
            message,
            request_id=record.request_id,
            scope=scope,
            used=used,
            limit=limit,
            owner=owner if scope == "owner" else None,
        )
        record.status = "ERROR"
        record.error_code = err.error_code
        record.error_payload = err.to_dict()
        record.completed_at = now
        self._persist_locked()
        g_used, per_owner = self._usage_locked()
        logger.info(
            "allocate decision request_id=%s owner=%s basis=%s quota global=%d/%d owner=%d/%d",
            record.request_id, owner,
            "GLOBAL_QUOTA_EXHAUSTED" if scope == "global" else "OWNER_QUOTA_EXHAUSTED",
            g_used, self.global_limit,
            per_owner.get(owner, 0), self._owner_limit(owner),
        )
        raise err

    def _sweep_expired_locked(self, now: float) -> list[str]:
        """把租约到期的 ACTIVE 环境迁到 EXPIRED，返回本次新回收的 env_id 列表。

        重复调用结果为空列表 —— 回收天然幂等。
        """
        swept: list[str] = []
        g_before, _ = self._usage_locked()
        for env in self._envs.values():
            if (
                env.state is EnvironmentState.ACTIVE
                and env.lease_expires_at is not None
                and env.lease_expires_at <= now
            ):
                self._transition_locked(env, EnvironmentState.EXPIRED, now)
                env.expired_at = now
                swept.append(env.env_id)
                logger.info(
                    "reclaim decision request_id=%s owner=%s env_id=%s "
                    "basis=LEASE_TIMEOUT lease_expires_at=%.3f now=%.3f",
                    env.request_id, env.owner, env.env_id,
                    env.lease_expires_at, now,
                )
        if swept:
            g_after, per_owner = self._usage_locked()
            logger.info(
                "reclaim summary env_ids=%s count=%d quota global %d/%d -> %d/%d",
                swept, len(swept), g_before, self.global_limit, g_after, self.global_limit,
            )
        return swept

    def _transition_locked(
        self, env: Environment, target: EnvironmentState, now: float
    ) -> None:
        allowed = ALLOWED_TRANSITIONS.get(env.state, frozenset())
        if target not in allowed:
            raise InvalidTransitionError(
                f"illegal transition {env.state.value} -> {target.value}",
                request_id=env.request_id,
                env_id=env.env_id,
                from_state=env.state.value,
                to_state=target.value,
            )
        env.state = target
        env.updated_at = now

    def _usage_locked(self) -> tuple[int, dict[str, int]]:
        """返回（全局 ACTIVE 数，{owner: ACTIVE 数}）。EXPIRED/RELEASED 不占配额。"""
        per_owner: dict[str, int] = {}
        total = 0
        for env in self._envs.values():
            if env.state is EnvironmentState.ACTIVE:
                total += 1
                per_owner[env.owner] = per_owner.get(env.owner, 0) + 1
        return total, per_owner

    def _usage_for_log(self, owner: str) -> tuple[int, int, int, int]:
        g_used, per_owner = self._usage_locked()
        return (
            g_used,
            self.global_limit,
            per_owner.get(owner, 0),
            self._owner_limit(owner),
        )

    def _owner_limit(self, owner: str) -> int:
        # 未单独配置的归属方，默认上限等于全局上限（可独占整个池，除非显式收紧）。
        return self.owner_limits.get(owner, self.global_limit)

    def _require_env(self, env_id: str) -> Environment:
        env = self._envs.get(env_id)
        if env is None:
            raise EnvironmentNotFoundError(
                f"environment {env_id} not found", env_id=env_id
            )
        return env

    def _require_owner(self, env: Environment, owner: str, request_id: str) -> None:
        if env.owner != owner:
            raise OwnerMismatchError(
                f"environment {env.env_id} is owned by {env.owner}, not {owner}",
                request_id=request_id,
                env_id=env.env_id,
                expected_owner=env.owner,
                actual_owner=owner,
            )

    @staticmethod
    def _require_id(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequestError(f"{field_name} must be a non-empty string")
        return value.strip()

    def _resolve_ttl(self, ttl: float | None) -> float:
        if ttl is None:
            return self.default_lease_ttl_seconds
        if ttl <= 0:
            raise InvalidRequestError("lease_ttl_seconds must be > 0")
        if ttl > self.max_lease_ttl_seconds:
            raise InvalidRequestError(
                f"lease_ttl_seconds {ttl} exceeds max {self.max_lease_ttl_seconds}"
            )
        return float(ttl)

    @staticmethod
    def _raise_recorded_error(payload: dict) -> None:
        code = payload.get("error", ControlPlaneError.error_code)
        cls = _ERROR_CODE_MAP.get(code, ControlPlaneError)
        message = payload.get("message", "")
        request_id = payload.get("request_id")
        if cls is QuotaExhaustedError:
            raise QuotaExhaustedError(
                message,
                request_id=request_id,
                scope=payload.get("scope", "global"),
                used=int(payload.get("used", 0)),
                limit=int(payload.get("limit", 0)),
                owner=payload.get("owner"),
            )
        raise cls(message, request_id=request_id)

    def _persist_locked(self) -> None:
        if self._store is None:
            return
        self._store.save(
            self._envs,
            self._requests,
            global_limit=self.global_limit,
            owner_limits=self.owner_limits,
            default_lease_ttl_seconds=self.default_lease_ttl_seconds,
            max_lease_ttl_seconds=self.max_lease_ttl_seconds,
        )

    def _load(self) -> None:
        if self._store is None:
            return
        data = self._store.load()
        for raw in data.get("environments", []):
            env = Environment.from_dict(raw)
            self._envs[env.env_id] = env
        for raw in data.get("requests", []):
            record = RequestRecord.from_dict(raw)
            # 崩溃恢复：正常流程下 PENDING 不会落盘（落盘发生在结论产生之后）；
            # 若旧快照里残留 PENDING，按确定语义标记为中断，提交方需换 request_id 重试。
            if record.status == "PENDING":
                record.status = "ERROR"
                record.error_code = "REQUEST_INTERRUPTED"
                record.error_payload = {
                    "error": "REQUEST_INTERRUPTED",
                    "message": (
                        "request was interrupted before completion; "
                        "retry with a new request_id"
                    ),
                    "request_id": record.request_id,
                }
            self._requests[record.request_id] = record
        # 重启即恢复：按当前时间回收已过期租约，使对外视图与配额立即一致。
        if self._envs:
            now = self._clock.now()
            if self._sweep_expired_locked(now):
                self._persist_locked()
