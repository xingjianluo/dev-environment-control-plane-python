"""控制面错误类型。

每一种拒绝原因对应一个独立的错误码（error_code），调用方据此区分：
配额不足、非法状态迁移、占用方不匹配、租约过期、请求冲突等。
"""


class ControlPlaneError(Exception):
    """所有控制面业务错误的基类。"""

    error_code = "CONTROL_PLANE_ERROR"
    http_status = 400

    def __init__(self, message: str = "", *, request_id: str | None = None):
        self.request_id = request_id
        detail = message or self.error_code
        super().__init__(detail)

    def to_dict(self) -> dict:
        return {
            "error": self.error_code,
            "message": str(self),
            "request_id": self.request_id,
        }


class InvalidRequestError(ControlPlaneError):
    """请求参数非法（如 request_id / owner 为空）。"""

    error_code = "INVALID_REQUEST"
    http_status = 422


class RequestConflictError(ControlPlaneError):
    """同一 request_id 被用于与首次请求不一致的意图。

    重复提交只有在“同一意图”时才被幂等折叠；意图不一致则拒绝。
    """

    error_code = "REQUEST_CONFLICT"
    http_status = 409


class QuotaExhaustedError(ControlPlaneError):
    """容量/配额不足。通过 scope 区分是全局配额还是归属方配额。"""

    error_code = "QUOTA_EXHAUSTED"
    http_status = 409

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        scope: str = "global",
        used: int = 0,
        limit: int = 0,
        owner: str | None = None,
    ):
        super().__init__(message, request_id=request_id)
        self.scope = scope  # "global" 或 "owner"
        self.used = used
        self.limit = limit
        self.owner = owner

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(scope=self.scope, used=self.used, limit=self.limit, owner=self.owner)
        return d


class InvalidTransitionError(ControlPlaneError):
    """环境当前状态不允许目标迁移。"""

    error_code = "INVALID_TRANSITION"
    http_status = 409

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        env_id: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
    ):
        super().__init__(message, request_id=request_id)
        self.env_id = env_id
        self.from_state = from_state
        self.to_state = to_state

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            env_id=self.env_id,
            from_state=self.from_state,
            to_state=self.to_state,
        )
        return d


class OwnerMismatchError(ControlPlaneError):
    """操作方不是环境的持有方（或 request_id 归属不匹配）。"""

    error_code = "OWNER_MISMATCH"
    http_status = 403

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        env_id: str | None = None,
        expected_owner: str | None = None,
        actual_owner: str | None = None,
    ):
        super().__init__(message, request_id=request_id)
        self.env_id = env_id
        self.expected_owner = expected_owner
        self.actual_owner = actual_owner

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            env_id=self.env_id,
            expected_owner=self.expected_owner,
            actual_owner=self.actual_owner,
        )
        return d


class LeaseExpiredError(ControlPlaneError):
    """租约已过期，持有方必须先续约（或重新申请）。"""

    error_code = "LEASE_EXPIRED"
    http_status = 409

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        env_id: str | None = None,
        lease_expires_at: float | None = None,
        now: float | None = None,
    ):
        super().__init__(message, request_id=request_id)
        self.env_id = env_id
        self.lease_expires_at = lease_expires_at
        self.now = now

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            env_id=self.env_id,
            lease_expires_at=self.lease_expires_at,
            now=self.now,
        )
        return d


class EnvironmentNotFoundError(ControlPlaneError):
    """环境或请求记录不存在。"""

    error_code = "ENVIRONMENT_NOT_FOUND"
    http_status = 404

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str | None = None,
        env_id: str | None = None,
    ):
        super().__init__(message or "", request_id=request_id)
        self.env_id = env_id

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(env_id=self.env_id)
        return d
