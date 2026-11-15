"""许可证门禁的单元测试。"""

import pytest

from streampilot.tooling import (
    Component,
    Decision,
    Policy,
    classify,
    load_policy,
    normalize_license,
)


def _component(license_text: str, name: str = "demo") -> Component:
    """构造一个待检查组件。"""
    return Component(name=name, version="1.0.0", license_text=license_text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MIT License", "MIT"),
        ("MIT", "MIT"),
        ("Apache Software License", "Apache-2.0"),
        ("BSD License", "BSD-3-Clause"),
        ("BSD-2-Clause", "BSD-2-Clause"),
        ("ISC License (ISCL)", "ISC"),
        ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
        ("GPL-3.0", "GPL-3.0"),
        (
            "GNU Lesser General Public License v2 or later (LGPLv2+)",
            "LGPL-2.1",
        ),
        ("GNU General Public License v3 (GPLv3)", "GPL-3.0"),
        ("  ", ""),
        ("Custom Research License", ""),
    ],
)
def test_normalize_license(text: str, expected: str) -> None:
    """常见许可证声明可归一化为 SPDX 标识。"""
    assert normalize_license(text) == expected


def test_permissive_component_is_allowed() -> None:
    """MIT 依赖通过门禁。"""
    finding = classify(_component("MIT"), Policy.default())

    assert finding.decision is Decision.ALLOW
    assert finding.normalized == "MIT"


@pytest.mark.parametrize(
    "license_text", ["GPL-3.0", "AGPL-3.0", "LGPL-2.1", "SSPL-1.0"]
)
def test_copyleft_component_is_denied(license_text: str) -> None:
    """传染性许可证一律阻断。"""
    finding = classify(_component(license_text), Policy.default())

    assert finding.decision is Decision.DENY


def test_unknown_license_needs_review() -> None:
    """无许可证或无法识别的声明进入人工复核。"""
    finding = classify(_component(""), Policy.default())

    assert finding.decision is Decision.REVIEW
    assert finding.normalized == ""


def test_exception_only_affects_review() -> None:
    """例外只能放行复核项，不能放行阻断项。"""
    policy = Policy(
        allowed=frozenset({"MIT"}),
        denied=frozenset(),
        exceptions={"review_pkg": "approved by legal"},
    )
    reviewed = classify(_component("Weird License", name="review_pkg"), policy)
    denied = classify(_component("AGPL-3.0", name="review_pkg"), policy)

    assert reviewed.decision is Decision.ALLOW
    assert reviewed.reason == "approved by legal"
    assert denied.decision is Decision.DENY


def test_load_policy_rejects_bad_shape() -> None:
    """策略文件结构错误时给出明确异常。"""
    with pytest.raises(ValueError, match="mapping"):
        load_policy([])
    with pytest.raises(ValueError, match="non-empty allowed"):
        load_policy({"allowed": []})
    with pytest.raises(ValueError, match="package and reason"):
        load_policy({"allowed": ["MIT"], "exceptions": [{"package": "x"}]})


def test_load_policy_builds_policy() -> None:
    """合法策略文件可正确解析。"""
    policy = load_policy(
        {
            "allowed": ["MIT", "Apache-2.0"],
            "denied": ["GPL-3.0"],
            "exceptions": [{"package": "demo", "reason": "legal approval"}],
        }
    )

    assert policy.allowed == frozenset({"MIT", "Apache-2.0"})
    assert policy.denied == frozenset({"GPL-3.0"})
    assert policy.exceptions == {"demo": "legal approval"}
