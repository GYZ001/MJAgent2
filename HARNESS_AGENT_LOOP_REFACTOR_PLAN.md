# MJAgent2 Harness + Agent Loop 全项目可信化改造方案

> 状态：v1.1 修订稿，可直接用于拆分迭代与评审  
> 编制日期：2026-07-18  
> 基线更新：2026-07-18 第二轮代码优化后  
> 适用范围：后端、前端、数据模型、提示词、模型调用、任务队列、质量评估、测试与交付流程  
> 核心目标：让每一次生成都可追溯、每一次修复都可解释、每一个采用结果都有证据、每一份客户交付都能被复验。

### v1.1 修订说明

本版已按 2026-07-18 最新代码重新校准，不再把以下已经完成的工作列为未来整改项：数据库唯一约束/父子校验/级联与孤儿修复、后台任务统一注册与停机清理、派生媒体统一失效、固定 5 秒视频合同、正则“一章一集”、开发依赖、CI 和基础回归验证。

当前已实测：148 项测试通过、Ruff 通过、Python 全量编译通过、TypeScript/Vite 构建通过。现有备份与隔离资产也已核实位于 `data/backups/`。

---

## 0. 执行摘要

本项目已经不是“从零搭 Agent”的阶段。当前代码已经拥有结构化输出、业务校验、修复重试、视频队列、幂等键、成本熔断、关键帧/视频 QA、多模型路由、错误日志、任务统一注册、派生产物统一失效、数据库完整性修复和重启恢复能力。`planning.py`、`system_api.py`、`task_registry.py`、`artifacts.py` 的拆分也降低了原有大文件的部分职责。真正剩余的问题是：这些能力还没有形成跨阶段、可持久化、可复验的运行与交付协议。

这会产生四类不可信：

1. **过程不可信**：自动流水线的阶段、循环、尝试和退出原因没有统一持久化模型，部分状态只在内存中。
2. **结果不可信**：Schema 合法、业务规则通过、VLM 高分、人工采用等不同强度的证据混在一起，UI 上难以回答“为什么它可以交付”。
3. **恢复证据不完整**：任务注册和启动恢复已有统一入口，但 Run/Step 的检查点、重试原因和恢复轨迹尚未形成统一持久化记录；媒体延迟重试仍有进程内状态。
4. **改进不可信**：提示词、模型、规则修改后缺少可重复的金样基线和版本对比，难以证明改动让客户结果变好，而不是只让某个样例变好。

因此，本次改造不建议引入“多 Agent 自由对话”或新的分布式基础设施。目标架构是：

```text
用户意图 / 项目目标
        ↓
Workflow Engine（唯一编排者、持久化状态机）
        ↓
Execution Harness（合同、上下文、预算、模型/工具网关、证据、事件）
        ↓
Bounded Agent Loop（观察 → 决策 → 执行 → 评估 → 修复/升级 → 提交）
        ↓
Immutable Artifacts + Evaluations + Human Gates
        ↓
Delivery Manifest + 验收报告 + 可复验交付包
```

一句话原则：**Harness 决定 Agent 能做什么、用什么证据证明做对了；Agent Loop 只在受控边界内寻找更好的结果。**

---

## 1. 改造目标与非目标

### 1.1 业务目标

改造后的系统必须能对客户和制作人员回答以下问题：

- 这一集依据了哪些原文章节、哪一版人物谱、场景库和提示词？
- 每个阶段运行了几次，为什么重试，为什么停止？
- 当前成片通过了哪些确定性检查、模型评估和人工门禁？
- 哪些问题仍然存在，系统为何仍允许交付，谁做了接受决定？
- 模型、规则或 Prompt 变化后，质量相对上一基线是提升还是退化？
- 进程中断后从哪个检查点恢复，是否可能重复花费或覆盖已确认资产？

### 1.2 工程目标

1. 所有长流程都有持久化的 `run → step → attempt/iteration → artifact → evaluation → decision` 证据链。
2. 所有 Agent 循环都有最大轮次、成本、时间、停滞检测、质量阈值和人工升级条件。
3. 所有模型调用和外部工具调用必须经过统一 Harness，不允许业务模块直接绕过。
4. 所有下游输入只引用已版本化 Artifact，不直接读取“当前最新 JSON”作为隐式上下文。
5. 所有交付都生成 manifest、文件校验结果、质量卡和残余风险清单。
6. 保持单机 FastAPI + SQLite + 文件系统 + asyncio，不引入 Redis、Celery、Kafka 或微服务。

### 1.3 明确非目标

- 不建设通用 Agent 平台、插件市场、角色聊天群或任意工具自主调用框架。
- 不让多个模型通过自由辩论决定最终结果；关键结论必须落到可检查的 Artifact 和 Evaluation。
- 不用一个“综合分”掩盖硬性失败。任何硬门禁失败都不能被其他高分抵消。
- 不用 LLM 自评替代确定性校验、文件检查或人工验收。
- 不一次性重写全部业务代码；采用旁路记录、适配器和逐阶段迁移。
- 不把“流程治理”重新膨胀成 1.0 式企业平台；新增抽象必须直接改善恢复、验收或质量。

---

## 2. 当前项目基线与主要缺口

### 2.1 2026-07-18 新基线

| 现有能力 | 当前位置 | 改造后的归属 |
|---|---|---|
| Pydantic Schema + 业务校验 | `app/schemas.py`、`app/validators.py` | Contract Registry + Deterministic Evaluator |
| 校验失败回喂模型修复、停滞检测 | `app/stages.py::_run_with_repair` | 通用 `AgentLoop` 的 repair policy |
| 顺序生成分镜、分镜大纲和上下文接力 | `app/stages.py` | Storyboard Loop |
| Prompt 确定性编译与安全清洗 | `app/compiler.py` | Tool Harness / Prompt Compiler |
| 视频队列、幂等、轮询、成本暂停 | `app/worker.py` | Media Job Executor + Budget Policy |
| 关键帧和视频 VLM QA | `app/stages.py`、`app/worker.py`、`app/video_modes.py` | Independent Evaluator |
| Provider 调用生命周期记录 | `app/hiagent.py`、`provider_calls` | Model Gateway Trace |
| 错误分类与错误 ID | `app/errors.py`、`error_logs` | Run Event + Failure Taxonomy |
| 人物定妆、场景参考、参考图一致性 | `app/portraits.py`、`app/scenes.py`、`app/refs.py`、`app/video_modes.py` | Reference Asset Loop |
| 正则章节映射，一章一集 | `app/planning.py` | 确定性 Episode Mapping Step，不再使用 Agent |
| 后台任务统一注册/取消/等待/停机 | `app/task_registry.py` | 进程内 Task Runtime，后续由持久化 Engine 包装 |
| 派生关键帧、视频、成片统一失效 | `app/artifacts.py` | Runtime Asset Cleanup，不等同于证据 Artifact Store |
| 唯一约束、父子校验、级联和历史修复 | `app/db.py` | Data Integrity Foundation |
| 固定 5 秒视频合同 | `app/config.py`、Schema、编译器、Prompt、测试 | Shot Contract 硬约束 |
| 开发依赖与 CI | `requirements-dev.txt`、`pyproject.toml`、`.github/workflows/ci.yml` | Release Gate 基础 |
| 前端监控与评审入口 | `MonitorPage`、`BoardPage`、`WallPage`、`CinemaPage` | Run Center + Evidence Panel + Delivery Gate |

本轮本地复验结果：

- `pytest -q`：148 passed；
- `ruff check app tests`：通过；
- `python -m compileall -q app`：通过；
- `npm run build`：TypeScript 与 Vite 构建通过；
- 数据备份：`data/backups/manju-before-integrity-20260718-194753.db`；
- 孤儿项目隔离：`data/backups/orphan-projects-20260718-194753/`。

### 2.2 关键缺口

#### A. 任务生命周期已集中，但运行证据仍未持久化

`task_registry.py` 已解决“找不到真实 asyncio.Task”“项目删除时任务仍回写”“停机不清理”的主要问题，这是正确的运行时基础。但 registry、`auto.py::_states` 和 `worker.py::_retry_tasks` 都是进程内状态；数据库仍没有一次业务运行的 Run、Step、iteration、checkpoint 和退出原因。

影响：系统能恢复部分任务，却不能完整回答“恢复了哪一次运行、从哪一步恢复、此前做过哪些尝试、是否重复消耗”。

当前还存在一个需要优先热修的兼容残留：`app/auto.py` 仍引用已移除的 `api._bible_tasks` 和 `api._plan_task`。这不会被现有模块级测试发现，但会影响“一键全自动从尚未生成人物谱/分集的项目启动”。应在 Harness 改造前修复，并增加该端到端回归测试。

#### B. 状态字段承载过多语义

`projects`、`episodes`、`shots`、`jobs` 各自维护多套字符串状态，状态迁移散落在 API、auto、worker 中，没有一个权威状态机校验迁移是否合法。

影响：难以证明某个结果经过了完整步骤，也难以安全地重新执行局部阶段。

#### C. 已有运行时清理层，但缺少不可变证据产物

新的 `app/artifacts.py` 已成为派生媒体清理与失效的权威入口，能防止旧关键帧/视频/成片继续展示；但它的 Artifact 含义是“运行时资产清理”，不是方案所需的不可变证据产物。人物谱、剧本、分镜、参考图、Prompt、视频、QA 仍缺少统一 Artifact ID、内容哈希、父产物列表、创建运行、采用状态与失效原因。

影响：运行时旧文件虽然会被正确作废，但系统仍无法证明某次交付曾经使用过什么、为什么作废、由什么版本替代。后续应采用“双层语义”：展示/生成层可以清理旧派生文件，Evidence Store 保留 tombstone、hash、血缘和作废原因。

#### D. 修复循环没有统一决策记录

`_run_with_repair` 已经有错误历史、最大尝试和 stall 检测，这是很好的基础；但不同阶段的 `fallback_to_last=True`、残余错误、QA 恢复解析、媒体重试各自定义“可接受”的含义。

影响：一个结果可能是“完全通过”“结构通过但有残余错误”“QA 解析不完整后补零”“模型不可用但保留旧结果”，用户难以区分可信等级。

#### E. 评估证据强度未分层

当前系统会记录业务错误、VLM 分数、`qa_recovered`、人工 adopted version，但尚未将它们组合成明确的硬门禁、软评分、证据覆盖率与人工决策。

影响：高分不等于可交付；低分也不一定说明模型真的失败，尤其是评估器自身异常时。

#### F. 工程验证基线已补齐，生成质量基线仍不足

148 项测试、Ruff、Python 编译、前端构建和 CI 已经可重复执行，原方案中的环境缺口已关闭。剩余问题是 `.benchmarks` 尚未成为 Prompt、模型、评估器和规则变更的强制质量门，测试主要证明工程行为正确，不能证明客户交付质量提升。

影响：代码回归可被发现，但剧情忠实度、视觉一致性、客户修改率等结果退化仍可能进入主干。

#### G. 核心文件虽已拆分但仍较大

拆分后代码规模已下降，但核心文件仍较大：`app/api.py` 约 1608 行、`app/worker.py` 约 1157 行、`app/validators.py` 约 1164 行、`app/stages.py` 约 1152 行、`app/video_modes.py` 约 953 行。`planning.py`、`system_api.py`、`task_registry.py`、`artifacts.py` 已形成清晰边界，后续应沿相同方式按职责迁移，而不是再次在新模块中堆积跨层逻辑。

#### H. 数据完整性修复需要可审计化

本轮已对存量数据完成备份、孤儿清理与隔离，当前孤儿计数为 0。后续 `_repair_integrity` 或数据库迁移若继续删除重复/孤儿行，应记录迁移版本、删除计数、受影响 ID 和备份位置；涉及媒体文件时优先隔离而不是静默永久删除。数据库正确不应以失去修复证据为代价。

#### I. 产品合同已改，但规范文档仍有漂移

代码已采用正则“一章一集”和固定 5 秒镜头，但当前 `PRD.md` 仍保留滚动章节摘要、LLM Episode Plan、`target_duration_s/10`、15 秒口播预算和“最多额外 2 镜”等旧描述；`docs/PROMPT_SPEC.md` 也仍保留 Episode Plan 章节、滚动摘要、15 秒口播与部分旧时长规则。它们会误导后续开发或 Agent 再次引入已删除逻辑。

处理原则：H0 阶段必须将 PRD、Prompt Spec、README、Schema、运行时代码和测试视为同一 Contract Surface，一次性消除冲突；CI 增加静态合同扫描，禁止旧关键词重新出现。

---

## 3. 目标架构

### 3.1 分层

```text
┌──────────────────────────────────────────────────────────────┐
│ UI / API                                                     │
│ 创建运行、查看时间线、人工门禁、采用版本、下载交付包          │
├──────────────────────────────────────────────────────────────┤
│ Workflow Control Plane                                      │
│ 持久化状态机、调度、恢复、取消、依赖、并发、检查点            │
├──────────────────────────────────────────────────────────────┤
│ Execution Harness                                           │
│ 合同、ContextPack、Policy、预算、幂等、Model/Tool Gateway     │
├──────────────────────────────────────────────────────────────┤
│ Bounded Agent Loops                                         │
│ Bible / EpisodeMap / Screenplay / Storyboard / Reference / Video │
├──────────────────────────────────────────────────────────────┤
│ Evaluation & Gates                                          │
│ 确定性规则、来源忠实度、独立模型评估、人工验收、交付门禁       │
├──────────────────────────────────────────────────────────────┤
│ Artifact & Evidence Store                                   │
│ 不可变版本、血缘、哈希、事件、评估、决策、文件 manifest       │
├──────────────────────────────────────────────────────────────┤
│ Provider / Filesystem / SQLite / ffmpeg                     │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 单一编排者原则

全项目只保留一个 Workflow Engine 作为业务编排权威。各阶段 Agent 不互相直接调用，不自行更新项目/剧集总状态；它们只接收 `StepContext`，产出候选 Artifact 和 Evaluation，再由 Engine 提交状态迁移。

这能避免：

- Screenplay Agent 直接清空下游；
- Video worker 自行决定整集 done；
- API 路由、auto 流程和恢复任务各自复制状态机；
- 某阶段不知道是谁、基于什么输入触发了它。

### 3.3 Harness 的六个职责

1. **Contract**：输入、输出、业务不变量、可修复问题、不可修复问题。
2. **Context**：只把经过选择且带版本/hash 的上下文交给 Agent，控制长度并记录裁剪策略。
3. **Policy**：模型、温度、超时、最大轮次、成本、并发、重试、降级和人工升级规则。
4. **Gateway**：统一承接模型与工具调用，生成 trace、调用幂等键、错误分类和敏感信息脱敏。
5. **Evidence**：记录每次候选、每条评估、问题差异、选择原因和残余风险。
6. **Commit**：只有满足门禁的候选才能成为 adopted Artifact；上游变更通过血缘自动标记下游 stale。

---

## 4. 统一 Agent Loop 协议

### 4.1 标准循环

所有文本、图像、视频 Agent 使用同一个逻辑骨架，不同阶段只替换策略和工具：

```text
OBSERVE
  读取 ContextPack、目标、合同、上轮问题、预算和可用工具
    ↓
PLAN
  选择 generate / patch / regenerate / compare / escalate
    ↓
ACT
  调用模型或确定性工具，产生候选 Artifact
    ↓
EVALUATE
  Schema → 业务规则 → 来源/连续性 → 独立质量评估
    ↓
DECIDE
  pass → COMMIT
  repairable → 下一轮（携带结构化 issue + diff）
  no-progress / budget-exhausted / hard-fail → ESCALATE 或 FAIL
    ↓
COMMIT
  冻结 Artifact、记录评估与采用理由、发出下游事件
```

### 4.2 每一轮必须记录

- `run_id / step_run_id / iteration_no`
- `goal` 与明确的完成定义
- 输入 Artifact ID、版本、hash
- ContextPack 的选择结果与被截断内容摘要
- Prompt/Contract/Policy 版本
- provider、model、模型参数、调用 trace
- 候选 Artifact ID、hash、生成耗时与成本
- 评估器版本、各维度分数、硬门禁结果、证据定位
- 相对上一轮解决的问题、新增问题、是否有净改进
- 决策：接受、定向修复、整版重生、换模型、人工升级、失败
- 退出原因：达标、停滞、预算耗尽、不可修复、取消、外部故障

### 4.3 统一退出策略

Agent Loop 不是“重试到成功”。每个阶段必须同时配置：

| 限制 | 默认策略 |
|---|---|
| 最大生成轮次 | 文本 4，图片 3，视频 2；按阶段可调整 |
| 最大同类错误停滞轮次 | 2；问题指纹不变则停止 |
| 最小质量增益 | 连续两轮综合改善 `< 0.03` 则停止无效修复 |
| 最大单步耗时 | 按 provider P95 的 3~5 倍，写入 policy snapshot |
| 最大成本 | 项目、剧集、镜头、step 四层预算中取最小剩余额度 |
| 硬失败 | 鉴权、非法参数、源材料缺失、合同版本不支持、文件校验失败 |
| 人工升级 | 预算耗尽、有争议的来源忠实度、评估器分歧、硬门禁无法自动修复 |

现有 `STALL_ROUNDS` 可迁入统一策略，但不再只比较错误字符串；应比较标准化 `issue_code + subject + evidence_span` 指纹。

### 4.4 修复必须面向问题，不面向“再试一次”

每个 Issue 使用结构化格式：

```json
{
  "code": "SOURCE_CLAIM_UNSUPPORTED",
  "severity": "blocker",
  "subject": "episode:3/shot:7/action_desc",
  "message": "动作包含原文和剧本均未出现的法器",
  "evidence": {
    "artifact_id": "art_screenplay_xxx",
    "span": "scene_outline[2]"
  },
  "repair_hint": "删除该法器，改用 source_excerpt 中的木剑",
  "repairable": true
}
```

修复轮优先产出 JSON Patch 或局部字段替换，并重新跑全量校验。只有结构大面积失效、问题跨越多数节点时才整版重生，以减少“修好 A 又破坏 B”。

---

## 5. Artifact、证据链与可信等级

### 5.1 所有交付中间物统一为 Artifact

Artifact 类型至少包含：

- `novel_source`、`chapter_set`、`chapter_summary`
- `character_bible`、`scene_bible`、`episode_mapping`
- `episode_screenplay`、`storyboard_outline`、`storyboard`
- `compiled_prompt`、`character_reference`、`scene_reference`、`shot_reference_set`
- `shot_video`、`episode_video`、`delivery_package`

每个 Artifact 必须具备：

- 不可变内容或文件引用；修改即创建新版本；
- `content_hash` 或文件 SHA-256；
- 创建它的 step/iteration；
- 父 Artifact 列表；
- Contract、Prompt、Model、Policy 版本；
- 状态：`candidate / validated / approved / rejected / superseded / stale`；
- 失效原因和替代 Artifact；
- 评估、人工决定与导出记录。

### 5.2 可信等级不是一个模糊分数

UI 对每个 Artifact 展示明确等级：

| 等级 | 含义 | 可否进入下游 |
|---|---|---|
| T0 Generated | 只有生成声明，未验证 | 否 |
| T1 Structured | Schema、文件格式、字段完整性通过 | 仅允许进入进一步评估 |
| T2 Validated | 确定性业务规则和来源约束通过 | 允许进入低成本下游 |
| T3 Independently Evaluated | 独立评估器通过且证据覆盖达标 | 允许进入自动媒体生成，仍受预算门禁 |
| T4 Human Approved | 人工明确采用并记录意见 | 允许进入昂贵或交付阶段 |
| T5 Delivery Verified | 交付包完整性、可播放性、manifest 和客户标准通过 | 可交付 |

`qa_recovered=true`、评估解析不完整、残余 blocker、输入已 stale 的产物不得标为 T3。

### 5.3 硬门禁与软评分分开

任何以下条件失败，结果一律不可交付，不能被平均分抵消：

- 文件不存在、不可读取、视频无法解码，或单镜时长偏离固定 5 秒合同；
- 上游 Artifact 已 stale；
- 角色、专名、源章节、镜头编号、台词归属等确定性合同失败；
- 有未解决 blocker；
- 必须人工确认的门禁未确认；
- 成本超过硬上限且未得到明确授权；
- 评估证据缺失或评估器本身失败，却被误标为“通过”；
- 交付 manifest 与真实文件 hash 不一致。

硬门禁通过后，再计算 0~100 软质量分。建议剧集级初始维度：

| 维度 | 权重 | 主要证据 |
|---|---:|---|
| 原著忠实与关键剧情覆盖 | 25 | source excerpt 对齐、事实/专名校验、人工抽检 |
| 戏剧完整性与节奏 | 20 | 剧本/分镜规则、独立 critic、人工评审 |
| 角色与场景一致性 | 20 | 参考图血缘、跨镜 VLM、人工抽检 |
| 动作与画面符合度 | 15 | 视频抽帧 VLM、镜头 action 对齐 |
| 技术完整性 | 10 | 文件、时长、分辨率、音视频/拼接检查 |
| 交付完整性与可复验性 | 10 | manifest、hash、版本和残余风险清单 |

建议初始交付线为总分 ≥80、任一核心维度不得低于 70、证据覆盖率 ≥90%，但这些阈值必须用人工验收数据校准，不能凭主观永久固定。

### 5.4 评估器独立性

- 生成模型不得成为唯一评估者。
- 同模型评估可以提供建议，但不能单独升级可信等级。
- 确定性校验优先；视觉问题使用 VLM；审美和客户偏好保留人工门禁。
- 关键评估尽量使用不同 prompt、不同上下文，必要时使用不同模型/provider。
- 当确定性规则与 VLM 冲突时，规则结果优先；当两个评估器分差过大时，不取平均，进入“评估分歧”人工队列。

---

## 6. 数据模型改造

保留现有业务表和媒体表，新增 5 张统一控制表。SQLite 足够，不引入事件总线。

### 6.1 `workflow_runs`

记录一次项目级、剧集级或镜头级运行。

```text
id, workflow_type, scope_type, scope_id, parent_run_id
status, current_step_key, requested_by, trigger_type
input_fingerprint, policy_snapshot_json, config_snapshot_json
budget_limit_cny, cost_cny, deadline_at
started_at, updated_at, finished_at
failure_code, failure_message, resume_from_step
```

状态：

```text
CREATED → RUNNING ↔ WAITING_RETRY
                  ↔ WAITING_HUMAN
                  ↔ PAUSED_BUDGET
                  ↔ PAUSED_EXTERNAL
        → SUCCEEDED | PARTIAL | FAILED | CANCELLED
```

### 6.2 `step_runs`

记录每个阶段及其循环轮次。若希望减少表数，iteration 直接作为同一 step_key 下多行。

```text
id, run_id, step_key, iteration_no, parent_step_run_id
status, agent_name, contract_version, prompt_version, policy_version
input_artifact_ids_json, context_manifest_json
output_artifact_id, issue_fingerprint, decision, exit_reason
started_at, finished_at, latency_ms, cost_cny
error_code, error_message
```

状态：

```text
PENDING → READY → RUNNING → EVALUATING
                          → REPAIRING → RUNNING
                          → WAITING_HUMAN
                          → SUCCEEDED | WARNING | FAILED | CANCELLED | SKIPPED
```

### 6.3 `artifacts`

```text
id, type, scope_type, scope_id, version
status, trust_level, content_json, file_path, content_hash
created_by_step_run_id, parent_artifact_ids_json
contract_version, prompt_version, model_snapshot_json
stale_reason, superseded_by_artifact_id
created_at, approved_at
```

大 JSON 初期仍可存 SQLite；媒体只存路径和 hash。后续若 JSON 变大，可迁到项目目录，表内保留 manifest，不改变上层接口。

### 6.4 `evaluations`

```text
id, artifact_id, step_run_id, evaluator_type, evaluator_name
evaluator_version, status, hard_gate_passed
score, dimension_scores_json, issues_json, evidence_json
raw_result_ref, confidence, recovered
created_at
```

`recovered=1` 表示评估输出经过容错解析，只能提供参考，不能独立触发自动采用。

### 6.5 `run_events`

追加写的时间线，不做复杂事件溯源，只作为审计和 UI 增量读取：

```text
id, run_id, step_run_id, ts, event_type, severity
message, payload_json, trace_id
```

事件示例：`RUN_STARTED`、`STEP_STARTED`、`MODEL_CALLED`、`ARTIFACT_CREATED`、`EVALUATION_FAILED`、`REPAIR_REQUESTED`、`HUMAN_APPROVED`、`BUDGET_PAUSED`、`RUN_RESUMED`、`DELIVERY_VERIFIED`。

### 6.6 与现有表的关系

- `provider_calls` 继续保存外部调用明细，新增 `run_id / step_run_id / trace_id`。
- `jobs` 继续作为图片/视频 provider 任务队列，新增 `run_id / step_run_id / lease_owner / lease_expires_at / retry_count / next_retry_at`。
- `shot_versions`、`shot_scenes` 继续服务现有 UI，同时关联对应 Artifact ID。
- `projects`、`episodes`、`shots` 的旧状态暂时保留作为投影字段，由 Workflow Engine 统一更新；业务代码禁止自行写状态。
- 迁移稳定后，再删除重复且容易漂移的细粒度状态列，而不是第一阶段就破坏兼容。

---

## 7. 状态机、恢复、并发与幂等

### 7.1 状态迁移只能经过 Engine

提供统一 API：

```python
transition_step(step_run_id, expected_from, to, reason, evidence=None)
transition_run(run_id, expected_from, to, reason)
commit_artifact(step_run_id, artifact_id, evaluations)
invalidate_descendants(artifact_id, reason)
```

更新必须使用事务和 compare-and-set：`UPDATE ... WHERE id=? AND status IN (...)`。受影响行数不是 1 即视为并发冲突，不允许悄悄覆盖。

### 7.2 Lease 代替“内存里有 task 就算正在运行”

Worker 领取 `step_run/job` 时写入 `lease_owner` 和 `lease_expires_at`，执行期间续租。进程崩溃后，过期 lease 自动回到 READY/WAITING_RETRY；恢复逻辑不再依赖 `_tasks` 字典是否存在。

### 7.3 三层幂等

1. **业务幂等**：同一 workflow、scope、输入 Artifact hash、policy snapshot 生成 `input_fingerprint`；已有成功 Run 可提示复用。
2. **步骤幂等**：`step_key + input_artifact_hashes + contract/prompt/policy version` 生成 step fingerprint。
3. **Provider 幂等**：保留视频 `idem_key`，并扩展到图片和可安全复用的模型调用。

“重抽”必须显式加入 nonce；“修复”必须改变 issue set 或 patch；不能用时间戳偷偷破坏正常幂等语义。

### 7.4 取消语义

取消分为：

- `CANCEL_REQUESTED`：停止领取新步骤；
- 可取消的本地/模型调用尽快中止；
- 上游不可取消的 provider 任务继续观察并记录成本，但结果标为 orphan candidate，不自动采用；
- 已提交 Artifact 不删除，只停止下游；
- 重新启动创建新 Run 或从安全检查点 resume，不能把 cancelled 直接改回 running。

### 7.5 依赖与失效传播

上游 Artifact 新版本被采用时，通过 `parent_artifact_ids` 找到后代：

- 未开始的 step 直接重新绑定新输入；
- 已生成但未人工采用的产物标 `stale`；
- 已人工采用或已交付的产物不自动删除，创建变更影响报告，由用户决定是否重做；
- 媒体文件默认保留，避免现有清理逻辑误删可回滚资产。

---

## 8. 各业务阶段的目标 Agent Loop

### 8.1 Character Bible Loop

```text
章节采样/摘要 Artifact
→ 生成候选人物谱
→ Schema/专名/重复角色/覆盖率校验
→ 对后段章节做角色发现抽检
→ 缺失则定向增补，不整版重写已锁定角色
→ 人工确认并锁定 T4
```

门禁：主角、核心关系、画风锚点完整；角色名可定位原文；已锁定 canonical 字段未经人工批准不得自动变化。

### 8.2 Deterministic Episode Mapping Step

本阶段不再是 Agent Loop。最新产品合同为：小说摄入按正则切章后，每章确定性映射为一集，`app/planning.py` 是唯一实现。

```text
Chapter Set Artifact
→ 按 idx 排序
→ episode_no 连续编号
→ 每集 source_chapters = [chapter.idx]
→ title/preview 确定性生成
→ 覆盖率、唯一性、父子完整性校验
→ Episode Mapping Artifact
```

门禁：章节覆盖率 100%、一章且仅对应一集、集号连续、`UNIQUE(project_id, episode_no)` 通过、没有 LLM 调用。该步骤必须纳入 Run/Artifact 证据链，但不得重新引入 AI 分集、章节摘要或 Plan Schema。

### 8.3 Screenplay Loop

```text
源章节 + Bible + Episode Mapping + 上集结尾
→ 生成可拍剧本
→ Schema 与业务校验
→ Source Fidelity Evaluator 标注无依据事实/遗漏关键点
→ Dramaturgy Critic 评估目标、阻力、转折、尾钩
→ 局部修复
→ 全量重验
→ 人工确认
```

改造重点：把现有 `fallback_to_last=True` 改为显式 `WARNING` 候选。残余问题可展示和编辑，但 blocker 未解决时不能被标为 ready，也不能进入昂贵下游。

### 8.4 Storyboard Loop

保留当前“先大纲、再逐镜生成”的正确方向，增强为持久化循环：

```text
Screenplay T2/T4
→ 生成 Storyboard Outline Artifact
→ 覆盖率/顺序/声轨/固定 5 秒拆镜预检
→ 每镜 Observe（大纲项 + 上一镜尾状态 + 剩余预算）
→ 生成候选镜头
→ 局部 + 全局增量校验
→ 写入 checkpoint
→ 末尾跑整集连续性、剧情覆盖和声音覆盖评估
→ 必要时只重做问题镜段
```

每生成一镜即创建 checkpoint，重启后从最后一条通过 T2 的镜头继续。不得重新生成已通过且未被依赖变化影响的前序镜头。

### 8.5 Reference Asset Loop

```text
锁定的 canonical 描述 + 场景/角色血缘
→ 生成 N 个候选
→ 文件/尺寸/内容安全预检
→ 单图 VLM QA
→ 与锚点、上一版本、同组资产做一致性比较
→ 选择最佳或 i2i 定向修复
→ 人工采用/自动采用（仅在高证据条件下）
```

自动采用条件必须包含：评估器未 recovered、最低分与相对一致性均过线、无 blocker、父 Artifact 未 stale。

### 8.6 Video Generation Loop

```text
Storyboard Shot + Compiled Prompt + Reference Set
→ 参数/长度/预算/输入文件 preflight
→ provider 创建与轮询
→ 下载并校验 hash、容器、时长约等于 5 秒、分辨率、解码
→ VLM 对角色/动作/干净画面评分
→ 基于具体 issue 选择：换参考图 / 改 prompt / 重抽 seed / 人工处理
→ 比较全部成功版本
→ 采用有明确选择理由的版本
```

“第一个成功视频”只能成为候选，不能因为 `adopted_version_id IS NULL` 就天然成为最终选择。若自动采用，应记录选择函数、比较集合和分差。

### 8.7 Episode Delivery Loop

```text
全部 adopted shot videos
→ 顺序/缺口/文件/单镜 5 秒合同与总时长检查
→ 合成或生成镜头序列
→ 全集视觉连续性与关键剧情覆盖抽检
→ 计算硬门禁、质量卡、证据覆盖率
→ 人工交付门禁
→ 生成 delivery package + manifest + report
```

交付包至少包含：

- 成片或按序镜头文件；
- 最终人物谱、剧本、分镜和使用的 Prompt 版本快照；
- `manifest.json`：所有文件 hash、Artifact ID、模型/规则版本；
- `quality-report.json/html`：硬门禁、各维度分数、人工决定；
- `known-issues.md`：残余问题、风险、已接受原因；
- `run-summary.json`：运行耗时、成本、失败/修复次数、恢复记录。

---

## 9. 代码目录与模块拆分建议

在不一次性搬空现有文件的前提下新增以下目录：

```text
app/
├── orchestration/
│   ├── engine.py             # 创建、推进、恢复、取消 Run
│   ├── state_machine.py      # 合法状态与 CAS 迁移
│   ├── scheduler.py          # READY step、依赖、lease、并发
│   ├── policies.py           # 重试、预算、停滞、升级策略
│   └── recovery.py           # 过期 lease 和孤儿任务恢复
├── harness/
│   ├── contracts.py          # StageContract 注册表
│   ├── context.py            # ContextPack 构造与 manifest
│   ├── model_gateway.py      # 包装 hiagent，强制 trace/policy
│   ├── tool_gateway.py       # compiler/ffmpeg/files/provider 工具
│   ├── budget.py             # 多层预算预留与结算
│   └── idempotency.py
├── loops/
│   ├── base.py               # AgentLoop 标准协议
│   ├── bible.py
│   ├── screenplay.py
│   ├── storyboard.py
│   ├── references.py
│   ├── video.py
│   └── delivery.py
├── evaluations/
│   ├── base.py
│   ├── deterministic.py
│   ├── source_fidelity.py
│   ├── visual.py
│   ├── scorecard.py
│   └── gates.py
├── evidence/
│   ├── repository.py
│   ├── lineage.py
│   └── manifest.py
├── prompts/
│   ├── registry.py
│   └── versions/             # 一阶段一文件，显式版本
└── observability/
    ├── events.py
    ├── tracing.py
    └── metrics.py
```

### 9.1 现有模块迁移映射

| 当前模块 | 目标动作 |
|---|---|
| `stages.py` | 保留领域 prompt/normalize，逐个阶段迁到 `loops/`；删除通用重试复制 |
| `validators.py` | 按 bible/screenplay/storyboard/media 拆分，统一返回结构化 Issue |
| `api.py` | 路由只做鉴权/参数/响应；不再 `create_task` 或直接写业务状态 |
| `auto.py` | 最终由 Workflow Engine 替代；前期变成创建/查询 run 的薄适配器 |
| `worker.py` | 分成 job repository、media executor、QA trigger、delivery executor |
| `hiagent.py` | 成为 Model/Provider Gateway 的底层 adapter，业务模块禁止直接 import 调用 |
| `db.py` | 引入版本化迁移文件；逐步停止一个 MIGRATIONS tuple 无限追加 |
| `compiler.py` | 保持确定性纯函数，增加 contract version 和 preflight report |
| `planning.py` | 保持纯确定性“一章一集”，作为 Engine 的普通 Step，不进入 Agent Loop |
| `task_registry.py` | 保留为进程内 Task Runtime；Engine 负责持久 Run/Step，registry 负责真实 Task 句柄 |
| `artifacts.py` | 保留为派生媒体清理层；证据存储使用 `evidence/`，避免名称和语义冲突 |

### 9.2 防止再次过度设计的约束

- 第一阶段只新增上述 5 张控制表。
- 不引入依赖注入框架、消息总线或通用 DAG DSL。
- Workflow 定义先用 Python 数据结构；出现至少 3 个真实复用场景后再抽象 DSL。
- Agent 之间不共享自由文本“思考”；只共享 Artifact、Issue、Evaluation 和 Decision。
- 每个新抽象必须有一个现有重复点作为迁移对象，并有删除旧代码的计划。

---

## 10. API 与前端体验改造

### 10.1 新 API

```text
POST   /api/runs                         创建 workflow run
GET    /api/runs/{run_id}                当前状态、进度、预算、门禁
GET    /api/runs/{run_id}/steps          step/iteration 列表
GET    /api/runs/{run_id}/events         增量时间线
POST   /api/runs/{run_id}/cancel         请求取消
POST   /api/runs/{run_id}/resume         从安全检查点恢复
POST   /api/runs/{run_id}/retry          创建受控重试
GET    /api/artifacts/{artifact_id}       版本、血缘、可信等级
GET    /api/artifacts/{artifact_id}/evals 评估证据
POST   /api/gates/{gate_id}/decision      人工采用/拒绝/带条件接受
GET    /api/deliveries/{id}/report        交付质量报告
```

现有业务 API 暂时保留，内部改为创建相应 Run；前端迁移完成后再废弃旧入口。

### 10.2 前端适配原则：能力必须出现在用户操作路径上

前端适配不是在监制房增加更多原始日志，而是把 Harness 能力放到用户做决定的地方：

- **全局可见**：任务在后台运行时，用户切换到任何工作台都能看到状态和失败；
- **就地解释**：剧本问题在剧本台解释，镜头问题在分镜台解释，视频问题在评审墙解释；
- **先告知影响再执行**：修改、删除、重生和切换采用版前展示影响范围与预计成本；
- **渐进披露**：默认只显示“是否可继续”和最重要问题，技术 trace 按需展开；
- **行动导向**：每个失败都提供明确下一步，例如“修复 3 个问题”“从检查点恢复”“请求人工接受风险”；
- **统一语言**：同一种状态、可信等级、Issue 严重度在所有页面使用相同文案和颜色。

### 10.3 保留现有导航，新增全局 Run Dock

现有书房、人物谱、场景图、分集、剧本台、分镜台、评审墙、成片台、监制房的导航结构保持不变。在 `App.tsx` 根层增加常驻 `RunDock`：

```text
┌ 当前任务：第 3 集分镜  12/18 镜 ───────────────┐
│ 运行中 · 本轮 ¥0.00 · 预计剩余 2 分钟          │
│ [查看详情] [暂停/取消]                         │
└───────────────────────────────────────────────┘
```

Run Dock 展示：

- 当前项目/剧集/镜头和阶段；
- `running / waiting_human / paused_budget / failed / recovered / succeeded`；
- 结构化进度，而不是只有旋转图标；
- 本轮耗时、成本和预计剩余成本；
- 最近一个 blocker；
- 查看详情、取消、恢复或跳到待处理对象。

没有活动任务时收起为侧栏状态点；有失败或等待人工时保持醒目，但不使用持续弹窗打断用户。

### 10.4 监制房 / MonitorPage → Run Center

监制房保留现有模型中心、任务和调用日志，并新增 Run Center 作为默认首屏：

- **运行列表**：按项目、剧集、状态、时间筛选；显示目标、进度、成本和最终结果；
- **步骤时间线**：确定性分集、剧本、分镜、参考图、视频、QA、交付逐步展开；
- **循环视图**：每轮候选解决了什么、新增了什么、质量变化和退出原因；
- **恢复视图**：服务重启、lease 过期、检查点和重复执行防护；
- **外部调用**：provider、模型、延迟、错误 ID，可按 trace 展开现有 `provider_calls`；
- **门禁队列**：集中显示等待人工采用、预算授权或风险接受的项目；
- **操作**：取消、从安全检查点恢复、只重试失败步骤、打开对应工作台。

调用日志仍是诊断工具，不作为普通用户判断“是否可以交付”的主要界面。

### 10.5 各工作台的用户可感知适配

| 当前页面 | 需要增加的 Harness 适配 | 用户直接感知 |
|---|---|---|
| `Studio` 书房 | 最近运行、数据完整性状态、导入后“一章一集”映射报告 | 上传后立即知道识别了多少章、会生成多少集 |
| `BiblePage` 人物谱 | Artifact 版本、T 等级、来源覆盖、保存前影响预览 | 知道修改角色会影响哪些定妆照、镜头和视频 |
| `ScenesPage` 场景图 | 场景图血缘、一致性结果、stale 标记、候选对比 | 能区分“当前使用”“已过期”“质检未通过” |
| `EpisodesPage` 分集 | 明示“正则一章一集、0 LLM”、章节覆盖率和每集就绪状态 | 不再等待或误解 AI 分集，来源关系清晰 |
| `ScriptPage` 剧本台 | Contract 检查、来源证据、Issue 定位、修复前后 diff | 不只看到生成失败，而是跳到具体字段修改 |
| `BoardPage` 分镜台 | 每镜 T 等级、5 秒合同、剧情/声轨覆盖、上下游影响 | 编辑镜头前知道会作废哪些关键帧、视频和成片 |
| `WallPage` 评审墙 | 候选版本横向比较、QA 证据、采用理由、重生目标和成本 | 知道为什么推荐这一版、重生要解决什么问题 |
| `CinemaPage` 成片台 | Delivery Readiness、缺失镜头、stale 检查、质量卡、manifest | 在导出前明确看到“可交付/不可交付”及原因 |
| `MonitorPage` 监制房 | Run Center、门禁队列、恢复和 trace | 能跨项目掌握后台执行与异常 |

### 10.6 统一 Evidence Drawer

剧本、分镜、图片、视频和成片复用同一个侧滑证据面板，避免每个页面各做一套 QA UI。

默认摘要层只显示：

- T0~T5 可信等级；
- 硬门禁：通过、阻塞或等待人工；
- 最重要的 1~3 个 Issue；
- 当前输入版本是否仍有效；
- 推荐下一步操作。

展开后显示：

- Artifact ID、版本、hash 和父产物；
- 确定性校验、VLM/LLM 评估和人工决定分别由谁产生；
- 证据定位到原文章节、剧本字段、镜头或视频帧；
- Prompt、模型、Contract、Evaluator 版本；
- 历次修复 diff、质量变化和残余风险；
- 跳转到 Run Center 对应 step/trace。

可信等级不能只靠颜色表达，必须同时显示文字和解释，例如“`T2 已通过确定性校验，尚未人工确认`”。

### 10.7 修改影响预览与人工门禁

所有会导致下游失效或付费的操作统一使用 `ImpactDialog`，替代简单的 `window.confirm`：

```text
保存人物谱 v6 将导致：
- 3 张定妆照过期
- 7 个镜头参考图失效
- 7 个视频版本和第 3 集成片需要重做
预计重新生成成本：¥28.00

[返回修改] [保存但暂不重生] [保存并创建重做任务]
```

点击确认/采用/交付前展示：

- 本次决策对象及版本；
- 已通过检查和证据覆盖率；
- 尚未解决的问题；
- 进入下一阶段预计成本；
- 上游未来变化会使哪些下游失效。

决策类型：`approve / reject / approve_with_risk / request_revision`。带风险接受必须填写备注并写入 Evaluation/Decision；blocker 默认不能被普通确认绕过。

### 10.8 版本比较与重生成交互

评审墙和场景图至少支持两版并排比较：

- 媒体预览同步播放/切帧；
- 使用的 Prompt 与参考图差异；
- 各质量维度及评估器可信状态；
- 本轮解决 Issue、新增 Issue、成本与耗时；
- 当前采用版、推荐版、已过期版明确标识。

“重生成”按钮必须先选择目标问题：角色漂移、动作不符、画面脏、参考图错误、原词重抽或自定义修改。系统据此选择修复策略，避免无目标地再次烧钱。

### 10.9 前端状态模型与组件建议

新增统一类型和 hooks：

```text
RunSummary / RunDetail / StepRun / RunEvent
ArtifactSummary / EvaluationSummary / Issue / GateDecision
useActiveRuns / useRun / useArtifactEvidence / usePendingGates
```

新增可复用组件：

```text
components/harness/
├── RunDock.tsx
├── RunTimeline.tsx
├── StepIterationList.tsx
├── TrustBadge.tsx
├── GateStatus.tsx
├── IssueList.tsx
├── EvidenceDrawer.tsx
├── ImpactDialog.tsx
├── VersionCompare.tsx
├── CostEstimate.tsx
└── DeliveryChecklist.tsx
```

第一阶段继续复用现有 `usePoll`，只增加 `/runs?active=true` 和事件游标轮询，降低改造风险；运行事件量增大后再升级 SSE，不需要一开始引入 WebSocket 或新的状态管理框架。

### 10.10 前端验收标准

- 用户在任意页面最多 1 次点击可看到当前任务阶段和阻塞原因；
- 任一 Issue 最多 2 次点击可定位到对应字段、镜头或媒体版本；
- 任一会作废下游的操作在执行前展示影响数量，付费重做同时展示成本估算；
- 服务重启恢复后，Run Dock 明确提示“已从检查点恢复”，不伪装成全新任务；
- 用户无法把 stale 或 blocker 未解决的产物误认为“可交付”；
- 成片台可在一个页面完成交付检查、风险确认和 manifest 导出；
- UI 不直接展示内部状态码，必须映射为一致的用户语言，并保留技术详情入口。

---

## 11. 可观测性、质量指标与客户反馈闭环

### 11.1 三类指标

#### 运行可靠性

- Run 成功率、部分成功率、恢复成功率；
- 各阶段 P50/P95 时延；
- provider 故障率和错误分类；
- 孤儿 step 数、重复执行数、幂等复用率；
- 预算暂停率、取消后额外成本。

#### 生成质量

- 首轮 T2 通过率；
- 平均修复轮次；
- 每类 Issue 的发生率和修复成功率；
- 质量增益/每元成本、质量增益/每分钟；
- 视频一次采用率、一次重生后采用率；
- 评估器与人工结论的一致率、误放行率、误拦截率。

#### 客户结果

- 客户一次验收通过率；
- 每集客户修改项数和修改类型；
- 从初稿到验收的总周期和总成本；
- 客户退回原因与系统已有 Issue 的命中率；
- 相同题材/模型/Prompt 版本的满意度趋势。

### 11.2 “更值得信任”的北极星指标

建议使用：

```text
Verified Delivery Rate
= 首次提交即通过客户验收，且交付前硬门禁全部通过、无未知 blocker 的剧集数
  / 首次提交剧集总数
```

同时保留 `Evidence Coverage`：交付检查项中拥有可定位证据的比例。不能只追求模型分数。

### 11.3 客户反馈进入下一轮，但不污染历史

客户反馈结构化为 `customer_feedback` Evaluation，映射到具体 Artifact/shot/issue code。它可触发新的修订 Run，但不能改写原 Run 的评估与决策。这样可以对比“交付前系统判断”和“客户真实判断”，用于校准阈值、评估器和 Prompt。

---

## 12. 测试、Benchmark 与发布门禁

### 12.1 工程验证入口已完成，继续补质量门

当前已经具备 `requirements-dev.txt`、`pyproject.toml` 和 GitHub Actions，CI 会执行 Ruff、Python 编译、148 项 pytest、`npm ci` 与前端构建。后续不再把“补测试环境”作为改造阶段，而是保持这些命令为所有 PR 的硬门禁。

仍需增加：

- 一键全自动从空项目启动的端到端回归，覆盖人物谱、正则分集、剧本和分镜串联；
- `auto.py` 与 `task_registry.py` 的接口契约测试，禁止引用已移除的任务字典/函数；
- 数据完整性迁移报告测试，验证备份、修复计数和重复执行幂等；
- 测试不得依赖真实付费 provider，provider adapter 使用录制响应或 fake transport；生产主链路仍禁止 mock；
- 在 CI 之外建立受预算控制的真实金样回归，用来验证生成质量而非仅验证代码行为。

### 12.2 测试金字塔

1. **纯函数单测**：合同、validator、状态迁移、预算、幂等、hash、评分。
2. **数据库集成测试**：CAS、lease、恢复、失效传播、并发领取、迁移。
3. **Loop 仿真测试**：给定候选与评估结果，验证 repair、stall、budget、escalate。
4. **Provider 合同测试**：使用真实响应 fixture 检查解析和错误分类。
5. **端到端 dry run**：不花费媒体成本，使用固定录制响应走完整 Run/Artifact/Evaluation 链。
6. **真实金样回归**：少量真实 LLM/VLM/视频调用，受预算控制，输出永久留档。
7. **故障注入**：重启、超时、429、5xx、坏 JSON、坏视频、磁盘写失败、重复回调、取消竞态。

### 12.3 金样数据集

至少建立：

- 3 个题材、每个 3 集、每集 8~15 镜的固定素材；
- 原文关键事实、必保剧情点、角色/场景锚点的人工标签；
- 20~30 个已知视频质量问题样本；
- 客户已接受/已拒绝的真实样本（脱敏）；
- 每个样本的人工评分与理由，不只存一个数字。

### 12.4 Prompt/模型/规则变更门禁

任何 `prompt_version / contract_version / evaluator_version / model selection` 变化都必须输出基线对比：

- 硬门禁退化数必须为 0；
- 来源忠实度、结构合法率不得下降；
- 质量分提升必须同时报告成本和时延变化；
- 人工抽检至少覆盖所有自动评分发生明显变化的样例；
- 评估器版本变化时，必须重算基线，不能把新旧分数直接比较。

---

## 13. 安全、成本与隐私

### 13.1 预算先预留、后结算

外部调用前由 Budget Manager 原子预留估算成本，完成后结算真实成本，失败按 provider 真实计费规则处理。没有预留成功就不允许提交昂贵任务，避免并发下多个 worker 同时越过单集上限。

预算层级：项目 → 剧集 → 镜头 → step。任何一级余额不足都进入 `PAUSED_BUDGET`，必须人工调整或终止。

### 13.2 Prompt Injection 与源文本边界

小说原文、客户反馈、模型输出均视为数据，不得改变 Harness Policy 或工具权限。ContextPack 中使用明确标签分隔：`SYSTEM_POLICY / CONTRACT / SOURCE_DATA / PRIOR_ARTIFACT / ISSUES`。模型输出不能自行请求调用未授权工具或提高预算。

### 13.3 凭证与日志

- API Key 永不写 Artifact、run event、provider request snapshot 或交付包；
- 模型请求记录保存脱敏摘要和 hash，必要时完整正文写本地受控文件；
- 导出前扫描 secret pattern、绝对本机路径、内部错误堆栈；
- 客户原文与媒体默认不上传到非选定 provider。

---

## 14. 分阶段实施路线

以下工期按 1 名熟悉项目的后端/全栈开发估算。数据库完整性、任务注册、派生失效、固定时长、开发依赖和 CI 已作为前置工程基线完成，不再重复建设。

### Phase H0：兼容残留热修与新基线封板（1~2 天）

任务：

- 修复 `auto.py` 对 `api._bible_tasks`、`api._plan_task` 的残留引用，改用 `task_registry` 与 `planning.run_regex_plan` 的正式接口；
- 新增“一键全自动从空项目启动”的端到端回归；
- 清理 `PRD.md`、`docs/PROMPT_SPEC.md` 中残留的 AI 分集、滚动章节摘要、动态镜头时长、15 秒口播和旧镜头数规则；
- 在 CI 增加 Contract Surface 扫描，保证“一章一集/单镜 5 秒/超长动作与口播拆镜”在代码、Prompt、文档和测试中一致；
- 将 148 tests、Ruff、compileall、前端 build 记录为 v1.1 工程基线；
- 为本轮完整性修复生成机器可读报告：备份路径、孤儿/重复计数、隔离目录和最终计数；
- 冻结“一章一集”和“单镜固定 5 秒”为不可被 Agent 修改的产品合同。

退出标准：全自动空项目路径不访问任何已删除 API；旧 Plan/摘要/动态时长合同在活跃文档和代码中无残留；现有验证全部通过；完整性修复有可追溯报告。

### Phase 1：Evidence Harness 骨架与旁路记录（1 周）

任务：

- 新增 5 张控制表与 repository；
- 实现 Run/Step 状态机、事件、Artifact、Evaluation；
- 给现有 `hiagent`/worker 调用补 run/step/trace 关联；
- 将 `task_registry` 作为实际协程句柄层接入 Run/Step，而不是重新实现第二套内存 registry；
- 将 `artifacts.py` 的每次清理/失效写成 event + tombstone，保留 hash、原因和替代关系；
- 同步上线最小 `RunDock` 与 Run Center 时间线，让旁路记录从第一阶段就对用户可见；
- 现有业务继续执行，同时旁路写证据链，不改变结果。

退出标准：用户在任意工作台都能看到活动任务、进度和失败；任意一次现有剧本/分镜/视频任务都能在 Run Center 查询到完整时间线；旧功能行为不变。

### Phase 2：统一文本 Agent Loop（1~2 周）

任务：

- 抽取通用 `AgentLoop`；
- validator 返回结构化 Issue；
- 依次迁移 Bible、Screenplay、Storyboard；正则 Episode Mapping 只接入普通确定性 Step；
- 引入 ContextPack、Artifact 版本、checkpoint 和 stall/增益策略；
- 把残余错误从隐式 fallback 改为明确 warning/blocker。
- 在人物谱、剧本台和分镜台接入 `TrustBadge / IssueList / EvidenceDrawer / ImpactDialog`。

退出标准：文本 Agent 阶段不再直接调用 `hiagent.chat`；Episode Mapping 保持 0 LLM；重启后可从逐镜 checkpoint 恢复；所有采用结果有 Evaluation。

### Phase 3：统一媒体 Loop 与可靠队列（1~2 周）

任务：

- jobs 增加 lease、持久 retry_count/next_retry_at、预算预留；保留 `task_registry` 负责当前进程句柄；
- 迁移参考图生成、一致性检测、视频生成、视频 QA；
- 文件级技术校验，单镜必须满足固定 5 秒合同；
- 候选比较后采用，不再默认首个成功版本；
- 取消和 provider 不可取消任务语义落地。
- 在场景图和评审墙接入候选横向比较、采用理由、重生目标与成本预估。

退出标准：进程在任意媒体阶段被杀后可无重复花费恢复；自动采用可解释；预算并发下不穿透。

### Phase 4：人工门禁、质量卡与交付包（1 周）

任务：

- 补齐 Run Center 门禁队列、Artifact 血缘、Issue 定位和统一门禁 UI；
- Delivery Loop、manifest、质量报告、known issues；
- 成片台接入 Delivery Readiness、检查清单和交付报告导出；
- 支持 approve with risk 和客户反馈回流。

退出标准：任意一集可生成 T5 交付包；另一个人仅凭报告即可复验文件、来源、模型版本与接受风险。

### Phase 5：金样校准、灰度与删旧代码（1 周）

任务：

- 在金样和真实项目上跑新旧双轨；
- 校准阈值与评估器；
- 先按项目开启新 Engine，再成为默认；
- 删除 `auto.py::_states` 和已迁移的重复编排/状态写入；保留 `task_registry` 作为 Engine 的进程内执行适配器；
- 更新 PRD、README、Prompt Spec 和运行手册。

退出标准：连续 3 个真实项目没有状态漂移/重复计费；Verified Delivery Rate 和一次验收通过率不低于旧链路，且证据覆盖率 ≥90%。

---

## 15. 建议的首批 PR 拆分

1. **PR-01 全自动兼容热修**：移除 `auto.py` 对已删除 API 的引用，补空项目全链路回归。
2. **PR-02 Contract Surface 对齐**：清理 PRD/Prompt Spec/README/代码中的旧 Plan、摘要、动态时长和口播规则，增加 CI 漂移扫描。
3. **PR-03 完整性修复报告**：记录备份、修复/隔离计数、ID 摘要和迁移版本；重复执行必须幂等。
4. **PR-04 Issue 与 Contract 类型**：不改业务行为，让 validators 返回结构化 Issue 的兼容层；固化一章一集与 5 秒合同。
5. **PR-05 Run/Step/Event Schema**：迁移、repository、CAS 状态机单测。
6. **PR-06 Evidence Artifact/Evaluation Schema**：hash、血缘、tombstone、失效传播单测；不与现有 `artifacts.py` 混名。
7. **PR-07 Provider Trace 贯通**：`run_id/step_run_id/trace_id` 进入 provider_calls/jobs。
8. **PR-08 旁路 Run Recorder + RunDock**：现有链路产生完整记录，任意页面可见活动任务。
9. **PR-09 Run Center 骨架**：运行列表、步骤时间线、失败/恢复与 trace 跳转。
10. **PR-10 通用 AgentLoop + Script Evidence**：先迁移 Screenplay，并在剧本台呈现 Contract、Issue 和修复 diff。
11. **PR-11 Storyboard Checkpoint + Board Evidence**：逐镜持久化、恢复、局部修复和修改影响预览。
12. **PR-12 Lease Scheduler**：在 `task_registry` 之上补持久 lease/retry，而不是替换 registry。
13. **PR-13 Media Candidate Selection + Wall Compare**：5 秒文件校验、独立评估、版本比较和明确采用策略。
14. **PR-14 统一 ImpactDialog 与 Gate UI**：覆盖人物谱、场景图、分镜、评审墙的失效/付费操作。
15. **PR-15 Delivery Manifest、Cinema Readiness、T5 门禁与客户反馈闭环**。
16. **PR-16 新旧双轨 Benchmark 与旧编排删除**。

每个 PR 必须包含：迁移/回滚说明、状态机测试、至少一个失败路径测试、观测字段、兼容性说明。不得在同一 PR 同时重写数据库、核心流程和 UI。

---

## 16. 完成定义（全项目 DoD）

当且仅当满足以下条件，可信化改造才算完成：

### 运行可信

- 所有长任务都由 Workflow Engine 创建和推进；
- 所有状态迁移可追溯且经过 CAS；
- 服务重启后无永久假 running；
- 同输入重复触发不会重复付费，除非用户明确重抽；
- 取消、预算暂停、外部故障均有清晰退出与恢复语义。

### 结果可信

- 所有被采用结果都有 Artifact ID、hash、父版本和 Evaluation；
- blocker 不能被综合分覆盖；
- VLM/LLM 评估失败不会被当作质量通过；
- 上游变更能自动标记下游 stale；
- 任一视频版本的采用理由可复查。

### 交付可信

- 每集交付都有 T5 manifest 和质量报告；
- 文件可播放、顺序、时长、hash 和来源版本全部验证；
- 残余问题和风险接受有明确责任人/时间/理由；
- 客户反馈可定位到具体 Artifact，并能回放交付前系统是否已发现该问题。

### 改进可信

- 新环境可一键运行后端测试和前端构建；
- Prompt/模型/规则变更有金样对比；
- 评估器与人工结论的一致率被持续测量；
- 质量提升同时报告成本和时延，不以无限重试换分；
- 关键客户指标至少包括一次验收通过率和 Verified Delivery Rate。

---

## 17. 立即执行的 P0 清单

如果本周只做最有价值的工作，建议按以下顺序：

1. 热修 `auto.py` 的 `_bible_tasks/_plan_task` 兼容残留，并补“一键全自动空项目”回归。
2. 清理 PRD、Prompt Spec 和代码中的旧 AI 分集、滚动摘要、动态时长与 15 秒口播合同，并用 CI 防止漂移回归。
3. 将本轮数据库清理结果固化为可机器读取的 integrity report；后续修复必须先备份、后记录、再清理/隔离。
4. 定义 `Issue / StageContract / EvidenceArtifact / Evaluation / Decision` 五个核心类型，避免与现有清理模块 `artifacts.py` 混淆。
5. 新增 `workflow_runs / step_runs / artifacts / evaluations / run_events`，其中 Artifact 表用于证据与血缘，现有 `artifacts.py` 继续负责运行时清理。
6. 为现有生成链路旁路写 Run 和 Evidence Artifact，并同步上线最小 Run Dock/Run Center，让用户立即感知任务进度、失败和恢复；正则“一章一集”作为确定性 Step 记录。
7. 先迁移 Screenplay Loop，验证通用闭环；再迁移逐镜 Storyboard；不重建 Episode Plan Agent。
8. 把 `jobs` 的重试计数、next retry 和 lease 持久化，并让 Engine 复用现有 `task_registry`。
9. 在剧本台、分镜台和评审墙接入统一 Evidence Drawer 与 ImpactDialog，让用户就地看到“为什么通过/为什么失败/修改会影响什么”。
10. 选 3 个真实客户样例建立人工标注金样，开始测一次验收通过率。

优先级判断标准始终是：**能否减少错误交付、重复花费、无法恢复或无法解释，而不是是否让系统看起来更像一个复杂 Agent 平台。**
