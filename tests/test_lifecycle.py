"""Lifecycle state machine: apply -> occupy -> renew -> release, idempotency."""
from __future__ import annotations

import pytest

from controlplane.models import ApplyRequest, ControlPlaneError, RejectCode
from controlplane.store import Store


def apply(manager, request_id="r1", owner="alice", lease=30):
    return manager.apply(ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease))


def test_full_lifecycle(manager, clock):
    from datetime import datetime

    def ts(value):
        return datetime.fromisoformat(value).timestamp()

    d = apply(manager)
    assert d.accepted and d.state == "PENDING" and d.env_id
    assert d.quota_after["global"]["used"] == 1
    assert d.quota_after["owners"]["alice"]["used"] == 1

    d2 = manager.occupy(d.env_id, "alice")
    assert d2.state == "ACTIVE" and d2.lease_expires_at is not None
    # occupy ran at FakeClock start 1_000_000 with a 30s lease
    assert abs(ts(d2.lease_expires_at) - (1_000_000 + 30)) < 1e-6
    first_expiry = ts(d2.lease_expires_at)

    clock.advance(10)
    d3 = manager.renew(d.env_id, "alice")
    assert d3.accepted
    # lease started at t0+30; renew at t0+10 extends from the live expiry -> t0+60
    assert abs(ts(d3.lease_expires_at) - (first_expiry + 30)) < 1e-6


def test_duplicate_apply_returns_same_env_and_result(manager):
    d1 = apply(manager, "dup-1", "alice")
    d2 = apply(manager, "dup-1", "alice")
    assert d2.accepted and d2.deduplicated
    assert d1.env_id == d2.env_id
    assert d2.state == "PENDING"
    # only one slot reserved globally
    assert d2.quota_after["global"]["used"] == 1

    # after state advances, duplicate apply keeps reporting the live state
    manager.occupy(d1.env_id, "alice")
    d3 = apply(manager, "dup-1", "alice")
    assert d3.env_id == d1.env_id and d3.state == "ACTIVE" and d3.deduplicated


def test_release_is_idempotent_and_returns_quota(manager):
    d = apply(manager, "rel-1", "alice")
    manager.occupy(d.env_id, "alice")
    r1 = manager.release(d.env_id, "alice")
    assert r1.state == "RELEASED" and r1.quota_after["global"]["used"] == 0
    r2 = manager.release(d.env_id, "alice")
    assert r2.deduplicated and r2.state == "RELEASED"


def test_occupy_twice_is_idempotent(manager):
    d = apply(manager, "occ-1", "alice")
    o1 = manager.occupy(d.env_id, "alice")
    o2 = manager.occupy(d.env_id, "alice")
    assert o1.accepted and o2.deduplicated and o2.state == "ACTIVE"


def test_illegal_transitions_are_rejected_with_distinct_reasons(manager):
    d = apply(manager, "tr-1", "alice")
    manager.occupy(d.env_id, "alice")
    manager.release(d.env_id, "alice")

    with pytest.raises(ControlPlaneError) as e:
        manager.occupy(d.env_id, "alice")
    assert e.value.code is RejectCode.INVALID_STATE_TRANSITION

    with pytest.raises(ControlPlaneError) as e:
        manager.renew(d.env_id, "alice")
    assert e.value.code is RejectCode.INVALID_STATE_TRANSITION

    # released -> release idempotent; but released -> reclaim is a no-op
    rec = manager.reclaim(d.env_id)
    assert rec.deduplicated and rec.state == "RELEASED"


def test_owner_mismatch_is_rejected(manager):
    d = apply(manager, "own-1", "alice")
    with pytest.raises(ControlPlaneError) as e:
        manager.release(d.env_id, "bob")
    assert e.value.code is RejectCode.OWNER_MISMATCH


def test_unknown_request_and_env(manager):
    with pytest.raises(ControlPlaneError) as e:
        manager.get_by_request("nope")
    assert e.value.code is RejectCode.UNKNOWN_REQUEST
    with pytest.raises(ControlPlaneError) as e:
        manager.reclaim("env-missing")
    assert e.value.code is RejectCode.UNKNOWN_REQUEST


def test_invalid_apply_request(manager):
    with pytest.raises(ControlPlaneError) as e:
        manager.apply(ApplyRequest(request_id="", owner="alice"))
    assert e.value.code is RejectCode.INVALID_REQUEST
    with pytest.raises(ControlPlaneError) as e:
        manager.apply(ApplyRequest(request_id="x", owner="alice", lease_seconds=0))
    assert e.value.code is RejectCode.INVALID_REQUEST


def test_state_persists_across_restart(manager, state_path, clock):
    d = apply(manager, "persist-1", "alice", lease=30)
    manager.occupy(d.env_id, "alice")
    env_id = d.env_id

    # simulate process restart: new store + manager over the same state file
    restarted = Store(state_path)
    from controlplane.manager import ControlPlaneManager as CPM

    mgr2 = CPM(restarted, global_limit=2, per_owner_limit=1, clock=clock)
    rec = mgr2.get_by_request("persist-1")
    assert rec.env_id == env_id and rec.state == "ACTIVE"
    assert rec.lease_expires_at is not None
    assert mgr2.quota()["global"]["used"] == 1


def test_decision_log_contains_request_id_basis_and_quota(manager, log_capture):
    _logger, handler = log_capture
    apply(manager, "log-1", "alice")
    entries = [r for r in handler.records if r["request_id"] == "log-1"]
    assert entries, "expected a decision log line for request log-1"
    e = entries[0]
    assert e["action"] == "APPLY" and e["accepted"] is True
    assert "basis" in e and e["basis"]
    assert e["quota_before"]["global"]["used"] == 0
    assert e["quota_after"]["global"]["used"] == 1
