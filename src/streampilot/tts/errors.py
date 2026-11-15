"""语音合成的错误分类。

``retryable`` 决定网关是否自动重试：鉴权、参数与协议错误重试无益，
限流、超时、上游不可用才可以重试。
"""


class TTSError(Exception):
    """语音合成错误基类。

    Args:
        message: 错误描述，会写入日志（不得包含密钥）。
    """

    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.__name__
        super().__init__(self.message)


class TTSAuthFailed(TTSError):
    """凭据无效或已过期。"""


class TTSBadRequest(TTSError):
    """请求参数非法或协议消息无法解析。"""


class TTSRateLimited(TTSError):
    """触发服务端限流或配额。"""

    retryable = True


class TTSUnavailable(TTSError):
    """上游不可用或连接中断。"""

    retryable = True


class TTSTimeout(TTSError):
    """等待音频分片超时。"""

    retryable = True


class TTSBusy(TTSError):
    """本地并发额度已满。"""

    retryable = True


_AUTH_CODES = frozenset({"InvalidApiKey", "Unauthorized", "AccessDenied"})
_RATE_CODES = frozenset(
    {"Throttling", "Throttling.RateQuota", "Throttling.AllocationQuota"}
)
_BAD_CODES = frozenset(
    {"InvalidParameter", "BadRequest", "InvalidParameterValue"}
)


def tts_error_from_code(
    code: str | None, message: str | None = None
) -> TTSError:
    """把上游错误码映射为已分类错误。

    Args:
        code: 上游 ``error_code``，可为空。
        message: 上游 ``error_message``。

    Returns:
        对应的错误实例；未知错误码按上游不可用处理。
    """
    normalized = (code or "").strip()
    base = normalized.split(".", 1)[0]
    detail = message or normalized or None
    if base in _AUTH_CODES:
        return TTSAuthFailed(detail)
    if base in _RATE_CODES:
        return TTSRateLimited(detail)
    if base in _BAD_CODES:
        return TTSBadRequest(detail)
    return TTSUnavailable(detail)
