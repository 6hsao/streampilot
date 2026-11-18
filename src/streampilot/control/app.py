"""控制面 HTTP 应用：接管、运行控制、状态与审计。

接口契约见 docs/API.md 第 4.2/4.3 节。控制台页面由同一应用托管，
方便单进程联调；页面只调用本应用暴露的 REST 接口。
"""

import uuid
from functools import lru_cache
from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from streampilot.contracts import ApiError, ErrorCode, error_body
from streampilot.control.models import (
    AuditPage,
    ControlRequest,
    ControlResult,
    ControlSnapshot,
    TakeoverRequest,
    TakeoverResult,
)
from streampilot.control.service import ControlService

OPERATOR_HEADER = "x-operator-id"
CONSOLE_PATH = "/console"


@lru_cache(maxsize=1)
def console_html() -> str:
    """读取控制台静态页面。

    Returns:
        打包在包内的 HTML 文本。
    """
    resource = files("streampilot.control").joinpath("console.html")
    return resource.read_text(encoding="utf-8")


def _trace_id(request: Request) -> str:
    """读取或生成追踪 ID。"""
    incoming = request.headers.get("x-trace-id", "").strip()
    return incoming or uuid.uuid4().hex


def _operator(request: Request) -> str:
    """读取操作人标识。

    Args:
        request: 当前请求。

    Returns:
        请求头中的操作人标识。

    Raises:
        ApiError: 请求头缺失或为空。
    """
    operator = request.headers.get(OPERATOR_HEADER, "").strip()
    if not operator:
        raise ApiError(
            ErrorCode.UNAUTHENTICATED,
            f"{OPERATOR_HEADER} header is required",
        )
    return operator


def create_app(
    service: ControlService, *, title: str = "StreamPilot Control Plane"
) -> FastAPI:
    """构建控制面应用。

    Args:
        service: 接管与控制服务。
        title: 应用标题。

    Returns:
        已注册路由与异常处理的 FastAPI 应用。
    """
    app = FastAPI(title=title, version="0.1.0")

    @app.exception_handler(ApiError)
    async def _handle_api_error(
        request: Request, exc: ApiError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None) or _trace_id(
            request
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
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_body(
                ErrorCode.INVALID_ARGUMENT,
                "invalid request payload",
                _trace_id(request),
            ),
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """存活探针，不依赖外部组件。"""
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """根路径跳转到控制台。"""
        return RedirectResponse(url=CONSOLE_PATH)

    @app.get(CONSOLE_PATH, response_class=HTMLResponse)
    async def console() -> HTMLResponse:
        """人工接管控制台页面。"""
        return HTMLResponse(console_html())

    @app.post("/v1/streams/{stream_id}/takeover")
    async def takeover(
        stream_id: str, payload: TakeoverRequest, request: Request
    ) -> TakeoverResult:
        """接管或释放场次。

        Args:
            stream_id: 场次标识。
            payload: 接管请求。
            request: 当前请求，用于读取操作人。

        Returns:
            接管结果。
        """
        request.state.trace_id = _trace_id(request)
        return service.takeover(
            stream_id,
            action=payload.action,
            operator_id=_operator(request),
            reason=payload.reason,
        )

    @app.post("/v1/streams/{stream_id}/control")
    async def control(
        stream_id: str, payload: ControlRequest, request: Request
    ) -> ControlResult:
        """执行运行控制动作。

        Args:
            stream_id: 场次标识。
            payload: 控制请求。
            request: 当前请求，用于读取操作人。

        Returns:
            控制结果。
        """
        request.state.trace_id = _trace_id(request)
        return service.control(
            stream_id,
            request=payload,
            operator_id=_operator(request),
        )

    @app.get("/v1/streams/{stream_id}/state")
    async def state(stream_id: str, request: Request) -> ControlSnapshot:
        """查询场次控制状态。

        Args:
            stream_id: 场次标识。
            request: 当前请求，用于读取操作人。

        Returns:
            控制面快照。
        """
        request.state.trace_id = _trace_id(request)
        _operator(request)
        return service.snapshot(stream_id)

    @app.get("/v1/streams/{stream_id}/audit")
    async def audit(
        stream_id: str, request: Request, limit: int = 50
    ) -> AuditPage:
        """查询场次操作审计。

        Args:
            stream_id: 场次标识。
            request: 当前请求，用于读取操作人。
            limit: 返回条数上限。

        Returns:
            时间倒序的审计记录。
        """
        request.state.trace_id = _trace_id(request)
        _operator(request)
        return AuditPage(entries=service.audit(stream_id, limit))

    return app
