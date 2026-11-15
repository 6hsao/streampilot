"""流式文本的句级切分。

TTS 与审核都以「句」为单位，需要在流式输出中尽早切出完整句子。
"""

from collections.abc import Iterable

# 句末标点，含中英文
TERMINATORS = "。！？!?；;\n"
# 单句最大长度，超长按停顿切分，避免一句话拖太久
DEFAULT_MAX_CHARS = 60


class SentenceSplitter:
    """按标点与长度上限切分流式文本。

    Args:
        max_chars: 单句最大字符数。

    Raises:
        ValueError: 上限非正。
    """

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self._max_chars = max_chars
        self._buffer = ""

    @property
    def pending(self) -> str:
        """尚未成句的缓冲文本。"""
        return self._buffer

    def feed(self, chunk: str) -> list[str]:
        """喂入一段增量文本。

        Args:
            chunk: 新增文本。

        Returns:
            本次切出的完整句子，可能为空。
        """
        self._buffer += chunk
        sentences: list[str] = []
        while True:
            index = self._find_terminator(self._buffer)
            if index is None:
                break
            sentence = self._buffer[: index + 1].strip()
            self._buffer = self._buffer[index + 1 :]
            if sentence:
                sentences.append(sentence)
        sentences.extend(self._split_long())
        return sentences

    def flush(self) -> list[str]:
        """写出剩余缓冲，流结束时调用。"""
        remaining = self._buffer.strip()
        self._buffer = ""
        return [remaining] if remaining else []

    def _find_terminator(self, text: str) -> int | None:
        positions = [text.find(mark) for mark in TERMINATORS if mark in text]
        return min(positions) if positions else None

    def _split_long(self) -> list[str]:
        """超长缓冲按上限硬切，保证 TTS 能尽早开始。"""
        sentences: list[str] = []
        while len(self._buffer) > self._max_chars:
            cut = self._buffer[: self._max_chars].strip()
            self._buffer = self._buffer[self._max_chars :]
            if cut:
                sentences.append(cut)
        return sentences


def split_sentences(
    chunks: Iterable[str], max_chars: int = DEFAULT_MAX_CHARS
) -> list[str]:
    """把完整文本切成句子列表。

    Args:
        chunks: 文本片段。
        max_chars: 单句最大字符数。

    Returns:
        句子列表。
    """
    splitter = SentenceSplitter(max_chars)
    sentences: list[str] = []
    for chunk in chunks:
        sentences.extend(splitter.feed(chunk))
    sentences.extend(splitter.flush())
    return sentences
