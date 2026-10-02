"""Core control-plane logic: lifecycle state machine, quotas and leases.

All operations are serialized by the store's reentrant lock so that concurrent
or duplicate submissions are evaluated against a single consistent state.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .logging_util import configure_logging, log_decision
from .models import (
    ApplyRequest,
    ConclusionOutcome,
    ControlPlaneError,
    Decision,
    EnvState,
    EnvironmentRecord,
    RejectCode,
    RequestConclusion,
)
from .store import Store

# States that still occupy quota: a PENDING reservation counts, as does an
# ACTIVE environment. Terminal states (RELEASED/RECLAIMED) have returned quota.
QUOTA_OCCUPYING = (EnvState.PENDING.value, EnvState.ACTIVE.value)
TERMINAL = (EnvState.RELEASED.value, EnvState.RECLAIMED.value)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


class ControlPlaneManager:
    def __init__(
        self,
        store: Store,
        global_limit: int = 2,
        per_owner_limit: int = 1,
        clock: Callable[[], float] | None = None,
        logger: Any | None = None,
    ):
        self.store = store
        self.global_limit = global_limit
        self.per_owner_limit = per_owner_limit
        self.clock = clock or _default_clock
        self.logger = logger or configure_logging()
        self._id_lock = threading.Lock()

    # ------------------------------------------------------------------ utils

    def _now(self) -> float:
        return self.clock()

    def _new_env_id(self) -> str:
        with self._id_lock:
            return f"env-{uuid.uuid4().hex[:12]}"

    def _quota_view(self) -> dict[str, Any]:
        used_by_owner: dict[str, int] = {}
        global_used = 0
        for rec in self.store.environments.values():
            if rec.state in QUOTA_OCCUPYING:
                global_used += 1
                used_by_owner[rec.owner] = used_by_owner.get(rec.owner, 0) + 1
        owners = {
            owner: {
                "used": used,
                "limit": self.per_owner_limit,
                "available": max(0, self.per_owner_limit - used),
            }
            for owner, used in sorted(used_by_owner.items())
        }
        return {
            "global": {
                "used": global_used,
                "limit": self.global_limit,
                "available": max(0, self.global_limit - global_used),
            },
            "owners": owners,
        }

    def _is_expired(self, rec: EnvironmentRecord, now: float) -> bool:
        # Legacy records (written before lease_expires_at existed) that are
        # ACTIVE are treated deterministically as already expired.
        if rec.lease_expires_at is None:
            return True
        return _parse_iso(rec.lease_expires_at) <= now

    def _decision(
        self,
        rec: EnvironmentRecord | None,
        *,
        accepted: bool,
        code: RejectCode | None = None,
        reason: str | None = None,
        deduplicated: bool = False,
    ) -> Decision:
        return Decision(
            accepted=accepted,
            request_id=rec.request_id if rec else "",
            env_id=rec.env_id if rec else None,
            state=rec.state if rec else None,
            owner=rec.owner if rec else None,
            code=code.value if code else None,
            reason=reason,
            deduplicated=deduplicated,
            quota_after=self._quota_view(),
            lease_expires_at=rec.lease_expires_at if rec else None,
        )

    def _get_by_env(self, env_id: str) -> EnvironmentRecord:
        rec = self.store.environments.get(env_id)
        if rec is None:
            raise ControlPlaneError(
                RejectCode.UNKNOWN_REQUEST, f"environment '{env_id}' does not exist"
            )
        return rec

    def _require_owner(self, rec: EnvironmentRecord, owner: str) -> None:
        if rec.owner != owner:
            raise ControlPlaneError(
                RejectCode.OWNER_MISMATCH,
                f"environment '{rec.env_id}' belongs to '{rec.owner}', not '{owner}'",
            )

    # ------------------------------------------------------------- lifecycle

    def apply(self, req: ApplyRequest) -> Decision:
        """Allocate one environment for an idempotency key (request_id).

        The conclusion of a request_id is decided exactly once and then
        persisted: the first submission either reserves an environment or is
        rejected with a stable quota code. Every later submission of the same
        request_id replays that stored conclusion verbatim — a rejection is
        never "upgraded" to a success just because capacity freed up, and a
        success always returns the originally allocated environment.
        """
        if not req.request_id or not req.owner:
            raise ControlPlaneError(
                RejectCode.INVALID_REQUEST, "request_id and owner are required"
            )
        if req.lease_seconds <= 0:
            raise ControlPlaneError(
                RejectCode.INVALID_REQUEST, "lease_seconds must be positive"
            )
        with self.store.lock:
            before = self._quota_view()
            conclusion = self.store.conclusions.get(req.request_id)
            if conclusion is not None:
                return self._replay_conclusion(conclusion, before)

            owner_used = before["owners"].get(req.owner, {}).get("used", 0)
            if owner_used >= self.per_owner_limit:
                return self._reject_once(
                    req,
                    RejectCode.OWNER_QUOTA_EXHAUSTED,
                    reason=(
                        f"owner quota exhausted: {owner_used}/"
                        f"{self.per_owner_limit} environments in use by '{req.owner}'"
                    ),
                    basis=f"owner used {owner_used} >= per-owner limit {self.per_owner_limit}",
                    before=before,
                )

            if before["global"]["used"] >= self.global_limit:
                return self._reject_once(
                    req,
                    RejectCode.GLOBAL_QUOTA_EXHAUSTED,
                    reason=(
                        f"global quota exhausted: {before['global']['used']}/"
                        f"{self.global_limit} environments in use"
                    ),
                    basis=f"global used {before['global']['used']} >= limit {self.global_limit}",
                    before=before,
                )

            now = self._now()
            rec = EnvironmentRecord(
                env_id=self._new_env_id(),
                owner=req.owner,
                request_id=req.request_id,
                state=EnvState.PENDING.value,
                lease_seconds=req.lease_seconds,
                created_at=_iso(now),
                updated_at=_iso(now),
            )
            conclusion = RequestConclusion(
                request_id=req.request_id,
                owner=req.owner,
                outcome=ConclusionOutcome.ACCEPTED.value,
                decided_at=_iso(now),
                env_id=rec.env_id,
            )
            self.store.environments[rec.env_id] = rec
            self.store.request_index[req.request_id] = rec.env_id
            self.store.conclusions[req.request_id] = conclusion
            self.store.save()
            decision = self._decision(rec, accepted=True)
            decision.decided_at = conclusion.decided_at
            log_decision(
                self.logger,
                request_id=req.request_id,
                action="APPLY",
                accepted=True,
                basis="quota available -> reserve one slot (PENDING); conclusion recorded",
                quota_before=before,
                quota_after=decision.quota_after,
                env_id=rec.env_id,
                outcome=conclusion.outcome,
                decided_at=conclusion.decided_at,
            )
            return decision

    def _reject_once(
        self,
        req: ApplyRequest,
        code: RejectCode,
        *,
        reason: str,
        basis: str,
        before: dict[str, Any],
    ) -> Decision:
        """Persist a REJECTED conclusion (consumes no quota) and return it."""
        conclusion = RequestConclusion(
            request_id=req.request_id,
            owner=req.owner,
            outcome=ConclusionOutcome.REJECTED.value,
            decided_at=_iso(self._now()),
            code=code.value,
            reason=reason,
        )
        self.store.conclusions[req.request_id] = conclusion
        self.store.save()
        decision = Decision(
            accepted=False,
            request_id=req.request_id,
            owner=req.owner,
            code=code.value,
            reason=reason,
            quota_after=self._quota_view(),
            decided_at=conclusion.decided_at,
        )
        log_decision(
            self.logger,
            request_id=req.request_id,
            action="APPLY",
            accepted=False,
            basis=basis,
            quota_before=before,
            quota_after=decision.quota_after,
            code=code.value,
            outcome=conclusion.outcome,
            decided_at=conclusion.decided_at,
        )
        return decision

    def _replay_conclusion(
        self, conclusion: RequestConclusion, before: dict[str, Any]
    ) -> Decision:
        """Return the stored conclusion for an already-decided request_id."""
        if conclusion.outcome == ConclusionOutcome.ACCEPTED.value:
            rec = self.store.environments[conclusion.env_id]
            decision = self._decision(
                rec,
                accepted=True,
                deduplicated=True,
                reason="conclusion already recorded; returning original environment",
            )
            decision.decided_at = conclusion.decided_at
            log_decision(
                self.logger,
                request_id=conclusion.request_id,
                action="APPLY",
                accepted=True,
                basis="conclusion already recorded (ACCEPTED) -> replay original env",
                quota_before=before,
                quota_after=decision.quota_after,
                env_id=rec.env_id,
                deduplicated=True,
                outcome=conclusion.outcome,
                decided_at=conclusion.decided_at,
            )
            return decision
        decision = Decision(
            accepted=False,
            request_id=conclusion.request_id,
            owner=conclusion.owner,
            code=conclusion.code,
            reason=conclusion.reason,
            deduplicated=True,
            quota_after=self._quota_view(),
            decided_at=conclusion.decided_at,
        )
        log_decision(
            self.logger,
            request_id=conclusion.request_id,
            action="APPLY",
            accepted=False,
            basis="conclusion already recorded (REJECTED) -> replay original rejection",
            quota_before=before,
            quota_after=decision.quota_after,
            code=conclusion.code,
            deduplicated=True,
            outcome=conclusion.outcome,
            decided_at=conclusion.decided_at,
        )
        return decision

    def occupy(self, env_id: str, owner: str) -> Decision:
        with self.store.lock:
            before = self._quota_view()
            rec = self._get_by_env(env_id)
            self._require_owner(rec, owner)
            if rec.state == EnvState.ACTIVE.value:
                decision = self._decision(
                    rec, accepted=True, deduplicated=True,
                    reason="environment already ACTIVE (idempotent occupy)",
                )
            elif rec.state == EnvState.PENDING.value:
                now = self._now()
                rec.state = EnvState.ACTIVE.value
                rec.occupied_at = _iso(now)
                rec.lease_expires_at = _iso(now + rec.lease_seconds)
                rec.updated_at = _iso(now)
                rec.occupy_count += 1
                self.store.save()
                decision = self._decision(rec, accepted=True)
            else:
                raise ControlPlaneError(
                    RejectCode.INVALID_STATE_TRANSITION,
                    f"cannot occupy environment in state {rec.state}",
                )
            log_decision(
                self.logger,
                request_id=rec.request_id,
                action="OCCUPY",
                accepted=True,
                basis=(
                    "already ACTIVE -> no-op"
                    if decision.deduplicated
                    else "PENDING + owner match -> ACTIVE, lease starts"
                ),
                quota_before=before,
                quota_after=decision.quota_after,
                env_id=rec.env_id,
            )
            return decision

    def renew(self, env_id: str, owner: str, extend_seconds: int | None = None) -> Decision:
        with self.store.lock:
            before = self._quota_view()
            rec = self._get_by_env(env_id)
            self._require_owner(rec, owner)
            extend = rec.lease_seconds if extend_seconds is None else extend_seconds
            if extend <= 0:
                raise ControlPlaneError(
                    RejectCode.INVALID_REQUEST, "extend_seconds must be positive"
                )
            if rec.state != EnvState.ACTIVE.value:
                raise ControlPlaneError(
                    RejectCode.INVALID_STATE_TRANSITION,
                    f"cannot renew environment in state {rec.state}; lease runs only in ACTIVE",
                )
            now = self._now()
            base = now
            if rec.lease_expires_at is not None:
                base = max(now, _parse_iso(rec.lease_expires_at))
            rec.lease_expires_at = _iso(base + extend)
            rec.updated_at = _iso(now)
            rec.renew_count += 1
            self.store.save()
            decision = self._decision(rec, accepted=True)
            log_decision(
                self.logger,
                request_id=rec.request_id,
                action="RENEW",
                accepted=True,
                basis=f"ACTIVE + owner match -> expiry extended by {extend}s",
                quota_before=before,
                quota_after=decision.quota_after,
                env_id=rec.env_id,
            )
            return decision

    def release(self, env_id: str, owner: str) -> Decision:
        with self.store.lock:
            before = self._quota_view()
            rec = self._get_by_env(env_id)
            self._require_owner(rec, owner)
            if rec.state == EnvState.RELEASED.value:
                decision = self._decision(
                    rec, accepted=True, deduplicated=True,
                    reason="environment already RELEASED (idempotent release)",
                )
            elif rec.state == EnvState.RECLAIMED.value:
                raise ControlPlaneError(
                    RejectCode.INVALID_STATE_TRANSITION,
                    "cannot release an environment that was already RECLAIMED",
                )
            else:
                now = self._now()
                rec.state = EnvState.RELEASED.value
                rec.released_at = _iso(now)
                rec.lease_expires_at = None
                rec.updated_at = _iso(now)
                rec.release_count += 1
                self.store.save()
                decision = self._decision(rec, accepted=True)
            log_decision(
                self.logger,
                request_id=rec.request_id,
                action="RELEASE",
                accepted=True,
                basis=(
                    "already RELEASED -> no-op"
                    if decision.deduplicated
                    else "PENDING/ACTIVE -> RELEASED, quota returned"
                ),
                quota_before=before,
                quota_after=decision.quota_after,
                env_id=rec.env_id,
            )
            return decision

    def reclaim(self, env_id: str) -> Decision:
        """System operation: recycle an ACTIVE environment whose lease expired."""
        with self.store.lock:
            before = self._quota_view()
            rec = self._get_by_env(env_id)
            if rec.state == EnvState.RECLAIMED.value:
                decision = self._decision(
                    rec, accepted=True, deduplicated=True,
                    reason="environment already RECLAIMED (idempotent reclaim)",
                )
                basis = "already RECLAIMED -> no-op"
            elif rec.state == EnvState.RELEASED.value:
                decision = self._decision(
                    rec, accepted=True, deduplicated=True,
                    reason="environment already RELEASED; nothing to reclaim",
                )
                basis = "already RELEASED -> no-op"
            elif rec.state == EnvState.PENDING.value:
                raise ControlPlaneError(
                    RejectCode.INVALID_STATE_TRANSITION,
                    "cannot reclaim a PENDING environment: no lease is running",
                )
            else:  # ACTIVE
                now = self._now()
                if not self._is_expired(rec, now):
                    raise ControlPlaneError(
                        RejectCode.LEASE_NOT_EXPIRED,
                        f"lease still valid until {rec.lease_expires_at}",
                    )
                rec.state = EnvState.RECLAIMED.value
                rec.reclaimed_at = _iso(now)
                rec.lease_expires_at = None
                rec.updated_at = _iso(now)
                rec.reclaim_count += 1
                self.store.save()
                decision = self._decision(rec, accepted=True)
                basis = "ACTIVE + lease expired -> RECLAIMED, quota returned"
            log_decision(
                self.logger,
                request_id=rec.request_id,
                action="RECLAIM",
                accepted=True,
                basis=basis,
                quota_before=before,
                quota_after=decision.quota_after,
                env_id=rec.env_id,
            )
            return decision

    # ----------------------------------------------------------------- views

    def get_by_request(self, request_id: str) -> EnvironmentRecord:
        with self.store.lock:
            env_id = self.store.request_index.get(request_id)
            if env_id is None:
                raise ControlPlaneError(
                    RejectCode.UNKNOWN_REQUEST,
                    f"no environment is associated with request '{request_id}'",
                )
            return self.store.environments[env_id]

    def get_conclusion(self, request_id: str) -> RequestConclusion:
        """Return the persisted conclusion for a request_id.

        Raises UNKNOWN_REQUEST only for request_ids that were never submitted;
        a REJECTED conclusion is a normal, queryable result and is clearly
        distinguishable from "never seen" via its outcome field.
        """
        with self.store.lock:
            conclusion = self.store.conclusions.get(request_id)
            if conclusion is None:
                raise ControlPlaneError(
                    RejectCode.UNKNOWN_REQUEST,
                    f"no conclusion has been recorded for request '{request_id}'",
                )
            return conclusion

    def list_environments(self, owner: str | None = None) -> list[EnvironmentRecord]:
        with self.store.lock:
            recs = list(self.store.environments.values())
        if owner is not None:
            recs = [r for r in recs if r.owner == owner]
        return recs

    def quota(self) -> dict[str, Any]:
        with self.store.lock:
            return self._quota_view()


def _default_clock() -> float:
    import time

    return time.time()
