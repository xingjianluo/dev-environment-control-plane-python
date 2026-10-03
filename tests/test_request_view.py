"""GET-by-request_id contract: original env fields survive the conclusion layer.

The frozen-verdict feature (accepted/code/reason/decided_at) is *additive*:
querying a successful request by id must still return every original
environment field (created_at, updated_at, lease_seconds, the occupy/release/
reclaim timestamps and the occupy/release/reclaim/renew counters) with
unchanged names, types and value ranges, at every lifecycle stage. Rejected
and never-submitted ids keep their own distinct, stable shapes.
"""
from __future__ import annotations

import threading

import pytest

from controlplane.manager import ControlPlaneManager
from controlplane.models import ApplyRequest, ControlPlaneError, RejectCode
from controlplane.store import Store

# Fields that existed on the environment record before conclusions were added.
# All of them must be present, with these types, in every accepted view.
ORIGINAL_ENV_FIELDS = {
    "env_id": str,
    "owner": str,
    "request_id": str,
    "state": str,
    "lease_seconds": int,
    "created_at": str,
    "updated_at": str,
    "release_count": int,
    "reclaim_count": int,
    "renew_count": int,
    "occupy_count": int,
}
# Nullable ISO timestamps: always present, str once set, None before.
NULLABLE_TS_FIELDS = ("lease_expires_at", "occupied_at", "released_at", "reclaimed_at")
# Additive conclusion fields on the accepted path.
CONCLUSION_FIELDS = ("accepted", "code", "reason", "decided_at")
# The rejected path keeps exactly this field set (no env record fields).
REJECTED_FIELDS = {
    "request_id", "accepted", "owner", "code", "reason",
    "decided_at", "env_id", "state", "lease_expires_at",
}


def apply(manager, request_id, owner, lease=60):
    return manager.apply(
        ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease)
    )


def assert_original_fields_intact(view, *, state):
    """Every original field present, correctly typed, consistent with state."""
    for name, typ in ORIGINAL_ENV_FIELDS.items():
        assert name in view, f"missing original field: {name}"
        assert isinstance(view[name], typ), f"{name} has wrong type: {view[name]!r}"
    for name in NULLABLE_TS_FIELDS:
        assert name in view, f"missing original field: {name}"
        assert view[name] is None or isinstance(view[name], str)
    assert view["state"] == state
    # additive conclusion fields sit alongside, never replacing the old ones
    assert view["accepted"] is True
    assert view["code"] is None and view["reason"] is None
    assert isinstance(view["decided_at"], str) and view["decided_at"]
    # counters are non-negative ints within their valid range
    for counter in ("occupy_count", "release_count", "reclaim_count", "renew_count"):
        assert view[counter] >= 0


def test_accepted_view_keeps_all_original_fields_through_lifecycle(manager, clock):
    apply(manager, "life-1", "alice", lease=30)

    # stage 1: PENDING (applied, not yet occupied)
    view = manager.request_view("life-1")
    assert_original_fields_intact(view, state="PENDING")
    assert view["occupied_at"] is None and view["occupy_count"] == 0
    created_at, decided_at = view["created_at"], view["decided_at"]

    # stage 2: ACTIVE (occupied, lease running)
    manager.occupy(view["env_id"], "alice")
    view = manager.request_view("life-1")
    assert_original_fields_intact(view, state="ACTIVE")
    assert view["occupied_at"] and view["occupy_count"] == 1
    assert view["lease_expires_at"] and view["lease_seconds"] == 30
    assert view["created_at"] == created_at and view["decided_at"] == decided_at

    # stage 3: renewed
    clock.advance(10)
    manager.renew(view["env_id"], "alice", extend_seconds=90)
    view = manager.request_view("life-1")
    assert_original_fields_intact(view, state="ACTIVE")
    assert view["renew_count"] == 1
    assert view["updated_at"] >= view["occupied_at"]

    # stage 4: RELEASED
    manager.release(view["env_id"], "alice")
    view = manager.request_view("life-1")
    assert_original_fields_intact(view, state="RELEASED")
    assert view["released_at"] and view["release_count"] == 1
    assert view["lease_expires_at"] is None
    # history is preserved, not wiped, in terminal state
    assert view["occupied_at"] and view["occupy_count"] == 1
    assert view["renew_count"] == 1 and view["created_at"] == created_at


def test_accepted_view_keeps_all_original_fields_after_reclaim(manager, clock):
    apply(manager, "life-2", "alice", lease=5)
    env_id = manager.request_view("life-2")["env_id"]
    manager.occupy(env_id, "alice")
    clock.advance(6)
    manager.reclaim(env_id)

    view = manager.request_view("life-2")
    assert_original_fields_intact(view, state="RECLAIMED")
    assert view["reclaimed_at"] and view["reclaim_count"] == 1
    assert view["occupied_at"] and view["occupy_count"] == 1
    assert view["released_at"] is None and view["release_count"] == 0
    assert view["lease_expires_at"] is None


def test_accepted_view_matches_environment_endpoint(client):
    """The by-id view must agree field-by-field with GET /environments/{env_id}."""
    client.post(
        "/environments/apply",
        json={"request_id": "http-1", "owner": "alice", "lease_seconds": 45},
    )
    view = client.get("/requests/http-1").json()
    env = client.get(f"/environments/{view['env_id']}").json()
    for name in ORIGINAL_ENV_FIELDS:
        assert view[name] == env[name], f"field {name} diverges"
    for name in NULLABLE_TS_FIELDS:
        assert view[name] == env[name], f"field {name} diverges"


def test_rejected_view_shape_unchanged_and_distinct_from_unknown(manager):
    apply(manager, "rj-ok", "alice")
    rejected = apply(manager, "rj-no", "alice")
    assert not rejected.accepted

    view = manager.request_view("rj-no")
    # rejected path keeps exactly its historical field set: nothing added,
    # nothing removed, and none of the env-record-only fields leak in
    assert set(view.keys()) == REJECTED_FIELDS
    assert view["accepted"] is False
    assert view["code"] == "OWNER_QUOTA_EXHAUSTED"
    assert view["reason"] and view["decided_at"]
    assert view["env_id"] is None and view["state"] is None

    # never-submitted stays a distinct, explicit UNKNOWN_REQUEST
    with pytest.raises(ControlPlaneError) as e:
        manager.request_view("never-submitted-id")
    assert e.value.code is RejectCode.UNKNOWN_REQUEST


def test_view_identical_across_restart_at_every_stage(state_path, clock):
    mgr1 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)
    apply(mgr1, "rs-pending", "alice")
    apply(mgr1, "rs-active", "bob", lease=10)
    env_bob = mgr1.request_view("rs-active")["env_id"]
    mgr1.occupy(env_bob, "bob")
    mgr1.renew(env_bob, "bob")
    apply(mgr1, "rs-denied", "alice")  # alice's owner slot is taken -> frozen rejection

    before = {rid: mgr1.request_view(rid) for rid in ("rs-pending", "rs-active", "rs-denied")}

    # restart over the same state file
    mgr2 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)
    for rid, snap in before.items():
        after = mgr2.request_view(rid)
        assert after == snap, f"view for {rid} changed across restart"
        if snap["accepted"]:
            assert_original_fields_intact(after, state=snap["state"])
        else:
            assert set(after.keys()) == REJECTED_FIELDS

    # lifecycle continues after restart; released view still complete
    mgr2.release(env_bob, "bob")
    view = mgr2.request_view("rs-active")
    assert_original_fields_intact(view, state="RELEASED")
    assert view["renew_count"] == 1 and view["release_count"] == 1

    # and a third restart still returns the very same content
    mgr3 = ControlPlaneManager(Store(state_path), global_limit=2, per_owner_limit=1, clock=clock)
    assert mgr3.request_view("rs-active") == view


def test_concurrent_same_id_views_converge_complete(manager):
    """Under quota pressure, every concurrent observer of one id must see the
    identical, fully-populated view -- nobody gets a partial payload."""
    barrier = threading.Barrier(16)
    views: list = []

    def worker() -> None:
        barrier.wait()
        apply(manager, "hot-view", "alice")
        views.append(manager.request_view("hot-view"))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(views) == 16
    first = views[0]
    assert all(v == first for v in views), "concurrent views diverged"
    assert_original_fields_intact(first, state="PENDING")
    assert len(manager.store.environments) == 1

    # after the env advances, concurrent readers still converge on full data
    manager.occupy(first["env_id"], "alice")
    views2: list = []
    barrier2 = threading.Barrier(16)

    def reader() -> None:
        barrier2.wait()
        views2.append(manager.request_view("hot-view"))

    threads = [threading.Thread(target=reader) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(v == views2[0] for v in views2)
    assert_original_fields_intact(views2[0], state="ACTIVE")


def test_concurrent_rejected_id_views_converge(manager):
    """Capacity-tight rejection: all observers of the denied id see the same
    stable rejected shape; the holder's accepted view stays complete."""
    apply(manager, "holder", "alice")

    barrier = threading.Barrier(12)
    views: list = []

    def worker() -> None:
        barrier.wait()
        apply(manager, "hot-denied", "alice")
        views.append(manager.request_view("hot-denied"))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(v == views[0] for v in views)
    assert set(views[0].keys()) == REJECTED_FIELDS
    assert views[0]["accepted"] is False
    assert views[0]["code"] == "OWNER_QUOTA_EXHAUSTED"

    holder_view = manager.request_view("holder")
    assert_original_fields_intact(holder_view, state="PENDING")
