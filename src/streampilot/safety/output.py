"""输出侧审核：逐句审核、违规句改写、兜底话术。

编排器把 LLM 流式输出交给 :class:`OutputGuard`，只把通过审核的句子
交给 TTS，避免「先合成再撤回」造成的合规事故。
"""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from streampilot.llm.types import StreamChunk
from streampilot.safety.sentences import DEFAULT_MAX_CHARS, SentenceSplitter
from streampilot.safety.service import SafetyService

# 兜底话术：内容中性、不涉及具体商品与承诺
DEFAULT_FALLBACK_PHRASE = "这个话题我们换个方向，先看看大家还有什么想问的～"


@dataclass(frozen=True)
class ApprovedSentence:
    """通过审核、可交给 TTS 的句子。

    Attributes:
        seq: 句序，从 0 开始。
        text: 待合成文本。
        action: 处置方式，``pass`` / ``rewrite`` / ``fallback``。
    """

    seq: int
    text: str
    action: str


@dataclass
class GuardStats:
    """输出审核计数。

    Attributes:
        passed: 直接通过的句子数。
        rewritten: 改写后通过的句子数。
        dropped: 被丢弃的句子数。
        fallbacks: 兜底话术使用次数。
    """

    passed: int = 0
    rewritten: int = 0
    dropped: int = 0
    fallbacks: int = 0


class OutputGuard:
    """逐句审核并过滤 LLM 输出。

    Args:
        service: 审核服务。
        rewriter: 安全改写函数；缺省时违规句直接兜底。
        fallback_phrase: 兜底话术。
        max_chars: 单句长度上限。
    """

    def __init__(
        self,
        service: SafetyService,
        *,
        rewriter: Callable[[str], str] | None = None,
        fallback_phrase: str = DEFAULT_FALLBACK_PHRASE,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        if not fallback_phrase.strip():
            raise ValueError("fallback_phrase must not be empty")
        self._service = service
        self._rewriter = rewriter
        self._fallback_phrase = fallback_phrase
        self._splitter = SentenceSplitter(max_chars)
        self.stats = GuardStats()
        self._fallback_used = False

    def guard(
        self, chunks: Iterable[StreamChunk]
    ) -> Iterator[ApprovedSentence]:
        """把流式分片转成通过审核的句子流。

        Args:
            chunks: LLM 流式分片。

        Yields:
            可发声的句子。
        """
        seq = 0
        for chunk in chunks:
            if not chunk.delta:
                continue
            for sentence in self._splitter.feed(chunk.delta):
                approved = self._approve(sentence, seq)
                if approved is not None:
                    yield approved
                    seq += 1
        for sentence in self._splitter.flush():
            approved = self._approve(sentence, seq)
            if approved is not None:
                yield approved
                seq += 1

    def _approve(self, sentence: str, seq: int) -> ApprovedSentence | None:
        """审核单句，返回可发声句子或 None。"""
        decision = self._service.check(sentence, "output")
        if decision.allowed:
            self.stats.passed += 1
            return ApprovedSentence(seq=seq, text=sentence, action="pass")
        rewritten = self._try_rewrite(sentence)
        if rewritten is not None:
            self.stats.rewritten += 1
            return ApprovedSentence(seq=seq, text=rewritten, action="rewrite")
        self.stats.dropped += 1
        if self._fallback_used:
            return None
        self._fallback_used = True
        self.stats.fallbacks += 1
        return ApprovedSentence(
            seq=seq, text=self._fallback_phrase, action="fallback"
        )

    def _try_rewrite(self, sentence: str) -> str | None:
        """改写一次并复审，失败返回 None。"""
        if self._rewriter is None:
            return None
        try:
            candidate = self._rewriter(sentence).strip()
        except Exception:  # noqa: BLE001 - 改写失败一律降级兜底
            return None
        if not candidate:
            return None
        if self._service.check(candidate, "output").allowed:
            return candidate
        return None
