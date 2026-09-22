"""HTTP 端到端：幂等、错误码区分、配额与回收接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from controlplane.http_api import create_app
from tests.conftest import make_cp


@pytest.fixture
def client(clock, tmp_state_path):
    cp = make_cp(
        clock,
        global_limit=2,
        owner_limits={"team-a": 1},
        default_lease_ttl_seconds=60,
        state_path=tmp_state_path,
    )
    return TestClient(create_app(cp))


def _allocate(client, request_id, owner, ttl=None):
    body = {"request_id": request_id, "owner": owner}
    if ttl is not None:
        body["lease_ttl_seconds"] = ttl
    return client.post("/v1/environments", json=body)


def test_allocate_and_replay_over_http(client):
    r1 = _allocate(client, "h-1", "team-a")
    assert r1.status_code == 201
    data = r1.json()
    assert data["replayed"] is False
    assert data["environment"]["state"] == "ACTIVE"

    r2 = _allocate(client, "h-1", "team-a")
    assert r2.status_code == 201
    assert r2.json()["replayed"] is True
    assert r2.json()["environment"]["env_id"] == data["environment"]["env_id"]


def test_conflict_and_quota_errors_are_distinguishable(client):
    _allocate(client, "h-2", "team-a")

    # 意图冲突
    r = _allocate(client, "h-2", "team-a", ttl=30)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "REQUEST_CONFLICT"

    # 归属方配额耗尽
    r = _allocate(client, "h-3", "team-a")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["error"] == "QUOTA_EXHAUSTED"
    assert body["scope"] == "owner"
    assert body["used"] == 1 and body["limit"] == 1


def test_owner_mismatch_and_renew_release_flow(client):
    env_id = _allocate(client, "h-4", "team-a").json()["environment"]["env_id"]

    r = client.post(
        f"/v1/environments/{env_id}/renew",
        json={"request_id": "x", "owner": "team-b"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "OWNER_MISMATCH"

    r = client.post(
        f"/v1/environments/{env_id}/renew",
        json={"request_id": "h-4", "owner": "team-a"},
    )
    assert r.status_code == 200
    assert r.json()["environment"]["renew_count"] == 1

    r = client.post(
        f"/v1/environments/{env_id}/release",
        json={"request_id": "h-4", "owner": "team-a"},
    )
    assert r.status_code == 200 and r.json()["result"] == "RELEASED"


def test_quota_and_list_endpoints(client):
    _allocate(client, "h-5", "team-a")
    _allocate(client, "h-6", "team-b")

    q = client.get("/v1/quota").json()["quota"]
    assert q["global"] == {"used": 2, "limit": 2}

    envs = client.get("/v1/environments").json()
    assert envs["count"] == 2

    req = client.get("/v1/requests/h-5").json()["request"]
    assert req["status"] == "SUCCESS" and req["env_id"]

    assert client.get("/v1/requests/missing").status_code == 404


def test_reclaim_endpoint_and_quota_reopen(client, clock):
    _allocate(client, "h-7", "team-a")
    _allocate(client, "h-8", "team-b")
    clock.advance(61)

    r = client.post("/v1/reclaim-expired")
    assert r.status_code == 200
    assert r.json()["count"] == 2
    # 回收幂等
    assert client.post("/v1/reclaim-expired").json()["count"] == 0

    # 配额重新开放
    again = _allocate(client, "h-9", "team-a")
    assert again.status_code == 201
    assert again.json()["replayed"] is False
