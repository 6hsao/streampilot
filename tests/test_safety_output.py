"""句级切分与输出审核的测试。"""

import pytest

from streampilot.llm.types import StreamChunk
from streampilot.safety import (
    OutputGuard,
    RuleSet,
    SafetyEngine,
    SafetyService,
    SentenceSplitter,
    Severity,
    WordRule,
    split_sentences,
)


def _service() -> SafetyService:
    """只跑本地规则的审核服务。"""
    engine = SafetyEngine(
        RuleSet(
            words=(
                WordRule(
                    term="包治百病",
                    category="medical",
                    severity=Severity.BLOCK,
                ),
            )
        )
    )
    return SafetyService(engine)


def _chunks(text: str, size: int = 4) -> list[StreamChunk]:
    """把文本切成固定长度的流式分片。"""
    return [
        StreamChunk(delta=text[index : index + size])
        for index in range(0, len(text), size)
    ]


def test_splitter_emits_on_terminators() -> None:
    """中英文句末标点都会触发切分。"""
    splitter = SentenceSplitter()

    assert splitter.feed("你好。今天") == ["你好。"]
    assert splitter.feed("怎么样？") == ["今天怎么样？"]
    assert splitter.flush() == []


def test_splitter_flushes_remainder() -> None:
    """流结束时输出没有句末标点的剩余文本。"""
    splitter = SentenceSplitter()

    assert splitter.feed("最后一句没有标点") == []
    assert splitter.flush() == ["最后一句没有标点"]
    assert splitter.flush() == []


def test_splitter_caps_long_sentences() -> None:
    """超长无标点文本按上限切分，保证尽早开始合成。"""
    sentence = "啊" * 130

    parts = split_sentences([sentence], max_chars=50)

    assert [len(item) for item in parts] == [50, 50, 30]


def test_splitter_rejects_bad_limit() -> None:
    """非法长度上限直接报错。"""
    with pytest.raises(ValueError):
        SentenceSplitter(0)


def test_clean_output_passes_through() -> None:
    """合规句子按句输出，序号连续。"""
    guard = OutputGuard(_service())

    approved = list(guard.guard(_chunks("欢迎来到直播间。今天讲讲新品。")))

    assert [item.text for item in approved] == [
        "欢迎来到直播间。",
        "今天讲讲新品。",
    ]
    assert [item.seq for item in approved] == [0, 1]
    assert guard.stats.passed == 2


def test_blocked_sentence_is_dropped_without_rewriter() -> None:
    """无改写能力时违规句丢弃，并补一句兜底话术。"""
    guard = OutputGuard(_service())

    approved = list(guard.guard(_chunks("这个能包治百病。谢谢大家。")))

    assert [item.action for item in approved] == ["fallback", "pass"]
    assert guard.stats.dropped == 1
    assert guard.stats.fallbacks == 1


def test_fallback_phrase_is_used_only_once() -> None:
    """多个违规句只补一次兜底，避免重复话术。"""
    guard = OutputGuard(_service(), fallback_phrase="换个话题聊聊～")

    approved = list(guard.guard(_chunks("包治百病。真的包治百病。好的。")))

    assert [item.action for item in approved] == ["fallback", "pass"]
    assert guard.stats.fallbacks == 1
    assert guard.stats.dropped == 2


def test_rewrite_success_replaces_sentence() -> None:
    """改写通过复审后使用改写文本。"""
    guard = OutputGuard(
        _service(), rewriter=lambda text: "这个我们按说明使用。"
    )

    approved = list(guard.guard(_chunks("这个能包治百病。")))

    assert [item.action for item in approved] == ["rewrite"]
    assert approved[0].text == "这个我们按说明使用。"
    assert guard.stats.rewritten == 1


def test_rewrite_still_blocked_falls_back() -> None:
    """改写后仍违规则丢弃并兜底。"""
    guard = OutputGuard(_service(), rewriter=lambda text: "还是包治百病。")

    approved = list(guard.guard(_chunks("这个能包治百病。")))

    assert [item.action for item in approved] == ["fallback"]
    assert guard.stats.rewritten == 0
    assert guard.stats.dropped == 1


def test_rewriter_error_falls_back() -> None:
    """改写抛错不冒泡，按兜底处理。"""

    def broken(text: str) -> str:
        raise RuntimeError("upstream down")

    guard = OutputGuard(_service(), rewriter=broken)

    approved = list(guard.guard(_chunks("这个能包治百病。")))

    assert [item.action for item in approved] == ["fallback"]


def test_empty_rewrite_is_rejected() -> None:
    """改写返回空串时不能当作通过。"""
    guard = OutputGuard(_service(), rewriter=lambda text: "   ")

    approved = list(guard.guard(_chunks("这个能包治百病。")))

    assert [item.action for item in approved] == ["fallback"]


def test_guard_requires_fallback_phrase() -> None:
    """兜底话术不能为空。"""
    with pytest.raises(ValueError):
        OutputGuard(_service(), fallback_phrase="   ")
