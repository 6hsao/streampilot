"""基于 DFA 的敏感词匹配内核。

规则来源见 docs/PLAN.md 的内容安全分层；单条匹配目标 < 5ms。
"""

import re
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from streampilot.safety.models import (
    Action,
    Hit,
    PatternRule,
    RuleSet,
    ScanResult,
    Severity,
    WordRule,
)

# 全角字符区间与半角偏移
_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0
_FULLWIDTH_SPACE = "\u3000"
# 零宽字符等不可见字符
_INVISIBLE = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
# 白名单屏蔽占位符，保证下标不变
_MASK = "\u3000"
# Trie 终止节点存放规则列表的键
_RULES_KEY = "\x00"
# 正则匹配摘要的最大展示长度
_MAX_PATTERN_LABEL = 32


def normalize(text: str) -> str:
    """归一化文本，消除大小写、全角与不可见字符干扰。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本；长度可能因去空白而变化。
    """
    chars: list[str] = []
    for char in text:
        code = ord(char)
        if _FULLWIDTH_START <= code <= _FULLWIDTH_END:
            char = chr(code - _FULLWIDTH_OFFSET)
        if char == _FULLWIDTH_SPACE or char.isspace():
            continue
        if char in _INVISIBLE:
            continue
        chars.append(char.casefold())
    return "".join(chars)


def _build_trie(words: Iterable[WordRule]) -> dict[str, Any]:
    """按归一化后的词构建 Trie。

    Args:
        words: 词表规则。

    Returns:
        以字符为键、终止节点带规则列表的嵌套字典。
    """
    root: dict[str, Any] = {}
    for rule in words:
        node = root
        for char in normalize(rule.term):
            node = node.setdefault(char, {})
        node.setdefault(_RULES_KEY, []).append(rule)
    return root


def _mask_whitelist(text: str, whitelist: Sequence[str]) -> str:
    """把白名单短语替换为占位符。

    Args:
        text: 归一化后的文本。
        whitelist: 已归一化的白名单短语。

    Returns:
        等长且下标稳定的屏蔽后文本。
    """
    if not whitelist:
        return text
    masked = text
    for phrase in whitelist:
        if not phrase:
            continue
        cursor = masked.find(phrase)
        while cursor != -1:
            masked = (
                masked[:cursor]
                + _MASK * len(phrase)
                + masked[cursor + len(phrase) :]
            )
            cursor = masked.find(phrase, cursor + len(phrase))
    return masked


def _scan_trie(
    root: dict[str, Any], text: str
) -> list[tuple[int, int, WordRule]]:
    """扫描 Trie，返回所有候选命中。

    Args:
        root: Trie 根节点。
        text: 已屏蔽白名单的归一化文本。

    Returns:
        (起始下标, 结束下标, 规则) 列表。
    """
    matches: list[tuple[int, int, WordRule]] = []
    length = len(text)
    for start in range(length):
        node = root
        index = start
        while index < length and text[index] in node:
            node = node[text[index]]
            index += 1
            for rule in node.get(_RULES_KEY, ()):
                matches.append((start, index, rule))
    return matches


def _dedupe_overlaps(
    matches: list[tuple[int, int, WordRule]],
) -> list[tuple[int, int, WordRule]]:
    """按起始位置优先、长度次优保留互不重叠的命中。

    Args:
        matches: 候选命中。

    Returns:
        去重后的命中列表。
    """
    ordered = sorted(matches, key=lambda item: (item[0], -item[1]))
    selected: list[tuple[int, int, WordRule]] = []
    last_end = -1
    for match in ordered:
        if match[0] >= last_end:
            selected.append(match)
            last_end = match[1]
    return selected


def _pattern_label(pattern: PatternRule) -> str:
    """生成正则命中的可读摘要。

    Args:
        pattern: 正则规则。

    Returns:
        截断后的规则摘要。
    """
    return f"re:{pattern.pattern[:_MAX_PATTERN_LABEL]}"


class SafetyEngine:
    """敏感词与正则规则引擎，支持原子热替换。

    Args:
        rules: 初始规则集。
    """

    def __init__(self, rules: RuleSet) -> None:
        self._lock = threading.Lock()
        self._trie: dict[str, Any] = {}
        self._patterns: list[tuple[re.Pattern[str], PatternRule]] = []
        self._whitelist: tuple[str, ...] = ()
        self._rules = rules
        self._apply(rules)

    @property
    def rules(self) -> RuleSet:
        """返回当前生效的规则集。"""
        return self._rules

    def reload(self, rules: RuleSet) -> None:
        """原子替换规则集。

        Args:
            rules: 新规则集。

        Raises:
            re.error: 正则规则无法编译。
        """
        compiled = self._compile(rules)
        with self._lock:
            self._rules = rules
            self._trie, self._patterns, self._whitelist = compiled

    def _apply(self, rules: RuleSet) -> None:
        """初始化时应用规则。"""
        trie, patterns, whitelist = self._compile(rules)
        self._trie = trie
        self._patterns = patterns
        self._whitelist = whitelist

    def _compile(
        self, rules: RuleSet
    ) -> tuple[
        dict[str, Any],
        list[tuple[re.Pattern[str], PatternRule]],
        tuple[str, ...],
    ]:
        """编译规则集；正则错误会在加载期暴露。"""
        patterns = [
            (re.compile(rule.pattern), rule) for rule in rules.patterns
        ]
        whitelist = tuple(
            phrase
            for phrase in (normalize(x) for x in rules.whitelist)
            if phrase
        )
        return _build_trie(rules.words), patterns, whitelist

    def scan(self, text: str) -> ScanResult:
        """扫描单条文本。

        Args:
            text: 待检测文本。

        Returns:
            含命中解释与处置结论的结果。
        """
        with self._lock:
            trie = self._trie
            patterns = tuple(self._patterns)
            whitelist = self._whitelist

        normalized = normalize(text)
        masked = _mask_whitelist(normalized, whitelist)
        hits: list[Hit] = []
        for start, end, rule in _dedupe_overlaps(_scan_trie(trie, masked)):
            hits.append(
                Hit(
                    term=rule.term,
                    category=rule.category,
                    severity=rule.severity,
                    start=start,
                    end=end,
                )
            )
        for compiled, pattern_rule in patterns:
            for match in compiled.finditer(normalized):
                hits.append(
                    Hit(
                        term=_pattern_label(pattern_rule),
                        category=pattern_rule.category,
                        severity=pattern_rule.severity,
                        start=match.start(),
                        end=match.end(),
                    )
                )
        return ScanResult(
            text_length=len(text),
            hits=tuple(hits),
            action=_decide(hits),
        )


def _decide(hits: Sequence[Hit]) -> Action:
    """按最高严重级别给出处置结论。

    Args:
        hits: 命中列表。

    Returns:
        处置结论。
    """
    severities = {hit.severity for hit in hits}
    if Severity.BLOCK in severities:
        return Action.BLOCK
    if Severity.REVIEW in severities:
        return Action.REVIEW
    return Action.ALLOW
