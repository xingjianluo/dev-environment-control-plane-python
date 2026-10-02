"""Frozen request conclusions: rejection permanence, restart recovery, concurrency.

A request_id gets exactly one verdict, made at first submission and never
revisited afterwards: granting stays granting (same env), quota rejections
stay rejected with the same code/reason/decided_at even after quota frees up,
and concurrent submissions converge on that single verdict.
"""
from __future__ import annotations

import threading

import pytest

from controlplane.manager import ControlPlaneManager
from controlplane.models import ApplyRequest, ControlPlaneError, RejectCode
from controlplane.store import Store


def apply(manager, request_id, owner, lease=60):
    return manager.apply(
        ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease)
    )


def test_rejected_verdict_is_frozen_after_quota_recovers(manager):
    # alice has per-owner limit 1; first apply takes it, second is rejected.
    first = apply(manager, "alice-ok", "alice")
    assert first.accepted
    rejected = apply(manager, "alice-denied", "alice")
    assert not rejected.accepted
    assert rejected.code == "OWNER_QUOTA_EXHAUSTED"
    rejected_reason = rejected.reason
    rejected_at = rejected.decided_at
    assert rejected_at

    # the rejection reserves no quota
    assert rejected.quota_after["global"]["used"] == 1
    assert rejected.quota_after["owners"]["alice"]["used"] == 1

    # free the owner slot (and therefore global capacity)
    manager.release(first.env_id, "alice")
    assert manager.quota()["global"]["used"] == 0

    # same id replays the *same frozen rejection* despite free capacity
    replay = apply(manager, "alice-denied", "alice")
    assert not replay.accepted
    assert replay.deduplicated is True
    assert replay.code == "OWNER_QUOTA_EXHAUSTED"
    assert replay.reason == rejected_reason
    assert replay.decided_at == rejected_at
    assert replay.env_id is None
    # replay still reserves nothing
    assert replay.quota_after["global"]["used"] == 0

    # a brand-new id with capacity available succeeds normally
    fresh = apply(manager, "alice-new", "alice")
    assert fresh.accepted
    assert fresh.env_id != first.env_id
    assert fresh.decided_at


def test_global_rejection_is_frozen_after_global_capacity_recovers(manager):
    ok_a = apply(manager, "ga", "alice")
    ok_b = apply(manager, "gb", "bob")
    assert ok_a.accepted and ok_b.accepted

    denied = apply(manager, "gc", "carol")
    assert not denied.accepted
    assert denied.code == "GLOBAL_QUOTA_EXHAUSTED"
    denied_reason, denied_at = denied.reason, denied.decided_at

    # release one global slot
    manager.release(ok_b.env_id, "bob")
    assert manager.quota()["global"]["used"] == 1

    replay = apply(manager, "gc", "carol")
    assert not replay.accepted and replay.deduplicated
    assert replay.code == "GLOBAL_QUOTA_EXHAUSTED"
    assert replay.reason == denied_reason
    assert replay.decided_at == denied_at

    # carol can still succeed under a *different* id
    other = apply(manager, "gc-other", "carol")
    assert other.accepted


def test_verdict_survives_restart_for_accepted_and_rejected(state_path, clock):
    mgr1 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)
    accepted = mgr1.apply(ApplyRequest(request_id="keep-ok", owner="alice"))
    denied = mgr1.apply(ApplyRequest(request_id="keep-no", owner="alice"))
    assert accepted.accepted and not denied.accepted
    accepted_env = accepted.env_id
    accepted_at = accepted.decided_at
    denied_reason, denied_at = denied.reason, denied.decided_at

    # restart: brand-new store/manager over the same file
    mgr2 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)

    replay_accepted = mgr2.apply(ApplyRequest(request_id="keep-ok", owner="alice"))
    assert replay_accepted.accepted and replay_accepted.deduplicated
    assert replay_accepted.env_id == accepted_env
    assert replay_accepted.decided_at == accepted_at

    replay_denied = mgr2.apply(ApplyRequest(request_id="keep-no", owner="alice"))
    assert not replay_denied.accepted and replay_denied.deduplicated
    assert replay_denied.code == "OWNER_QUOTA_EXHAUSTED"
    assert replay_denied.reason == denied_reason
    assert replay_denied.decided_at == denied_at
    # still only the one accepted environment occupies quota
    assert mgr2.quota()["global"]["used"] == 1

    # direct verdict lookups recover identically after restart
    c_ok = mgr2.get_conclusion("keep-ok")
    c_no = mgr2.get_conclusion("keep-no")
    assert c_ok.accepted and c_ok.env_id == accepted_env and c_ok.decided_at == accepted_at
    assert (not c_no.accepted) and c_no.code == "OWNER_QUOTA_EXHAUSTED"
    assert c_no.reason == denied_reason and c_no.decided_at == denied_at


def test_concurrent_same_id_under_pressure_converges_to_one_verdict(manager):
    # fill the single owner slot for alice
    holder = apply(manager, "holder", "alice")
    assert holder.accepted

    decisions: list = []
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        decisions.append(apply(manager, "hot-id", "alice"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(decisions) == 20
    # every caller observes the identical frozen rejection
    assert all(not d.accepted for d in decisions)
    assert all(d.code == "OWNER_QUOTA_EXHAUSTED" for d in decisions)
    assert len({d.reason for d in decisions}) == 1
    assert len({d.decided_at for d in decisions}) == 1
    assert all(d.env_id is None for d in decisions)
    # exactly the pre-existing holder occupies quota; no env over-created
    assert manager.quota()["global"]["used"] == 1
    assert manager.quota()["owners"]["alice"]["used"] == 1
    stored = [c for c in manager.store.decisions.values() if not c.accepted]
    assert len(stored) == 1 and stored[0].request_id == "hot-id"


def test_concurrent_same_id_with_capacity_creates_one_environment(manager):
    env_ids: list = []
    decided_ats: list = []
    barrier = threading.Barrier(15)

    def worker() -> None:
        barrier.wait()
        d = apply(manager, "win-id", "alice")
        env_ids.append(d.env_id)
        decided_ats.append(d.decided_at)

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(env_ids)) == 1
    assert len(set(decided_ats)) == 1
    assert manager.quota()["global"]["used"] == 1
    assert len(manager.store.environments) == 1


def test_never_submitted_is_distinct_from_rejected(manager):
    # create a real rejected id
    apply(manager, "filler", "alice")
    rejected = apply(manager, "denied", "alice")
    assert not rejected.accepted

    # the rejected id is queryable with its frozen verdict
    conclusion = manager.get_conclusion("denied")
    assert conclusion.accepted is False
    assert conclusion.code == "OWNER_QUOTA_EXHAUSTED"
    assert conclusion.decided_at

    # an id that was never submitted is explicitly UNKNOWN
    with pytest.raises(ControlPlaneError) as e:
        manager.get_conclusion("never-seen")
    assert e.value.code is RejectCode.UNKNOWN_REQUEST


def test_legacy_v2_state_without_decisions_backfills_and_keeps_working(state_path, clock):
    """Old v2 file (no decisions table) loads: accepted verdicts backfilled,
    semantics unchanged, and new rejections are persisted as v3 verdicts."""
    legacy = {
        "version": 2,
        "environments": [
            {
                "env_id": "env-old",
                "owner": "alice",
                "request_id": "old-1",
                "state": "PENDING",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "request_index": {"old-1": "env-old"},
    }
    with open(state_path, "w", encoding="utf-8") as fh:
        import json

        json.dump(legacy, fh)

    mgr = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)

    # old successful request is now queryable as a backfilled ACCEPTED verdict
    view = mgr.request_view("old-1")
    assert view["accepted"] is True and view["env_id"] == "env-old"
    assert view["state"] == "PENDING"
    assert view["decided_at"] == "2026-01-01T00:00:00+00:00"
    # replaying the same id returns the same environment (legacy semantics kept)
    again = mgr.apply(ApplyRequest(request_id="old-1", owner="alice"))
    assert again.accepted and again.deduplicated and again.env_id == "env-old"
    assert mgr.quota()["global"]["used"] == 1

    # new submissions under new ids behave per current rules and persist v3
    denied = mgr.apply(ApplyRequest(request_id="new-no", owner="alice"))
    assert not denied.accepted and denied.code == "OWNER_QUOTA_EXHAUSTED"

    # and the file is now v3 with both decisions; survives another restart
    store2 = Store(state_path)
    assert store2.decisions["old-1"].accepted is True
    assert store2.decisions["new-no"].accepted is False
    assert store2.decisions["new-no"].code == "OWNER_QUOTA_EXHAUSTED"
    mgr2 = ControlPlaneManager(store2, global_limit=2, per_owner_limit=1, clock=clock)
    assert not mgr2.apply(ApplyRequest(request_id="new-no", owner="alice")).accepted


def test_frozen_rejection_log_shows_request_id_basis_and_quota(manager, log_capture):
    _logger, handler = log_capture
    apply(manager, "log-ok", "alice")
    denied = apply(manager, "log-no", "alice")
    assert not denied.accepted

    first = next(
        r for r in handler.records
        if r["request_id"] == "log-no" and not r.get("deduplicated")
    )
    assert first["action"] == "APPLY" and first["accepted"] is False
    assert first["code"] == "OWNER_QUOTA_EXHAUSTED"
    assert "per-owner limit" in first["basis"]
    assert first["quota_before"]["owners"]["alice"]["used"] == 1
    assert first["quota_after"]["owners"]["alice"]["used"] == 1
    assert first["decided_at"]

    # replay logs a distinct frozen-verdict line with the same request_id
    apply(manager, "log-no", "alice")
    replay = next(
        r for r in handler.records
        if r["request_id"] == "log-no" and r.get("deduplicated")
    )
    assert replay["accepted"] is False
    assert "frozen REJECTED" in replay["basis"]
    assert replay["code"] == "OWNER_QUOTA_EXHAUSTED"
