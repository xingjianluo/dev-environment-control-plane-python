"""进程重启后的状态/视图一致性，以及历史记录缺字段的兼容读取。"""

from __future__ import annotations

import json

import pytest

from controlplane.errors import LeaseExpiredError
from controlplane.models import Environment, EnvironmentState, RequestRecord
from tests.conftest import FakeClock, make_cp


def test_restart_preserves_active_env_ownership_and_views(tmp_state_path):
    clk = FakeClock(1000.0)
    cp = make_cp(
        clk,
        global_limit=3,
        owner_limits={"team-a": 2},
        default_lease_ttl_seconds=600,
        state_path=tmp_state_path,
    )
    env = cp.allocate(request_id="persist-1", owner="team-a").env
    cp.renew(request_id="persist-1", env_id=env.env_id, owner="team-a")

    # 模拟进程重启：新时钟、新实例、同一状态文件（时间未越过新到期点）
    clk2 = FakeClock(1100.0)
    cp2 = make_cp(
        clk2,
        global_limit=3,
        owner_limits={"team-a": 2},
        default_lease_ttl_seconds=600,
        state_path=tmp_state_path,
    )

    restored = cp2.get_environment(env.env_id)
    assert restored.state is EnvironmentState.ACTIVE
    assert restored.owner == "team-a"
    assert restored.request_id == "persist-1"
    assert restored.renew_count == 1
    # 对外视图一致
    assert cp2.quota().global_used == 1
    assert cp2.quota("team-a").owner_used == 1

    # 重启后幂等结论仍然成立
    replay = cp2.allocate(request_id="persist-1", owner="team-a")
    assert replay.replayed is True
    assert replay.env.env_id == env.env_id


def test_restart_reclaims_expired_leases_and_reopens_quota(tmp_state_path):
    clk = FakeClock(1000.0)
    cp = make_cp(
        clk, global_limit=1, default_lease_ttl_seconds=60, state_path=tmp_state_path
    )
    env = cp.allocate(request_id="persist-2", owner="team-a").env

    # 时间越过到期点后重启：加载时即回收，对外视图立即一致
    clk2 = FakeClock(1000.0 + 61)
    cp2 = make_cp(
        clk2, global_limit=1, default_lease_ttl_seconds=60, state_path=tmp_state_path
    )
    assert cp2.get_environment(env.env_id).state.value == "EXPIRED"
    assert cp2.quota().global_used == 0
    # 归还的配额可再次分配
    fresh = cp2.allocate(request_id="persist-3", owner="team-a")
    assert fresh.env.state.value == "ACTIVE"
    with pytest.raises(LeaseExpiredError):
        cp2.renew(request_id="persist-2", env_id=env.env_id, owner="team-a")


def test_restart_denied_request_keeps_stable_conclusion(tmp_state_path):
    clk = FakeClock(1000.0)
    cp = make_cp(
        clk, global_limit=1, default_lease_ttl_seconds=3600, state_path=tmp_state_path
    )
    cp.allocate(request_id="ok", owner="team-a")
    with pytest.raises(Exception):
        cp.allocate(request_id="denied", owner="team-a")

    cp2 = make_cp(
        FakeClock(1000.0),
        global_limit=1,
        default_lease_ttl_seconds=3600,
        state_path=tmp_state_path,
    )
    record = cp2.get_request("denied")
    assert record.status == "ERROR"
    assert record.error_code == "QUOTA_EXHAUSTED"
    # 重启后重复提交仍回放同样的拒绝
    from controlplane.errors import QuotaExhaustedError

    with pytest.raises(QuotaExhaustedError) as exc:
        cp2.allocate(request_id="denied", owner="team-a")
    assert exc.value.scope == "global"
    assert exc.value.used == 1 and exc.value.limit == 1
    # 拒绝回放没有产生新环境
    assert len(cp2.list_environments()) == 1


def test_legacy_environment_record_without_new_fields_reads_with_defaults():
    # 模拟历史版本快照：只有身份字段，缺少 state/lease/renew_count 等新增字段
    legacy_env = Environment.from_dict(
        {"env_id": "legacy-1", "owner": "team-old", "request_id": "old-1"}
    )
    assert legacy_env.state is EnvironmentState.ACTIVE
    assert legacy_env.lease_ttl_seconds == 60.0
    assert legacy_env.lease_expires_at is None
    assert legacy_env.renew_count == 0
    assert legacy_env.released_at is None and legacy_env.expired_at is None

    legacy_req = RequestRecord.from_dict(
        {"request_id": "old-1", "owner": "team-old"}
    )
    assert legacy_req.intent == "ALLOCATE"
    assert legacy_req.status == "PENDING"
    assert legacy_req.lease_ttl_seconds == 60.0


def test_legacy_state_file_missing_fields_loads_without_failure(tmp_state_path):
    # 手工写一份缺 config / 缺新增字段的旧快照
    payload = {
        "environments": [
            {"env_id": "legacy-2", "owner": "team-old", "request_id": "old-2"}
        ],
        "requests": [
            {"request_id": "old-2", "owner": "team-old"}
        ],
    }
    tmp_state_path.write_text(json.dumps(payload), encoding="utf-8")

    cp = make_cp(
        FakeClock(1000.0),
        global_limit=5,
        default_lease_ttl_seconds=60,
        state_path=tmp_state_path,
    )
    env = cp.get_environment("legacy-2")
    assert env.state is EnvironmentState.ACTIVE and env.owner == "team-old"

    # 历史环境无 lease_expires_at：不会被回收（视为仍在占用），但也不允许续约
    # （缺到期点无法判定租约有效）——这里验证它仍占配额，视图可解释
    assert cp.quota("team-old").owner_used == 1
