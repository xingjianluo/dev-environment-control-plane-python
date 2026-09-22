"""租约语义：续约顺延、超时回收归还配额、回收/释放幂等、回收后可再分配。"""

from __future__ import annotations

import pytest

from controlplane.errors import LeaseExpiredError


def test_renew_extends_expiry_within_active_lease(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=60)
    env = cp.allocate(request_id="lease-1", owner="team-a").env
    original_expiry = env.lease_expires_at

    clock.advance(50)
    renewed = cp.renew(request_id="lease-1", env_id=env.env_id, owner="team-a")
    assert renewed.lease_expires_at == pytest.approx(clock.now() + 60)
    assert renewed.lease_expires_at > original_expiry
    assert renewed.renew_count == 1

    # 推进到原过期时间之后、但在新到期时间之前，环境仍 ACTIVE
    clock.advance(15)  # now = start + 65，旧租约已过期、新租约未过期
    assert cp.get_environment(env.env_id).state.value == "ACTIVE"


def test_reclaim_transitions_expired_and_returns_quota(make_plane, clock):
    cp = make_plane(global_limit=2, default_lease_ttl_seconds=60)
    e1 = cp.allocate(request_id="l1", owner="team-a").env
    e2 = cp.allocate(request_id="l2", owner="team-a").env
    assert cp.quota().global_used == 2

    clock.advance(61)
    swept = cp.reclaim_expired()
    assert sorted(swept) == sorted([e1.env_id, e2.env_id])

    for env_id in swept:
        env = cp.get_environment(env_id)
        assert env.state.value == "EXPIRED"
        assert env.expired_at == pytest.approx(clock.now())
    assert cp.quota().global_used == 0
    assert cp.quota("team-a").owner_used == 0
    # ACTIVE 列表不再包含已回收环境
    assert cp.list_environments() == []
    terminal = cp.list_environments(include_terminal=True)
    assert {e.env_id for e in terminal} == {e1.env_id, e2.env_id}


def test_reclaim_is_idempotent(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=10)
    env = cp.allocate(request_id="l3", owner="team-a").env
    clock.advance(11)

    assert cp.reclaim_expired() == [env.env_id]
    # 再次回收：无新变化，不重复计数
    assert cp.reclaim_expired() == []
    assert cp.get_environment(env.env_id).state.value == "EXPIRED"


def test_release_after_expiry_is_idempotent(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=10)
    env = cp.allocate(request_id="l4", owner="team-a").env
    clock.advance(11)
    cp.reclaim_expired()

    # 已回收（EXPIRED 终态）后再释放：幂等返回 EXPIRED 视图，配额不变
    view = cp.release(request_id="l4", env_id=env.env_id, owner="team-a")
    assert view.state.value == "EXPIRED"
    assert view.released_at is None
    assert cp.quota().global_used == 0


def test_expired_env_cannot_be_renewed_or_operated(make_plane, clock):
    cp = make_plane(default_lease_ttl_seconds=10)
    env = cp.allocate(request_id="l5", owner="team-a").env
    clock.advance(11)
    cp.reclaim_expired()

    with pytest.raises(LeaseExpiredError):
        cp.renew(request_id="l5", env_id=env.env_id, owner="team-a")


def test_reclaimed_quota_is_reallocatable(make_plane, clock):
    cp = make_plane(global_limit=1, default_lease_ttl_seconds=10)
    old = cp.allocate(request_id="l6", owner="team-a").env
    clock.advance(11)
    cp.reclaim_expired()
    assert cp.quota().global_used == 0

    fresh = cp.allocate(request_id="l7", owner="team-a")
    assert not fresh.replayed
    assert fresh.env.env_id != old.env_id
    assert fresh.env.state.value == "ACTIVE"
    assert cp.quota().global_used == 1


def test_expiry_boundary_is_deterministic(make_plane, clock):
    # now == expires_at 视为到期（<=），判定确定无歧义
    cp = make_plane(default_lease_ttl_seconds=10)
    cp.allocate(request_id="l8", owner="team-a")
    clock.advance(10)
    assert cp.reclaim_expired() != []


def test_implicit_sweep_before_quota_decisions(make_plane, clock):
    # 不显式回收时，新申请/查询也会先回收过期租约，结论随时钟确定
    cp = make_plane(global_limit=1, default_lease_ttl_seconds=10)
    cp.allocate(request_id="l9", owner="team-a")
    clock.advance(11)
    fresh = cp.allocate(request_id="l10", owner="team-a")
    assert fresh.env.state.value == "ACTIVE"
    assert cp.quota().global_used == 1
