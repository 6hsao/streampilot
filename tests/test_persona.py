"""人设版本、回滚、A/B 与提示词组装的测试。"""

import pytest

from streampilot.llm.types import ChatMessage
from streampilot.persona import (
    ABExperiment,
    Persona,
    PersonaVersion,
    compose_messages,
)


def _persona() -> Persona:
    """构造带两个版本的人设。"""
    persona = Persona(persona_id="p_1", name="小航")
    persona.add_version(
        PersonaVersion(
            version=1,
            system_prompt="你是直播间主播小航。",
            style_rules=("口语化",),
        )
    )
    persona.add_version(
        PersonaVersion(
            version=2,
            system_prompt="你是直播间主播小航，语气更活泼。",
            style_rules=("口语化", "多用语气词"),
            banned_topics=("医疗建议",),
        )
    )
    return persona


def test_publish_switches_version_and_rolls_back() -> None:
    """新增版本不会自动生效，发布后才切换，可回滚。"""
    persona = _persona()

    assert persona.current_version == 1

    persona.publish(2)
    assert persona.current_version == 2

    persona.publish(1)

    assert persona.current_version == 1
    assert persona.get(1).digest == persona.get(1).digest


def test_versions_must_increase() -> None:
    """版本号必须递增，重复版本被拒绝。"""
    persona = _persona()

    with pytest.raises(ValueError, match="not increasing"):
        persona.add_version(
            PersonaVersion(version=2, system_prompt="重复版本")
        )


def test_publish_unknown_version_fails() -> None:
    """发布不存在的版本直接失败。"""
    with pytest.raises(KeyError):
        _persona().publish(99)


def test_snapshot_tracks_version_and_digest() -> None:
    """场次快照记录生效版本与提示词摘要。"""
    persona = _persona()
    persona.publish(2)

    snapshot = persona.snapshot("ss_1")

    assert snapshot.persona_id == "p_1"
    assert snapshot.version == 2
    assert snapshot.prompt_digest == persona.get(2).digest


def test_ab_experiment_is_deterministic() -> None:
    """A/B 分配对同一场次稳定，且比例可控。"""
    persona = _persona()
    persona.publish(1)
    persona.experiment = ABExperiment(candidate_version=2, percent=50)

    first = persona.resolve_version("ss_42")
    assert first == persona.resolve_version("ss_42")

    buckets = [persona.resolve_version(f"ss_{i}") for i in range(400)]
    candidate_ratio = buckets.count(2) / len(buckets)

    assert 0.35 <= candidate_ratio <= 0.65


def test_ab_ignores_missing_candidate_version() -> None:
    """候选版本不存在时退回当前版本。"""
    persona = _persona()
    persona.publish(1)
    persona.experiment = ABExperiment(candidate_version=99, percent=100)

    assert persona.resolve_version("ss_1") == 1


def test_render_system_prompt_includes_rules() -> None:
    """系统提示词包含风格与禁忌小节。"""
    rendered = _persona().get(2).render_system_prompt()

    assert "你是直播间主播小航，语气更活泼。" in rendered
    assert "【风格要求】" in rendered
    assert "- 多用语气词" in rendered
    assert "【禁止讨论】" in rendered
    assert "- 医疗建议" in rendered


def test_compose_messages_truncates_history() -> None:
    """历史按上限截断，当前评论总在最后。"""
    history = [ChatMessage(role="user", content=f"历史{i}") for i in range(10)]

    messages = compose_messages(
        _persona().get(1), "  主播怎么买  ", history=history, max_history=3
    )

    assert messages[0].role == "system"
    assert [item.content for item in messages[1:-1]] == [
        "历史7",
        "历史8",
        "历史9",
    ]
    assert messages[-1] == ChatMessage(role="user", content="主播怎么买")


def test_compose_rejects_empty_comment() -> None:
    """空评论不进入提示词。"""
    with pytest.raises(ValueError):
        compose_messages(_persona().get(1), "   ")


def test_version_requires_prompt() -> None:
    """空提示词的人设版本被拒绝。"""
    with pytest.raises(ValueError, match="system_prompt"):
        PersonaVersion(version=1, system_prompt="  ")
    with pytest.raises(ValueError, match="version"):
        PersonaVersion(version=0, system_prompt="x")


def test_persona_requires_identity() -> None:
    """人设标识与名称不能为空。"""
    with pytest.raises(ValueError):
        Persona(persona_id="", name="小航")
    with pytest.raises(ValueError):
        Persona(persona_id="p_1", name=" ")
