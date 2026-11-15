"""TTS 文本规范化的测试。"""

import pytest

from streampilot.tts import int_to_chinese, normalize_for_tts, strip_emoji


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "零"),
        (3, "三"),
        (10, "十"),
        (15, "十五"),
        (20, "二十"),
        (100, "一百"),
        (105, "一百零五"),
        (1001, "一千零一"),
        (1010, "一千零一十"),
        (1080, "一千零八十"),
        (10000, "一万"),
        (10086, "一万零八十六"),
        (110000, "十一万"),
        (1000001, "一百万零一"),
        (100000000, "一亿"),
        (100000001, "一亿零一"),
    ],
)
def test_int_to_chinese(value: int, expected: str) -> None:
    """整数按中文读数规则转换。"""
    assert int_to_chinese(value) == expected


def test_negative_is_rejected() -> None:
    """负数不是可读文本，直接报错。"""
    with pytest.raises(ValueError):
        int_to_chinese(-1)


def test_normalize_numbers_and_units() -> None:
    """整数、小数、千分位与百分比都转成中文。"""
    assert normalize_for_tts("今天 1080 元") == "今天一千零八十元"
    assert normalize_for_tts("售价 3.14 元") == "售价三点一四元"
    assert normalize_for_tts("库存 1,080 件") == "库存一千零八十件"
    assert normalize_for_tts("涨了 50%") == "涨了百分之五十"


def test_long_digits_are_read_one_by_one() -> None:
    """手机号、订单号按位朗读，避免读成天文数字。"""
    assert normalize_for_tts("电话13800138000") == (
        "电话一三八零零一三八零零零"
    )


def test_emoji_and_zero_width_are_removed() -> None:
    """emoji 与零宽字符不进入合成文本。"""
    assert strip_emoji("好耶🎉\u200b") == "好耶"
    assert normalize_for_tts("上新啦🔥") == "上新啦"


def test_punctuation_is_normalized() -> None:
    """停顿符号统一为中文逗号，波浪号去掉。"""
    assert normalize_for_tts("先看这个……再看那个") == ("先看这个，再看那个")
    assert normalize_for_tts("来啦～") == "来啦"
    assert normalize_for_tts("A、B") == "A，B"
