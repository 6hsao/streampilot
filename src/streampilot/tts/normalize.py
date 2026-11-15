"""TTS 前的文本规范化：数字、小数、百分比、符号与停顿。

只做「读得对」所必需的处理；多音字与韵律由 TTS 供应商负责，
项目内不做词典级干预，避免两处规则互相盖掉。
"""

import re

# 需要直接丢弃的字符：emoji、零宽字符、装饰符号
_STRIP_PATTERN = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u200b-\u200f\u2060\ufe0f]"
)
# 一切空白折叠
_SPACE_PATTERN = re.compile(r"\s+")
# 数字（含小数、千分位）与百分比
_NUMBER_PATTERN = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(%|％)?")
# 句末与停顿标点映射
_PUNCT_MAP = {
    "...": "，",
    "⋯⋯": "，",
    "……": "，",
    "~": "",
    "～": "",
    "、": "，",
    ";": "；",
    "!": "！",
    "?": "？",
    ":": "：",
}
_DIGITS = "零一二三四五六七八九"
_UNITS = ("", "十", "百", "千")
_SECTIONS = ("", "万", "亿")
# 超过该长度的连续数字按位朗读（手机号、订单号等）
_DIGIT_BY_DIGIT_THRESHOLD = 7


def _section_to_chinese(section: int) -> str:
    """把 0~9999 的四位段转成中文。"""
    if section == 0:
        return ""
    if section < 20:
        if section < 10:
            return _DIGITS[section]
        tail = _DIGITS[section % 10] if section % 10 else ""
        return "十" + tail
    parts: list[str] = []
    zero_pending = False
    for index in range(3, -1, -1):
        digit = section // (10**index) % 10
        if digit == 0:
            zero_pending = bool(parts)
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        parts.append(_DIGITS[digit] + _UNITS[index])
    return "".join(parts)


def int_to_chinese(value: int) -> str:
    """把非负整数转成中文读法。

    Args:
        value: 非负整数。

    Returns:
        中文读法；0 返回「零」。

    Raises:
        ValueError: 传入负数。
    """
    if value < 0:
        raise ValueError("value must not be negative")
    if value == 0:
        return "零"
    if value < 20:
        if value < 10:
            return _DIGITS[value]
        return "十" + (_DIGITS[value % 10] if value % 10 else "")
    sections: list[int] = []
    remaining = value
    while remaining:
        sections.append(remaining % 10000)
        remaining //= 10000
    parts: list[str] = []
    need_zero = False
    for index in range(len(sections) - 1, -1, -1):
        section = sections[index]
        if section == 0:
            need_zero = bool(parts)
            continue
        if parts and (need_zero or section < 1000):
            parts.append("零")
        parts.append(_section_to_chinese(section) + _SECTIONS[index])
        need_zero = False
    return "".join(parts)


def _read_number(match: re.Match[str]) -> str:
    """把正则命中的数字读成中文。"""
    raw, fraction, percent = match.groups()
    digits = raw.replace(",", "")
    if len(digits) >= _DIGIT_BY_DIGIT_THRESHOLD:
        spoken = "".join(_DIGITS[int(item)] for item in digits)
    else:
        spoken = int_to_chinese(int(digits))
    if fraction:
        spoken += "点" + "".join(_DIGITS[int(item)] for item in fraction)
    if percent:
        spoken = "百分之" + spoken
    return spoken


def strip_emoji(text: str) -> str:
    """移除 emoji 与不可见字符。"""
    return _STRIP_PATTERN.sub("", text)


def normalize_for_tts(text: str) -> str:
    """把直播文案规范化为适合朗读的文本。

    Args:
        text: 原始文本。

    Returns:
        数字化、去 emoji、标点统一的文本。
    """
    cleaned = strip_emoji(text)
    for source, target in _PUNCT_MAP.items():
        cleaned = cleaned.replace(source, target)
    cleaned = _SPACE_PATTERN.sub("", cleaned)
    return _NUMBER_PATTERN.sub(_read_number, cleaned)
