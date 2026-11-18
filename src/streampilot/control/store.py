"""控制面审计与接管记录的存储抽象。

首发提供进程内实现，字段与 docs/DATA-MODEL.md 的 audit_logs /
takeovers 对齐；接入 MySQL 后按同一协议替换即可。
"""

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from streampilot.control.models import AuditEntry


@dataclass
class TakeoverRecord:
    """一次人工接管的完整记录。

    Attributes:
        id: 记录标识。
        session_id: 场次标识。
        operator_id: 接管人标识。
        reason: 接管原因。
        started_at: 接管开始时间。
        ended_at: 释放时间，进行中为空。
        conclusion: 释放时的处置结论。
    """

    id: str
    session_id: str
    operator_id: str
    reason: str
    started_at: datetime
    ended_at: datetime | None = None
    conclusion: str | None = None


class AuditStore(Protocol):
    """审计记录存储协议。"""

    def append(self, entry: AuditEntry) -> None:
        """追加一条审计记录。"""
        ...

    def entries(self, target: str, limit: int = 50) -> list[AuditEntry]:
        """按对象查询审计记录，返回时间倒序的至多 limit 条。"""
        ...


class TakeoverStore(Protocol):
    """接管记录存储协议。"""

    def start(self, record: TakeoverRecord) -> None:
        """登记一次接管开始。"""
        ...

    def active(self, session_id: str) -> TakeoverRecord | None:
        """返回进行中的接管记录。"""
        ...

    def finish(
        self, session_id: str, conclusion: str, ended_at: datetime
    ) -> TakeoverRecord | None:
        """结束进行中的接管并写入处置结论。"""
        ...

    def history(self, session_id: str) -> list[TakeoverRecord]:
        """返回场次的全部接管记录。"""
        ...


class InMemoryAuditStore:
    """进程内审计存储，按写入顺序保留。"""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: AuditEntry) -> None:
        """追加审计记录。"""
        with self._lock:
            self._entries.append(entry)

    def entries(self, target: str, limit: int = 50) -> list[AuditEntry]:
        """返回指定对象的审计记录，时间倒序。"""
        with self._lock:
            matched = [item for item in self._entries if item.target == target]
        matched.reverse()
        return matched[:limit]


class InMemoryTakeoverStore:
    """进程内接管记录存储，同一场次同时只有一条进行中记录。"""

    def __init__(self) -> None:
        self._records: list[TakeoverRecord] = []
        self._active: dict[str, TakeoverRecord] = {}
        self._lock = threading.Lock()

    def start(self, record: TakeoverRecord) -> None:
        """登记接管开始。"""
        with self._lock:
            self._records.append(record)
            self._active[record.session_id] = record

    def active(self, session_id: str) -> TakeoverRecord | None:
        """返回进行中的接管记录。"""
        with self._lock:
            return self._active.get(session_id)

    def finish(
        self, session_id: str, conclusion: str, ended_at: datetime
    ) -> TakeoverRecord | None:
        """结束接管；无进行中记录时返回 None。"""
        with self._lock:
            record = self._active.pop(session_id, None)
            if record is None:
                return None
            record.ended_at = ended_at
            record.conclusion = conclusion
            return record

    def history(self, session_id: str) -> list[TakeoverRecord]:
        """返回场次全部接管记录。"""
        with self._lock:
            return [
                item for item in self._records if item.session_id == session_id
            ]
