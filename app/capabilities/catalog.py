"""PRD §5 能力目录：Resource / Domain Tool / UI / Human-only 全量登记。

M2+M3：每个 CommandSpec 挂载真实领域 Handler（见 ``app.capabilities.handlers``），
Handler 只调用现有 ``app.api`` / ``app.planning`` / ``app.orchestration.*`` /
``app.delivery`` / ``app.worker`` / ``app.system_api`` 函数，
禁止用 httpx 反向回调本机 REST。
"""
from __future__ import annotations

from app.capabilities.exemptions import _register_exemptions
from app.capabilities.commands import account as cd_account
from app.capabilities.commands import bible as cd_bible
from app.capabilities.commands import delivery as cd_delivery
from app.capabilities.commands import episode as cd_episode
from app.capabilities.commands import project as cd_project
from app.capabilities.commands import run as cd_run
from app.capabilities.commands import scene as cd_scene
from app.capabilities.commands import screenplay as cd_screenplay
from app.capabilities.commands import storyboard as cd_storyboard
from app.capabilities.commands import system as cd_system
from app.capabilities.commands import video as cd_video
from app.capabilities.registry import HumanOnlySpec, ResourceSpec, UiIntentSpec, get_registry

# 领域顺序沿用原 catalog.py `_register_commands` 里各区块出现的先后次序
# （项目 -> 账号删除/配额 -> 人物谱/定妆 -> 场景 -> 分集规划 -> 剧本 -> 分镜 ->
# 视频/参考图 -> 交付 -> Run/Job -> 系统），纯粹为了让 diff 好读，注册顺序本身
# 对 CommandSpec 的唯一性没有影响（registry 按 name 去重）。
_COMMAND_DOMAINS = (
    cd_project,
    cd_account,
    cd_bible,
    cd_scene,
    cd_episode,
    cd_screenplay,
    cd_storyboard,
    cd_video,
    cd_delivery,
    cd_run,
    cd_system,
)

_REGISTERED = False


def ensure_registered() -> None:
    global _REGISTERED
    if _REGISTERED:
        _bind_handlers(get_registry())
        return
    registry = get_registry()
    _register_resources(registry)
    _register_ui(registry)
    _register_human_only(registry)
    _register_commands(registry)
    _register_exemptions(registry)
    _bind_handlers(registry)
    _REGISTERED = True


def _bind_handlers(registry) -> None:
    """仅补挂仍缺 handler / preflight 的命令；不覆盖 catalog 已声明的实现。

    ``domain.HANDLER_MAP`` 已掏空，仅作测试补丁兼容；生产 handler 一律在 ``_register_commands`` 声明。
    """
    from dataclasses import replace

    from app.capabilities.handlers.domain import HANDLER_MAP
    from app.capabilities.preflight import PREFLIGHT_MAP

    for name, handler in HANDLER_MAP.items():
        spec = registry.commands.get(name)
        if spec is None or spec.handler is not None:
            continue
        registry.commands[name] = replace(spec, handler=handler)

    for name, preflight_fn in PREFLIGHT_MAP.items():
        spec = registry.commands.get(name)
        if spec is None or spec.preflight is not None:
            continue
        registry.commands[name] = replace(spec, preflight=preflight_fn)


def _register_resources(registry) -> None:
    specs = [
        ResourceSpec("projects", "manju://projects", "项目列表", "列出本地项目", rest_routes=("GET /api/projects",)),
        ResourceSpec(
            "project",
            "manju://projects/{project_id}",
            "项目快照",
            "单个项目状态与摘要",
            rest_routes=("GET /api/projects/{project_id}",),
        ),
        ResourceSpec(
            "chapter",
            "manju://projects/{project_id}/chapters/{idx}",
            "章节正文",
            "按章节读取原著（分页/单章，不整本塞入上下文）",
            rest_routes=("GET /api/projects/{project_id}/chapters/{idx}",),
        ),
        ResourceSpec(
            "bible",
            "manju://projects/{project_id}/bible",
            "人物谱",
            "项目人物谱与定妆状态（当前嵌在项目快照中，M1 可拆专用 Resource 读取器）",
        ),
        ResourceSpec(
            "character_portraits",
            "manju://projects/{project_id}/characters/{name}/portraits",
            "角色定妆历史",
            "角色定妆候选与历史版本",
        ),
        ResourceSpec(
            "scenes",
            "manju://projects/{project_id}/scenes",
            "场景库",
            "场景圣经与场景图状态",
        ),
        ResourceSpec(
            "episodes",
            "manju://projects/{project_id}/episodes",
            "分集列表",
            "项目剧集与制作进度（当前嵌在项目快照中）",
        ),
        ResourceSpec(
            "episode",
            "manju://episodes/{episode_id}",
            "剧集快照",
            "单集状态、剧本与分镜摘要",
            rest_routes=("GET /api/episodes/{episode_id}",),
        ),
        ResourceSpec(
            "screenplay",
            "manju://episodes/{episode_id}/screenplay",
            "剧本",
            "单集已发布剧本文本与版本",
        ),
        ResourceSpec(
            "screenplay_working",
            "manju://episodes/{episode_id}/screenplay/working",
            "剧本工作文档",
            "Repair 环节的服务端工作文档（working Artifact，由 working_screenplay_artifact_id 指向）；供局部修补，不可作为页面交付，既非页面会话草稿（screenplay_drafts）也非已发布剧本（screenplay_json）",
        ),
        ResourceSpec(
            "storyboard",
            "manju://episodes/{episode_id}/storyboard",
            "分镜",
            "单集已发布分镜镜头列表",
        ),
        ResourceSpec(
            "storyboard_working",
            "manju://episodes/{episode_id}/storyboard/working",
            "分镜工作副本",
            "Supervisor 工作镜头集与进度（不可确认/进视频）",
        ),
        ResourceSpec(
            "run_issues",
            "manju://runs/{run_id}/issues",
            "Run Issue 集",
            "未解决结构化 Issue",
        ),
        ResourceSpec(
            "run_patches",
            "manju://runs/{run_id}/patches",
            "Run Patch 历史",
            "局部修补与 diff 证据",
        ),
        ResourceSpec(
            "artifact_certificate",
            "manju://artifacts/{artifact_id}/certificate",
            "完成凭证",
            "绑定精确 Artifact hash 的 Completion Certificate",
        ),
        ResourceSpec(
            "shot",
            "manju://shots/{shot_id}",
            "镜头",
            "单镜字段、版本与媒体状态",
        ),
        ResourceSpec(
            "run",
            "manju://runs/{run_id}",
            "Workflow Run",
            "Run 状态、步骤与关联产物",
            rest_routes=("GET /api/runs/{run_id}",),
        ),
        ResourceSpec(
            "run_events",
            "manju://runs/{run_id}/events",
            "Run 事件流",
            "Run 事件列表（可分页）",
            rest_routes=("GET /api/runs/{run_id}/events",),
        ),
        ResourceSpec(
            "artifact",
            "manju://artifacts/{artifact_id}",
            "Artifact",
            "产物内容与可信等级",
            rest_routes=("GET /api/artifacts/{artifact_id}",),
        ),
        ResourceSpec(
            "artifact_lineage",
            "manju://artifacts/{artifact_id}/lineage",
            "Artifact 血缘",
            "产物上下游血缘",
            rest_routes=("GET /api/artifacts/{artifact_id}/lineage",),
        ),
        ResourceSpec(
            "delivery",
            "manju://episodes/{episode_id}/delivery",
            "交付状态",
            "拼接、readiness 与交付包",
            rest_routes=(
                "GET /api/episodes/{episode_id}/delivery/readiness",
                "GET /api/episodes/{episode_id}/delivery/packages",
                "GET /api/episodes/{episode_id}/mix-status",
            ),
        ),
        ResourceSpec(
            "system_health",
            "manju://system/health",
            "系统健康",
            "Jobs/Calls/Health 脱敏视图",
            rest_routes=(
                "GET /api/system/health",
                "GET /api/system/jobs",
                "GET /api/system/calls",
            ),
        ),
        ResourceSpec(
            "gates",
            "manju://gates",
            "人工门禁",
            "待审批 Gate 列表",
            rest_routes=("GET /api/gates",),
        ),
    ]
    for spec in specs:
        registry.register_resource(spec)


def _register_ui(registry) -> None:
    for spec in [
        UiIntentSpec("ui.navigate", "页面导航", "跳转到白名单工作台视图", "navigate", tags=("ui",)),
        UiIntentSpec("ui.select_shot", "选中镜头", "在分镜/生成台选中指定镜头", "select_shot", tags=("ui",)),
        UiIntentSpec("ui.select_version", "选中版本", "打开指定视频版本比较/预览", "select_version", tags=("ui",)),
        UiIntentSpec("ui.open_evidence", "打开证据", "打开 Artifact / lineage 证据抽屉", "open_evidence", tags=("ui",)),
        UiIntentSpec("ui.open_delivery", "打开交付页", "定位成片台指定 Tab", "open_delivery", tags=("ui",)),
        UiIntentSpec("ui.open_download", "打开下载", "构造本站 allowlist 下载路径，由用户端完成", "open_download", tags=("ui",)),
        UiIntentSpec("ui.preview", "预览媒体", "打开只读预览（图片/视频）", "preview", tags=("ui",)),
        UiIntentSpec(
            "ui.request_directory_grant",
            "请求目录授权",
            "引导用户亲自选择/授权导出目录",
            "request_directory_grant",
            tags=("ui", "human"),
        ),
        UiIntentSpec(
            "ui.open_credentials",
            "打开密钥表单",
            "导航到模型凭证专用表单，不把密钥交给模型",
            "open_credentials",
            tags=("ui", "human"),
        ),
    ]:
        registry.register_ui(spec)


def _register_human_only(registry) -> None:
    for spec in [
        HumanOnlySpec(
            "human.select_upload_file",
            "选择上传小说文件",
            "用户在系统文件选择器中挑选 TXT 或 EPUB；前端换发短时效 attachment_token",
            reason="禁止把任意 file_path 暴露给 Agent",
            rest_routes=("POST /api/attachments/novel",),
            tags=("human", "project"),
        ),
        HumanOnlySpec(
            "human.provide_api_key",
            "填写模型 API Key",
            "用户在专用 password input 提交密钥",
            reason="API Key 永不进入模型上下文、对话状态或可见日志",
            related_ui_intent="ui.open_credentials",
            rest_routes=("PUT /api/keys", "PUT /api/models/{model_id}/credentials"),
            tags=("human", "secret"),
        ),
        HumanOnlySpec(
            "human.grant_export_directory",
            "授权导出目录",
            "用户亲自选择本机导出目录",
            reason="Agent 不得浏览任意文件系统",
            related_ui_intent="ui.request_directory_grant",
            tags=("human", "filesystem"),
        ),
        HumanOnlySpec(
            "human.choose_episode_target_duration",
            "选择单集目标时长",
            "用户在首版剧本生成前确认单集节奏预算",
            reason="这是会改变剧作节奏与后续成本的创作取舍，当前仅允许用户在前端显式选择",
            rest_routes=("PUT /api/episodes/{episode_id}/target-duration",),
            tags=("human", "episode", "screenplay"),
        ),
        HumanOnlySpec(
            "human.delete_episode",
            "删除单集",
            "用户在分集规划中确认永久删除一集及其全部下游制作内容",
            reason="单集删除不可撤销，且属于用户对原著改编范围的人工取舍",
            rest_routes=("DELETE /api/episodes/{episode_id}",),
            tags=("human", "episode", "destructive"),
        ),
    ]:
        registry.register_human_only(spec)


def _register_commands(registry) -> None:
    """按领域汇总 ``app.capabilities.commands.*`` 里声明的 CommandSpec 并注册。

    每个领域模块（project/account/bible/scene/episode/screenplay/storyboard/
    video/delivery/run/system）只负责声明自己那批 CommandSpec，不接触
    registry——这条函数是唯一调用 ``registry.register_command()`` 的地方，
    与原 catalog.py 里 `_register_commands` 单函数注册全部命令的行为完全一致。
    """
    for domain in _COMMAND_DOMAINS:
        for spec in domain.commands():
            registry.register_command(spec)


