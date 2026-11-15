"""观众标识哈希化与去重窗口的单元测试。"""

import pytest

from streampilot.ingest.privacy import (
    HASH_PREFIX,
    HashSaltError,
    hash_identifier,
)

SALT = b"unit-test-salt-0123456789"


def test_hash_is_deterministic_and_prefixed() -> None:
    """同一标识与盐得到稳定哈希。"""
    first = hash_identifier("user-123", SALT)
    second = hash_identifier("user-123", SALT)

    assert first == second
    assert first.startswith(HASH_PREFIX)
    assert len(first) == len(HASH_PREFIX) + 32


def test_hash_does_not_leak_raw_id() -> None:
    """哈希结果不包含原始 ID。"""
    hashed = hash_identifier("user-123", SALT)

    assert "user-123" not in hashed


def test_different_salt_changes_hash() -> None:
    """换盐后同一 ID 的哈希不同。"""
    assert hash_identifier("user-123", SALT) != hash_identifier(
        "user-123", b"another-salt-0123456789"
    )


def test_short_salt_is_rejected() -> None:
    """盐值过短时拒绝哈希。"""
    with pytest.raises(HashSaltError):
        hash_identifier("user-123", b"short")


def test_empty_identifier_is_rejected() -> None:
    """空标识不允许哈希。"""
    with pytest.raises(ValueError, match="must not be empty"):
        hash_identifier("", SALT)
