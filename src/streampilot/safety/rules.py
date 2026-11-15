"""安全规则文件的加载与热更新。"""

import threading
from pathlib import Path

import yaml
from pydantic import ValidationError

from streampilot.safety.models import RuleSet

# 规则文件结构版本
SUPPORTED_VERSION = "1"


class RuleLoadError(ValueError):
    """规则文件缺失、格式错误或版本不支持。"""


def load_rules(path: Path) -> RuleSet:
    """从 YAML 文件加载并校验规则。

    Args:
        path: 规则文件路径。

    Returns:
        校验通过的规则集。

    Raises:
        RuleLoadError: 文件不可读、结构非法或版本不支持。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleLoadError(f"cannot read rule file: {path}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuleLoadError(f"invalid yaml in {path}") from exc
    if not isinstance(data, dict):
        raise RuleLoadError("rule file must be a mapping")
    version = data.get("version")
    if version is not None and str(version) != SUPPORTED_VERSION:
        raise RuleLoadError(f"unsupported rule version: {version}")
    try:
        return RuleSet.model_validate(data)
    except ValidationError as exc:
        raise RuleLoadError(f"invalid rule file: {exc}") from exc


class RuleProvider:
    """按文件修改时间热加载规则。

    服务应每 10 秒内至少调用一次 :meth:`maybe_reload`，以满足热更新
    生效时间要求。

    Args:
        path: 规则文件路径。

    Raises:
        RuleLoadError: 初始加载失败。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._rules = load_rules(path)
        self._mtime = path.stat().st_mtime

    @property
    def rules(self) -> RuleSet:
        """返回当前生效规则。"""
        return self._rules

    def maybe_reload(self) -> RuleSet | None:
        """文件变更时重新加载。

        Returns:
            重新加载后的规则；未变更时返回 None。

        Raises:
            RuleLoadError: 新文件非法，此时保留旧规则。
        """
        mtime = self._path.stat().st_mtime
        if mtime <= self._mtime:
            return None
        rules = load_rules(self._path)
        with self._lock:
            self._rules = rules
            self._mtime = mtime
        return rules
