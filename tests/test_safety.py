"""内容安全 DFA 引擎与规则加载的测试。"""

import os
import re
import time
from pathlib import Path

import pytest

from streampilot.safety import (
    Action,
    PatternRule,
    RuleLoadError,
    RuleProvider,
    RuleSet,
    SafetyEngine,
    Severity,
    WordRule,
    load_rules,
    normalize,
)

DATA_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "safety" / ("lexicon.yml")
)


def _ruleset() -> RuleSet:
    """构造用于单测的最小规则集。"""
    return RuleSet(
        words=(
            WordRule(term="微信", category="引流", severity=Severity.REVIEW),
            WordRule(term="加微信", category="引流", severity=Severity.BLOCK),
        ),
        patterns=(
            PatternRule(
                pattern=r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])",
                category="隐私",
                severity=Severity.REVIEW,
            ),
        ),
        whitelist=("官方微信",),
    )


def test_normalize_removes_noise() -> None:
    """全角、大小写、空白与零宽字符被归一化。"""
    assert normalize("ＡＢＣ 微\u200b信") == "abc微信"


def test_longest_match_wins_and_explains() -> None:
    """重叠命中保留最长词，并给出分类与位置。"""
    engine = SafetyEngine(_ruleset())

    result = engine.scan("我要加微信聊")

    assert result.action is Action.BLOCK
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.term == "加微信"
    assert hit.category == "引流"
    assert (hit.start, hit.end) == (2, 5)


def test_whitelist_masks_phrase() -> None:
    """白名单短语不会触发命中。"""
    engine = SafetyEngine(_ruleset())

    assert engine.scan("请关注官方微信").action is Action.ALLOW


def test_pattern_hit_is_review() -> None:
    """手机号命中隐私规则，结论为复核。"""
    engine = SafetyEngine(_ruleset())

    result = engine.scan("我的电话13812345678")

    assert result.action is Action.REVIEW
    assert result.hits[0].category == "隐私"


def test_clean_text_is_allowed() -> None:
    """正常文本放行。"""
    engine = SafetyEngine(_ruleset())

    result = engine.scan("主播这个怎么买")

    assert result.action is Action.ALLOW
    assert result.hits == ()


def test_reload_swaps_rules() -> None:
    """热替换后按新规则判定。"""
    engine = SafetyEngine(_ruleset())
    new_rules = RuleSet(
        words=(
            WordRule(term="测试词", category="测试", severity=Severity.BLOCK),
        )
    )

    engine.reload(new_rules)

    assert engine.scan("我要加微信").action is Action.ALLOW
    assert engine.scan("这是测试词").action is Action.BLOCK
    assert engine.rules is new_rules


def test_scan_is_fast() -> None:
    """单条文本匹配平均耗时低于 5ms。"""
    engine = SafetyEngine(_ruleset())
    text = "主播这个怎么买我要加微信聊" * 5

    started = time.perf_counter()
    for _ in range(200):
        engine.scan(text)
    average_ms = (time.perf_counter() - started) / 200 * 1000

    assert average_ms < 5


def test_invalid_regex_fails_at_load() -> None:
    """非法正则在编译期暴露。"""
    with pytest.raises(re.error):
        SafetyEngine(
            RuleSet(
                patterns=(
                    PatternRule(
                        pattern="(",
                        category="非法",
                        severity=Severity.BLOCK,
                    ),
                )
            )
        )


def test_load_project_lexicon() -> None:
    """仓库内词库文件可加载且包含规则。"""
    rules = load_rules(DATA_FILE)

    assert rules.version == "1"
    assert rules.words
    assert rules.whitelist == ("官方微信",)


def test_load_rules_rejects_bad_files(tmp_path: Path) -> None:
    """结构错误、版本不支持与未知字段均拒绝。"""
    bad_yaml = tmp_path / "bad.yml"
    bad_yaml.write_text("words: [", encoding="utf-8")
    with pytest.raises(RuleLoadError, match="invalid yaml"):
        load_rules(bad_yaml)

    wrong_version = tmp_path / "version.yml"
    wrong_version.write_text('version: "2"', encoding="utf-8")
    with pytest.raises(RuleLoadError, match="unsupported"):
        load_rules(wrong_version)

    unknown = tmp_path / "unknown.yml"
    unknown.write_text("unexpected: true", encoding="utf-8")
    with pytest.raises(RuleLoadError, match="invalid rule file"):
        load_rules(unknown)

    with pytest.raises(RuleLoadError, match="cannot read"):
        load_rules(tmp_path / "missing.yml")


def test_provider_reloads_on_change(tmp_path: Path) -> None:
    """文件更新后 provider 返回新规则。"""
    path = tmp_path / "rules.yml"
    path.write_text(
        "version: '1'\nwords:\n  - term: 甲\n    category: 测试\n"
        "    severity: block\n",
        encoding="utf-8",
    )
    provider = RuleProvider(path)
    assert provider.maybe_reload() is None
    original_mtime = path.stat().st_mtime

    path.write_text(
        "version: '1'\nwords:\n  - term: 乙\n    category: 测试\n"
        "    severity: block\n",
        encoding="utf-8",
    )
    os.utime(path, (original_mtime + 2, original_mtime + 2))
    new_rules = provider.maybe_reload()

    assert new_rules is not None
    assert new_rules.words[0].term == "乙"
