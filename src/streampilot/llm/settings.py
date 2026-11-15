"""LLM 网关配置：只从环境变量读取，密钥不落代码与日志。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from streampilot.llm.provider import DASHSCOPE_BASE_URL

_MIN_KEY_LENGTH = 8


@dataclass(frozen=True)
class LLMSettings:
    """LLM 网关启动配置。

    Attributes:
        api_key: 百炼 API Key。
        base_url: 兼容模式基础地址。
        model_chain: 模型降级链。
        max_tokens: 单次输出上限。
        temperature: 采样温度。
    """

    api_key: str
    base_url: str = DASHSCOPE_BASE_URL
    model_chain: tuple[str, ...] = ("qwen-plus", "qwen-turbo")
    max_tokens: int = 200
    temperature: float = 0.7

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMSettings":
        """从环境变量读取配置。

        Args:
            env: 环境变量映射；缺省读取当前进程环境。

        Returns:
            配置对象。

        Raises:
            RuntimeError: 缺少或非法配置项。
        """
        values = os.environ if env is None else env
        api_key = values.get("DASHSCOPE_API_KEY", "").strip()
        if len(api_key) < _MIN_KEY_LENGTH:
            raise RuntimeError(
                "DASHSCOPE_API_KEY is required and must be at least "
                f"{_MIN_KEY_LENGTH} characters"
            )
        raw_chain = values.get("LLM_MODEL_CHAIN", "")
        chain = tuple(
            item.strip() for item in raw_chain.split(",") if item.strip()
        )
        return cls(
            api_key=api_key,
            base_url=values.get("DASHSCOPE_BASE_URL", DASHSCOPE_BASE_URL),
            model_chain=chain or ("qwen-plus", "qwen-turbo"),
            max_tokens=int(values.get("LLM_MAX_TOKENS", "200")),
            temperature=float(values.get("LLM_TEMPERATURE", "0.7")),
        )
