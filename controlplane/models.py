"""Domain types: environment records, state machine, error codes, request bodies."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class EnvState(str, Enum):
    """Environment lifecycle states.

    PENDING   : allocated (quota reserved) but not yet occupied, no lease running
    ACTIVE    : occupied, lease running; holder must renew before lease_expires_at
    RELEASED  : explicitly released by owner; quota returned
    RECLAIMED : reclaimed by control plane after lease expiry; quota returned
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    RECLAIMED = "RECLAIMED"


class RejectCode(str, Enum):
    """Stable, distinguishable reasons for a rejected operation."""

    UNKNOWN_REQUEST = "UNKNOWN_REQUEST"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    LEASE_NOT_EXPIRED = "LEASE_NOT_EXPIRED"
    GLOBAL_QUOTA_EXHAUSTED = "GLOBAL_QUOTA_EXHAUSTED"
    OWNER_QUOTA_EXHAUSTED = "OWNER_QUOTA_EXHAUSTED"
    INVALID_REQUEST = "INVALID_REQUEST"


class ConclusionOutcome(str, Enum):
    """The two possible terminal conclusions for one request_id."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass
class RequestConclusion:
    """The immutable, persisted conclusion of one request_id.

    Once written it never changes: a later submission of the same request_id
    replays this record verbatim, regardless of how capacity has evolved.
    A REJECTED conclusion consumes no quota; it only records that this
    request_id was turned away, with the stable code/reason and the time the
    conclusion was made.
    """

    request_id: str
    owner: str
    outcome: str  # ConclusionOutcome value
    decided_at: str
    env_id: str | None = None  # set iff outcome == ACCEPTED
    code: str | None = None  # RejectCode value, set iff outcome == REJECTED
    reason: str | None = None  # human-readable rejection reason

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ControlPlaneError(Exception):
    """Domain error carrying a stable rejection code and an explanatory message."""

    def __init__(self, code: RejectCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class EnvironmentRecord:
    """Persistent record of one environment.

    Stored as JSON; fields added later must have deterministic defaults when
    loading older records (see store.DEFAULT_FIELD_VALUES).
    """

    env_id: str
    owner: str
    request_id: str
    state: str
    lease_seconds: int
    created_at: str
    updated_at: str
    lease_expires_at: str | None = None
    occupied_at: str | None = None
    released_at: str | None = None
    reclaimed_at: str | None = None
    release_count: int = 0
    reclaim_count: int = 0
    renew_count: int = 0
    occupy_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyRequest:
    request_id: str
    owner: str
    lease_seconds: int = 60


@dataclass
class OwnerRequest:
    owner: str


@dataclass
class Decision:
    """Result of an operation, rendered uniformly by the HTTP layer."""

    accepted: bool
    request_id: str
    env_id: str | None = None
    state: str | None = None
    owner: str | None = None
    code: str | None = None
    reason: str | None = None
    deduplicated: bool = False
    quota_after: dict[str, Any] | None = None
    lease_expires_at: str | None = None
    decided_at: str | None = None  # when the persisted conclusion was made

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
