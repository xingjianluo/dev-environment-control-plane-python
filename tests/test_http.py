"""End-to-end HTTP behaviour: status codes, distinguishable codes, idempotency."""
from __future__ import annotations


def _apply(client, request_id, owner, lease=60):
    return client.post(
        "/environments/apply",
        json={"request_id": request_id, "owner": owner, "lease_seconds": lease},
    )


def test_root_and_quota(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["quota"]["global"]["limit"] == 2
    assert client.get("/quota").json()["global"]["available"] == 2


def test_apply_then_full_http_flow(client):
    r = _apply(client, "h1", "alice", 30)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] and body["state"] == "PENDING"
    env_id = body["env_id"]

    assert client.post(f"/environments/{env_id}/occupy", json={"owner": "alice"}).status_code == 200
    assert client.post(f"/environments/{env_id}/renew", json={"owner": "alice"}).status_code == 200
    assert client.post(f"/environments/{env_id}/release", json={"owner": "alice"}).status_code == 200

    assert client.get(f"/environments/{env_id}").json()["state"] == "RELEASED"
    assert client.get("/requests/h1").json()["env_id"] == env_id


def test_duplicate_apply_http_returns_same_env(client):
    b1 = _apply(client, "dup", "alice").json()
    b2 = _apply(client, "dup", "alice").json()
    assert b1["env_id"] == b2["env_id"]
    assert b2["deduplicated"] is True


def test_quota_exhaustion_http_codes(client):
    _apply(client, "x1", "alice")
    _apply(client, "x2", "bob")

    owner = _apply(client, "x3", "alice")
    assert owner.status_code == 409
    assert owner.json()["code"] == "OWNER_QUOTA_EXHAUSTED"

    glob = _apply(client, "x4", "carol")
    assert glob.status_code == 409
    assert glob.json()["code"] == "GLOBAL_QUOTA_EXHAUSTED"


def test_owner_mismatch_and_unknown_http(client):
    _apply(client, "y1", "alice")
    env_id = client.get("/requests/y1").json()["env_id"]

    mismatch = client.post(f"/environments/{env_id}/release", json={"owner": "bob"})
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "OWNER_MISMATCH"

    missing = client.post("/environments/env-nope/release", json={"owner": "alice"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "UNKNOWN_REQUEST"

    invalid = client.post(
        "/environments/apply", json={"request_id": "z", "owner": "alice", "lease_seconds": -1}
    )
    assert invalid.status_code == 422 or invalid.json().get("code") == "INVALID_REQUEST"


def test_lease_not_expired_http(client, clock):
    _apply(client, "w1", "alice", 30)
    env_id = client.get("/requests/w1").json()["env_id"]
    client.post(f"/environments/{env_id}/occupy", json={"owner": "alice"})
    r = client.post(f"/environments/{env_id}/reclaim")
    assert r.status_code == 409
    assert r.json()["code"] == "LEASE_NOT_EXPIRED"


def test_reclaim_http(client, clock):
    _apply(client, "w2", "alice", 5)
    env_id = client.get("/requests/w2").json()["env_id"]
    client.post(f"/environments/{env_id}/occupy", json={"owner": "alice"})
    clock.advance(6)
    r = client.post(f"/environments/{env_id}/reclaim")
    assert r.status_code == 200 and r.json()["state"] == "RECLAIMED"
    # idempotent
    assert client.post(f"/environments/{env_id}/reclaim").json()["deduplicated"] is True


def test_list_filtering(client):
    _apply(client, "l1", "alice")
    _apply(client, "l2", "bob")
    alice = client.get("/environments", params={"owner": "alice"}).json()["environments"]
    assert len(alice) == 1 and alice[0]["owner"] == "alice"
    assert len(client.get("/environments").json()["environments"]) == 2
