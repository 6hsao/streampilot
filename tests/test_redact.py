"""日志脱敏的测试。"""

import pytest

from streampilot.config import MASK, carries_secret, redact


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "rtmp://push.example.com/live/abc123?txSecret=x",
            f"rtmp://push.example.com/live/{MASK}",
        ),
        ("Authorization: Bearer abc.def-123", f"Authorization: Bearer {MASK}"),
        ("key=LTAI5tAbCdEfGh1234", f"key={MASK}"),
        ("dashscope sk-abcdef123456", f"dashscope {MASK}"),
        (
            "https://x.test/hook?token=t-1&a=2",
            f"https://x.test/hook?token={MASK}&a=2",
        ),
        ("联系 13800138000", f"联系 {MASK}"),
    ],
)
def test_redact_masks_sensitive_values(raw: str, expected: str) -> None:
    """推流地址、令牌、密钥与手机号都被替换。"""
    assert redact(raw) == expected


def test_redact_keeps_plain_text() -> None:
    """普通文案不受影响，且脱敏结果稳定。"""
    text = "今天上新三款产品，欢迎来直播间。"

    assert redact(text) == text
    assert redact(redact(text)) == text


def test_redact_is_idempotent_on_masked_text() -> None:
    """已脱敏文本再次脱敏不变。"""
    once = redact("Bearer abc123")

    assert redact(once) == once


def test_carries_secret_detects_leaks() -> None:
    """可快速判断文本是否仍含凭据特征。"""
    assert carries_secret("rtmp://host/live/secret-key")
    assert not carries_secret("普通日志")
    assert not carries_secret("")
