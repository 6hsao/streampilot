"""许可证门禁：对依赖许可证做归一化、分类与准入判定。

规则来源：docs/OPEN-SOURCE.md 第 2 节。MIT 项目允许宽松许可证；
GPL/AGPL/LGPL/无许可证/自定义限制许可证默认阻断。
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# 常见许可证字符串 → SPDX 标识
_ALIASES: dict[str, str] = {
    "mit license": "MIT",
    "mit": "MIT",
    "bsd license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "apache software license": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "isc license (iscl)": "ISC",
    "isc": "ISC",
    "python software foundation license": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "unlicense": "Unlicense",
    # GNU 系列按传染性处理
    "gnu lesser general public license v2 or later": "LGPL-2.1",
    "gnu lesser general public license v2.1": "LGPL-2.1",
    "gnu lesser general public license v3": "LGPL-3.0",
    "gnu library general public license v2": "LGPL-2.0",
    "gnu general public license v2": "GPL-2.0",
    "gnu general public license v3": "GPL-3.0",
    "gnu affero general public license v3": "AGPL-3.0",
}

# 允许的宽松许可证
ALLOWED: frozenset[str] = frozenset(
    {
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "PSF-2.0",
        "MPL-2.0",
        "Unlicense",
    }
)

# 出现即阻断的许可证前缀
_DENIED_PREFIXES = (
    "GPL",
    "AGPL",
    "LGPL",
    "SSPL",
    "BUSL",
    "Elastic-2.0",
    "Commons-Clause",
    "CC-BY-NC",
    "Proprietary",
)

_CLASSIFIER_PREFIX = "License :: OSI Approved :: "


class Decision(StrEnum):
    """准入结论。"""

    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"


@dataclass(frozen=True)
class Policy:
    """许可证策略。

    Attributes:
        allowed: 允许的 SPDX 标识集合。
        denied: 明确拒绝的 SPDX 标识集合。
        exceptions: 包名到理由的映射，仅用于把 REVIEW 放行。
    """

    allowed: frozenset[str]
    denied: frozenset[str]
    exceptions: dict[str, str]

    @classmethod
    def default(cls) -> "Policy":
        """返回内置默认策略，供无策略文件时使用。"""
        return cls(allowed=ALLOWED, denied=frozenset(), exceptions={})


@dataclass(frozen=True)
class Component:
    """一个待检查的依赖组件。

    Attributes:
        name: 分发包名。
        version: 版本号。
        license_text: 原始许可证声明。
    """

    name: str
    version: str
    license_text: str


@dataclass(frozen=True)
class Finding:
    """单个组件的检查结果。

    Attributes:
        component: 组件信息。
        normalized: 归一化后的许可证标识，无法识别时为空串。
        decision: 准入结论。
        reason: 结论说明。
    """

    component: Component
    normalized: str
    decision: Decision
    reason: str


def normalize_license(text: str) -> str:
    """把许可证声明归一化为 SPDX 标识。

    Args:
        text: 元数据中的许可证原文。

    Returns:
        识别到的 SPDX 标识；无法识别时返回空串。
    """
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    for alias, spdx in _ALIASES.items():
        if lowered.startswith(alias):
            return spdx
    if re.fullmatch(r"[A-Za-z0-9.+-]+", cleaned) and (
        cleaned in ALLOWED or cleaned.startswith(_DENIED_PREFIXES)
    ):
        return cleaned
    return ""


def classify(component: Component, policy: Policy) -> Finding:
    """按策略判定单个组件的准入结论。

    Args:
        component: 组件信息。
        policy: 许可证策略。

    Returns:
        含归一化许可证与结论的检查结果。
    """
    normalized = normalize_license(component.license_text)
    if normalized and (
        normalized in policy.denied or normalized.startswith(_DENIED_PREFIXES)
    ):
        return Finding(
            component=component,
            normalized=normalized,
            decision=Decision.DENY,
            reason="copyleft or restricted license",
        )
    if normalized in policy.allowed:
        return Finding(
            component=component,
            normalized=normalized,
            decision=Decision.ALLOW,
            reason="permissive license compatible with MIT",
        )
    if component.name in policy.exceptions:
        return Finding(
            component=component,
            normalized=normalized,
            decision=Decision.ALLOW,
            reason=policy.exceptions[component.name],
        )
    if not normalized:
        return Finding(
            component=component,
            normalized="",
            decision=Decision.REVIEW,
            reason="unrecognized license, manual review required",
        )
    return Finding(
        component=component,
        normalized=normalized,
        decision=Decision.REVIEW,
        reason="license not in allowlist",
    )


def load_policy(data: Any) -> Policy:
    """从已解析的 YAML 数据构造策略。

    Args:
        data: ``infra/license-policy.yml`` 的解析结果。

    Returns:
        策略对象。

    Raises:
        ValueError: 数据缺失或格式不符合预期。
    """
    if not isinstance(data, dict):
        raise ValueError("license policy must be a mapping")
    allowed = data.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("license policy requires a non-empty allowed list")
    denied_raw = data.get("denied", [])
    if not isinstance(denied_raw, list):
        raise ValueError("license policy denied must be a list")
    exceptions_raw = data.get("exceptions", [])
    exceptions: dict[str, str] = {}
    if not isinstance(exceptions_raw, list):
        raise ValueError("license policy exceptions must be a list")
    for item in exceptions_raw:
        if not isinstance(item, dict):
            raise ValueError("each exception must be a mapping")
        name = item.get("package")
        reason = item.get("reason")
        if not isinstance(name, str) or not isinstance(reason, str):
            raise ValueError("exception requires package and reason strings")
        exceptions[name] = reason
    return Policy(
        allowed=frozenset(str(x) for x in allowed),
        denied=frozenset(str(x) for x in denied_raw),
        exceptions=exceptions,
    )
