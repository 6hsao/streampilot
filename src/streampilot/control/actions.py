"""运行控制动作的具体执行。"""

from typing import Any

from streampilot.contracts import ApiError, ErrorCode
from streampilot.control.models import ControlAction, ControlRequest
from streampilot.control.session import SessionControl
from streampilot.orchestrator.state import SessionState

# 控制动作参数键
PARAM_PERSONA_VERSION_ID = "persona_version_id"
PARAM_TEMPLATE_ID = "template_id"


def apply_control(
    control: SessionControl, request: ControlRequest
) -> dict[str, Any]:
    """执行单个控制动作。

    Args:
        control: 场次运行时。
        request: 控制请求。

    Returns:
        写入审计的补充信息。

    Raises:
        ApiError: 参数缺失或当前状态不允许该动作。
    """
    action = request.action
    if action is ControlAction.PAUSE:
        control.paused = True
        if control.state.state is not SessionState.TAKEOVER:
            control.sink.silence()
        return {}
    if action is ControlAction.RESUME:
        control.paused = False
        if control.state.state is not SessionState.TAKEOVER:
            control.sink.set_muted(control.muted)
            control.sink.resume()
        return {}
    if action is ControlAction.MUTE:
        control.muted = True
        control.sink.set_muted(True)
        return {}
    if action is ControlAction.UNMUTE:
        control.muted = False
        control.sink.set_muted(False)
        return {}
    if action is ControlAction.SWITCH_PERSONA:
        version = request.params.get(PARAM_PERSONA_VERSION_ID, "").strip()
        if not version:
            raise ApiError(
                ErrorCode.INVALID_ARGUMENT,
                "persona_version_id is required",
            )
        control.sink.switch_persona(version)
        control.persona_version_id = version
        return {"persona_version_id": version}
    if action is ControlAction.SAY_TEMPLATE:
        return _say_template(control, request)
    control.sink.reconnect_stream()
    return {}


def _say_template(
    control: SessionControl, request: ControlRequest
) -> dict[str, Any]:
    """播放预设话术；静音时拒绝，避免操作者误判。"""
    template_id = request.params.get(PARAM_TEMPLATE_ID, "").strip()
    if not template_id:
        raise ApiError(ErrorCode.INVALID_ARGUMENT, "template_id is required")
    text = control.templates.get(template_id)
    if text is None:
        raise ApiError(
            ErrorCode.INVALID_ARGUMENT,
            "unknown template_id",
            details={"available": sorted(control.templates)},
        )
    if control.muted:
        raise ApiError(ErrorCode.STATE_CONFLICT, "audio is muted")
    control.sink.speak_template(template_id, text)
    return {"template_id": template_id}
