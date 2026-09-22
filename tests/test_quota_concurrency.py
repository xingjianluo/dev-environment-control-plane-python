"""全局/归属方配额：并发争抢不超额、拒绝方稳定可区分、释放后配额可再分配。"""

from __future__ import annotations

import threading

import pytest

from controlplane.errors import QuotaExhaustedError


def test_global_quota_concurrent_contention_never_overcommits(make_plane):
    # 全局容量 4，32 个不同 request_id 并发争抢：恰好 4 个成功，其余可解释拒绝
    cp = make_plane(global_limit=4, default_lease_ttl_seconds=3600)
    n = 32
    granted: list[str] = []
    denied: list[QuotaExhaustedError] = []
    start = threading.Barrier(n)
    lock = threading.Lock()

    def worker(i: int):
        start.wait()
        try:
            result = cp.allocate(request_id=f"g-{i:02d}", owner="team-a")
            with lock:
                granted.append(result.env.env_id)
        except QuotaExhaustedError as exc:
            with lock:
                denied.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 4, f"成功数必须恰为容量上限，实际 {len(granted)}"
    assert len(denied) == n - 4
    assert len(set(granted)) == 4
    # 占用视图不超过容量
    assert cp.quota().global_used == 4
    for exc in denied:
        assert exc.error_code == "QUOTA_EXHAUSTED"
        assert exc.scope == "global"
        assert exc.used == 4 and exc.limit == 4


def test_owner_quota_is_independent_and_reason_distinguishable(make_plane):
    # 全局容量 4、双方各 1：归属方先耗尽时原因为 owner；
    # 再把全局放满后，新归属方拿到的才是 global 原因。
    cp = make_plane(
        global_limit=4,
        owner_limits={"team-a": 1, "team-b": 1},
        default_lease_ttl_seconds=3600,
    )
    a1 = cp.allocate(request_id="a1", owner="team-a")
    b1 = cp.allocate(request_id="b1", owner="team-b")
    assert a1.env.env_id != b1.env.env_id

    # team-a 撞到归属方配额（全局尚有空位），拒绝原因必须是 owner
    with pytest.raises(QuotaExhaustedError) as exc:
        cp.allocate(request_id="a2", owner="team-a")
    assert exc.value.scope == "owner"
    assert exc.value.used == 1 and exc.value.limit == 1
    assert exc.value.owner == "team-a"

    # 用不受归属方限制的 team-c 把全局放满
    cp.allocate(request_id="c1", owner="team-c")
    cp.allocate(request_id="c2", owner="team-c")

    # 全局容量耗尽时，新归属方拿到的是 global 原因
    with pytest.raises(QuotaExhaustedError) as exc:
        cp.allocate(request_id="d1", owner="team-d")
    assert exc.value.scope == "global"

    snap = cp.quota("team-a")
    assert snap.owner_used == 1 and snap.owner_limit == 1
    assert snap.global_used == 4 and snap.global_limit == 4


def test_denied_request_replays_stable_result(make_plane):
    cp = make_plane(global_limit=1, default_lease_ttl_seconds=3600)
    cp.allocate(request_id="win", owner="team-a")

    for _ in range(3):
        with pytest.raises(QuotaExhaustedError) as exc:
            cp.allocate(request_id="lose", owner="team-a")
        assert exc.value.scope == "global"
        assert exc.value.used == 1 and exc.value.limit == 1

    # 被拒的 request_id 不会占用任何资源
    assert len(cp.list_environments()) == 1


def test_released_quota_is_reallocatable(make_plane):
    cp = make_plane(global_limit=1, default_lease_ttl_seconds=3600)
    env = cp.allocate(request_id="first", owner="team-a").env
    assert cp.quota().global_used == 1

    with pytest.raises(QuotaExhaustedError):
        cp.allocate(request_id="second", owner="team-a")

    cp.release(request_id="first", env_id=env.env_id, owner="team-a")
    assert cp.quota().global_used == 0

    # 释放出的配额立即可再次分配（新 request_id）
    realloc = cp.allocate(request_id="third", owner="team-a")
    assert realloc.env.env_id != env.env_id
    assert cp.quota().global_used == 1


def test_owner_released_quota_does_not_affect_other_owners(make_plane):
    # 全局 4 留有余量，确保下面的拒绝只由归属方配额触发
    cp = make_plane(
        global_limit=4,
        owner_limits={"team-a": 2, "team-b": 2},
        default_lease_ttl_seconds=3600,
    )
    a = cp.allocate(request_id="a", owner="team-a")
    cp.allocate(request_id="b1", owner="team-b")
    cp.allocate(request_id="b2", owner="team-b")
    assert cp.quota("team-b").owner_used == 2
    with pytest.raises(QuotaExhaustedError) as exc:
        cp.allocate(request_id="b3", owner="team-b")
    assert exc.value.scope == "owner"

    cp.release(request_id="a", env_id=a.env.env_id, owner="team-a")
    # team-a 释放不会缓解 team-b 的归属方配额
    with pytest.raises(QuotaExhaustedError) as exc:
        cp.allocate(request_id="b4", owner="team-b")
    assert exc.value.scope == "owner"
    # team-a 自己的配额仍然可用
    assert cp.allocate(request_id="a2", owner="team-a").replayed is False
