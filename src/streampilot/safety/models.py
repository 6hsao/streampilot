"""内容安全的规则模型与命中结果。"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """拒绝未知字段的规则模型基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(StrEnum):
    """规则严重级别。"""

    REVIEW = "review"
    BLOCK = "block"


class Action(StrEnum):
    """单条文本的处置结论。"""

    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


class WordRule(StrictModel):
    """词表规则。

    Attributes:
        term: 需要匹配的词或短语。
        category: 违规分类，便于解释与统计。
        severity: 命中后的处置级别。
    """

    term: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=32)
    severity: Severity


class PatternRule(StrictModel):
    """正则规则。

    Attributes:
        pattern: Python 正则表达式，按规范化后的文本匹配。
        category: 违规分类。
        severity: 命中后的处置级别。
    """

    pattern: str = Field(min_length=1, max_length=512)
    category: str = Field(min_length=1, max_length=32)
    severity: Severity


class RuleSet(StrictModel):
    """某一版本的全量规则。

    Attributes:
        version: 规则结构版本，当前固定为 "1"。
        words: 词表规则。
        patterns: 正则规则。
        whitelist: 白名单短语，匹配前会被屏蔽。
    """

    version: Literal["1"] = "1"
    words: tuple[WordRule, ...] = ()
    patterns: tuple[PatternRule, ...] = ()
    whitelist: tuple[str, ...] = ()


class Hit(StrictModel):
    """一次命中。

    Attributes:
        term: 命中的词或正则摘要。
        category: 违规分类。
        severity: 处置级别。
        start: 在规范化文本中的起始下标。
        end: 在规范化文本中的结束下标。
    """

    term: str
    category: str
    severity: Severity
    start: int
    end: int


class ScanResult(StrictModel):
    """扫描结果。

    Attributes:
        text_length: 原始文本长度。
        hits: 命中列表，互不重叠。
        action: 综合处置结论。
    """

    text_length: int
    hits: tuple[Hit, ...]
    action: Action
