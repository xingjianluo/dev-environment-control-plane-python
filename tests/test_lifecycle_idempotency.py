"""生命周期、重复/并发提交幂等、非法状态迁移与拒绝原因区分。"""

from __future__ import annotations

import threading

import pytest

from controlplane.errors import (
    InvalidRequestError,
    InvalidTransitionError,
    LeaseExpiredError,
    OwnerMismatchError,
    RequestConflictError,
)


def test_allocate_returns_active_env_with_lease(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=60)
    result = cp.allocate(request_id="req-1", owner="team-a")

    assert result.replayed is False
    env = result.env
    assert env.owner == "team-a"
    assert env.request_id == "req-1"
    assert env.state.value == "ACTIVE"
    assert env.env_id.startswith("env-")
    assert env.lease_expires_at == pytest.approx(clock.now() + 60)


def test_duplicate_submit_returns_same_env_and_consistent_result(make_plane):
    cp = make_plane()
    first = cp.allocate(request_id="req-dup", owner="team-a")

    second = cp.allocate(request_id="req-dup", owner="team-a")

    assert second.replayed is True
    assert second.env.env_id == first.env.env_id
    # 无论提交多少次，环境只有一个
    assert len(cp.list_environments()) == 1


def test_concurrent_identical_requests_collapse_to_one(make_plane):
    cp = make_plane(global_limit=50)
    outcomes: list = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        outcomes.append(
            cp.allocate(request_id="req-concurrent", owner="team-a")
        )

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    env_ids = {o.env.env_id for o in outcomes}
    assert len(env_ids) == 1, f"并发同 request_id 产生了多个环境: {env_ids}"
    success = [o for o in outcomes if not o.replayed]
    replays = [o for o in outcomes if o.replayed]
    assert len(success) == 1
    assert len(replays) == 15
    assert len(cp.list_environments()) == 1


def test_same_request_id_with_different_intent_is_conflict(make_plane):
    cp = make_plane(default_lease_ttl_seconds=60)
    cp.allocate(request_id="req-c", owner="team-a", lease_ttl_seconds=60)

    with pytest.raises(RequestConflictError) as exc:
        cp.allocate(request_id="req-c", owner="team-a", lease_ttl_seconds=30)
    assert exc.value.error_code == "REQUEST_CONFLICT"

    with pytest.raises(RequestConflictError):
        cp.allocate(request_id="req-c", owner="team-b")


def test_invalid_arguments_rejected(make_plane):
    cp = make_plane(max_lease_ttl_seconds=600)
    with pytest.raises(InvalidRequestError) as exc:
        cp.allocate(request_id="  ", owner="team-a")
    assert exc.value.error_code == "INVALID_REQUEST"

    with pytest.raises(InvalidRequestError):
        cp.allocate(request_id="r", owner="team-a", lease_ttl_seconds=0)

    with pytest.raises(InvalidRequestError):
        cp.allocate(request_id="r", owner="team-a", lease_ttl_seconds=601)


def test_renew_requires_owner_and_active_lease(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=60)
    env = cp.allocate(request_id="req-renew", owner="team-a").env

    # 非持有方续约被拒
    with pytest.raises(OwnerMismatchError) as exc:
        cp.renew(request_id="req-other", env_id=env.env_id, owner="team-b")
    assert exc.value.error_code == "OWNER_MISMATCH"
    assert exc.value.expected_owner == "team-a"

    # 持有方续约成功，过期时间顺延
    clock.advance(30)
    renewed = cp.renew(request_id="req-renew", env_id=env.env_id, owner="team-a")
    assert renewed.renew_count == 1
    assert renewed.lease_expires_at == pytest.approx(clock.now() + 60)


def test_renew_after_expiry_is_rejected_with_distinct_reason(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=60)
    env = cp.allocate(request_id="req-exp", owner="team-a").env
    clock.advance(61)

    with pytest.raises(LeaseExpiredError) as exc:
        cp.renew(request_id="req-exp", env_id=env.env_id, owner="team-a")
    assert exc.value.error_code == "LEASE_EXPIRED"
    assert exc.value.env_id == env.env_id


def test_release_is_idempotent_and_renew_after_release_rejected(make_plane):
    cp = make_plane()
    env = cp.allocate(request_id="req-rel", owner="team-a").env

    first = cp.release(request_id="req-rel", env_id=env.env_id, owner="team-a")
    assert first.state.value == "RELEASED"
    # 重复释放幂等：同一终态视图，不报错
    again = cp.release(request_id="req-rel", env_id=env.env_id, owner="team-a")
    assert again.state.value == "RELEASED"
    assert again.released_at == first.released_at

    # 终态上续约属于非法迁移，拒绝原因与租约过期可区分
    with pytest.raises(InvalidTransitionError) as exc:
        cp.renew(request_id="req-rel", env_id=env.env_id, owner="team-a")
    assert exc.value.error_code == "INVALID_TRANSITION"
    assert exc.value.from_state == "RELEASED"
    assert exc.value.to_state == "ACTIVE"
