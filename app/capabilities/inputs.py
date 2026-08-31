"""领域命令输入模型。同一份 Schema 将生成 REST / Agent Tool / MCP inputSchema。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.capabilities.schemas import StandardCommandInput


class ProjectImportNovelInput(StandardCommandInput):
    attachment_token: str
    name: str | None = None
    #: 统一画风预设名（app.visual_styles.VISUAL_STYLE_PRESETS 之一）；省略时
    #: 世界观判定按 DEFAULT_VISUAL_STYLE_NAME 兜底。2026-08-31 用户拍板：画风
    #: 选择挪到导入项目时一次性定下，人物谱/场景库不再提供换风格入口。
    style_name: str | None = None


class ProjectDeleteInput(StandardCommandInput):
    project_id: str


class ProjectRestoreInput(StandardCommandInput):
    project_id: str


class ProjectPurgeInput(StandardCommandInput):
    project_id: str


class ProjectPurgeAllInput(StandardCommandInput):
    pass


class AccountSelfDeleteInput(StandardCommandInput):
    """自删不带参数：目标账号取自当前会话 Principal，不接受调用方指定别的账号。"""


class AccountAdminDeleteInput(StandardCommandInput):
    user_id: str


class AccountAdminRestoreInput(StandardCommandInput):
    user_id: str


class QuotaGrantVideoAddonInput(StandardCommandInput):
    """管理员手工发放视频加量包。

    ``packages`` 是包数（每包 ``ADDON_PACKAGE_SECONDS`` 秒），不是秒数——单位
    写在名字里，避免调用方把 600 当成"600 包"传进来。``idempotency_key`` 未来
    接真实支付时应传订单号；留空时路由层生成一次性 key，重复调用会重复发放。
    """

    user_id: str
    packages: int
    idempotency_key: str = ""


class ProjectScopedInput(StandardCommandInput):
    project_id: str
    feedback: str = ""


class BibleGenerateInput(StandardCommandInput):
    project_id: str
    feedback: str | None = None
    style_name: str | None = None
    confirm: bool = False
    quote_id: str | None = None
    require_quote_id: bool = False


class BibleUpdateInput(StandardCommandInput):
    project_id: str
    bible: dict[str, Any]
    confirm: bool = False
    impact_preview_fingerprint: str | None = None


class BibleSetStyleInput(StandardCommandInput):
    project_id: str
    style_name: str
    confirm: bool = False
    quote_id: str | None = None


class CharacterNominateInput(StandardCommandInput):
    """用户提名一个原文称呼（人物谱页"没被选上的角色"手动入口）。

    ``from_episode_no`` 是原文证据检索的起点集数，省略时端点按 1 处理——见
    ``app.domain.bible_ops.nominate`` 模块 docstring；本模型里保持 ``int | None``
    是为了让调用方可以显式省略，实际归一化发生在领域函数里，不在这里塞默认值
    逻辑（配套参数/归一化只应有一处权威实现）。
    """

    project_id: str
    label: str
    from_episode_no: int | None = None


class PortraitUpdatePromptInput(StandardCommandInput):
    project_id: str
    character: str
    prompt: str


class PortraitGenerateInput(StandardCommandInput):
    project_id: str
    character: str | None = None
    characters: list[str] | None = None
    resume: bool = False
    confirm: bool = False
    quote_id: str | None = None


class PortraitViewRegenerateInput(StandardCommandInput):
    project_id: str
    character_name: str
    portrait_id: str
    view_role: str
    confirm: bool = False
    quote_id: str | None = None


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


class VideoGenerateEpisodeInput(EpisodeScopedInput):
    """整集生成视频（``video.generate_episode``）专用输入。

    ``EpisodeScopedInput`` 被 storyboard.cancel / video.stop_episode /
    video.clear_episode / video.clear_episode_videos / video.resume_episode /
    delivery.concatenate / delivery.check 共 6 个命令复用（``delivery.create_package``
    另有 ``DeliveryCreatePackageInput`` 专用子类），其 Schema 会原样生成那些命令的
    REST/Agent Tool/MCP inputSchema——直接往 ``EpisodeScopedInput`` 上加
    ``only_incomplete``/``qualification_version``/``plan_id`` 会把这些只对本命令
    有意义的字段污染进那 6 个不相关命令的对外契约，所以在这里新建一个专用子类，
    只挂在 ``video.generate_episode`` 一个命令上。

    ``extra="forbid"``：REST 包装层与 handler 都必须显式声明要转发的字段，
    命令总线看不到的参数在这里会直接报错而不是被 pydantic 默认的
    ``extra="ignore"`` 静默吞掉——这正是本次要修的“丢参数”问题的同类保险丝。
    （``StandardCommandInput`` 现已全局 ``extra="forbid"``，这里保留显式声明
    只是历史沿革，不再是唯一保险丝。）
    """

    model_config = ConfigDict(extra="forbid")

    only_incomplete: bool = False
    qualification_version: str | None = None
    # 仅 POST /episodes/{id}/video-generation-plan/{plan_id}/execute 这条路由填充；
    # 直接命中 /generate 的请求不带这个字段。非空时 handler 会把它转交给
    # api._generate_episode_core 做「请求执行的计划就是当前有效 revision」的
    # 权威复核（而不是只在 REST 层做一次事后 TOCTOU 检查）。
    plan_id: str | None = None


class ScreenplayGenerateInput(StandardCommandInput):
    episode_id: str


class ScreenplayResumeInput(StandardCommandInput):
    episode_id: str


class ScreenplayRepairDraftInput(StandardCommandInput):
    episode_id: str
    screenplay: dict[str, Any]


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


class ScreenplayCancelInput(StandardCommandInput):
    """单集取消传 ``episode_id``；批量取消传 ``project_id``。"""

    episode_id: str | None = None
    project_id: str | None = None


class StoryboardGenerateInput(StandardCommandInput):
    episode_id: str
    preflight_token: str | None = None


class ShotUpdateInput(StandardCommandInput):
    shot_id: str
    patch: dict[str, Any]
    expected_version: str | None = None
    edit_session_token: str | None = None
    preview_token: str | None = None
    baseline_content_hash: str | None = None
    change_source: str = "standard_edit"
    source_binding: dict[str, Any] | None = None


class StoryboardConfirmInput(StandardCommandInput):
    episode_id: str
    preview_token: str | None = None


class VideoGenerateShotInput(StandardCommandInput):
    shot_id: str
    prompt_override: str | None = None
    reroll: bool = False
    qualification_version: str | None = None


class VideoCompleteEpisodeInput(StandardCommandInput):
    episode_id: str
    mode: Literal["fresh", "resume"] = "fresh"
    budget_cap_cny: float | None = Field(default=None, ge=1, le=100000, allow_inf_nan=False)
    wall_clock_cap_s: float | None = Field(default=None, ge=60, le=604800, allow_inf_nan=False)
    allow_fallback_adopt: bool = True
    max_fallback_shots: int | None = Field(default=None, ge=0, le=10000)
    allow_storyboard_edit: bool = False
    completion_grant_id: str | None = None
    add_budget_cny: float | None = Field(default=None, ge=1, le=100000, allow_inf_nan=False)
    add_wall_clock_s: float | None = Field(default=None, ge=60, le=604800, allow_inf_nan=False)
    qualification_version: str | None = None


class VideoCompleteProjectInput(StandardCommandInput):
    project_id: str
    episode_ids: list[str] | None = None
    global_budget_cap_cny: float | None = Field(default=None, ge=1, le=1000000, allow_inf_nan=False)
    per_episode_cap_cny: float | None = Field(default=None, ge=1, le=100000, allow_inf_nan=False)
    wall_clock_cap_s: float | None = Field(default=None, ge=60, le=604800, allow_inf_nan=False)
    allow_fallback_adopt: bool = True
    allow_storyboard_edit: bool = False


class ShotScopedInput(StandardCommandInput):
    shot_id: str


class VideoAdoptVersionInput(StandardCommandInput):
    shot_id: str
    version_id: str
    qualification_version: str | None = None
    playback_rate: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)


class VideoRepairStaleAssetsInput(StandardCommandInput):
    episode_id: str
    shot_ids: list[str] = Field(default_factory=list)
    confirm: bool = False
    preview_version: str | None = None
    qualification_version: str | None = None


class VersionScopedInput(StandardCommandInput):
    version_id: str


class ReferenceReviewInput(StandardCommandInput):
    version_id: str
    ref_id: str
    action: Literal["discard", "restore"]
    override_reason: str | None = None


class DeliveryCreatePackageInput(EpisodeScopedInput):
    """生成交付候选（``delivery.create_package``）专用输入。

    ``package_id`` 只在客户端重放「已校验过的交付包 id」时才有意义（对应
    ``app.orchestration.api.create_delivery_package`` 里 payload.get("package_id")
    的续跑分支）；省略时按 sha256(episode_id + idempotency_key) 确定性重算。
    ``EpisodeScopedInput`` 还被 storyboard.cancel / video.stop_episode /
    video.clear_episode / video.clear_episode_videos / video.resume_episode /
    delivery.concatenate / delivery.check 共 6 个命令复用，往共享基类加这个字段
    会把它污染进那些不相关命令的对外契约，所以单独建子类。

    ``reason``（写入交付包 quality-report 的说明性文本）继承自
    ``StandardCommandInput``，不需要在这里重复声明；REST 包装层与本命令的
    handler 都必须显式转发它——历史上两层都手写 dict 重建请求体，各自漏了
    一次，效果等同完全丢弃。

    故意没有 ``decided_by`` 字段：build_delivery_package 阶段 decision 恒为
    None（这不是审批动作），但 decided_by 仍写入 WorkflowRecorder.requested_by
    与 quality-report 的 human_decision，属于审计相邻字段。这类字段一律不接受
    客户端自报——``app.orchestration.api.create_delivery_package`` 改为用
    ``app.auth.principal.current_actor_name()`` 从已鉴权身份派生，与
    ``approve_delivery`` 的真正审批 decided_by 已经在用的机制一致。
    """

    package_id: str | None = None


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
    allow_new_submission: bool = False


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
