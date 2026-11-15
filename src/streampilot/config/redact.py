"""日志与错误信息脱敏。

任何可能被写入日志的文本都应先经过 :func:`redact`：推流地址、
访问密钥、令牌、手机号一律不得出现在日志、告警与工单中。
"""

import re

# 推流地址中的 key 段及其查询参数（rtmp://host/live/<key>?xxx）
_RTMP_PATTERN = re.compile(r"(rtmp[s]?://[^\s/]+/[^\s/]+/)([^\s]+)")
# Bearer / AccessKey / API Key 等凭据
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+")
_ACCESS_KEY_PATTERN = re.compile(r"\bLTAI[A-Za-z0-9]{8,}\b")
_DASHSCOPE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
# 参数形式的密钥
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)\b(key|secret|token|sign|password|passwd)=([^&\s]+)"
)
# 中国大陆手机号
_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

MASK = "***"


def redact(text: str) -> str:
    """对文本做统一脱敏。

    Args:
        text: 原始文本。

    Returns:
        脱敏后的文本；多次调用结果稳定。
    """
    if not text:
        return text
    cleaned = _RTMP_PATTERN.sub(rf"\1{MASK}", text)
    cleaned = _BEARER_PATTERN.sub(rf"\1 {MASK}", cleaned)
    cleaned = _ACCESS_KEY_PATTERN.sub(MASK, cleaned)
    cleaned = _DASHSCOPE_PATTERN.sub(MASK, cleaned)
    cleaned = _QUERY_SECRET_PATTERN.sub(rf"\1={MASK}", cleaned)
    return _PHONE_PATTERN.sub(MASK, cleaned)


def carries_secret(text: str) -> bool:
    """判断文本是否仍包含疑似凭据。

    用于单测与启动自检，不用于替代脱敏。

    Args:
        text: 待检查文本。

    Returns:
        命中任一凭据特征返回 True。
    """
    return redact(text) != text
