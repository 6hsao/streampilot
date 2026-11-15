"""许可证门禁 CLI：扫描已安装依赖并阻断不合规许可证。

用法：

    uv run python scripts/check_licenses.py

退出码 0 表示全部通过；1 表示存在 DENY 或待复核组件。
"""

import sys
from importlib import metadata
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from streampilot.tooling import (
    Component,
    Decision,
    Finding,
    classify,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "infra" / "license-policy.yml"
SELF_PACKAGE = "streampilot"
_CLASSIFIER_PREFIX = "License :: OSI Approved :: "


def read_license(dist: metadata.Distribution) -> str:
    """读取分发包的许可证声明。

    Args:
        dist: 已安装的分发包元数据。

    Returns:
        许可证原文；无法获取时返回空串。
    """
    value = dist.metadata.get("License-Expression")
    if value and value.strip():
        return value.strip()
    for classifier in dist.metadata.get_all("Classifier") or []:
        if classifier.startswith(_CLASSIFIER_PREFIX):
            return classifier.removeprefix(_CLASSIFIER_PREFIX).strip()
    license_text = dist.metadata.get("License")
    if license_text and license_text.strip():
        return license_text.strip()
    return ""


def runtime_closure() -> set[str]:
    """计算本项目运行期依赖的传递闭包。

    仅统计 ``streampilot`` 的运行时依赖，开发工具不参与许可证门禁，
    因为开发工具不会随产品分发给使用者。

    Returns:
        归一化后的包名集合。
    """
    pending = [SELF_PACKAGE]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        key = canonicalize_name(current)
        if key in seen:
            continue
        seen.add(key)
        for raw in metadata.requires(current) or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
            pending.append(requirement.name)
    return seen


def collect_components() -> list[Component]:
    """枚举运行期依赖并读取其许可证声明。

    Returns:
        去重后的组件列表，按名称排序。
    """
    allowed = runtime_closure()
    seen: set[tuple[str, str]] = set()
    components: list[Component] = []
    for dist in metadata.distributions():
        raw_name = dist.metadata.get("Name")
        if not raw_name:
            continue
        canonical = canonicalize_name(raw_name)
        if canonical == SELF_PACKAGE or canonical not in allowed:
            continue
        version = dist.version or ""
        key = (raw_name.lower(), version)
        if key in seen:
            continue
        seen.add(key)
        components.append(
            Component(
                name=raw_name,
                version=version,
                license_text=read_license(dist),
            )
        )
    return sorted(components, key=lambda item: item.name.lower())


def evaluate(policy_path: Path) -> list[Finding]:
    """对当前环境执行许可证检查。

    Args:
        policy_path: 策略文件路径。

    Returns:
        全部组件的检查结果。
    """
    data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy = load_policy(data)
    return [classify(item, policy) for item in collect_components()]


def main() -> int:
    """执行检查并输出结论。

    Returns:
        进程退出码，存在阻断项时为 1。
    """
    findings = evaluate(POLICY_PATH)
    failures = [
        item for item in findings if item.decision is not Decision.ALLOW
    ]
    for item in findings:
        print(
            f"{item.decision.value:6} {item.component.name} "
            f"{item.component.version} [{item.normalized or '?'}]"
        )
    if failures:
        print(f"\n{len(failures)} component(s) require action:")
        for item in failures:
            print(
                f"  - {item.component.name}: {item.reason} "
                f"({item.component.license_text or 'no license'})"
            )
        return 1
    print(f"\nAll {len(findings)} components passed the license gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
