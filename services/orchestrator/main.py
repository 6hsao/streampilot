"""编排服务：人工接管控制面，运行时事件循环由后续任务接入。

控制面的会话运行时由场次管理（LIV-06）登记；本地联调可设置
CONTROL_DEMO_STREAM_ID 自动登记一个内存场次。
"""

import os

from fastapi import FastAPI

from streampilot.control.app import create_app
from streampilot.control.service import ControlService, SessionControl
from streampilot.control.sink import LoggingControlSink
from streampilot.control.store import (
    InMemoryAuditStore,
    InMemoryTakeoverStore,
)
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.state import SessionStateMachine

# 本地联调用的预设话术
DEMO_TEMPLATES = {
    "welcome": "欢迎来到直播间，我是 AI 主播，有问题随时发在评论里。",
    "thanks": "感谢关注，喜欢的话点个赞再走呀。",
    "break": "主播正在确认信息，请稍等一下。",
}


def build_app() -> FastAPI:
    """按环境变量装配控制面应用。

    Returns:
        可直接由 uvicorn 启动的应用。
    """
    service = ControlService(
        audit=InMemoryAuditStore(),
        takeovers=InMemoryTakeoverStore(),
        org_id=os.environ.get("CONTROL_ORG_ID", "org_default"),
    )
    demo_stream = os.environ.get("CONTROL_DEMO_STREAM_ID", "").strip()
    if demo_stream:
        service.register(
            SessionControl(
                stream_id=demo_stream,
                state=SessionStateMachine(),
                queue=InMemorySpeechQueue(),
                sink=LoggingControlSink(),
                templates=DEMO_TEMPLATES,
            )
        )
    return create_app(service)


app = build_app()
