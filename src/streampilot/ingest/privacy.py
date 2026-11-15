"""观众标识哈希化，保证原始 ID 不进入下游存储。"""

import hashlib

# 哈希值前缀，便于识别为脱敏字段
HASH_PREFIX = "h_"
# 截取的十六进制长度，平衡碰撞概率与存储
HASH_HEX_LENGTH = 32
# 盐值最小长度
MIN_SALT_LENGTH = 16


class HashSaltError(ValueError):
    """盐值缺失或过短。"""


def hash_identifier(raw_id: str, salt: bytes) -> str:
    """对观众原始标识做加盐哈希。

    Args:
        raw_id: 平台侧原始用户 ID。
        salt: 至少 16 字节的盐值，来自环境变量。

    Returns:
        以 ``h_`` 开头的不可逆哈希。

    Raises:
        HashSaltError: 盐值过短。
        ValueError: 原始标识为空。
    """
    if len(salt) < MIN_SALT_LENGTH:
        raise HashSaltError(
            f"viewer id salt must be at least {MIN_SALT_LENGTH} bytes"
        )
    if not raw_id:
        raise ValueError("raw identifier must not be empty")
    digest = hashlib.sha256(salt + b":" + raw_id.encode("utf-8"))
    return HASH_PREFIX + digest.hexdigest()[:HASH_HEX_LENGTH]
