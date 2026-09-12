"""能力覆盖闸门的豁免登记：协议/编排/运维/渠道回调入口本身不是领域命令。

从 ``catalog.py`` 抽出（2026-08-30）：加完支付的 4 条豁免后该文件 516/500 行、
``_register_exemptions`` 188/164 行，两条都撞线。按 CLAUDE.md「装不下时先想
怎么拆，不要先想加基线」——豁免和命令一样是**声明式清单**，而命令早已拆进
``app/capabilities/commands/`` 包，这里照同一个分法。

每条豁免必须写明**为什么它不是领域命令**，不能只写"不需要"。这份清单是
``scripts/check_capability_coverage.py`` 唯一承认的"已知且有意不分类"来源；
写不出理由的端点就该老老实实注册成命令，而不是塞进这里。

⚠️ 切分按 AST 的顶层语句边界做，不按正则抓 ``registry.exempt_rest(...)``
调用——本文件中段有一个 ``for route, reason in {...}.items():`` 的表驱动循环，
按调用抓会把循环体抽走、把表和循环头丢掉，产出一句引用未定义名字的裸调用
（2026-08-30 第一次拆分就是这么错的，NameError 当场炸出来）。

EP-01 第三阶段（2026-09-12）：``exempt_rest(route, reason)`` 扩成
``exempt_rest(route, reason, *, scopes, admin_only=False)``，值从纯字符串
理由变成 ``_RouteExemption`` 记录，分类判据全部来自路由自身的结构事实
（挂哪个依赖、方法读写、是否 payments 公开回调），不猜路由名，详见交付报告。
"""
from __future__ import annotations

from typing import NamedTuple


class _RouteExemption(NamedTuple):
    """豁免路由登记：理由 + 角色治理元数据（EP-01 第三阶段新增）。

    ``scopes`` 复用 ``ALL_SCOPES`` 词汇表，额外加一个不在其中的哨兵值
    ``manju:public``——标注"这条路由的 router 没挂 require_project_owner_access，
    route_allowed() 结构上到不了这里"，不能靠"碰巧没被拦"。``admin_only`` 与
    ``CommandSpec.admin_only`` 同语义："只放行 is_system_admin"；组织治理端点
    （团队/角色/授权/配额）限定的是 org_admin 角色，不是 is_system_admin，因此
    仍是 ``admin_only=False``，靠 ``scopes={"manju:admin"}`` 只被 org_admin/owner
    模板收进权限点集合。
    """

    reason: str
    scopes: frozenset[str]
    admin_only: bool = False


#: 见 _RouteExemption 文档：与 CommandSpec.scopes 同词汇表 + 一个哨兵值。
_ADMIN = frozenset({"manju:admin"})
_PUBLIC = frozenset({"manju:public"})
_READ = frozenset({"manju:read"})
_WRITE = frozenset({"manju:project-write"})
_DECIDE = frozenset({"manju:media-decide"})
_GENERATE = frozenset({"manju:media-generate"})


#: 路由 -> 豁免登记（理由 + scopes/admin_only）。整份清单是**数据**，不是散落在若干函数里的调用——
#: 它们本来就形状完全相同（``registry.exempt_rest(路由, 理由, scopes=..., admin_only=...)``），按域拆成
#: 多个函数只是把同一张表切碎，既不降低复杂度，也让"这条路由豁免了吗"变成
#: 要翻几个函数才答得上来的问题。做成常量后 function_lines 也不再惩罚它
#: （同 app/quota_tiers.py 把档位表从配额引擎里拆出来的道理）。
#:
#: 每条必须写明**为什么它不是领域命令**，不能只写"不需要"。这是
#: ``scripts/check_capability_coverage.py`` 唯一承认的"已知且有意不分类"来源；
#: 写不出理由的端点就该老实注册成命令，而不是塞进这里。
EXEMPT_ROUTE_PERMISSIONS: dict[str, _RouteExemption] = {
    "PUT /api/episodes/{episode_id}/screenplay/draft": _RouteExemption(
        "映射台会话草稿写入（screenplay_drafts）；仅页面自动保存的未发布编辑内容，不发布、不触发生成、不进入下游；与 Repair 工作文档、已发布剧本无关",
        _WRITE,
    ),
    "DELETE /api/episodes/{episode_id}/screenplay/draft": _RouteExemption(
        "映射台会话草稿清理；只删除 screenplay_drafts 中该分集的会话草稿记录，不影响 Repair 工作文档（working_screenplay_artifact_id）与已发布剧本（screenplay_json）",
        _WRITE,
    ),
    "DELETE /api/shots/{shot_id}/drafts/{draft_id}": _RouteExemption(
        "分镜编辑会话草稿清理；不改变已发布产物",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/confirm-preview": _RouteExemption(
        "分集确认前只读影响预览；正式确认仍走已登记命令",
        _DECIDE,
    ),
    "POST /api/episodes/{episode_id}/screenplay/preflight": _RouteExemption(
        "剧本生成前只读输入范围与人物资产影响预检",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/screenplay/impact-preview": _RouteExemption(
        "剧本发布前只读影响预览；不写库、不建任务，正式发布仍走 screenplay.update",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/storyboard/preflight": _RouteExemption(
        "分镜生成前只读预检与短时凭证签发",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/storyboard/clear-preview": _RouteExemption(
        "整集分镜清空前的影响预览与短时凭证签发；不删除制作数据",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/storyboard/clear": _RouteExemption(
        "分镜台本机人工清空入口；必须消费当前影响预览凭证，不向 Agent/MCP 开放",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/video-model": _RouteExemption(
        "分镜台人工切换本集绑定视频模型入口；已有生成产物时必须显式 confirm_clear_prompts 二次确认，且该清空分支要求 manju:project-write（与 video.clear_episode_videos 同档，review/readonly 会被 403 挡下）；无产物的普通切换不受此限，不向 Agent/MCP 开放",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/provider-tasks/reconcile": _RouteExemption(
        "分镜台清空/切换视频模型撞上 409 PROVIDER_TASKS_NOT_TERMINAL 的人工恢复入口；只做两件事——核对仍疑似在途的供应商任务真实终态（供应商自己确认成功/失败才结算），以及关闭本地证据已证明「从未提交给供应商、所属镜头已有其他成功版本」的孤儿任务；不下载或采用任何结果、不提交新任务、不放宽闸门，要求 manju:project-write（与 video-model 清空分支同档），不向 Agent/MCP 开放",
        _WRITE,
    ),
    "PUT /api/projects/{project_id}/text-models": _RouteExemption(
        "世界书/映射台/分镜台分环节专属文本模型的人工切换入口（bible_text_provider/script_text_provider/board_text_provider）；项目级设置，与分镜台「视频模型」的分集级强绑定是两回事——这里只影响之后新发起的该环节生成调用选哪个 provider，不清空已有产出、不需要二次确认。全端点收在 manju:project-write（与 video-model 的清空分支同档，review/readonly 会被 403 挡下），不向 Agent/MCP 开放",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/storyboard/structure-preview": _RouteExemption(
        "分镜结构变更前只读预览",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/storyboard/structure": _RouteExemption(
        "分镜台人工结构编辑入口；必须消费页面预览凭证",
        _WRITE,
    ),
    "POST /api/gates/{artifact_id}/decision": _RouteExemption(
        "证据门禁的人工决策入口；禁止 Agent 自行代替人类批准",
        _DECIDE,
    ),
    "POST /api/projects/{project_id}/observability/gates/{artifact_id}/decision": _RouteExemption(
        "项目范围观测台的证据门禁代理；校验项目归属后转发到既有人工决策入口，仍禁止 Agent 代替用户批准",
        _ADMIN, admin_only=True,
    ),
    "POST /api/projects/{project_id}/observability/jobs/{job_id}/{action}": _RouteExemption(
        "项目范围观测台的任务动作代理；校验项目归属后仅转发到已登记的 Run 控制、媒体 Job 取消或本机管理员重试入口",
        _ADMIN, admin_only=True,
    ),
    "POST /api/projects/{project_id}/observability/runs/{run_id}/{action}": _RouteExemption(
        "项目范围观测台的 Run 动作代理；校验项目归属后转发到已登记的 run.control 路由并沿用其批准策略",
        _ADMIN, admin_only=True,
    ),
    "POST /api/projects/{project_id}/scene-bible/precheck": _RouteExemption(
        "场景清单生成前只读付费预检",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/scene-bible/preview": _RouteExemption(
        "场景清单与付费范围只读预览",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/scene-refs/precheck": _RouteExemption(
        "场景图付费生成前只读预检",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate/cancel": _RouteExemption(
        "页面定向取消单场景视角任务，不创建新付费作业",
        _DECIDE,
    ),
    "POST /api/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/rollback": _RouteExemption(
        "场景库历史版本人工回滚；页面评审入口，不向 Agent/MCP 开放",
        _DECIDE,
    ),
    "PUT /api/projects/{project_id}/scenes/{scene_name}": _RouteExemption(
        "场景库人工元数据修订；页面编辑入口，不自动触发付费出图",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/props/{name}/regenerate": _RouteExemption(
        "物件库人工重生成单件道具参考图；页面评审入口，不向 Agent/MCP 开放",
        _GENERATE,
    ),
    "POST /api/shots/{shot_id}/edit-session": _RouteExemption(
        "分镜编辑租约签发；不改变分镜内容",
        _WRITE,
    ),
    "POST /api/shots/{shot_id}/impact-preview": _RouteExemption(
        "分镜修订前只读影响预览",
        _WRITE,
    ),
    "POST /api/shots/{shot_id}/spoken-conflict-preview": _RouteExemption(
        "分镜口播冲突处理前只读预览",
        _WRITE,
    ),
    "POST /api/system/jobs/{job_id}/retry": _RouteExemption(
        "监制房运维重试入口；只限本机管理员",
        _ADMIN, admin_only=True,
    ),
    "POST /api/system/provider-tasks/zero-cost-release": _RouteExemption(
        "供应商任务已终态拒绝、且本地证据已证明零扣费（或所用视频模型已声明不产生真实账单）的预留释放入口；只做把预留结算为 0 这一件事，不提交新任务、不放宽 PROVIDER_TASKS_NOT_TERMINAL 闸门本身的判据，本地二段式确认（?confirm=true），只限本机系统管理员，不向 Agent/MCP 开放",
        _ADMIN, admin_only=True,
    ),
    "POST /api/system/monitor/events": _RouteExemption(
        "前端监控事件采集入口；只记录遥测，不执行领域动作",
        _READ,
    ),
    "POST /api/versions/{version_id}/archive": _RouteExemption(
        "生成台人工归档操作；不删除媒体，不向 Agent/MCP 开放",
        _WRITE,
    ),
    "DELETE /api/versions/{version_id}/archive": _RouteExemption(
        "生成台人工取消归档操作；不向 Agent/MCP 开放",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/video-generation-plan": _RouteExemption(
        "整集 AI 模式计划阶段；只生成并校验版本化计划，不创建视频供应商付费任务",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/video-generation-plan/validate": _RouteExemption(
        "视频模式计划确定性只读复核；不创建或执行媒体任务",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/video-generation-plan/reconcile": _RouteExemption(
        "采用版本变更后的工具输入绑定与 stale 标记；不创建新付费任务",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/video-generation-plan/override": _RouteExemption(
        "生成台运营调试的人工覆盖入口；必须记录理由且生成新计划 revision，不向 Agent/MCP 开放",
        _WRITE,
    ),
    "POST /api/video-capabilities/{provider}/{model:path}/probe": _RouteExemption(
        "供应商能力真实付费探针；仅本机操作者显式 confirm 后执行，不向 Agent/MCP 开放"
        "（EP-01 三阶段审查修复：路由曾零鉴权，任何登录用户都能触发真实付费探针；"
        "现挂 require_system_admin，admin_only=True 对齐）",
        _ADMIN, admin_only=True,
    ),
    "POST /api/provider-media-publications": _RouteExemption(
        "内部媒体发布协议入口；仅发布项目自有媒体并执行 URL/哈希校验，不作为 Agent 领域工具"
        "（EP-01 三阶段审查修复：路由曾零鉴权且不校验项目归属；"
        "现挂 require_system_admin，admin_only=True 对齐）",
        _ADMIN, admin_only=True,
    ),
    "POST /api/system/mcp-tokens": _RouteExemption(
        "MCP token 生命周期管理是运维端点；Agent/外部 MCP 客户端不能自我签发或升级授权范围"
        "（EP-01 三阶段审查修复：路由曾零鉴权，任何登录用户可自签 manju:admin token 提权；"
        "现挂 require_system_admin，admin_only=True 对齐，与 DELETE 同档）",
        _ADMIN, admin_only=True,
    ),
    "DELETE /api/system/mcp-tokens/{token_id}": _RouteExemption(
        "同上：token 撤销只能由本机操作者通过监制房页面执行，不进入 Agent/MCP 能力面",
        _ADMIN, admin_only=True,
    ),
    "POST /api/system/users": _RouteExemption(
        "开户是运维身份管理，不是制作领域命令；仅系统管理员可调用，不向 Agent/MCP 开放",
        _ADMIN, admin_only=True,
    ),
    "PUT /api/system/users/{user_id}": _RouteExemption(
        "编辑账号（改密/启停/管理员标记）同上，仅系统管理员可调用",
        _ADMIN, admin_only=True,
    ),
    "POST /api/auth/login": _RouteExemption(
        "账号登录是鉴权入口本身：签发会话先于任何账号归属/scope 判定，不经 Command Bus",
        _PUBLIC,
    ),
    "POST /api/auth/logout": _RouteExemption(
        "只撤销调用者自己当前的会话，不改变任何制作领域状态",
        _PUBLIC,
    ),
    "POST /api/auth/change-password": _RouteExemption(
        "账号自助改密：仅影响操作者自身口令与会话，不是领域命令，不向 Agent/MCP 开放",
        _PUBLIC,
    ),
    "POST /api/agent/conversations": _RouteExemption(
        "Agent 会话编排入口，不直接改变制作领域状态",
        _WRITE,
    ),
    "POST /api/agent/conversations/{conversation_id}/messages": _RouteExemption(
        "Agent Turn 编排入口；写操作仍经 Command Bus",
        _WRITE,
    ),
    "POST /api/agent/turns/{turn_id}/cancel": _RouteExemption(
        "仅取消 Agent Turn；底层 Run 需显式 cancel_run 才走 run.control",
        _WRITE,
    ),
    "POST /api/agent/tool-calls/{tool_call_id}/approve": _RouteExemption(
        "批准卡协议入口，执行仍走已注册 Domain Tool",
        _DECIDE,
    ),
    "POST /api/agent/tool-calls/{tool_call_id}/reject": _RouteExemption(
        "拒绝卡协议入口，不执行领域命令",
        _DECIDE,
    ),
    "POST /api/episodes/{episode_id}/video-completion/reset": _RouteExemption(
        "生成台死锁解锁：强制停止补齐 Supervisor 并复位面板；不创建新付费任务",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/video-completion/repair": _RouteExemption(
        "遗留事故收口端点：必须先 dry-run 且由本机操作者 confirm=true；只停止任务和采用既有候选，不创建付费任务",
        _WRITE,
    ),
    "POST /api/episodes/{episode_id}/migrate-shot-ids": _RouteExemption(
        "VAL-422 历史 ID 空间迁移：把误写入 story_event_id 的 S* 迁到 spine_beat_ids；只修合同字段，不启动付费任务",
        _WRITE,
    ),
    "POST /api/shots/{shot_id}/resolve-spoken-conflict": _RouteExemption(
        "口播合同冲突人工消解：在 dialogues / audio_timeline 间选基准同步；页面直达，不进入 Agent/MCP 能力面",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/bible/impact-preview": _RouteExemption(
        "人物谱定稿前只读影响预检：不写库、不失效下游；正式定稿仍走 bible.update",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/refs/precheck": _RouteExemption(
        "定妆照/单视角付费只读预检：返回报价与范围；正式生成仍走 portrait.generate / portrait.regenerate_view",
        _WRITE,
    ),
    "POST /api/projects/{project_id}/bible/generate-precheck": _RouteExemption(
        "首次生成人物谱+定妆只读费用预估；正式启动仍走 bible.generate",
        _WRITE,
    ),
    "GET /api/projects/{project_id}/refs/gaps": _RouteExemption(
        "定妆缺口只读扫描",
        _READ,
    ),
    "GET /api/projects/{project_id}/refs/progress": _RouteExemption(
        "定妆进度只读汇总",
        _READ,
    ),
    "POST /api/projects/{project_id}/bible/draft": _RouteExemption(
        "人物谱草稿保存：不定稿、不失效下游",
        _WRITE,
    ),
    "GET /api/projects/{project_id}/bible/draft": _RouteExemption(
        "读取人物谱草稿",
        _READ,
    ),
    "GET /api/projects/{project_id}/auto-changes": _RouteExemption(
        "自动变更待审队列只读",
        _READ,
    ),
    "POST /api/projects/{project_id}/auto-changes/{change_id}/decide": _RouteExemption(
        "自动变更批准/拒绝/回滚；页面人工决策入口",
        _DECIDE,
    ),
    "PUT /api/projects/{project_id}/characters/{character_name}": _RouteExemption(
        "角色级人物谱保存：局部替换角色对象，内部复用人物谱版本与影响预检",
        _WRITE,
    ),
    "GET /api/projects/{project_id}/characters/{character_name}/portrait-candidates": _RouteExemption(
        "角色定妆候选只读列表",
        _READ,
    ),
    "POST /api/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/adopt": _RouteExemption(
        "人物定妆候选人工采纳；页面评审入口，写入 gate/change 决策",
        _DECIDE,
    ),
    "POST /api/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/rollback": _RouteExemption(
        "人物定妆候选人工回滚；页面评审入口，复用采纳切换逻辑",
        _DECIDE,
    ),
    "POST /api/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/manual-rollback": _RouteExemption(
        "场景库手动新增/替换场景图的人工回滚；页面评审入口，与既有 POST .../scenes/{scene_name}/refs/{scene_reference_id}/rollback、POST .../characters/{character_name}/portraits/{portrait_id}/rollback 同一分类口径——回滚本身不是独立领域命令",
        _DECIDE,
    ),
    "POST /api/payments/notify/wechat": _RouteExemption(
        "微信支付渠道异步回调：不是领域命令，调用方是微信服务器而非本产品用户，"
        "挂在 public_router 上无会话鉴权——**验签是唯一防线**"
        "（app.payments.wechat.verify_and_decrypt_notify，任何一步失败整体拒绝）。"
        "发货复用 quota_addon 的 attempt_key=order_id 幂等，重复回调只发一次货",
        _PUBLIC,
    ),
    "POST /api/payments/notify/alipay": _RouteExemption(
        "支付宝渠道异步回调：理由同微信回调，验签走 app.payments.alipay 的 RSA2 "
        "公钥校验；金额与订单号在 app.payments.fulfillment 里二次核对，"
        "不只信渠道说「成功」",
        _PUBLIC,
    ),
    "POST /api/payments/orders": _RouteExemption(
        "账号自助购买（加量包/档位升级）下单：只影响操作者自己的账号余额与档位，"
        "不改变任何制作领域状态；该路由挂 require_local_session，Agent 拿不到会话",
        _PUBLIC,
    ),
    "POST /api/payments/orders/{order_id}/sync": _RouteExemption(
        "账号自助主动查单：只读查询渠道支付状态，按既有幂等逻辑（订单状态机 CAS + "
        "quota_ledger 的 UNIQUE(attempt_key)）收敛，不创建新的付费实体",
        _PUBLIC,
    ),
    "POST /mcp": _RouteExemption(
        "MCP JSON-RPC 传输端点；具体 tools/call 映射到 Capability Registry",
        _PUBLIC,
    ),
    # ---- EP-01 第二阶段（2026-09-11）：组织/团队/角色/项目授权 REST ----
    # 全部走 app/orgs/api.py 手写的 _require_org_admin/_require_project_manage_access
    # 校验，不经 Command Bus——组织治理是运维身份管理，不是制作领域命令，与
    # POST /api/system/users 同一分类口径；仅组织管理员/项目所有者可调用，不向
    # Agent/MCP 开放。
    "POST /api/teams": _RouteExemption(
        "建团队是组织治理操作，不是制作领域命令；仅组织管理员可调用",
        _ADMIN,
    ),
    "PUT /api/teams/{team_id}": _RouteExemption(
        "团队改名/停用同上，仅组织管理员可调用",
        _ADMIN,
    ),
    "POST /api/teams/{team_id}/members": _RouteExemption(
        "团队成员批量增删同上，仅组织管理员可调用",
        _ADMIN,
    ),
    "DELETE /api/teams/{team_id}/members/{user_id}": _RouteExemption(
        "同上",
        _ADMIN,
    ),
    "POST /api/roles": _RouteExemption(
        "自定义角色创建是组织治理操作，不是制作领域命令；仅组织管理员可调用",
        _ADMIN,
    ),
    "PUT /api/roles/{role_id}": _RouteExemption(
        "角色权限点编辑同上；内置模板本身也被 service 层拦住（422）",
        _ADMIN,
    ),
    "DELETE /api/roles/{role_id}": _RouteExemption(
        "角色删除同上；被引用时 409 并在响应体列出引用方",
        _ADMIN,
    ),
    "POST /api/projects/{project_id}/grants": _RouteExemption(
        "项目协作授权是组织治理操作，不是制作领域命令；仅项目所有者或组织管理员可调用",
        _ADMIN,
    ),
    "DELETE /api/projects/{project_id}/grants/{subject_type}/{subject_id}": _RouteExemption(
        "撤销项目协作授权同上",
        _ADMIN,
    ),
    # ---- EP-03 第一阶段（2026-09-11）：CSV 批量导入 + 离职移交 ----
    # 与 POST /api/system/users 同一分类口径：账号运维操作，不是制作领域命令，
    # 仅系统管理员可调用，不向 Agent/MCP 开放。
    "POST /api/system/users/import/preview": _RouteExemption(
        "CSV 批量导入预检；不写 users/teams 等业务表，只落一条批次台账，仅系统管理员可调用",
        _ADMIN, admin_only=True,
    ),
    "POST /api/system/users/import/{batch_id}/apply": _RouteExemption(
        "CSV 批量导入确认提交，仅系统管理员可调用",
        _ADMIN, admin_only=True,
    ),
    "POST /api/system/users/{user_id}/handover": _RouteExemption(
        "离职资产移交是账号运维操作，不是制作领域命令；仅系统管理员可调用",
        _ADMIN, admin_only=True,
    ),
    # ---- EP-02 第一阶段（2026-09-11）：OIDC 单点登录内核 ----
    # 与 POST /api/auth/login 同一分类口径：鉴权入口本身，签发/绑定/解绑会话
    # 先于任何账号归属/scope 判定，不经 Command Bus。GET start/callback 是
    # 302 重定向流程，不在 _MUTATING_METHODS 扫描范围内，不需要豁免项。
    "POST /api/auth/sso/exchange": _RouteExemption(
        "把 OIDC callback 回跳 URL 里的一次性交换码换成真会话令牌（2026-09-12 修复：会话交接不能走查询串，否则真令牌会明文落进 nginx access log/浏览器历史/Referer）；与 POST /api/auth/login 同一类签发会话入口，不经 Command Bus",
        _PUBLIC,
    ),
    "POST /api/auth/sso/link": _RouteExemption(
        "已登录用户发起绑定 IdP：只签发一次性 state 并返回跳转 URL，真正绑定发生在 callback；鉴权入口本身，不改变组织/角色数据",
        _PUBLIC,
    ),
    "DELETE /api/auth/sso/link/{idp_id}": _RouteExemption(
        "解绑 IdP 身份：仅影响调用者自身的登录方式集合；解绑最后一个登录方式会被拒绝（422）",
        _PUBLIC,
    ),
    "POST /api/auth/sso/break-glass": _RouteExemption(
        "PRD EP-02 §6 强制 SSO 下的应急本地登录通道：签发会话本身是鉴权入口，与 POST /api/auth/login 同一分类口径；一次性恢复码由服务器 shell 侧脚本签发，仅限系统管理员账号，用后强制改密并写审计",
        _PUBLIC,
    ),
    "POST /api/admin/sso/providers": _RouteExemption(
        "登记企业身份提供方是运维安全配置，不是制作领域命令；仅系统管理员可调用，不向 Agent/MCP 开放",
        _ADMIN, admin_only=True,
    ),
    "PUT /api/admin/sso/providers/{idp_id}": _RouteExemption(
        "同上，修改 IdP 配置",
        _ADMIN, admin_only=True,
    ),
    "DELETE /api/admin/sso/providers/{idp_id}": _RouteExemption(
        "同上，删除 IdP 配置",
        _ADMIN, admin_only=True,
    ),
    "PUT /api/admin/sso/local-login-policy": _RouteExemption(
        "切换本地口令登录策略是运维安全配置，不是制作领域命令；仅系统管理员可调用，切到 disabled 前强制做一次真实 SSO 连通性自检，不通过则拒绝切换",
        _ADMIN, admin_only=True,
    ),
    # ---- EP-04 第一阶段（2026-09-11）：配额策略表化 + 三级取最紧 ----
    # 全部走 app/quota_policy/api.py 手写的 _require_org_admin 校验，不经 Command
    # Bus——配额治理是运维/组织管理操作，不是制作领域命令，与 POST /api/teams
    # 同一分类口径；仅组织管理员/系统管理员可调用，不向 Agent/MCP 开放。
    "POST /api/system/quota/plans": _RouteExemption(
        "创建组织/团队配额策略是配额治理操作，不是制作领域命令；仅组织管理员可调用",
        _ADMIN,
    ),
    "PUT /api/system/quota/allocations/{scope_type}/{scope_id}": _RouteExemption(
        "分配/追加组织·团队·用户三级配额同上，仅组织管理员可调用",
        _ADMIN,
    ),
}


def _register_exemptions(registry) -> None:
    """协议/编排/运维/渠道回调入口本身不是领域命令：登记豁免原因 + scopes/admin_only，
    满足覆盖门禁（``scripts/check_capability_coverage.py``）与 EP-01 第三阶段的
    route_allowed() 角色判定（``app/authz/resolve.py``）。"""
    for route, item in EXEMPT_ROUTE_PERMISSIONS.items():
        registry.exempt_rest(route, item.reason, scopes=item.scopes, admin_only=item.admin_only)
