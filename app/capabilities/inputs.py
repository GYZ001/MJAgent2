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
    resume: bool = False


class PortraitViewRegenerateInput(StandardCommandInput):
    project_id: str
    character_name: str
    portrait_id: str
    view_role: str


class SceneGenerateRefsInput(StandardCommandInput):
    project_id: str
    scene_name: str | None = None


class SceneUpdatePromptInput(StandardCommandInput):
    project_id: str
    scene_name: str
    prompt: str


class SceneViewRegenerateInput(StandardCommandInput):
    project_id: str
    scene_name: str
    scene_reference_id: str
    view_role: str


class SceneAdoptCandidateInput(StandardCommandInput):
    project_id: str
    scene_name: str
    artifact_id: str
    reason: str = "人工采纳候选"


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
    force: bool = False  # 已废弃：存在 published 时服务端拒绝全量重生
    required_dialogue_lines: list[str] = Field(default_factory=list)


class ScreenplayResumeInput(StandardCommandInput):
    episode_id: str


class ScreenplayReviseInput(StandardCommandInput):
    episode_id: str
    instruction: str = ""


class ScreenplayDeleteInput(StandardCommandInput):
    episode_id: str


class ScreenplayPatchInput(StandardCommandInput):
    episode_id: str
    production_revision_id: str
    expected_artifact_id: str
    expected_hash: str
    issue_set_hash: str = ""
    operations: list[dict[str, Any]]
    idempotency_key: str = ""
    reason: str = ""


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
    # fresh 已废弃：映射为创建/恢复 production revision，禁止删光后整版重做
    mode: Literal["create", "resume", "fresh"] = "create"
    completion_mode: Literal[
        "ready_for_manual_confirm",
        "auto_confirm",
    ] = "ready_for_manual_confirm"
    completion_grant_id: str | None = None


class StoryboardPatchShotInput(StandardCommandInput):
    episode_id: str
    shot_uid: str | None = None
    shot_no: int | None = None
    patch: dict[str, Any]
    expected_hash: str = ""
    production_revision_id: str = ""
    idempotency_key: str = ""


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


class VideoCompleteEpisodeInput(StandardCommandInput):
    episode_id: str
    mode: Literal["fresh", "resume"] = "fresh"
    budget_cap_cny: float | None = None
    wall_clock_cap_s: float | None = None
    allow_fallback_adopt: bool = True
    max_fallback_shots: int | None = None
    allow_storyboard_edit: bool = False
    completion_grant_id: str | None = None
    add_budget_cny: float | None = None
    add_wall_clock_s: float | None = None


class VideoCompleteProjectInput(StandardCommandInput):
    project_id: str
    episode_ids: list[str] | None = None
    global_budget_cap_cny: float | None = None
    per_episode_cap_cny: float | None = None
    wall_clock_cap_s: float | None = None
    allow_fallback_adopt: bool = True
    allow_storyboard_edit: bool = False


class ShotScopedInput(StandardCommandInput):
    shot_id: str


class VideoAdoptVersionInput(StandardCommandInput):
    shot_id: str
    version_id: str


class VideoRepairStaleAssetsInput(StandardCommandInput):
    episode_id: str
    shot_ids: list[str] = Field(default_factory=list)
    confirm: bool = False


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
    action: Literal["cancel", "resume", "retry", "pause", "handoff"]


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
