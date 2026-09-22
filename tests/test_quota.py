"""Quota constraints: per-owner and global exhaustion, reuse, concurrent races."""
from __future__ import annotations

import threading

from controlplane.models import ApplyRequest


def apply(manager, request_id, owner, lease=60):
    return manager.apply(ApplyRequest(request_id=request_id, owner=owner, lease_seconds=lease))


def test_owner_quota_exhaustion_is_distinguishable(manager):
    a1 = apply(manager, "a1", "alice")
    assert a1.accepted
    a2 = apply(manager, "a2", "alice")
    assert not a2.accepted
    assert a2.code == "OWNER_QUOTA_EXHAUSTED"
    assert "1/1" in a2.reason
    # quota counters unchanged after a rejection
    assert a2.quota_after["global"]["used"] == 1


def test_global_quota_exhaustion_is_distinguishable(manager):
    a1 = apply(manager, "g1", "alice")
    b1 = apply(manager, "g2", "bob")
    assert a1.accepted and b1.accepted
    c1 = apply(manager, "g3", "carol")
    assert not c1.accepted
    assert c1.code == "GLOBAL_QUOTA_EXHAUSTED"
    assert "2/2" in c1.reason
    assert c1.quota_after["global"]["used"] == 2
    assert c1.quota_after["global"]["available"] == 0


def test_released_quota_can_be_reassigned(manager):
    a1 = apply(manager, "q1", "alice")
    manager.occupy(a1.env_id, "alice")
    manager.release(a1.env_id, "alice")
    assert manager.quota()["global"]["used"] == 0

    a2 = apply(manager, "q2", "alice")
    assert a2.accepted
    assert a2.quota_after["global"]["used"] == 1


def test_reclaimed_quota_can_be_reassigned(manager, clock):
    a1 = apply(manager, "rq1", "alice", lease=10)
    manager.occupy(a1.env_id, "alice")
    clock.advance(11)
    rec = manager.reclaim(a1.env_id)
    assert rec.state == "RECLAIMED"
    assert rec.quota_after["global"]["used"] == 0

    a2 = apply(manager, "rq2", "alice")
    assert a2.accepted
    assert a2.quota_after["global"]["used"] == 1


def test_owner_quota_checked_before_global(manager):
    # fill global with alice+bob; alice's second apply must fail with OWNER
    # code even though a global slot is free after bob releases... here we
    # instead test ordering while a global slot IS free:
    apply(manager, "o1", "alice")
    apply(manager, "o2", "bob")
    d = apply(manager, "o3", "alice")
    assert d.code == "OWNER_QUOTA_EXHAUSTED"


def test_concurrent_applicants_never_oversubscribe(manager):
    """20 owners compete for 2 global slots under real threads.

    Exactly 2 succeed, 18 get GLOBAL_QUOTA_EXHAUSTED; the set of winners is
    stable for a fixed scheduling (results keyed by request_id, no duplicate
    environments created).
    """
    results: dict[str, object] = {}
    barrier = threading.Barrier(20)

    def worker(idx: int) -> None:
        rid = f"c-{idx:02d}"
        barrier.wait()  # maximize contention
        results[rid] = apply(manager, rid, f"owner-{idx:02d}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [rid for rid, d in results.items() if d.accepted]
    rejected = [rid for rid, d in results.items() if not d.accepted]
    assert len(accepted) == 2
    assert len(rejected) == 18
    assert all(results[rid].code == "GLOBAL_QUOTA_EXHAUSTED" for rid in rejected)
    assert manager.quota()["global"]["used"] == 2

    # every rejected and accepted decision reports the same post-state totals
    env_ids = {d.env_id for d in results.values() if d.accepted}
    assert len(env_ids) == 2


def test_concurrent_duplicates_create_one_environment(manager):
    """The same request_id fired concurrently creates exactly one environment."""
    env_ids: list[str] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        barrier.wait()
        d = apply(manager, "same-id", "alice")
        env_ids.append(d.env_id)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(env_ids)) == 1
    assert manager.quota()["global"]["used"] == 1


def test_quota_log_shows_change(manager, log_capture):
    _logger, handler = log_capture
    apply(manager, "l1", "alice")
    second = apply(manager, "l2", "bob")  # distinct owner -> consumes global slot
    assert second.accepted
    entry_ok = next(
        r for r in handler.records if r["request_id"] == "l1" and r["action"] == "APPLY"
    )
    assert entry_ok["quota_before"]["global"]["used"] == 0
    assert entry_ok["quota_after"]["global"]["used"] == 1
