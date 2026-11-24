"""统一的业务错误类型与响应体构造。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有可预期业务错误的基类。

    details 中的键会被展开到错误响应体顶层，方便调用方直接读取
    例如 ``shortages`` / ``violations``。
    """

    status_code: int = 400
    code: str = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        return error_body(
            code=self.code,
            message=self.message,
            status_code=self.status_code,
            details=self.details,
        )


class CsvParseError(AppError):
    status_code = 422
    code = "CSV_PARSE_ERROR"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class VolumeContentMismatch(ConflictError):
    code = "VOLUME_CONTENT_MISMATCH"


class InsufficientCopies(ConflictError):
    code = "INSUFFICIENT_COPIES"


class ShardCoverageConflict(ConflictError):
    code = "SHARD_COVERAGE_CONFLICT"


def error_body(
    *,
    code: str,
    message: str,
    status_code: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造对各种调用习惯都友好的错误 JSON。

    - ``detail`` 遵循 FastAPI 习惯，是结构化对象；
    - ``error`` 是同样内容的对象；
    - details（如 shortages / violations）同时展开到顶层。
    """

    details = details or {}
    structured_detail: dict[str, Any] = {"code": code, "message": message}
    if status_code is not None:
        structured_detail["status_code"] = status_code
    structured_detail.update(details)

    body: dict[str, Any] = {
        "success": False,
        "ok": False,
        "code": code,
        "status": code.lower(),
        "message": message,
        "error": {"code": code, "message": message, **details},
        "detail": structured_detail,
    }
    body.update(details)
    return body
