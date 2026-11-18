"""标识生成工具。"""

import secrets
import time

# Crockford Base32，用于生成 ULID 风格标识
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_CHARS = 10


def _encode(value: int, length: int) -> str:
    """把整数编码为固定长度的 Base32 文本。

    Args:
        value: 待编码的非负整数。
        length: 输出字符数。

    Returns:
        高位在前的 Base32 文本。
    """
    chars: list[str] = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def new_id() -> str:
    """生成 26 位 ULID 风格标识。

    Returns:
        前 10 位为毫秒时间戳、后 16 位为随机串的标识。
    """
    stamp = _encode(int(time.time() * 1000), _TIME_CHARS)
    noise = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return stamp + noise
