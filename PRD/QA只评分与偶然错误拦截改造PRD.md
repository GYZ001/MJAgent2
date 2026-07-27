# QA 只评分与偶然错误拦截改造 PRD

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 文档编号 | PRD-QA-SCORE-ONLY-001 |
| 文档标题 | QA 只评分与偶然错误拦截改造 PRD |
| 版本 | v1.0 |
| 日期 | 2026-07-27 |
| 文档状态 | 待评审 |
| 扫描范围 | `app/`、`frontend/src/`、`tests/`、运行配置与历史证据模型 |
| 目标 | 删除所有由 QA 结果驱动的运行时拦截、自动修复和重新生成；QA 此后只负责评分、说明问题和辅助排序 |
| 最高原则 | 只有偶然性、可恢复的结构/传输错误可以进入自动重试；能力不足、内容质量不足和主观不满意不得触发拦截或额外生成 |

## 2. 决策摘要

本次整改必须同时完成以下两件事，缺一不可：

1. **QA 与控制流彻底解耦。** QA 可以输出分数、维度分、问题说明、置信度和建议，但不得再决定候选是否落库、是否可采用、是否可确认、是否可进入下游、是否可交付，也不得触发 LLM 修复、图片重生、视频重抽、参考图重建或分镜重规划。
2. **拦截与重试改为显式错误分类。** 只有 JSON/Schema 格式错误、必填参数缺失、必需上下文遗漏、网络超时、限流、5xx、供应商响应字段偶然缺失、文件缺失/空文件/不可解码等结构性或瞬时错误可以拦截。是否重试还需进一步满足“错误可恢复、请求幂等、不会因同一能力不足持续烧钱”三个条件。

最终必须满足：

- QA 驱动的运行时拦截数为 **0**。
- QA 驱动的 LLM 修复、图片重生、视频重抽数为 **0**。
- 已成功生成且可读取的图片/视频，即使 QA 低分、存在漂移、水印、构图问题、动作问题或一致性问题，也必须保留并允许进入后续流程。
- QA 失败、超时或返回非法 JSON 时，目标资产不得随 QA 一起失败；仅 QA 自身可标记为“未评分”。
- 权限、用户确认、预算授权、并发冲突、版本一致性、幂等、防重复扣费、文件安全等非 QA 保护继续保留，且不得被本次整改削弱。

## 3. 术语与边界

### 3.1 QA 的新定义

QA 是旁路评估能力，职责仅包括：

- 输出总分和维度分；
- 输出问题说明、风险提示、置信度和改进建议；
- 在已有候选之间辅助排序；
- 为用户评审、数据分析、模型评估和后续人工重做提供依据。

QA 不再拥有以下权限：

- 将任何生成结果标记为不可落库、不可采用、不可确认或不可交付；
- 产生 `blocker`、`must_fix` 等会改变运行状态的通用问题等级；
- 修改任务状态为 `failed`、`repairing`、`waiting_retry`；
- 发起任何新的付费生成；
- 删除低分候选、撤销当前资产或阻止原子替换；
- 修改生成提示词后自动再试；
- 触发剧本补丁、分镜重规划、参考图重建、视频重抽或降级链路。

### 3.2 “拦截”与“重试”不是同一件事

- **拦截**：当前请求不应继续，例如请求体不是合法 JSON、必填参数为空、选定模式要求的上下文没有提供。
- **重试**：系统在不改变用户意图的前提下，再执行一次相同语义的操作。

参数缺失和上下文无法补齐时可以拦截，但不能盲目重试；只有系统能确定性补齐上下文时，才允许先补齐一次再继续。任何重试都必须由明确的错误分类授权，不能由 QA 分数、问题文本或“看起来不够好”间接触发。

### 3.3 不属于 QA 的保留控制

以下能力继续保留，并在代码、接口和页面上明确标记为“业务/安全控制”，不得继续使用“QA 门禁”命名：

- 身份认证、权限范围、人工确认和高风险操作确认；
- 预算/时间授权、费用上限、防重复扣费和幂等；
- 并发写入、任务互斥、状态机合法性、版本号和依赖快照一致性；
- 必需源文件不存在、文件为空、不可读取、不可解码或校验和不一致；
- 必需对象、ID、来源摘录或上下文绑定缺失；
- 供应商明确返回不支持、鉴权失败、内容政策拒绝等确定性失败；
- CI、离线基准和发布质量检查。`app/benchmarks.py::release_gate_status` 属于工程发布治理，不参与用户项目的运行时内容拦截，本次不删除。

## 4. 现状统计结论

### 4.1 统计口径

本次按“能够独立改变流程状态、拒绝候选/确认/交付，或触发额外模型/媒体调用的位置”去重统计。通用引擎与实际业务接入点分别计算，因为只改通用引擎或只改业务调用方都无法完整消除拦截。

扫描结果：

| 类别 | 数量 | 说明 |
|---|---:|---|
| 运行时 QA 决策点 | 33 | 后端可直接阻断或触发重试/修复的独立位置 |
| 其中具备拒绝/阻断效果 | 30 | 包括不落库、不发布、不采用、不确认、不进入生产或不交付 |
| 其中具备自动重试/修复效果 | 16 | 包括文本补写、分镜重规划、图片重生、参考图重建、视频重抽；与阻断项有重叠 |
| 前端/配置控制面 | 2 | 页面根据硬门禁禁用操作，以及监制房暴露 QA 重试阈值 |
| 盘点总项 | 35 | 33 个运行时点 + 2 个控制面 |

### 4.2 全量 QA 拦截清单

标记说明：`B`=可阻断/拒绝，`R`=可触发自动重试或修复，`B+R`=两者兼有。

| # | 当前拦截点 | 主要代码位置 | 类型 | 当前行为 | 本次最终处理 |
|---:|---|---|---|---|---|
| 1 | 通用 AgentLoop 评估闭环 | `app/loops/base.py` | B+R | 任一 evaluator issue 都使候选不提交，并携带问题再次生成完整 JSON | 只让 JSON/Schema/必填字段错误进入格式重试；业务质量 issue 全部改为评分，候选正常提交 |
| 2 | 证据仓库 artifact 提交门禁 | `app/evidence/repository.py` | B | `hard_gate_passed=false`、failed/error 或 blocker 会拒绝 artifact commit | 提交资格只看结构完整性；QA evaluation 永远不参与 commit 判定 |
| 3 | 完成证书与发布门禁 | `app/production/certificate.py`、`app/production/publish.py` | B | blocker/must_fix 非零不能签发或发布 | 证书只证明版本、来源、结构和上下文完整；QA 分数作为附带报告，不作为签发条件 |
| 4 | 人物 Bible 生成 | `app/stages.py`、`app/validators.py` | B+R | 人物业务校验失败时最多多轮重写 | 仅非法 JSON/Schema/必填字段重试；人物描述质量、丰富度、一致性只评分 |
| 5 | 场景 Bible 生成 | `app/stages.py`、`app/validators.py` | B+R | 场景业务校验失败时多轮重写并可能整阶段失败 | 与人物 Bible 相同；实体 ID/必填字段保留结构校验，内容好坏不拦截 |
| 6 | 剧本生产 QA | `app/production/screenplay_repair.py::run_screenplay_qa` | B | 所有 validator/adaptation 问题转换为 blocker，`hard_gate_passed=false` | 拆为结构验证报告和 QA 评分报告；剧情、节奏、覆盖、可拍性等只计分 |
| 7 | 剧本局部修复循环 | `app/production/screenplay_repair.py` | B+R | 单次 activation 最多 12 个 patch，同一问题最多 3 个策略，直到 QA 通过才发布 | 删除 QA 驱动 patch/续跑；只允许修复生成 JSON 中缺字段、类型错误或上下文绑定缺失 |
| 8 | 分镜大纲生成 | `app/stages.py` | B+R | 大纲业务问题进入 AgentLoop/修复链 | 结构合法即落库；镜数、节奏、覆盖等形成评分与提示，不自动重写 |
| 9 | 单镜分镜生成 | `app/stages.py` | B+R | 口播容量、覆盖、连续性等业务问题触发最多 4 轮修复 | 仅字段/Schema/上下文引用错误重试；可拍性与质量问题只评分 |
| 10 | 分镜 Supervisor 整集修复 | `app/storyboard_supervisor.py` | B+R | blocker/NEEDS_REPLAN 会删镜、插镜、拆镜、重规划，最多 6 轮并可续跑 | 删除 QA 驱动的删/插/拆/重规划；只对结构缺失或无法绑定的上下文执行确定性恢复 |
| 11 | 分镜确认预览 | `app/domain/video_ops.py::create_storyboard_confirmation_preview` | B | `evaluation.passed=false` 返回 `STORYBOARD_NOT_CONFIRMABLE` | 仅镜头集合不完整、必要字段/来源摘录/上下文绑定缺失时不可确认；QA 低分只展示 |
| 12 | 分镜最终确认 | `app/domain/video_ops.py::confirm_episode_core` | B | 再跑业务校验，不通过不能确认 | 与预览使用同一结构就绪判定；QA 不得在确认时重新变成门禁 |
| 13 | 剧本/分镜人工编辑保存 | `app/domain/screenplay_ops.py`、`app/domain/storyboard_ops.py` | B | 保存时业务校验错误可返回 4xx 或拒绝证据提交 | 请求 Schema、对象存在性、ID/时间线结构可拦截；主观质量和能力问题保存为 warning/score |
| 14 | 初始人物参考图 | `app/refs.py` | B+R | 单图 QA 不合格则带 critique 再生，通常最多 2 次；未通过不进入可用资产 | 第一张成功且可解码的图立即可用；QA 异步评分，不带 critique 自动再生 |
| 15 | 新发现人物肖像 | `app/portraits.py` | B+R | 候选 QA 未通过会删除/放弃并再次生成 | 保留所有技术有效候选；不因 QA 删除，不因低分重生 |
| 16 | 人物多视图整包 | `app/multiview.py`、`app/portrait_policy.py` | B | 单视图或组一致性 QA 失败使 pack 为 failed，阻止下游 | pack 完整性只看要求的视图文件和关联是否齐全；一致性只评分 |
| 17 | 人物单视图重做的原子替换 | `app/multiview.py`、`app/domain/bible_ops.py` | B | 新视图和整包 QA 通过后才替换当前视图 | 技术有效即形成新版本并允许替换；保留旧版用于回退，QA 只辅助用户选择 |
| 18 | 初始场景图生成 | `app/scenes.py` | B+R | 单图 QA 低分/硬失败时带 critique 再生成，通常最多 2 次 | 第一张技术有效图即可成为候选；不因无人、水印、空间/风格等评分项重生 |
| 19 | 场景候选复检、采用和人工签署 | `app/scenes.py`、`app/domain/bible_ops.py` | B | hard gate/unverified 会拒绝采用或要求人工逐项签署 | 采用只要求资产存在、可读、版本匹配；人工评审变为建议性决策，不再是资格证明 |
| 20 | 场景多视图整包 | `app/multiview.py`、`app/scene_policy.py` | B | 单视图/组 QA 决定 ready/failed 和下游资格 | 结构状态改为 complete/incomplete；QA 状态独立为 scored/unscored，不影响 complete |
| 21 | 场景状态演进、单视图重做与回滚 | `app/scenes.py`、`app/multiview.py`、`app/domain/bible_ops.py` | B | QA 不通过时禁止状态迁移、原子替换或回滚 | 仅文件存在、归属、版本和依赖一致性控制迁移；QA 只显示差异 |
| 22 | 资产依赖清单生产资格 | `app/multiview.py::manifest_production_blockers`、`assert_manifest_allows_production` | B | pack QA/status 不 ready 会阻止关键帧或视频 | 只拦截真实缺失的必需资产、损坏文件、错误关联和版本不匹配；低分资产仍满足上下文存在性 |
| 23 | 叙事关键帧 QA | `app/multiview.py`、`app/media_exec/run_job.py` | B | overall/action/body/identity/watermark 阈值决定关键帧能否作为视频输入 | 文件可读且上下文归属正确即可使用；各维度阈值仅用于评分、排序和页面提示 |
| 24 | 视频参考图阈值/地板筛选 | `app/video_modes.py` | B | 低于 quality threshold/floor 的图被淘汰，不喂视频模型 | 不再按分数过滤；在已有技术有效候选中可按分数排序，分数缺失时按稳定生成顺序 |
| 25 | 单张视频参考图质量重试 | `app/video_modes.py` | R | QA 不达标时按 `video_reference_gen_retries` 再生成 | 删除质量重试；生成成功后只评分一次，评分失败也不重生 |
| 26 | 参考图组一致性重生 | `app/video_modes.py` | R | 漂移图按 `video_reference_consistency_retries` 做 i2i 重生 | 删除一致性重生；漂移作为分数/问题展示 |
| 27 | 视频参考输入整链重建 | `app/video_modes.py`、`app/media_exec/run_job.py` | B+R | 无“合格”参考图时整条 pipeline 再试或阻止视频提交 | 只在模式必需的上下文文件真实缺失时拦截；低分不等于缺失，不重建整链 |
| 28 | 视频生成后自动 QA 重抽 | `app/media_exec/run_job.py::_maybe_auto_qa`、`app/media_pipeline/retry_policy.py` | R | hard failure/低分可排队 `QA_RETAKE`，再次付费生成 | 删除 `QA_RETAKE` 决策及调度；视频 QA 旁路执行，不创建 job/version |
| 29 | 视频候选分级与自动采用 | `app/evidence/media.py` | B | A/B/C、fatal QA、阈值和重抽耗尽共同决定是否可采用 | A/B/C 仅展示和排序；第一个技术有效版本可立即采用，后续版本不因 QA 被拒 |
| 30 | 视频问题收集与修复路由 | `app/video_issues.py`、`app/video_repair_router.py` | B+R | QA 问题被提升为 blocker，并路由到重抽、参考图重建、改词或改分镜 | QA 问题只进入报告；修复路由不得接收 QA issue，只有偶然错误分类可进入重试策略 |
| 31 | 完整模式视频 Supervisor | `app/video_supervisor.py` | B+R | 质量等级决定 coverage，按尝试预算反复生成，未达标不能收口 | Supervisor 只补齐“没有技术有效视频”的镜头；QA 分数不增加 attempts、不阻止收口 |
| 32 | 评审墙资产资格/恢复 | `app/domain/review_wall.py` | B | 上游人物/场景/参考图 hard gate 失败时不能作为新输入或恢复 | 仅不存在、损坏、关联错误、版本过期或上下文缺失时不可用；QA 只作为评审信息 |
| 33 | 交付就绪质量门禁 | `app/delivery.py` | B | `fatal_video_quality` 等 QA 结果进入 blockers，整集不能交付 | QA 风险移入 `warnings`/评分报告；交付只看必需文件、可读取性、血缘和结构完整性 |
| 34 | 前端硬门禁派生状态 | `frontend/src/lib/sceneUsability.ts`、Bible/Scenes/Wall 页面及 `api.ts` | 控制面 | 根据 `hard_gate_passed`、failed/unverified 禁用采用、恢复、生产等按钮 | 页面不再用 QA 字段决定可操作性；展示分数、风险和“未评分”，资格读取结构状态 |
| 35 | 监制房 QA 重试配置 | `app/config.py`、`app/monitoring.py`、`frontend/src/pages/MonitorPage.tsx` | 控制面 | 可配置自动重抽阈值、修复次数、硬门禁和参考图重试 | 删除可重新开启 QA 门禁/重试的控制能力；保留评分开关与展示分段，不保留行为开关 |

## 5. 目标规则：唯一允许的拦截与重试

### 5.1 错误分类矩阵

所有生产入口、AgentLoop、媒体 worker 和 Supervisor 必须先得到标准化 `error_class`，再决定是否拦截或重试。禁止从 QA 分数、QA issue 文本、A/B/C 等级或 evaluator 状态推断重试。

| 错误类别 | 典型示例 | 是否拦截 | 是否自动重试 | 约束 |
|---|---|---:|---:|---|
| `ACCIDENTAL_JSON_INVALID` | 模型输出不是 JSON、尾逗号、截断、无法解析 | 是 | 是，最多 2 次 | 只要求重新输出合法 JSON，不加入质量 critique，不改变任务目标 |
| `ACCIDENTAL_SCHEMA_MISMATCH` | 字段类型错误、必填字段在模型输出中偶然缺失、枚举非法 | 是 | 是，最多 2 次 | 仅修复结构；若同一错误持续出现则失败并返回，不无限续跑 |
| `INPUT_PARAMETER_MISSING` | API 请求缺少 episodeId、shotId、mode、目标对象 | 是 | 否 | 在付费调用前返回明确 4xx；调用方补齐后重新发起 |
| `CONTEXT_MISSING` | 必需剧本、来源摘录、人物/场景引用、模式必需的输入文件不存在 | 是 | 条件允许 | 仅允许确定性重载/重绑一次；无法确定性补齐则停止，不发起付费生成 |
| `PROVIDER_TRANSIENT` | 网络超时、连接中断、429、明确 5xx、临时不可用 | 是 | 是，按现有瞬时故障上限 | 必须复用幂等键和原请求；供应商已受理时只轮询原 task，不创建第二个付费任务 |
| `PROVIDER_RESPONSE_INCOMPLETE` | 成功响应偶然缺 URL/taskId、下载返回空体 | 是 | 是，最多 1 次 | 先查询原任务或重取结果，确认未创建重复任务后才可重提 |
| `MEDIA_TECHNICALLY_UNUSABLE` | 文件不存在、0 字节、不可解码、容器损坏 | 是 | 是，最多 1 次 | 只针对“没有可使用结果”；已可播放但画面不好不属于此类 |
| `CAPABILITY_OR_QUALITY_LIMIT` | 低分、漂移、人物不像、肢体异常、动作不完整、节奏差、构图差、水印、画面可播放但时长/内容不理想 | 否 | 否 | 必须落库、可采用、可确认、可交付；仅评分和提示 |
| `DETERMINISTIC_PROVIDER_REJECTION` | 鉴权失败、模型不支持、明确版权/内容政策拒绝、参数被供应商判定永久非法 | 是 | 否 | 不用“换词再试”掩盖确定性失败；明确返回原因，由用户决定是否修改请求 |
| `BUSINESS_OR_SAFETY_CONTROL` | 权限、预算、人工确认、并发冲突、版本冲突、删除保护 | 是 | 否 | 非 QA；沿用现有安全和审计机制 |

### 5.2 偶然错误重试的五条硬约束

1. 每一次自动重试都必须写入明确 `error_class`；没有分类不得重试。
2. 重试必须保持用户意图、业务参数和提示词语义不变；只允许补齐格式要求、恢复确定性上下文或重放同一供应商请求。
3. QA 输出不得成为 `retry_reason`，QA critique 不得自动注入下一次生成提示词。
4. 已产生技术有效的图片/视频后，该资产的自动生成次数立即封顶；后续 QA 再差也不能增加付费调用。
5. 达到重试上限后必须进入明确失败态并交还用户，禁止 checkpoint 自动恢复后重置预算形成无限续跑。

### 5.3 图片和视频的特殊规则

- 图片/视频只要已落盘、非空且可解码，就属于成功生成结果。
- 分辨率偏低、时长偏差、风格错误、动作错误、人物漂移、场景漂移、水印、穿帮、文字、审美和一致性都属于 QA 评分项，不属于技术损坏。
- QA 可以对已有多个候选排序，但不能为了“选出高分版本”再生成新候选。
- 默认采用规则为：当前已有采用版不被 QA 自动替换；没有采用版时选择第一个技术有效版本。若同一批次天然产生多个版本且评分及时返回，可推荐最高分，但评分超时不能阻塞采用。
- 用户主动点击“重做”属于新的显式业务请求，不属于自动 QA 重试，仍可执行并保留费用确认。
- 供应商提交成功后的等待轮询不是新生成，不计算为重做；必须继续轮询同一个 provider task。

### 5.4 上下文遗漏与“模型没用好上下文”的区别

- 上下文对象/文件根本没有提供、ID 不存在、版本不匹配、来源摘录未绑定：属于 `CONTEXT_MISSING`，可拦截。
- 上下文已完整传入，但模型生成结果没有体现人物、场景、动作或剧情：属于能力/质量不足，只评分，不拦截、不重试。
- 不能用 QA 的“人物不像”“场景不一致”等结果反推为 `CONTEXT_MISSING`。上下文是否缺失必须在请求发出前通过确定性证据判断。

## 6. 数据与接口契约改造

### 6.1 Evaluation 角色拆分

为 evaluation 增加显式字段，禁止继续用 `hard_gate_passed` 同时表达评分结果和运行资格：

```json
{
  "evaluation_role": "score_only",
  "score_status": "scored",
  "overall": 0.42,
  "dimension_scores": {"identity": 0.35, "composition": 0.61},
  "issues": [{"code": "identity_drift", "qa_priority": "high", "message": "人物相似度偏低"}],
  "confidence": 0.78,
  "runtime_blocking": false,
  "retry_eligible": false
}
```

QA 异常时：

```json
{
  "evaluation_role": "score_only",
  "score_status": "unavailable",
  "overall": null,
  "issues": [],
  "runtime_blocking": false,
  "retry_eligible": false,
  "diagnostic": "qa_output_json_invalid"
}
```

QA 自己返回非法 JSON 时，允许只重试 QA evaluator 一次；仍失败则记为 `unavailable`。这次重试不得重新生成被评图片/视频，也不得影响其状态。

### 6.2 拦截决策契约

新增统一错误决策对象，所有 retry enqueue 必须引用它：

```json
{
  "error_class": "PROVIDER_TRANSIENT",
  "source": "video_provider_submit",
  "blocking": true,
  "retry_eligible": true,
  "retry_scope": "same_request",
  "attempt": 1,
  "max_attempts": 3,
  "idempotency_key": "...",
  "paid_generation_created": false
}
```

必须建立中心 allowlist，只有 5.1 中明确允许自动重试的错误类可以进入 `waiting_retry`。任何 `qa_*`、`quality_*`、`hard_failure`、`score_below_*`、`consistency_drift` 原因都必须被中心策略拒绝并记录违规指标。

### 6.3 状态字段拆分

| 当前混合字段 | 新字段 | 迁移规则 |
|---|---|---|
| `hard_gate_passed` | `evaluation_role` + `runtime_blocking` | 新 QA 一律 `score_only/false`；旧字段只为兼容返回，不再被新代码读取 |
| `status=failed/unverified` | `asset_status` + `score_status` | 资产看文件/结构；评分看 QA 是否完成，二者互不推导 |
| `pack_status=ready/failed` | `pack_structure_status=complete/incomplete` | 只看必需视图存在、可读和归属；组一致性进入分数 |
| `selectedForSeedance` 的 QA 资格 | `selected` + `selection_reason` | 选择不再受 QA 阈值限制；可记录“默认首个有效版本/用户选择/分数推荐” |
| `blocker/must_fix` QA issue | `qa_priority=high/medium/low` | QA issue 不再使用能驱动控制流的 severity |
| 视频 A/B/C | 保留 `quality_grade` | 仅用于展示、筛选、排序和分析，不影响 coverage/交付 |

### 6.4 向后兼容

- 数据库先追加字段，不删除历史列，不重写用户历史资产和评分。
- 新 score-only QA 记录在旧的非空 `hard_gate_passed` 字段中写兼容值 `true`，同时以 `evaluation_role=score_only` 表明真实语义，防止旧读路径误阻断。
- 历史 `hard_gate_passed=false` 仍原样保留用于审计；API 适配层对其返回 `legacy_hard_gate`，但运行资格统一重新按结构状态计算。
- 旧 API 字段至少保留两个版本并标记 deprecated；前端先停止读取，确认无调用后再移除。
- 不做破坏性数据迁移，不删除已有候选、版本、QA 证据、证书或 provider 调用记录。

## 7. 分模块实施方案

### 7.1 通用评估与证据层

涉及：`app/harness/types.py`、`app/loops/base.py`、`app/evidence/repository.py`、`app/production/certificate.py`、`app/production/publish.py`、`app/db.py`。

- 将 validation 拆为 `structural_issues` 与 `qa_observations`，只有前者可影响提交。
- AgentLoop 的循环条件只接收结构错误；业务 evaluator 在 artifact 已产生后旁路运行。
- `commit_artifact` 不再检查 score-only evaluation 的 status、issue severity 或 hard gate。
- 完成证书记录 QA evaluation IDs 和分数快照，但签发条件只检查 artifact hash、输入指纹、契约版本、结构完整和上下文绑定。
- 禁止 QA evaluation 写入 blocker/must_fix；历史数据通过适配器读取。

### 7.2 人物、场景、剧本和分镜文本链路

涉及：`app/stages.py`、`app/validators.py`、`app/domain/bible_ops.py`、`app/domain/screenplay_ops.py`、`app/domain/storyboard_ops.py`、`app/domain/video_ops.py`、`app/production/screenplay_repair.py`、`app/storyboard_supervisor.py`。

- 把 Pydantic/JSON/必填字段/对象引用验证从业务质量规则中抽出。
- 人物/场景 Bible：结构成功即保存；业务规则生成维度分和问题列表。
- 剧本：取消“QA 不清零不得发布”，Baseline 结构成功即可发布；生产 QA 改为发布后的旁路评分。
- 删除 QA 驱动的局部 patch、策略轮换和自动续跑。若模型 JSON 缺字段，可用格式修复循环，且不得把剧情问题作为修复输入。
- 分镜：取消口播、覆盖、连续性、节奏等 QA 问题驱动的删镜、插镜、拆镜和重规划。
- 分镜确认只校验完整镜头集合、唯一 shotId/shotNo、必要字段、合法引用、来源/上下文绑定和版本一致性。
- 人工保存允许低质量内容；非法 JSON、字段缺失、ID 不存在、时间线无法解析等仍返回明确错误。

### 7.3 人物图、场景图和多视图

涉及：`app/refs.py`、`app/portraits.py`、`app/scenes.py`、`app/portrait_policy.py`、`app/scene_policy.py`、`app/multiview.py`、`app/domain/bible_ops.py`。

- 所有单图生成从“生成→QA→不合格重生”改为“生成→技术校验→落库/可用→异步 QA 评分”。
- 删除由 QA critique 驱动的第二次生成；只保留供应商瞬时故障和文件不可用重试。
- 多视图整包只因要求的视图缺失、文件不可读、人物/场景归属错误或版本冲突而 incomplete。
- 组一致性、正侧背一致性、无人/水印/文字/空间匹配等全部变为评分维度。
- 单视图重做成功后创建新版本；原子替换只受版本冲突和文件可用性控制，QA 不参与。
- 场景候选人工复核从“资格签署”改为“人工评价/选择”；不能再绕一圈形成新的硬门禁。
- 回滚只校验目标版本仍存在且依赖一致，不复跑 QA 拒绝历史资产。

### 7.4 关键帧与视频参考图

涉及：`app/video_modes.py`、`app/multiview.py`、`app/evidence/media.py`、`app/media_exec/run_job.py`。

- 删除 `apply_keep_gate` 的过滤语义；可保留改名后的排序函数。
- `video_reference_quality_threshold`、`video_reference_quality_floor` 只可作为 UI 分段/排序提示，不得决定输入是否保留。
- 删除 `video_reference_gen_retries` 和 `video_reference_consistency_retries` 所驱动的额外生成。
- 关键帧 action/body/identity/overall/watermark 分数均只展示；文件有效即可以作为视频输入。
- `narrative_keyframe_required` 只表达所选视频模式是否需要一个关键帧文件，不表达该关键帧必须达到 QA 阈值。
- 参考图链的全量重试只可发生于确定的结构/传输失败，不能因为“没有合格图”触发；低分图属于已存在的有效上下文。

### 7.5 视频生成、自动采用与完整模式

涉及：`app/media_pipeline/retry_policy.py`、`app/media_pipeline/scheduler.py`、`app/media_exec/enqueue.py`、`app/media_exec/run_job.py`、`app/evidence/media.py`、`app/video_issues.py`、`app/video_repair_router.py`、`app/video_supervisor.py`。

- 删除 `RetryKind.QA_RETAKE`、`decide_qa_retake` 及其排队、并发优先级、计数和耗尽逻辑。
- `_maybe_auto_qa` 改为 `_score_video` 一类的旁路操作，返回值不得控制 auto-adopt、force-best 或 job 状态。
- 视频可解码即形成可用版本；QA 超时或失败只使 `score_status=unavailable`。
- A/B/C 和 fatal issues 继续显示，但从采用资格、coverage ledger 和 supervisor issue 中移除。
- `video_issues` 不再把 QA 结果转换为 blocker；`video_repair_router` 不接受 QA issue。
- 完整模式只补齐“没有技术有效版本”的镜头；一镜一个有效版本即视为 coverage 完成。
- 删除按质量尝试 2~6 次、按收益升级策略、重建参考图、改词、改分镜等自动动作。
- 网络/429/5xx 的现有 job 级退避保留；版权/内容政策明确拒绝的“改词再提”自动循环删除，改为一次明确失败。

### 7.6 评审墙与交付

涉及：`app/domain/review_wall.py`、`app/delivery.py`、`app/domain/common.py`。

- 评审墙仍展示 QA 分数、问题和版本差异，但恢复/采用资格不读取 QA hard gate。
- QA 低分资产允许作为新视频输入；页面明确“分数仅供参考，当前选择由用户/默认规则决定”。
- `fatal_video_quality` 从 delivery blockers 移入 warnings/quality report。
- 交付阻断仅保留：必需镜头/文件缺失、文件不可读、采用版本不存在、结构不完整、来源/血缘/版本上下文缺失。
- 人工 review issue 的质量问题不能阻止标记完成；若属于真实结构缺失，必须使用结构错误类别而不是 QA severity。

### 7.7 前端、配置与能力目录

涉及：`frontend/src/api.ts`、`frontend/src/lib/sceneUsability.ts`、人物/场景/分镜/评审墙/监制房页面，以及 `app/config.py`、`app/monitoring.py`、`app/capabilities/catalog.py`、capability handlers。

- 所有按钮资格从 `hard_gate_passed`/QA status 改读结构可用性字段。
- “通过/失败”“硬门禁”“不可用于下游”改为“评分完成/未评分”“质量风险”“结构完整/不完整”。
- 删除或废弃以下行为配置：`auto_retake_threshold`、`video_hard_gate_enabled`、`max_repair_attempts` 的业务质量用途、`video_reference_quality_threshold` 的过滤用途、`video_reference_quality_floor`、`video_reference_gen_retries`、`video_reference_consistency_retries`、`video_auto_retake_limit`、各 keyframe QA threshold 的门禁用途、`watermark_qa_mode=reject` 的门禁用途。
- `auto_qa` 重命名为 `qa_scoring_enabled`；关闭它只是不评分，不影响资产可用性。
- 分数阈值如需保留，只用于颜色分段和默认排序，并在配置文案中明确“不会触发拦截或重做”。
- 能力目录中“通过整包 QA 后替换”“硬门禁复核”等描述同步改为结构成功和评分展示。
- 最终代码中不保留可以重新开启 QA 拦截的运行时开关；回滚依靠代码版本回滚，不依靠隐藏开关恢复旧行为。

## 8. 实施顺序与发布策略

### 阶段 0：冻结口径与建立保护测试

- 把本 PRD 的 35 项清单固化为追踪表，每项绑定负责人、改动 PR、测试和验收证据。
- 增加架构测试：QA evaluation 不得直接调用 enqueue/retry/repair/publish deny/HTTP 409。
- 增加付费调用计数基线，区分用户主动重做、瞬时故障重试和 QA 驱动调用。
- 不改变运行行为。

### 阶段 1：引入新数据契约和中心错误分类

- 数据库只做追加式迁移，增加 evaluation role、score status、runtime blocking、error class。
- 建立唯一 RetryPolicy allowlist；现有 retry 先接入审计但保持原行为，用于发现漏网入口。
- API 双写新旧字段，前端暂不切换。
- 验证历史资产、证据、证书和页面读取完全不回归。

### 阶段 2：先切断最高成本的媒体 QA 重试

- 删除图片、场景、多视图、关键帧、参考图、视频 QA 驱动的重新生成和过滤。
- 视频 QA 彻底旁路；完整模式 coverage 改为技术有效版本覆盖。
- 保留 provider 瞬时重试、原任务轮询和不可解码文件重试。
- 重点观察付费调用量、采用率、任务完成率和技术失败率。

### 阶段 3：切断文本 QA 修复与发布门禁

- AgentLoop 只处理 JSON/Schema/必填字段。
- 剧本/分镜业务 validator 全部转评分；删除自动 patch、重规划和质量续跑。
- 发布、确认和证书只使用结构/上下文就绪结果。
- 验证旧剧本、旧分镜、手工编辑和恢复流程。

### 阶段 4：切换评审墙、交付和前端语义

- 页面全面停止读取 QA hard gate。
- 交付 readiness 去除质量 blocker，保留 warning 报告。
- 移除监制房 QA 重试/门禁设置和错误引导文案。
- 更新 capability 文案、API 类型和用户提示。

### 阶段 5：清理旧能力

- 删除所有 `QA_RETAKE`、quality repair router、QA gate 条件分支及无调用代码。
- 旧字段继续只读兼容两个版本，确认无消费者后再单独做 schema 清理。
- 静态扫描确保不存在 QA→retry/enqueue/repair/block/adopt deny 的路径。
- 本阶段完成前不能宣称“QA 拦截已删除”。

### 发布方式

- 每个阶段独立 PR、独立回归、可通过代码版本回滚。
- 不使用可重新开启旧 QA 门禁的长期 feature flag。
- 数据迁移必须向前兼容；回滚代码不得要求回滚或删除数据库列。
- 在预发布环境用固定低分样本验证“低分但流程完成”，再灰度生产项目。

## 9. 不影响其他功能的保护措施

### 9.1 必须保持不变

- 用户主动重做图片/视频、修改提示词后重新生成的能力；
- 图片/视频版本历史、人工采用、废弃、恢复、回滚和审计；
- 已采用版本在新生成失败时不被覆盖；
- 供应商任务幂等、原 task 轮询、网络/限流/5xx 退避；
- 费用审批、预算上限、并发控制、停止/取消、清空和危险操作确认；
- 剧本/分镜的对象 ID、版本、来源摘录、上下文绑定和下游 stale 检测；
- 文件存在性、可读取性、媒体解码、下载与交付包结构；
- QA 分数、问题列表、证据抽屉、监控统计和人工比较能力；
- 历史 API 读取和历史数据展示。

### 9.2 禁止用“全部放行”实现

以下做法不符合本 PRD：

- 简单把所有 `hard_gate_passed` 写成 true，但仍让 QA 触发 repair/retry；
- 只把阈值调成 0，但保留可重新调高的门禁能力；
- 捕获所有异常后继续，导致非法 JSON、缺字段或损坏文件进入下游；
- 把 QA blocker 改名为 warning，却仍通过 `evaluation.passed` 或 status 间接阻断；
- QA 低分时不叫“重试”，改名为“优化”“兜底”“再抽一次”继续生成；
- 在 Supervisor 恢复 checkpoint 时重置质量尝试次数，形成隐性无限重试；
- 把模型忽略上下文误判为上下文缺失，借此继续自动生成。

## 10. 测试与验收方案

### 10.1 核心行为验收

| 场景 | 期望结果 |
|---|---|
| 人物/场景图生成成功但 QA=0.1 | 只生成一次，资产保留、可采用、可进入多视图/下游，页面展示低分 |
| 多视图组一致性失败 | pack 只要视图文件齐全即 complete；不重生、不阻止替换 |
| 关键帧 identity/action 低分或检测到水印 | 仍可作为视频输入；只显示风险 |
| 视频生成成功、可播放但 QA 有 fatal issue | 不触发 `QA_RETAKE`，版本可采用，完整模式把该镜视为已覆盖 |
| QA 服务超时/异常/非法 JSON | 资产状态不变，QA 为 unavailable；可只重试 QA evaluator 一次，不重生资产 |
| 剧本/分镜内容质量规则不通过 | 结构合法即保存/发布/确认；生成评分和问题报告，不自动 patch/重规划 |
| 模型首次输出非法 JSON，第二次合法 | 只做格式重试后成功；重试提示不含质量 critique |
| 模型连续两次输出非法 JSON | 明确失败并返回错误证据，不进入无限修复/自动续跑 |
| API 缺必填参数 | 在任何模型/供应商调用前 4xx；不自动重试、不产生费用 |
| 必需上下文缺失但可确定性重载 | 只重载/重绑一次后继续；不重做已有媒体 |
| 必需上下文无法补齐 | 明确阻断并说明缺什么；不发送付费请求 |
| 429/网络/明确 5xx | 使用原幂等键按上限重试；供应商已受理则只轮询原 task |
| 供应商明确版权/内容政策拒绝 | 直接失败并提示，不自动改词重提 |
| 媒体文件 0 字节或不可解码 | 允许一次技术重试；失败后停止，不能伪装成 QA 问题 |
| 低分视频交付 | 若文件、采用版本、结构和血缘齐全则 delivery ready；低分列在 warnings |
| 权限、预算、版本冲突、危险操作未确认 | 仍按现有规则阻断，证明本次未削弱非 QA 安全控制 |

### 10.2 回归测试范围

优先更新并执行以下现有测试族，同时新增 score-only 断言：

- `tests/test_agent_loop.py`、`tests/test_validators.py`；
- `tests/test_screenplay_stage.py`、`tests/test_screenplay_scene_repair.py`、`tests/test_screenplay_edit_save.py`、`tests/test_screenplay_workspace_prd.py`；
- `tests/test_storyboard_outline.py`、`tests/test_storyboard_supervisor_val422.py`、`tests/test_storyboard_sequential.py`、`tests/test_storyboard_resume.py`、`tests/test_storyboard_source_excerpt.py`、`tests/test_storyboard_workspace_prd.py`；
- `tests/test_portrait_qa_policy.py`、`tests/test_initial_multiview_bootstrap.py`、`tests/test_multiview_keyframe_qa.py`；
- `tests/test_scene_candidate_adopt.py`、`tests/test_scene_candidate_recovery.py`、`tests/test_scene_state_changes.py`、`tests/test_scene_prd_02.py`；
- `tests/test_video_modes.py`、`tests/test_media_evidence.py`、`tests/test_vlm_qa_parsing.py`；
- `tests/test_video_supervisor_unit.py`、`tests/test_video_supervisor_integration.py`、`tests/test_worker_reference_gallery.py`；
- `tests/test_review_wall_prd.py`、`tests/test_delivery.py`、`tests/test_golden_storyboard_e2e.py`；
- 前端 Scenes/Wall/监制房/API 类型测试。

### 10.3 必须新增的架构测试

1. QA evaluation 对象不可设置 `runtime_blocking=true`。
2. retry enqueue 必须提供 allowlist 中的 `error_class`。
3. `error_class` 以 `QA_`、`QUALITY_`、`SCORE_` 开头时，中心 RetryPolicy 必须拒绝。
4. 同一图片/视频生成成功后，QA 回调不能增加 provider create 调用数。
5. QA issue 不能进入 screenplay/storyboard/video repair router。
6. delivery blockers 中不能出现 QA 分数、fatal quality 或 consistency issue。
7. 前端可操作性选择器不能读取 `hard_gate_passed`、QA status 或 score threshold。
8. 历史 hard gate=false 的资产经新结构资格计算后仍可显示、采用和交付；真实文件/上下文缺失除外。

## 11. 监控与审计指标

上线后至少监控：

| 指标 | 目标/告警 |
|---|---|
| `qa_triggered_generation_total` | 必须恒为 0，任何非零立即告警 |
| `qa_triggered_block_total` | 必须恒为 0，任何非零立即告警 |
| `retry_total{error_class}` | 只能出现 allowlist 中的偶然错误类 |
| `retry_policy_violation_total` | 必须为 0 |
| `paid_generation_count_per_user_request` | QA 完成前后不得上升；用户主动重做和 provider 瞬时故障需分标签 |
| `qa_score_unavailable_rate` | 只影响评分服务健康，不影响主流程成功率 |
| `technical_media_failure_rate` | 用于确认放开 QA 后没有把损坏文件当成功 |
| `structural_interception_total` | 按 JSON/Schema/参数/上下文/文件类别可追溯 |
| `delivery_ready_rate` | 预计上升；同时观察缺文件、缺血缘等真实 blocker 不得下降为漏检 |
| `user_manual_regenerate_rate` | 用于衡量用户对低分结果的主动选择，不得转化为系统自动重做 |

所有 retry 事件需记录：请求 ID、幂等键、error class、是否产生新付费任务、次数上限、原始错误摘要和最终结果。QA 报告不得记录为 retry 原因。

## 12. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 低质量内容更容易进入下游 | 用户会看到更多低分结果 | 明确评分、风险提示、排序和人工重做入口；不替用户自动烧钱 |
| 旧代码仍读取 `hard_gate_passed` | 形成隐性拦截 | 新字段双写、前端先切换、静态扫描和架构测试；最终删除读取分支 |
| 把能力不足误分类为偶然错误 | 变相保留无限重试 | 中心 allowlist、稳定错误码、同指纹上限、禁止 QA 文本驱动分类 |
| 把上下文遗漏误判为内容没体现上下文 | 继续付费重做 | 上下文遗漏只能由请求前确定性证据判定，不能由 QA 结果反推 |
| 放开 QA 时误删真实结构门禁 | 损坏数据或不可播放文件进入下游 | 先拆结构 validator，再移除 QA 分支；按 10.1 逐类注入故障 |
| 历史数据 status 语义冲突 | 页面显示和运行资格不一致 | 保留 legacy 字段、增加适配层、结构资格实时重算，不破坏迁移 |
| Supervisor 仍通过其他名字触发质量重做 | 隐性费用增加 | 监控 provider create 调用来源，架构测试禁止 score/issue→enqueue 路径 |
| 版权/内容政策自动改词重试被误保留 | 确定性失败反复付费 | 单列 deterministic rejection，明确禁止自动重提 |

## 13. 完成定义（Definition of Done）

以下条件全部满足，整改才算完成：

- 35 项清单逐项有代码变更、测试或明确的无行为证明；
- 33 个运行时 QA 决策点全部移除 QA 控制权；
- QA 低分、QA hard failure、QA 超时和 QA 非法 JSON 都不会阻断主流程；
- 图片/视频不存在 QA 驱动的自动重新生成；
- 文本不存在业务质量驱动的自动修复、重规划或续跑；
- RetryPolicy 只允许本 PRD 5.1 中的偶然错误类；
- 结构错误、参数缺失、上下文遗漏和损坏文件仍能被准确拦截；
- 权限、预算、确认、幂等、并发、版本和安全控制全部回归通过；
- 交付可以携带低 QA 分数完成，但不能携带缺失/损坏文件或断裂血缘完成；
- 旧 QA 门禁和重试配置无法从 UI、API 或隐藏设置重新开启；
- 全量相关自动化测试通过，预发布低分样本和故障注入验收通过；
- 上线观察期内 `qa_triggered_generation_total=0` 且 `qa_triggered_block_total=0`。

## 14. 需求追踪表

| 需求 ID | 需求 | 优先级 | 验收证据 |
|---|---|---:|---|
| QA-SO-001 | QA evaluation 永远只评分，不具备运行时阻断权 | P0 | 架构测试 + 低分全链路 E2E |
| QA-SO-002 | 图片/视频取消 QA 驱动自动重做 | P0 | provider create 次数断言 |
| QA-SO-003 | 剧本/分镜取消业务质量修复和发布门禁 | P0 | 低质量但结构合法样本可发布/确认 |
| QA-SO-004 | 建立中心偶然错误分类和 retry allowlist | P0 | 分类单测 + 故障注入 |
| QA-SO-005 | 参数/上下文/结构/文件错误继续准确拦截 | P0 | 负向契约测试 |
| QA-SO-006 | 评审墙和交付停止读取 QA 资格 | P0 | API/前端/交付 E2E |
| QA-SO-007 | 历史数据、版本、证据和非 QA 安全控制不回归 | P0 | 迁移回归 + 历史 fixture |
| QA-SO-008 | 删除所有可重新开启 QA 门禁/重试的设置和代码分支 | P1 | 静态扫描 + 配置/API 测试 |
| QA-SO-009 | QA 分数、问题、排序和人工重做体验保留 | P1 | 页面与接口验收 |
| QA-SO-010 | 建立零 QA 拦截/零 QA 重做监控 | P1 | 指标面板与告警演练 |
