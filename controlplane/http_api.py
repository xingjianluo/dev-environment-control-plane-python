"""HTTP 适配层：把 ControlPlane 暴露为 FastAPI 路由。

通过工厂 create_app() 注入控制面实例，测试可直接用 TestClient 访问。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ControlPlaneError
from .service import ControlPlane


class AllocateRequest(BaseModel):
    request_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    lease_ttl_seconds: float | None = Field(default=None, gt=0)


class RenewRequest(BaseModel):
    request_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    lease_ttl_seconds: float | None = Field(default=None, gt=0)


class ReleaseRequest(BaseModel):
    request_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)


def create_app(control_plane: ControlPlane | None = None) -> FastAPI:
    cp = control_plane or ControlPlane()
    app = FastAPI(
        title="本地开发环境控制面",
        version="0.1.0",
        description=(
            "用本地状态模拟环境申请、占用与释放；支持 request_id 幂等、"
            "全局/归属方配额与租约回收。"
        ),
    )

    def _error_response(exc: ControlPlaneError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.to_dict()})

    @app.exception_handler(ControlPlaneError)
    async def _handle_control_plane_error(_request, exc: ControlPlaneError):  # noqa: ANN001
        return _error_response(exc)

    @app.get("/")
    def read_root():
        return {"service": "dev-environment-control-plane", "status": "ok"}

    @app.post("/v1/environments", status_code=201)
    def allocate(body: AllocateRequest):
        result = cp.allocate(
            request_id=body.request_id,
            owner=body.owner,
            lease_ttl_seconds=body.lease_ttl_seconds,
        )
        return {
            "result": "ALLOCATED",
            "replayed": result.replayed,
            "environment": result.env.to_dict(),
        }

    @app.post("/v1/environments/{env_id}/renew")
    def renew(env_id: str, body: RenewRequest):
        env = cp.renew(
            request_id=body.request_id,
            env_id=env_id,
            owner=body.owner,
            lease_ttl_seconds=body.lease_ttl_seconds,
        )
        return {"result": "RENEWED", "environment": env.to_dict()}

    @app.post("/v1/environments/{env_id}/release")
    def release(env_id: str, body: ReleaseRequest):
        env = cp.release(
            request_id=body.request_id, env_id=env_id, owner=body.owner
        )
        return {"result": env.state.value, "environment": env.to_dict()}

    @app.post("/v1/reclaim-expired")
    def reclaim_expired():
        env_ids = cp.reclaim_expired()
        return {"result": "RECLAIMED", "count": len(env_ids), "env_ids": env_ids}

    @app.get("/v1/environments/{env_id}")
    def get_environment(env_id: str):
        return {"environment": cp.get_environment(env_id).to_dict()}

    @app.get("/v1/requests/{request_id}")
    def get_request(request_id: str):
        return {"request": cp.get_request(request_id).to_dict()}

    @app.get("/v1/environments")
    def list_environments(include_terminal: bool = False):
        envs = cp.list_environments(include_terminal=include_terminal)
        return {"count": len(envs), "environments": [e.to_dict() for e in envs]}

    @app.get("/v1/quota")
    def get_quota(owner: str | None = None):
        return {"quota": cp.quota(owner).to_dict()}

    return app
