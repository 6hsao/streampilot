"""评论接入网关 HTTP 应用。

接口契约见 docs/API.md 第 3 节：验签 → 解析 → 按房间限流 → 幂等投递。
"""

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from streampilot.contracts import (
    ApiError,
    ErrorCode,
    IngestEvent,
    IngestResponse,
    error_body,
)
from streampilot.ingest.signing import (
    NonceStore,
    SignatureError,
    verify_request,
)
from streampilot.ingest.stores import (
    EventPublisher,
    IdempotencyStore,
    RateLimiter,
)

# 回调密钥最小长度，避免使用弱口令
MIN_SECRET_LENGTH = 16
# 幂等键默认保留时长，单位秒
DEFAULT_IDEMPOTENCY_TTL = 3600
# 默认每秒每房间限流阈值
DEFAULT_RATE_LIMIT = 50
INGEST_PATH = "/v1/ingest/douyin/events"


@dataclass(frozen=True)
class IngestSettings:
    """接入网关配置。

    Attributes:
        signing_secret: 平台回调签名密钥。
        rate_limit_per_second: 单房间每秒事件上限。
        idempotency_ttl_seconds: 幂等键保留时长。
    """

    signing_secret: bytes
    rate_limit_per_second: int = DEFAULT_RATE_LIMIT
    idempotency_ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "IngestSettings":
        """从环境变量读取配置。

        Args:
            env: 环境变量映射，默认读取进程环境。

        Returns:
            配置对象。

        Raises:
            RuntimeError: 密钥缺失或过短。
        """
        source = os.environ if env is None else env
        secret = source.get("INGEST_CALLBACK_SECRET", "")
        if len(secret) < MIN_SECRET_LENGTH:
            raise RuntimeError(
                "INGEST_CALLBACK_SECRET must be set with at least "
                f"{MIN_SECRET_LENGTH} characters"
            )
        limit = int(
            source.get("INGEST_RATE_LIMIT_PER_SECOND", DEFAULT_RATE_LIMIT)
        )
        ttl = int(
            source.get(
                "INGEST_IDEMPOTENCY_TTL_SECONDS", DEFAULT_IDEMPOTENCY_TTL
            )
        )
        return cls(
            signing_secret=secret.encode("utf-8"),
            rate_limit_per_second=limit,
            idempotency_ttl_seconds=ttl,
        )


def _trace_id(request: Request) -> str:
    """读取或生成追踪 ID。

    Args:
        request: 当前请求。

    Returns:
        调用方传入的追踪 ID，缺失时生成新的十六进制 ID。
    """
    incoming = request.headers.get("x-trace-id", "").strip()
    return incoming or uuid.uuid4().hex


def create_app(
    settings: IngestSettings,
    *,
    nonce_store: NonceStore,
    idempotency: IdempotencyStore,
    rate_limiter: RateLimiter,
    publisher: EventPublisher,
) -> FastAPI:
    """构建接入网关应用。

    Args:
        settings: 网关配置。
        nonce_store: 防重放存储。
        idempotency: 事件幂等存储。
        rate_limiter: 房间级限流器。
        publisher: 内部事件投递器。

    Returns:
        已注册路由与异常处理的 FastAPI 应用。
    """
    app = FastAPI(title="StreamPilot Ingest Gateway", version="0.1.0")

    @app.exception_handler(ApiError)
    async def _handle_api_error(
        request: Request, exc: ApiError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None) or _trace_id(
            request
        )
        headers = (
            {"Retry-After": "1"}
            if exc.code is ErrorCode.RATE_LIMITED
            else None
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=error_body(
                exc.code,
                exc.message,
                trace_id,
                retryable=exc.retryable,
                details=exc.details,
            ),
            headers=headers,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """存活探针，不依赖外部组件。"""
        return {"status": "ok"}

    @app.post(INGEST_PATH, status_code=202)
    async def ingest(request: Request) -> IngestResponse:
        """接收并异步投递平台评论事件。

        Args:
            request: 携带原始请求体的 HTTP 请求。

        Returns:
            受理结果。

        Raises:
            ApiError: 验签失败、参数非法或触发限流。
        """
        trace_id = _trace_id(request)
        request.state.trace_id = trace_id
        body = await request.body()
        try:
            verify_request(
                settings.signing_secret,
                request.method,
                request.url.path,
                request.headers,
                body,
                nonce_store,
            )
        except SignatureError as exc:
            raise ApiError(
                ErrorCode.UNAUTHENTICATED,
                "invalid signature",
            ) from exc

        try:
            event = IngestEvent.model_validate_json(body)
        except ValidationError as exc:
            raise ApiError(
                ErrorCode.INVALID_ARGUMENT,
                "invalid event payload",
            ) from exc

        if not rate_limiter.allow(f"room:{event.room_ref}"):
            raise ApiError(
                ErrorCode.RATE_LIMITED,
                "room event rate exceeded",
                retryable=True,
            )

        key = f"event:{event.event_id}"
        if idempotency.claim(key, settings.idempotency_ttl_seconds):
            publisher.publish(event, trace_id)

        return IngestResponse(event_id=event.event_id, trace_id=trace_id)

    return app
