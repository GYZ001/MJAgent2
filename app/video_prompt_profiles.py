"""Model-specific prompt contracts for the shared video direction draft."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoPromptProfile:
    profile_id: str
    version: str
    model_family: str
    output_language: str
    render_format: str
    generation_rules: tuple[str, ...]


SEEDANCE_2_PROFILE = VideoPromptProfile(
    profile_id="seedance_2_microdrama",
    version="seedance_2_microdrama_v1",
    model_family="Seedance 2.0",
    output_language="Chinese",
    render_format="seedance_compact_director_brief",
    generation_rules=(
        "用自然中文写紧凑的单镜头导演指令，按主体与动作、场景、镜头、表演、声音、约束组织",
        "先写一个可见主动作及其终点；用起因、动作、物理后果形成连续链，禁止只写抽象情绪",
        "每份参考素材只承担明确职责，身份、服装、场景、动作、镜头、节奏不得互相污染",
        "从 visual_style 判断媒介：二维漫剧使用赛璐璐、绘制背景、层移动、保持帧和动作拖影等动画语言；不得混入写实摄影质感",
        "镜头运动只保留一个有叙事动机的主运动，并确保关键动作、接触点和人物出入画可读",
        "短镜头优先紧凑自然语言，不堆叠空泛质量词；保留角色一致性、动作时序和连续性约束",
    ),
)


MINIMAX_H3_PROFILE = VideoPromptProfile(
    profile_id="minimax_h3_microdrama",
    version="minimax_h3_official_structure_v1",
    model_family="MiniMax H3",
    output_language="English except exact dialogue and visible text",
    render_format="minimax_h3_native_fields",
    generation_rules=(
        "Write every creative field in English; preserve authoritative dialogue and visible text verbatim in their original language",
        "Describe the complete audiovisual timeline with observable actions, body mechanics, camera behavior, and synchronized sound",
        "Use stable speaker identities; keep dialogue timing exact and never translate or paraphrase authoritative lines",
        "For first/last-frame modes, describe a continuous path from the opening state to the required terminal frame",
        "For reference modes, assign each asset one role and state what is preserved, transferred, and excluded",
        "Use concrete medium language for 2D animation and never mix a canonical illustrated style with photoreal rendering language",
    ),
)


def resolve_video_prompt_profile(
    *,
    provider: str,
    model: str = "",
) -> VideoPromptProfile:
    """Resolve the prompt dialect from the same provider selection used at submit."""
    from app import video_providers

    return video_providers.resolve(str(provider).strip().lower()).prompt_profile()


def video_prompt_target_fingerprint(*, provider: str, model: str) -> str:
    profile = resolve_video_prompt_profile(provider=provider, model=model)
    return "|".join((
        str(provider).strip(),
        str(model).strip(),
        profile.profile_id,
        profile.version,
    ))
