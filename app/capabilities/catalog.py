"""PRD §5 能力目录：Resource / Domain Tool / UI / Human-only 全量登记。

M2+M3：每个 CommandSpec 挂载真实领域 Handler（见 ``app.capabilities.handlers``），
Handler 只调用现有 ``app.api`` / ``app.planning`` / ``app.orchestration.*`` /
``app.delivery`` / ``app.worker`` / ``app.system_api`` 函数，
禁止用 httpx 反向回调本机 REST。
"""
from __future__ import annotations

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


def _register_exemptions(registry) -> None:
    """协议/编排/运维入口本身不是领域命令：登记豁免原因，满足覆盖门禁。"""
    registry.exempt_rest(
        "POST /api/system/mcp-tokens",
        "MCP token 生命周期管理是运维端点；Agent/外部 MCP 客户端不能自我签发或升级授权范围",
    )
    registry.exempt_rest(
        "DELETE /api/system/mcp-tokens/{token_id}",
        "同上：token 撤销只能由本机操作者通过监制房页面执行，不进入 Agent/MCP 能力面",
    )
    registry.exempt_rest(
        "POST /api/system/directory-grants",
        "本机人工授权可浏览/建目录根；仅本机会话可写，不向 Agent/MCP 开放",
    )
    registry.exempt_rest(
        "POST /api/auth/login",
        "账号登录是鉴权入口本身：签发会话先于任何账号归属/scope 判定，不经 Command Bus",
    )
    registry.exempt_rest(
        "POST /api/auth/logout",
        "只撤销调用者自己当前的会话，不改变任何制作领域状态",
    )
    registry.exempt_rest(
        "POST /api/auth/change-password",
        "账号自助改密：仅影响操作者自身口令与会话，不是领域命令，不向 Agent/MCP 开放",
    )
    registry.exempt_rest(
        "POST /api/system/users",
        "开户是运维身份管理，不是制作领域命令；仅系统管理员可调用，不向 Agent/MCP 开放",
    )
    registry.exempt_rest(
        "PUT /api/system/users/{user_id}",
        "编辑账号（改密/启停/管理员标记）同上，仅系统管理员可调用",
    )
    registry.exempt_rest(
        "POST /api/agent/conversations",
        "Agent 会话编排入口，不直接改变制作领域状态",
    )
    registry.exempt_rest(
        "POST /api/agent/conversations/{conversation_id}/messages",
        "Agent Turn 编排入口；写操作仍经 Command Bus",
    )
    registry.exempt_rest(
        "POST /api/agent/turns/{turn_id}/cancel",
        "仅取消 Agent Turn；底层 Run 需显式 cancel_run 才走 run.control",
    )
    registry.exempt_rest(
        "POST /api/agent/tool-calls/{tool_call_id}/approve",
        "批准卡协议入口，执行仍走已注册 Domain Tool",
    )
    registry.exempt_rest(
        "POST /api/agent/tool-calls/{tool_call_id}/reject",
        "拒绝卡协议入口，不执行领域命令",
    )
    registry.exempt_rest(
        "POST /api/episodes/{episode_id}/video-completion/reset",
        "生成台死锁解锁：强制停止补齐 Supervisor 并复位面板；不创建新付费任务",
    )
    registry.exempt_rest(
        "POST /api/episodes/{episode_id}/video-completion/repair",
        "遗留事故收口端点：必须先 dry-run 且由本机操作者 confirm=true；只停止任务和采用既有候选，不创建付费任务",
    )
    registry.exempt_rest(
        "POST /api/episodes/{episode_id}/migrate-shot-ids",
        "VAL-422 历史 ID 空间迁移：把误写入 story_event_id 的 S* 迁到 spine_beat_ids；只修合同字段，不启动付费任务",
    )
    registry.exempt_rest(
        "POST /api/shots/{shot_id}/resolve-spoken-conflict",
        "口播合同冲突人工消解：在 dialogues / audio_timeline 间选基准同步；页面直达，不进入 Agent/MCP 能力面",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/bible/impact-preview",
        "人物谱定稿前只读影响预检：不写库、不失效下游；正式定稿仍走 bible.update",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/refs/precheck",
        "定妆照/单视角付费只读预检：返回报价与范围；正式生成仍走 portrait.generate / portrait.regenerate_view",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/bible/generate-precheck",
        "首次生成人物谱+定妆只读费用预估；正式启动仍走 bible.generate",
    )
    registry.exempt_rest(
        "GET /api/projects/{project_id}/refs/gaps",
        "定妆缺口只读扫描",
    )
    registry.exempt_rest(
        "GET /api/projects/{project_id}/refs/progress",
        "定妆进度只读汇总",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/bible/draft",
        "人物谱草稿保存：不定稿、不失效下游",
    )
    registry.exempt_rest(
        "GET /api/projects/{project_id}/bible/draft",
        "读取人物谱草稿",
    )
    registry.exempt_rest(
        "GET /api/projects/{project_id}/auto-changes",
        "自动变更待审队列只读",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/auto-changes/{change_id}/decide",
        "自动变更批准/拒绝/回滚；页面人工决策入口",
    )
    registry.exempt_rest(
        "PUT /api/projects/{project_id}/characters/{character_name}",
        "角色级人物谱保存：局部替换角色对象，内部复用人物谱版本与影响预检",
    )
    registry.exempt_rest(
        "GET /api/projects/{project_id}/characters/{character_name}/portrait-candidates",
        "角色定妆候选只读列表",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/adopt",
        "人物定妆候选人工采纳；页面评审入口，写入 gate/change 决策",
    )
    registry.exempt_rest(
        "POST /api/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/rollback",
        "人物定妆候选人工回滚；页面评审入口，复用采纳切换逻辑",
    )
    for route, reason in {
        "PUT /api/episodes/{episode_id}/screenplay/draft": "映射台会话草稿写入（screenplay_drafts）；仅页面自动保存的未发布编辑内容，不发布、不触发生成、不进入下游；与 Repair 工作文档、已发布剧本无关",
        "DELETE /api/episodes/{episode_id}/screenplay/draft": "映射台会话草稿清理；只删除 screenplay_drafts 中该分集的会话草稿记录，不影响 Repair 工作文档（working_screenplay_artifact_id）与已发布剧本（screenplay_json）",
        "DELETE /api/shots/{shot_id}/drafts/{draft_id}": "分镜编辑会话草稿清理；不改变已发布产物",
        "POST /api/episodes/{episode_id}/confirm-preview": "分集确认前只读影响预览；正式确认仍走已登记命令",
        "POST /api/episodes/{episode_id}/screenplay/preflight": "剧本生成前只读输入范围与人物资产影响预检",
        "POST /api/episodes/{episode_id}/screenplay/impact-preview": "剧本发布前只读影响预览；不写库、不建任务，正式发布仍走 screenplay.update",
        "POST /api/episodes/{episode_id}/storyboard/preflight": "分镜生成前只读预检与短时凭证签发",
        "POST /api/episodes/{episode_id}/storyboard/clear-preview": "整集分镜清空前的影响预览与短时凭证签发；不删除制作数据",
        "POST /api/episodes/{episode_id}/storyboard/clear": "分镜台本机人工清空入口；必须消费当前影响预览凭证，不向 Agent/MCP 开放",
        "POST /api/episodes/{episode_id}/video-model": "分镜台人工切换本集绑定视频模型入口；已有生成产物时必须显式 confirm_clear_prompts 二次确认，且该清空分支要求 manju:project-write（与 video.clear_episode_videos 同档，review/readonly 会被 403 挡下）；无产物的普通切换不受此限，不向 Agent/MCP 开放",
        "POST /api/episodes/{episode_id}/provider-tasks/reconcile": "分镜台清空/切换视频模型撞上 409 PROVIDER_TASKS_NOT_TERMINAL 的人工恢复入口；只做两件事——核对仍疑似在途的供应商任务真实终态（供应商自己确认成功/失败才结算），以及关闭本地证据已证明「从未提交给供应商、所属镜头已有其他成功版本」的孤儿任务；不下载或采用任何结果、不提交新任务、不放宽闸门，要求 manju:project-write（与 video-model 清空分支同档），不向 Agent/MCP 开放",
        "PUT /api/projects/{project_id}/text-models": "世界书/映射台/分镜台分环节专属文本模型的人工切换入口（bible_text_provider/script_text_provider/board_text_provider）；项目级设置，与分镜台「视频模型」的分集级强绑定是两回事——这里只影响之后新发起的该环节生成调用选哪个 provider，不清空已有产出、不需要二次确认。全端点收在 manju:project-write（与 video-model 的清空分支同档，review/readonly 会被 403 挡下），不向 Agent/MCP 开放",
        "POST /api/episodes/{episode_id}/storyboard/structure-preview": "分镜结构变更前只读预览",
        "POST /api/episodes/{episode_id}/storyboard/structure": "分镜台人工结构编辑入口；必须消费页面预览凭证",
        "POST /api/gates/{artifact_id}/decision": "证据门禁的人工决策入口；禁止 Agent 自行代替人类批准",
        "POST /api/projects/{project_id}/observability/gates/{artifact_id}/decision": "项目范围观测台的证据门禁代理；校验项目归属后转发到既有人工决策入口，仍禁止 Agent 代替用户批准",
        "POST /api/projects/{project_id}/observability/jobs/{job_id}/{action}": "项目范围观测台的任务动作代理；校验项目归属后仅转发到已登记的 Run 控制、媒体 Job 取消或本机管理员重试入口",
        "POST /api/projects/{project_id}/observability/runs/{run_id}/{action}": "项目范围观测台的 Run 动作代理；校验项目归属后转发到已登记的 run.control 路由并沿用其批准策略",
        "POST /api/projects/{project_id}/scene-bible/precheck": "场景清单生成前只读付费预检",
        "POST /api/projects/{project_id}/scene-bible/preview": "场景清单与付费范围只读预览",
        "POST /api/projects/{project_id}/scene-refs/precheck": "场景图付费生成前只读预检",
        "POST /api/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate/cancel": "页面定向取消单场景视角任务，不创建新付费作业",
        "POST /api/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/rollback": "场景库历史版本人工回滚；页面评审入口，不向 Agent/MCP 开放",
        "PUT /api/projects/{project_id}/scenes/{scene_name}": "场景库人工元数据修订；页面编辑入口，不自动触发付费出图",
        "POST /api/shots/{shot_id}/edit-session": "分镜编辑租约签发；不改变分镜内容",
        "POST /api/shots/{shot_id}/impact-preview": "分镜修订前只读影响预览",
        "POST /api/shots/{shot_id}/spoken-conflict-preview": "分镜口播冲突处理前只读预览",
        "POST /api/system/jobs/{job_id}/retry": "监制房运维重试入口；只限本机管理员",
        "POST /api/system/monitor/events": "前端监控事件采集入口；只记录遥测，不执行领域动作",
        "POST /api/versions/{version_id}/archive": "生成台人工归档操作；不删除媒体，不向 Agent/MCP 开放",
        "DELETE /api/versions/{version_id}/archive": "生成台人工取消归档操作；不向 Agent/MCP 开放",
        "POST /api/episodes/{episode_id}/video-generation-plan": "整集 AI 模式计划阶段；只生成并校验版本化计划，不创建视频供应商付费任务",
        "POST /api/episodes/{episode_id}/video-generation-plan/validate": "视频模式计划确定性只读复核；不创建或执行媒体任务",
        "POST /api/episodes/{episode_id}/video-generation-plan/reconcile": "采用版本变更后的工具输入绑定与 stale 标记；不创建新付费任务",
        "POST /api/episodes/{episode_id}/video-generation-plan/override": "生成台运营调试的人工覆盖入口；必须记录理由且生成新计划 revision，不向 Agent/MCP 开放",
        "POST /api/video-capabilities/{provider}/{model:path}/probe": "供应商能力真实付费探针；仅本机操作者显式 confirm 后执行，不向 Agent/MCP 开放",
        "POST /api/provider-media-publications": "内部媒体发布协议入口；仅发布项目自有媒体并执行 URL/哈希校验，不作为 Agent 领域工具",
    }.items():
        registry.exempt_rest(route, reason)
    registry.exempt_rest(
        "POST /mcp",
        "MCP JSON-RPC 传输端点；具体 tools/call 映射到 Capability Registry",
    )
