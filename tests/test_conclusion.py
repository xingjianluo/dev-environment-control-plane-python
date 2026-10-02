"""Request conclusions: decided once, persisted, replayed verbatim.

Covers:
- a rejection stays rejected after capacity frees up; fresh ids still succeed
- conclusions (accepted and rejected) survive a process restart
- concurrent submissions of one id under quota pressure converge to one result
- rejected conclusions consume no quota and are queryable / distinguishable
  from never-submitted ids
- legacy v2 state files (no conclusions key) load and keep working
"""
from __future__ import annotations

import json
import threading

import pytest

from controlplane.manager import ControlPlaneManager
from controlplane.models import (
    ApplyRequest,
    ControlPlaneError,
    RejectCode,
)
from controlplane.store import Store


def apply(manager, request_id, owner, lease=60):
    return manager.apply(ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease))


def restart(state_path, clock):
    """Simulate a process restart over the same state file."""
    return ControlPlaneManager(
        Store(state_path), global_limit=2, per_owner_limit=1, clock=clock
    )


# ------------------------------------------------------- rejection is sticky


def test_rejection_is_final_after_capacity_frees_up(manager):
    d1 = apply(manager, "a1", "alice")
    d2 = apply(manager, "b1", "bob")
    rejected = apply(manager, "c1", "carol")
    assert not rejected.accepted
    assert rejected.code == "GLOBAL_QUOTA_EXHAUSTED"
    first_reason = rejected.reason
    first_decided_at = rejected.decided_at
    assert first_decided_at

    # capacity frees up completely
    manager.release(d1.env_id, "alice")
    manager.release(d2.env_id, "bob")
    assert manager.quota()["global"]["used"] == 0

    # same id still gets the original rejection, verbatim
    again = apply(manager, "c1", "carol")
    assert not again.accepted
    assert again.code == "GLOBAL_QUOTA_EXHAUSTED"
    assert again.reason == first_reason
    assert again.decided_at == first_decided_at
    assert again.deduplicated
    assert again.env_id is None

    # a brand-new id is unaffected and succeeds
    fresh = apply(manager, "c2", "carol")
    assert fresh.accepted and fresh.env_id


def test_owner_rejection_is_final_after_owner_frees_slot(manager):
    d1 = apply(manager, "o1", "alice")
    rejected = apply(manager, "o2", "alice")
    assert rejected.code == "OWNER_QUOTA_EXHAUSTED"

    manager.release(d1.env_id, "alice")
    assert manager.quota()["owners"] == {}  # nobody holds quota now

    again = apply(manager, "o2", "alice")
    assert not again.accepted
    assert again.code == "OWNER_QUOTA_EXHAUSTED"
    assert again.reason == rejected.reason

    fresh = apply(manager, "o3", "alice")
    assert fresh.accepted


def test_rejection_consumes_no_quota(manager):
    apply(manager, "q1", "alice")
    apply(manager, "q2", "bob")
    before = manager.quota()
    rejected = apply(manager, "q3", "carol")
    assert not rejected.accepted
    after = manager.quota()
    assert before == after
    assert rejected.quota_after["global"]["used"] == 2


# ------------------------------------------------------------ restart safety


def test_conclusions_survive_restart(manager, state_path, clock):
    ok = apply(manager, "r-ok", "alice")
    apply(manager, "r-fill", "bob")
    bad = apply(manager, "r-bad", "carol")
    assert ok.accepted and not bad.accepted

    mgr2 = restart(state_path, clock)

    # accepted conclusion replays the same environment
    again_ok = mgr2.apply(ApplyRequest(request_id="r-ok", owner="alice"))
    assert again_ok.accepted and again_ok.env_id == ok.env_id
    assert again_ok.decided_at == ok.decided_at

    # rejected conclusion replays the same code/reason even after capacity
    # is freed post-restart
    mgr2.release(ok.env_id, "alice")
    again_bad = mgr2.apply(ApplyRequest(request_id="r-bad", owner="carol"))
    assert not again_bad.accepted
    assert again_bad.code == bad.code
    assert again_bad.reason == bad.reason
    assert again_bad.decided_at == bad.decided_at

    # both conclusions are queryable after restart
    c_ok = mgr2.get_conclusion("r-ok")
    c_bad = mgr2.get_conclusion("r-bad")
    assert c_ok.outcome == "ACCEPTED" and c_ok.env_id == ok.env_id
    assert c_bad.outcome == "REJECTED" and c_bad.code == "GLOBAL_QUOTA_EXHAUSTED"
    assert c_bad.decided_at == bad.decided_at


# ------------------------------------------------------------- concurrency


def test_concurrent_same_id_under_quota_pressure_converges(manager):
    """One global slot left, many threads race the same request_id.

    All callers must observe the identical conclusion; exactly one
    environment may be created.
    """
    apply(manager, "filler", "bob")  # global: 1/2 used, one slot left

    results: list = []
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        results.append(apply(manager, "race-1", "alice"))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(d.accepted for d in results)
    assert len({d.env_id for d in results}) == 1
    assert manager.quota()["global"]["used"] == 2

    # and once the slot is gone, a racing id converges on one rejection
    results2: list = []
    barrier2 = threading.Barrier(16)

    def worker2() -> None:
        barrier2.wait()
        results2.append(apply(manager, "race-2", "carol"))

    threads = [threading.Thread(target=worker2) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(not d.accepted for d in results2)
    assert {d.code for d in results2} == {"GLOBAL_QUOTA_EXHAUSTED"}
    assert len({d.reason for d in results2}) == 1
    assert len({d.decided_at for d in results2}) == 1
    # the rejection consumed nothing; quota constraints never breached
    assert manager.quota()["global"]["used"] == 2
    assert len(manager.list_environments()) == 2


# --------------------------------------------------------- query & distinct


def test_rejected_is_queryable_and_distinct_from_unknown(manager):
    apply(manager, "k1", "alice")
    apply(manager, "k2", "bob")
    apply(manager, "k3", "carol")  # rejected

    conclusion = manager.get_conclusion("k3")
    assert conclusion.outcome == "REJECTED"
    assert conclusion.code == "GLOBAL_QUOTA_EXHAUSTED"
    assert conclusion.reason
    assert conclusion.decided_at
    assert conclusion.env_id is None

    accepted = manager.get_conclusion("k1")
    assert accepted.outcome == "ACCEPTED" and accepted.env_id

    with pytest.raises(ControlPlaneError) as e:
        manager.get_conclusion("never-submitted")
    assert e.value.code is RejectCode.UNKNOWN_REQUEST


def test_conclusion_queryable_via_http(client):
    client.post("/environments/apply", json={"request_id": "h1", "owner": "alice"})
    client.post("/environments/apply", json={"request_id": "h2", "owner": "bob"})
    resp = client.post("/environments/apply", json={"request_id": "h3", "owner": "carol"})
    assert resp.status_code == 409

    rejected = client.get("/requests/h3")
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["outcome"] == "REJECTED"
    assert body["code"] == "GLOBAL_QUOTA_EXHAUSTED"
    assert body["decided_at"]

    accepted = client.get("/requests/h1").json()
    assert accepted["outcome"] == "ACCEPTED" and accepted["env_id"]
    assert accepted["state"] == "PENDING"  # live env record still embedded

    unknown = client.get("/requests/never-seen")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "UNKNOWN_REQUEST"


# ------------------------------------------------------------- legacy compat


def test_legacy_v2_state_file_loads_and_replays(state_path, clock):
    """A v2 file has no conclusions key; accepted requests are backfilled."""
    legacy = {
        "version": 2,
        "environments": [
            {
                "env_id": "env-legacy1",
                "owner": "alice",
                "request_id": "legacy-1",
                "state": "ACTIVE",
                "lease_seconds": 60,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
                "lease_expires_at": "2026-09-01T00:01:00+00:00",
            }
        ],
        "request_index": {"legacy-1": "env-legacy1"},
    }
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    mgr = restart(state_path, clock)

    # the old accepted request is queryable through the new interface
    conclusion = mgr.get_conclusion("legacy-1")
    assert conclusion.outcome == "ACCEPTED"
    assert conclusion.env_id == "env-legacy1"
    assert conclusion.decided_at == "2026-09-01T00:00:00+00:00"

    # and re-submitting the old id replays the same environment
    again = mgr.apply(ApplyRequest(request_id="legacy-1", owner="alice"))
    assert again.accepted and again.env_id == "env-legacy1" and again.deduplicated

    # legacy quota semantics unchanged: alice holds 1 slot, owner limit is 1
    rejected = mgr.apply(ApplyRequest(request_id="legacy-2", owner="alice"))
    assert not rejected.accepted and rejected.code == "OWNER_QUOTA_EXHAUSTED"

    # the upgraded file now persists conclusions
    with open(state_path, "r", encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["version"] == 3
    ids = {c["request_id"] for c in saved["conclusions"]}
    assert ids == {"legacy-1", "legacy-2"}


# ------------------------------------------------------------------- logging


def test_conclusion_log_shows_id_basis_quota_and_outcome(manager, log_capture):
    _logger, handler = log_capture
    apply(manager, "lg1", "alice")
    apply(manager, "lg2", "bob")
    apply(manager, "lg3", "carol")  # rejected, conclusion recorded
    apply(manager, "lg3", "carol")  # replayed conclusion

    recorded = [
        r for r in handler.records
        if r["request_id"] == "lg3" and r["action"] == "APPLY"
    ]
    assert len(recorded) == 2

    first, replay = recorded
    assert first["accepted"] is False
    assert first["outcome"] == "REJECTED"
    assert "global used 2 >= limit 2" in first["basis"]
    assert first["quota_before"]["global"]["used"] == 2
    assert first["quota_after"]["global"]["used"] == 2  # rejection changed nothing
    assert first["decided_at"]

    assert replay["accepted"] is False
    assert replay["outcome"] == "REJECTED"
    assert "replay" in replay["basis"]
    assert replay["decided_at"] == first["decided_at"]

    accepted = next(
        r for r in handler.records if r["request_id"] == "lg1" and r["action"] == "APPLY"
    )
    assert accepted["accepted"] is True
    assert accepted["outcome"] == "ACCEPTED"
    assert accepted["quota_after"]["global"]["used"] == 1
