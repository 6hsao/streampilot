"""人设版本：系统提示词、风格规则、禁忌话题与 A/B 发布。

人设为聚合根：版本不可变，回滚即把当前指针指回旧版本，历史版本不删除。
"""

import hashlib
from dataclasses import dataclass, field

# 提示词中拼装规则的小节标题
STYLE_HEADER = "【风格要求】"
BANNED_HEADER = "【禁止讨论】"


@dataclass(frozen=True)
class PersonaVersion:
    """一版人设配置。

    Attributes:
        version: 版本号，从 1 开始递增。
        system_prompt: 系统提示词主体。
        style_rules: 风格规则，逐条拼进提示词。
        banned_topics: 禁忌话题，逐条拼进提示词。
        created_by: 创建人标识。
    """

    version: int
    system_prompt: str
    style_rules: tuple[str, ...] = ()
    banned_topics: tuple[str, ...] = ()
    created_by: str = "system"

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be empty")

    @property
    def digest(self) -> str:
        """提示词摘要，用于配置快照追溯。"""
        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()

    def render_system_prompt(self) -> str:
        """把主体、风格与禁忌话题拼成最终系统提示词。"""
        parts = [self.system_prompt.strip()]
        if self.style_rules:
            parts.append(STYLE_HEADER)
            parts.extend(f"- {item}" for item in self.style_rules)
        if self.banned_topics:
            parts.append(BANNED_HEADER)
            parts.extend(f"- {item}" for item in self.banned_topics)
        return "\n".join(parts)


@dataclass(frozen=True)
class ABExperiment:
    """把一部分场次切换到候选版本。

    Attributes:
        candidate_version: 候选版本号。
        percent: 命中比例，0~100。
    """

    candidate_version: int
    percent: int

    def __post_init__(self) -> None:
        if not 0 <= self.percent <= 100:
            raise ValueError("percent must be within 0..100")


@dataclass(frozen=True)
class PersonaSnapshot:
    """场次开播时固化的人设配置，用于事后追溯。

    Attributes:
        persona_id: 人设标识。
        version: 生效版本号。
        prompt_digest: 该版本提示词摘要。
    """

    persona_id: str
    version: int
    prompt_digest: str


@dataclass
class Persona:
    """人设聚合。

    Args:
        persona_id: 人设标识。
        name: 人设名称。

    Raises:
        ValueError: 标识或名称为空。
    """

    persona_id: str
    name: str
    versions: dict[int, PersonaVersion] = field(default_factory=dict)
    current_version: int = 0
    experiment: ABExperiment | None = None

    def __post_init__(self) -> None:
        if not self.persona_id.strip():
            raise ValueError("persona_id must not be empty")
        if not self.name.strip():
            raise ValueError("name must not be empty")

    def add_version(self, version: PersonaVersion) -> None:
        """登记新版本；版本号必须递增。

        Args:
            version: 新版本。

        Raises:
            ValueError: 版本号已存在或不递增。
        """
        if self.versions and version.version <= max(self.versions):
            raise ValueError(f"version {version.version} is not increasing")
        self.versions[version.version] = version
        if self.current_version == 0:
            self.current_version = version.version

    def publish(self, version: int) -> None:
        """切换当前版本；传旧版本号即回滚。

        Args:
            version: 目标版本号。

        Raises:
            KeyError: 版本不存在。
        """
        if version not in self.versions:
            raise KeyError(f"unknown persona version: {version}")
        self.current_version = version

    def get(self, version: int) -> PersonaVersion:
        """按版本号取配置。

        Raises:
            KeyError: 版本不存在。
        """
        return self.versions[version]

    def resolve_version(self, session_id: str) -> int:
        """按场次决定生效版本，支持 A/B 灰度。

        Args:
            session_id: 场次标识。

        Returns:
            生效版本号。
        """
        experiment = self.experiment
        if experiment is None or experiment.candidate_version not in (
            self.versions
        ):
            return self.current_version
        bucket = (
            int(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8], 16)
            % 100
        )
        if bucket < experiment.percent:
            return experiment.candidate_version
        return self.current_version

    def snapshot(self, session_id: str) -> PersonaSnapshot:
        """生成场次配置快照。

        Args:
            session_id: 场次标识。

        Returns:
            含生效版本与提示词摘要的快照。
        """
        version = self.get(self.resolve_version(session_id))
        return PersonaSnapshot(
            persona_id=self.persona_id,
            version=version.version,
            prompt_digest=version.digest,
        )
