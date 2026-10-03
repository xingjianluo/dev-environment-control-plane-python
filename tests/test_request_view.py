"""Query-by-request-id contract: original env fields survive at every stage.

GET /requests/{request_id} (manager.request_view) must return, for an
*accepted* request, the full original environment record -- created_at,
updated_at, lease_seconds, the occupy/release/reclaim timestamps and the
occupy/release/reclaim/renew counters -- at every lifecycle stage, with the
verdict fields (accepted/decided_at/code/reason) added on top, never
replacing them. Rejected requests keep their own stable field set, and a
never-submitted id stays distinguishable (UNKNOWN_REQUEST / 404).
"""
from __future__ import annotations

import threading

import pytest

from controlplane.manager import ControlPlaneManager
from controlplane.models import ApplyRequest, ControlPlaneError, RejectCode
from controlplane.store import Store

# Fields that existed before verdicts were introduced; all must be present
# with unchanged names/types for every accepted request, at every stage.
ORIGINAL_ENV_FIELDS = {
    "env_id",
    "owner",
    "request_id",
    "state",
    "lease_seconds",
    "created_at",
    "updated_at",
    "lease_expires_at",
    "occupied_at",
    "released_at",
    "reclaimed_at",
    "occupy_count",
    "release_count",
    "reclaim_count",
    "renew_count",
}

# Additive verdict fields introduced alongside the frozen conclusions.
VERDICT_FIELDS = {"accepted", "decided_at", "code", "reason"}

# The rejected path keeps exactly this stable field set.
REJECTED_FIELDS = {
    "request_id",
    "accepted",
    "owner",
    "code",
    "reason",
    "decided_at",
    "env_id",
    "state",
    "lease_expires_at",
}

COUNTER_FIELDS = ("occupy_count", "release_count", "reclaim_count", "renew_count")


def apply(manager, request_id, owner, lease=60):
    return manager.apply(
        ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease)
    )


def assert_original_fields(view, *, request_id, owner, state):
    """Every original field present, correctly typed, at any lifecycle stage."""
    missing = ORIGINAL_ENV_FIELDS - view.keys()
    assert not missing, f"original fields missing from view: {sorted(missing)}"
    # additive verdict fields are present too
    assert VERDICT_FIELDS <= view.keys()
    assert view["accepted"] is True
    assert view["decided_at"]
    assert view["code"] is None and view["reason"] is None

    assert view["request_id"] == request_id
    assert view["owner"] == owner
    assert view["state"] == state
    assert isinstance(view["env_id"], str) and view["env_id"]
    assert isinstance(view["lease_seconds"], int) and view["lease_seconds"] > 0
    assert isinstance(view["created_at"], str) and view["created_at"]
    assert isinstance(view["updated_at"], str) and view["updated_at"]
    for f in COUNTER_FIELDS:
        assert isinstance(view[f], int), f"{f} must stay an int"
    for f in ("lease_expires_at", "occupied_at", "released_at", "reclaimed_at"):
        assert view[f] is None or isinstance(view[f], str)


# ---------------------------------------------------------------- lifecycle


def test_view_after_apply_pending_keeps_all_original_fields(manager):
    apply(manager, "v-pending", "alice", lease=45)
    view = manager.request_view("v-pending")
    assert_original_fields(view, request_id="v-pending", owner="alice", state="PENDING")
    assert view["lease_seconds"] == 45
    assert view["occupied_at"] is None
    assert view["released_at"] is None
    assert view["reclaimed_at"] is None
    assert view["lease_expires_at"] is None
    assert all(view[f] == 0 for f in COUNTER_FIELDS)


def test_view_after_occupy_active_keeps_all_original_fields(manager):
    decision = apply(manager, "v-active", "alice", lease=30)
    manager.occupy(decision.env_id, "alice")
    view = manager.request_view("v-active")
    assert_original_fields(view, request_id="v-active", owner="alice", state="ACTIVE")
    assert view["occupied_at"] is not None
    assert view["lease_expires_at"] is not None
    assert view["occupy_count"] == 1
    assert view["released_at"] is None and view["reclaimed_at"] is None


def test_view_after_renew_keeps_fields_and_counts(manager):
    decision = apply(manager, "v-renew", "alice", lease=30)
    manager.occupy(decision.env_id, "alice")
    manager.renew(decision.env_id, "alice")
    manager.renew(decision.env_id, "alice", extend_seconds=10)
    view = manager.request_view("v-renew")
    assert_original_fields(view, request_id="v-renew", owner="alice", state="ACTIVE")
    assert view["renew_count"] == 2
    assert view["occupy_count"] == 1
    assert view["lease_expires_at"] is not None


def test_view_after_release_keeps_history_and_counts(manager):
    decision = apply(manager, "v-released", "alice")
    manager.occupy(decision.env_id, "alice")
    manager.release(decision.env_id, "alice")
    view = manager.request_view("v-released")
    assert_original_fields(view, request_id="v-released", owner="alice", state="RELEASED")
    assert view["occupied_at"] is not None
    assert view["released_at"] is not None
    assert view["release_count"] == 1
    assert view["occupy_count"] == 1
    assert view["reclaim_count"] == 0


def test_view_after_reclaim_keeps_history_and_counts(manager, clock):
    decision = apply(manager, "v-reclaimed", "alice", lease=5)
    manager.occupy(decision.env_id, "alice")
    clock.advance(10)
    manager.reclaim(decision.env_id)
    view = manager.request_view("v-reclaimed")
    assert_original_fields(view, request_id="v-reclaimed", owner="alice", state="RECLAIMED")
    assert view["occupied_at"] is not None
    assert view["reclaimed_at"] is not None
    assert view["reclaim_count"] == 1
    assert view["release_count"] == 0


# ------------------------------------------------- rejected vs never-seen


def test_rejected_view_field_set_is_stable_and_distinct(manager):
    apply(manager, "v-holder", "alice")
    apply(manager, "v-denied", "alice")  # owner quota exhausted -> frozen rejection

    view = manager.request_view("v-denied")
    assert set(view.keys()) == REJECTED_FIELDS
    assert view["accepted"] is False
    assert view["code"] == "OWNER_QUOTA_EXHAUSTED"
    assert view["reason"]
    assert view["decided_at"]
    assert view["env_id"] is None and view["state"] is None

    # never-submitted stays a different, explicit outcome
    with pytest.raises(ControlPlaneError) as e:
        manager.request_view("v-never-submitted")
    assert e.value.code is RejectCode.UNKNOWN_REQUEST


# ------------------------------------------------------------------ restart


def test_view_is_identical_before_and_after_restart(state_path, clock):
    mgr1 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)
    accepted = mgr1.apply(ApplyRequest(request_id="rst-ok", owner="alice", lease_seconds=30))
    mgr1.occupy(accepted.env_id, "alice")
    mgr1.renew(accepted.env_id, "alice")
    denied = mgr1.apply(ApplyRequest(request_id="rst-no", owner="alice"))
    assert not denied.accepted

    before_ok = mgr1.request_view("rst-ok")
    before_no = mgr1.request_view("rst-no")
    assert_original_fields(before_ok, request_id="rst-ok", owner="alice", state="ACTIVE")

    # restart: new store + manager over the same state file
    mgr2 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)
    after_ok = mgr2.request_view("rst-ok")
    after_no = mgr2.request_view("rst-no")

    assert after_ok == before_ok
    assert after_no == before_no
    assert_original_fields(after_ok, request_id="rst-ok", owner="alice", state="ACTIVE")

    # and after further lifecycle progress post-restart the fields stay complete
    mgr2.release(accepted.env_id, "alice")
    final = mgr2.request_view("rst-ok")
    assert_original_fields(final, request_id="rst-ok", owner="alice", state="RELEASED")
    assert final["release_count"] == 1
    assert final["occupied_at"] == before_ok["occupied_at"]
    assert final["created_at"] == before_ok["created_at"]


# --------------------------------------------------------------- concurrency


def test_concurrent_same_id_at_capacity_boundary_views_converge(manager):
    """One free global slot, many racers on the same id: every observer --
    and every later query -- sees the identical, complete view."""
    apply(manager, "cap-fill", "bob")  # global limit 2 -> exactly one slot left

    barrier = threading.Barrier(16)
    decisions: list = []

    def worker() -> None:
        barrier.wait()
        decisions.append(apply(manager, "cap-race", "alice"))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(decisions) == 16
    assert all(d.accepted for d in decisions)
    assert len({d.env_id for d in decisions}) == 1

    # concurrent readers all observe the identical full view
    views: list = []
    barrier2 = threading.Barrier(16)

    def reader() -> None:
        barrier2.wait()
        views.append(manager.request_view("cap-race"))

    readers = [threading.Thread(target=reader) for _ in range(16)]
    for t in readers:
        t.start()
    for t in readers:
        t.join()

    assert len(views) == 16
    first = views[0]
    assert_original_fields(first, request_id="cap-race", owner="alice", state="PENDING")
    for v in views[1:]:
        assert v == first
    assert all(set(v.keys()) == set(first.keys()) for v in views)


# -------------------------------------------------------------------- HTTP


def _apply_http(client, request_id, owner, lease=60):
    return client.post(
        "/environments/apply",
        json={"request_id": request_id, "owner": owner, "lease_seconds": lease},
    )


def test_http_view_returns_full_fields_through_lifecycle(client, clock):
    r = _apply_http(client, "http-full", "alice", lease=20)
    assert r.status_code == 200
    env_id = r.json()["env_id"]

    view = client.get("/requests/http-full").json()
    assert_original_fields(view, request_id="http-full", owner="alice", state="PENDING")

    client.post(f"/environments/{env_id}/occupy", json={"owner": "alice"})
    client.post(f"/environments/{env_id}/renew", json={"owner": "alice"})
    view = client.get("/requests/http-full").json()
    assert_original_fields(view, request_id="http-full", owner="alice", state="ACTIVE")
    assert view["occupy_count"] == 1 and view["renew_count"] == 1

    client.post(f"/environments/{env_id}/release", json={"owner": "alice"})
    view = client.get("/requests/http-full").json()
    assert_original_fields(view, request_id="http-full", owner="alice", state="RELEASED")
    assert view["release_count"] == 1
    assert view["released_at"] is not None


def test_http_rejected_and_unknown_stay_distinguishable(client):
    _apply_http(client, "http-holder", "alice")
    denied = _apply_http(client, "http-denied", "alice")
    assert denied.status_code == 409

    view = client.get("/requests/http-denied")
    assert view.status_code == 200
    assert set(view.json().keys()) == REJECTED_FIELDS
    assert view.json()["accepted"] is False
    assert view.json()["code"] == "OWNER_QUOTA_EXHAUSTED"

    missing = client.get("/requests/http-never")
    assert missing.status_code == 404
    assert missing.json()["code"] == "UNKNOWN_REQUEST"
