"""LLM 调用错误分类。

网关只依据 ``retryable`` 决定是否切换到下一个模型；错误信息不包含
请求体与密钥，避免把用户评论文本或凭据写入日志。
"""


class LLMError(Exception):
    """LLM 调用错误基类。

    Args:
        message: 错误描述，不得包含密钥或完整提示词。
        retryable: 是否允许切换模型重试。
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class RateLimited(LLMError):
    """上游限流或配额耗尽。"""

    def __init__(self, message: str = "upstream rate limited") -> None:
        super().__init__(message, retryable=True)


class UpstreamTimeout(LLMError):
    """上游超时，含首 token 超时。"""

    def __init__(self, message: str = "upstream timed out") -> None:
        super().__init__(message, retryable=True)


class UpstreamUnavailable(LLMError):
    """上游 5xx 或连接失败。"""

    def __init__(self, message: str = "upstream unavailable") -> None:
        super().__init__(message, retryable=True)


class AuthFailed(LLMError):
    """凭据无效，换模型也无济于事。"""

    def __init__(self, message: str = "upstream rejected credentials") -> None:
        super().__init__(message, retryable=False)


class BadRequest(LLMError):
    """请求本身非法，重试不会成功。"""

    def __init__(self, message: str = "upstream rejected request") -> None:
        super().__init__(message, retryable=False)


class LLMUnavailable(LLMError):
    """降级链路全部失败，调用方应改用预设话术或跳过该条。"""

    def __init__(self, attempts: int) -> None:
        super().__init__(
            f"all {attempts} model attempt(s) failed", retryable=False
        )
        self.attempts = attempts
