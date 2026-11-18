"""场次管理 HTTP 接口。"""

from fastapi import APIRouter, Request

from streampilot.control.app import (
    operator_from_request,
    trace_id_from_request,
)
from streampilot.session.manager import SessionManager
from streampilot.session.views import (
    CreateStreamRequest,
    ResumeStreamRequest,
    SessionView,
    StartStreamRequest,
    StopStreamRequest,
)


def create_session_router(manager: SessionManager) -> APIRouter:
    """构建场次管理路由。

    Args:
        manager: 场次管理器。

    Returns:
        可挂载到控制面应用的 APIRouter。
    """
    router = APIRouter()

    @router.post("/v1/streams", status_code=201)
    async def create(
        payload: CreateStreamRequest, request: Request
    ) -> SessionView:
        """创建场次。"""
        request.state.trace_id = trace_id_from_request(request)
        return manager.create(payload, operator=operator_from_request(request))

    @router.post("/v1/streams/{stream_id}/start")
    async def start(
        stream_id: str,
        payload: StartStreamRequest,
        request: Request,
    ) -> SessionView:
        """开播。"""
        request.state.trace_id = trace_id_from_request(request)
        return manager.start(
            stream_id,
            request=payload,
            operator=operator_from_request(request),
        )

    @router.post("/v1/streams/{stream_id}/stop")
    async def stop(
        stream_id: str,
        payload: StopStreamRequest,
        request: Request,
    ) -> SessionView:
        """收播，支持异常原因码。"""
        request.state.trace_id = trace_id_from_request(request)
        return manager.stop(
            stream_id,
            request=payload,
            operator=operator_from_request(request),
        )

    @router.post("/v1/streams/{stream_id}/resume")
    async def resume(
        stream_id: str,
        payload: ResumeStreamRequest,
        request: Request,
    ) -> SessionView:
        """恢复开播。"""
        request.state.trace_id = trace_id_from_request(request)
        return manager.resume(
            stream_id,
            request=payload,
            operator=operator_from_request(request),
        )

    return router
