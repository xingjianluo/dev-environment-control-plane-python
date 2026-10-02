"""HTTP layer exposing the control plane via FastAPI."""
from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .manager import ControlPlaneManager
from .models import ControlPlaneError, RejectCode
from .store import Store

# HTTP status used per rejection family; the stable code in the JSON body is
# what callers use to distinguish rejection reasons.
_ERROR_STATUS = {
    RejectCode.UNKNOWN_REQUEST: 404,
    RejectCode.OWNER_MISMATCH: 403,
    RejectCode.INVALID_REQUEST: 400,
    RejectCode.INVALID_STATE_TRANSITION: 409,
    RejectCode.LEASE_NOT_EXPIRED: 409,
    RejectCode.GLOBAL_QUOTA_EXHAUSTED: 409,
    RejectCode.OWNER_QUOTA_EXHAUSTED: 409,
}


class ApplyBody(BaseModel):
    request_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    lease_seconds: int = 60


class OwnerBody(BaseModel):
    owner: str = Field(min_length=1)


class RenewBody(BaseModel):
    owner: str = Field(min_length=1)
    extend_seconds: int | None = None


def _error_response(exc: ControlPlaneError) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS[exc.code],
        content={"accepted": False, "code": exc.code.value, "reason": exc.message},
    )


def create_app(
    state_path: str | None = None,
    global_limit: int | None = None,
    per_owner_limit: int | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    path = state_path or os.environ.get("CONTROLPLANE_STATE", "data/state.json")
    g_limit = global_limit if global_limit is not None else int(
        os.environ.get("CONTROLPLANE_GLOBAL_QUOTA", "2")
    )
    o_limit = per_owner_limit if per_owner_limit is not None else int(
        os.environ.get("CONTROLPLANE_OWNER_QUOTA", "1")
    )

    store = Store(path)
    manager = ControlPlaneManager(
        store, global_limit=g_limit, per_owner_limit=o_limit, clock=clock
    )

    app = FastAPI(title="Local Dev Environment Control Plane", version="0.1.0")
    app.state.manager = manager
    app.state.store = store

    @app.exception_handler(ControlPlaneError)
    async def _handle(_request: Any, exc: ControlPlaneError) -> JSONResponse:
        return _error_response(exc)

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"service": "dev-environment-control-plane", "quota": manager.quota()}

    @app.post("/environments/apply")
    def apply(body: ApplyBody) -> JSONResponse:
        from .models import ApplyRequest

        decision = manager.apply(
            ApplyRequest(
                request_id=body.request_id,
                owner=body.owner,
                lease_seconds=body.lease_seconds,
            )
        )
        status = 200 if decision.accepted else 409
        return JSONResponse(status_code=status, content=decision.to_dict())

    @app.post("/environments/{env_id}/occupy")
    def occupy(env_id: str, body: OwnerBody) -> dict[str, Any]:
        return manager.occupy(env_id, body.owner).to_dict()

    @app.post("/environments/{env_id}/renew")
    def renew(env_id: str, body: RenewBody) -> dict[str, Any]:
        return manager.renew(env_id, body.owner, body.extend_seconds).to_dict()

    @app.post("/environments/{env_id}/release")
    def release(env_id: str, body: OwnerBody) -> dict[str, Any]:
        return manager.release(env_id, body.owner).to_dict()

    @app.post("/environments/{env_id}/reclaim")
    def reclaim(env_id: str) -> dict[str, Any]:
        return manager.reclaim(env_id).to_dict()

    @app.get("/environments/{env_id}")
    def get_environment(env_id: str) -> dict[str, Any]:
        return manager._get_by_env(env_id).to_dict()

    @app.get("/environments")
    def list_environments(owner: str | None = None) -> dict[str, Any]:
        return {
            "environments": [r.to_dict() for r in manager.list_environments(owner)]
        }

    @app.get("/requests/{request_id}")
    def get_request(request_id: str) -> dict[str, Any]:
        """Query the immutable conclusion of one request_id.

        ACCEPTED conclusions also embed the live environment record (keeping
        the historical response shape); REJECTED conclusions carry the stable
        code/reason and decided_at. A request_id that was never submitted
        still raises UNKNOWN_REQUEST (404), distinct from a recorded rejection.
        """
        conclusion = manager.get_conclusion(request_id)
        body = conclusion.to_dict()
        if conclusion.env_id is not None:
            rec = manager.get_by_request(request_id)
            body.update(rec.to_dict())
        return body

    @app.get("/quota")
    def quota() -> dict[str, Any]:
        return manager.quota()

    return app


# Default app instance used by `fastapi dev main.py`.
app = create_app()
