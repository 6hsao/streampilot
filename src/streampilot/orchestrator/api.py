"""编排运行时 HTTP 入口：本地评论注入。

真实评论由评论接入服务（ING-02）验签后转发；该入口用于联调、
压测与人工触发发言，统一写入场次审计链之外的运行时计数。
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from streampilot.contracts import ApiError, ErrorCode
from streampilot.contracts.models import StrictModel
from streampilot.control.app import (
    operator_from_request,
    trace_id_from_request,
)
from streampilot.orchestrator.bootstrap import RuntimeRegistry
from streampilot.orchestrator.queue import Priority
from streampilot.orchestrator.runtime import CommentOutcome
from streampilot.session.manager import SessionManager
from streampilot.session.models import SessionStatus

# HTTP 层优先级名称到队列优先级的映射
PRIORITIES: dict[str, Priority] = {
    "normal": Priority.NORMAL,
    "high": Priority.HIGH,
    "urgent": Priority.URGENT,
}


class CommentRequest(StrictModel):
    """评论注入请求。

    Attributes:
        text: 评论正文。
        item_id: 评论唯一标识，缺省由服务端生成。
        priority: 发言优先级。
    """

    text: str = Field(min_length=1, max_length=500)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    priority: Literal["normal", "high", "urgent"] = "normal"


class CommentResponse(StrictModel):
    """评论注入结果。

    Attributes:
        accepted: 是否已入队。
        outcome: 受理结果码。
    """

    accepted: bool
    outcome: str


def create_runtime_router(
    manager: SessionManager, runtimes: RuntimeRegistry
) -> APIRouter:
    """构造评论注入路由。

    Args:
        manager: 场次管理器，用于校验直播状态。
        runtimes: 运行时注册表。

    Returns:
        挂载到编排服务应用的路由。
    """
    router = APIRouter()

    @router.post(
        "/v1/streams/{stream_id}/comments",
        status_code=202,
        response_model=CommentResponse,
    )
    async def submit_comment(
        stream_id: str, payload: CommentRequest, request: Request
    ) -> CommentResponse:
        """注入一条观众评论。

        Args:
            stream_id: 场次标识。
            payload: 评论内容。
            request: 当前请求。

        Returns:
            受理结果。

        Raises:
            ApiError: 场次不存在、未开播或评论为空。
        """
        request.state.trace_id = trace_id_from_request(request)
        operator_from_request(request)
        view = manager.view(stream_id)
        if view.status is not SessionStatus.LIVE:
            raise ApiError(
                ErrorCode.STATE_CONFLICT,
                f"stream is {view.status.value}, not live",
            )
        runtime = runtimes.get(stream_id)
        if runtime is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                "runtime is not registered for this stream",
            )
        if not payload.text.strip():
            raise ApiError(
                ErrorCode.INVALID_ARGUMENT, "comment must not be blank"
            )
        outcome = runtime.submit_comment(
            payload.text,
            item_id=payload.item_id,
            priority=PRIORITIES[payload.priority],
        )
        return CommentResponse(
            accepted=outcome is CommentOutcome.ACCEPTED,
            outcome=outcome.value,
        )

    return router
