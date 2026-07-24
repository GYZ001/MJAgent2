"""领域命令输入模型。同一份 Schema 将生成 REST / Agent Tool / MCP inputSchema。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.capabilities.schemas import StandardCommandInput


class ProjectImportNovelInput(StandardCommandInput):
    attachment_token: str
    name: str | None = None


class ProjectDeleteInput(StandardCommandInput):
    project_id: str


class ProductionAutoStartInput(StandardCommandInput):
    project_id: str
    directory_grant: str | None = None


class ProjectScopedInput(StandardCommandInput):
    project_id: str
    feedback: str = ""


class BibleGenerateInput(StandardCommandInput):
    project_id: str
    feedback: str | None = None


class BibleUpdateInput(StandardCommandInput):
    project_id: str
    bible: dict[str, Any]


class PortraitUpdatePromptInput(StandardCommandInput):
    project_id: str
    character: str
    prompt: str


class PortraitGenerateInput(StandardCommandInput):
    project_id: str
    character: str | None = None


class SceneGenerateRefsInput(StandardCommandInput):
    project_id: str
    scene_name: str | None = None


class SceneUpdatePromptInput(StandardCommandInput):
    project_id: str
    scene_name: str
    prompt: str


class EpisodePlanInput(StandardCommandInput):
    project_id: str
    replace_existing: bool = False


class SelectorInput(StandardCommandInput):
    project_id: str
    selector: dict[str, Any] = Field(default_factory=dict)


class EpisodeScopedInput(StandardCommandInput):
    episode_id: str


class ScreenplayGenerateInput(StandardCommandInput):
    episode_id: str
    force: bool = False


class ScreenplayUpdateInput(StandardCommandInput):
    episode_id: str
    screenplay: dict[str, Any]
    force: bool = False


class ScreenplayCancelInput(StandardCommandInput):
    """单集取消传 ``episode_id``；批量取消传 ``project_id``。"""

    episode_id: str | None = None
    project_id: str | None = None


class StoryboardGenerateInput(StandardCommandInput):
    episode_id: str
    mode: Literal["fresh", "resume"] = "fresh"


class ShotUpdateInput(StandardCommandInput):
    shot_id: str
    patch: dict[str, Any]


class StoryboardConfirmInput(StandardCommandInput):
    episode_id: str


class VideoGenerateShotInput(StandardCommandInput):
    shot_id: str
    prompt_override: str | None = None
    critique: str | None = None
    reroll: bool = False


class ShotScopedInput(StandardCommandInput):
    shot_id: str


class VideoAdoptVersionInput(StandardCommandInput):
    shot_id: str
    version_id: str


class VersionScopedInput(StandardCommandInput):
    version_id: str


class ReferenceReviewInput(StandardCommandInput):
    version_id: str
    ref_id: str
    action: Literal["discard", "restore"]
    override_reason: str | None = None


class DeliveryReviewInput(StandardCommandInput):
    episode_id: str
    package_id: str | None = None
    decision: Literal["approve", "approve_with_risk", "reject"]
    accepted_risk: str | None = None


class DeliveryFeedbackInput(StandardCommandInput):
    episode_id: str
    package_id: str | None = None
    feedback: str
    request_revision: bool = True


class RunControlInput(StandardCommandInput):
    run_id: str
    action: Literal["cancel", "resume", "retry"]


class JobCancelInput(StandardCommandInput):
    job_id: str


class SystemUpdateSettingsInput(StandardCommandInput):
    patch: dict[str, Any]


class SystemModelCreateInput(StandardCommandInput):
    model: dict[str, Any]


class SystemModelUpdateInput(StandardCommandInput):
    model_id: str
    patch: dict[str, Any]


class SystemModelDeleteInput(StandardCommandInput):
    model_id: str


class SystemModelTestInput(StandardCommandInput):
    model_id: str | None = None
    draft: dict[str, Any] | None = None


class SystemEngineInput(StandardCommandInput):
    project_id: str
    enabled: bool


class BenchmarkRunInput(StandardCommandInput):
    payload: dict[str, Any] = Field(default_factory=dict)


class SystemMkdirInput(StandardCommandInput):
    parent_grant: str
    name: str
