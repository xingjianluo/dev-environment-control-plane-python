"""Lease semantics: expiry, renewal, idempotent reclaim, legacy compatibility."""
from __future__ import annotations

import json
import threading

import pytest

from controlplane.models import ApplyRequest, ControlPlaneError, RejectCode
from controlplane.store import Store


def occupy(manager, request_id, owner="alice", lease=30):
    d = manager.apply(
        ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease)
    )
    return manager.occupy(d.env_id, "alice" if owner == "alice" else owner)


def test_reclaim_before_expiry_rejected(manager, clock):
    d = occupy(manager, "le1", lease=30)
    clock.advance(29)
    with pytest.raises(ControlPlaneError) as e:
        manager.reclaim(d.env_id)
    assert e.value.code is RejectCode.LEASE_NOT_EXPIRED
    assert manager._get_by_env(d.env_id).state == "ACTIVE"


def test_reclaim_after_expiry_returns_quota(manager, clock):
    d = occupy(manager, "le2", lease=30)
    clock.advance(30)
    rec = manager.reclaim(d.env_id)
    assert rec.accepted and rec.state == "RECLAIMED"
    assert rec.quota_after["global"]["used"] == 0


def test_reclaim_is_idempotent(manager, clock):
    d = occupy(manager, "le3", lease=10)
    clock.advance(10)
    r1 = manager.reclaim(d.env_id)
    r2 = manager.reclaim(d.env_id)
    assert r1.state == "RECLAIMED"
    assert r2.deduplicated and r2.state == "RECLAIMED"
    assert manager._get_by_env(d.env_id).reclaim_count == 1


def test_renew_prevents_reclaim(manager, clock):
    d = occupy(manager, "le4", lease=10)
    clock.advance(9)
    manager.renew(d.env_id, "alice", 100)
    clock.advance(95)  # t=104, old expiry would have been 10
    with pytest.raises(ControlPlaneError) as e:
        manager.reclaim(d.env_id)
    assert e.value.code is RejectCode.LEASE_NOT_EXPIRED


def test_cannot_reclaim_pending(manager):
    d = manager.apply(ApplyRequest(request_id="le5", owner="alice", lease_seconds=10))
    with pytest.raises(ControlPlaneError) as e:
        manager.reclaim(d.env_id)
    assert e.value.code is RejectCode.INVALID_STATE_TRANSITION


def test_reclaim_then_release_is_rejected(manager, clock):
    d = occupy(manager, "le6", lease=5)
    clock.advance(5)
    manager.reclaim(d.env_id)
    with pytest.raises(ControlPlaneError) as e:
        manager.release(d.env_id, "alice")
    assert e.value.code is RejectCode.INVALID_STATE_TRANSITION


def test_concurrent_reclaim_and_renew_has_one_winner(manager, clock):
    """At the exact expiry boundary, renew and reclaim race; exactly one wins.

    Lock serialization guarantees a single coherent verdict: the record ends
    either still ACTIVE (renew won) or RECLAIMED (reclaim won), never both.
    """
    d = occupy(manager, "le7", lease=10)
    clock.advance(10)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def do_renew() -> None:
        barrier.wait()
        try:
            manager.renew(d.env_id, "alice", 100)
            outcomes.append("renewed")
        except ControlPlaneError:
            outcomes.append("renew-rejected")

    def do_reclaim() -> None:
        barrier.wait()
        try:
            manager.reclaim(d.env_id)
            outcomes.append("reclaimed")
        except ControlPlaneError:
            outcomes.append("reclaim-rejected")

    t1 = threading.Thread(target=do_renew)
    t2 = threading.Thread(target=do_reclaim)
    t1.start(); t2.start(); t1.join(); t2.join()

    final = manager._get_by_env(d.env_id).state
    assert set(outcomes) in (
        {"renewed", "reclaim-rejected"},
        {"renew-rejected", "reclaimed"},
    )
    assert final in ("ACTIVE", "RECLAIMED")
    # consistency: outcome matches final state
    assert ("renewed" in outcomes) == (final == "ACTIVE")


def test_legacy_record_without_new_fields_loads_with_defaults(state_path):
    """v1 record missing v2 fields reads with deterministic defaults."""
    legacy = {
        "version": 1,
        "environments": [
            {
                "env_id": "env-old",
                "owner": "alice",
                "request_id": "old-1",
                "state": "ACTIVE",
                "created_at": "2026-01-01T00:00:00+00:00",
                # no lease_expires_at / occupied_at / counters / updated_at
            }
        ],
        "request_index": {"old-1": "env-old"},
    }
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    store = Store(state_path)
    rec = store.environments["env-old"]
    assert rec.updated_at == rec.created_at
    assert rec.lease_expires_at is None
    assert rec.release_count == rec.reclaim_count == rec.renew_count == 0
    assert rec.occupy_count == 0 and rec.lease_seconds == 60

    from controlplane.manager import ControlPlaneManager

    mgr = ControlPlaneManager(store, global_limit=2, per_owner_limit=1)
    # legacy ACTIVE without expiry is deterministically treated as expired
    decision = mgr.reclaim("env-old")
    assert decision.state == "RECLAIMED"
    assert decision.quota_after["global"]["used"] == 0


def test_restarted_manager_can_reclaim_and_view_consistently(state_path, clock):
    store1 = Store(state_path)
    from controlplane.manager import ControlPlaneManager

    mgr1 = ControlPlaneManager(store1, global_limit=2, per_owner_limit=1, clock=clock)
    d = mgr1.apply(ApplyRequest(request_id="le8", owner="alice", lease_seconds=10))
    mgr1.occupy(d.env_id, "alice")
    clock.advance(11)

    # restart
    store2 = Store(state_path)
    mgr2 = ControlPlaneManager(store2, global_limit=2, per_owner_limit=1, clock=clock)
    assert mgr2.quota()["global"]["used"] == 1
    rec = mgr2.reclaim(d.env_id)
    assert rec.state == "RECLAIMED"
    assert mgr2.quota()["global"]["used"] == 0
