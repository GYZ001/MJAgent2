# Debug Session: five-episode-concurrent-screenplay

- **Status**: [OPEN]
- **Issue**: 「我欲封天」前 5 集剧本必须在一次并发批次里全部生成成功，不允许逐集人工重试。
- **Project**: `proj_3ac0b627fa46`
- **Episodes**: EP1 `ep_3d523ff4d0a4`、EP2 `ep_94fc1dd627f5`、EP3 `ep_a0e90058f83c`、
  EP4 `ep_3b07c59c0856`、EP5 `ep_0a7130b7b402`
- **Driver**: `scripts/yyft_first5.py`（`status` / `clear` / `start` / `monitor` / `retry`）
- **Log**: `logs/yyft_first5.log`

## Reproduction
1. `py scripts/yyft_first5.py clear`
2. `py scripts/yyft_first5.py start`（5 集并发；`text_generation_concurrency=10`）
3. `py scripts/yyft_first5.py monitor`

## Round 1（2026-08-21 20:15）

| 集 | 阶段 | 结果 |
|----|------|------|
| EP1 | — | 未启动（需批准） |
| EP2 | CHARACTER_DISCOVERY | 失败 `ERR-20260821-3dd75d` |
| EP3 | CHARACTER_DISCOVERY | 失败 `ERR-20260821-124586` |
| EP4 | CHARACTER_DISCOVERY | 失败 `ERR-20260821-667dfc` |
| EP5 | BLUEPRINT_GENERATION | 失败 `ERR-20260821-e858df` |

### 根因 1 — future identity 的 K 决议在生产中永远不会被签发

三集同一报错：`future identity NEW 不得重新签发已有 authority`。

Provider Call 328/329/334 的证据完全一致：后续章节逐字写出
「"…孟浩，你是我李富贵这一辈子的好朋友。"小胖子感慨连连」，模型正确判断
「小胖子＝李富贵」，但**决议目录里只有 F 与 N，没有任何 K**。

- 设计层：合同给模型一个封闭决议目录，本应包含「绑定已有人物」（K）。
- 实现层：`_current/ future` K 决议要求 proof anchor 来自 `authority.aliases`
  且**显式排除 canonical_name**；Bible 播种的 authority 一律 `aliases=[]`，
  于是 K 永远为空。唯一能写出真名的 N 又被规则 5 硬拒——合同对
  「后续章节点名一个已登记人物」这一最常见情形不可满足。
- 排除 canonical_name 的理由是「A 谈到 B」的共现反例，但 alias 出现在同一窗口
  同样不构成证明，所以该排除没有换来安全，只是删掉了选项。

**修复**（`app/portraits.py`）
- K 决议的 proof anchor 增加 canonical_name，`proof_kind="canonical_name"`；
  校验端同等接受。锚点仍必须逐字出现在该组自己的证据里。
- 仅当该 authority **未以具名候选出现在本集**时才允许 canonical 锚点：已经在台上
  以本名出场的人，后续窗口再次提到只是共现（保留原「A 谈到 B」反例，
  `test_future_identity_cooccurrence_does_not_mint_known_authority` 仍然通过）。
- N 携带已有真名且证据逐字锚定时，规范化为后端自己签发的那条 K 决议
  （`normalize_payload`）；没有对应 K 决议则维持硬失败。
- `FUTURE_IDENTITY_DECISION_VERSION` → `screenplay-future-identity.v11`。

### 根因 2 — 蓝图局部修复的一轮"未送达"响应会终止整集

EP5 `screenplay.blueprint.patch` 收到 `finish_reason=stop` 的 3833 字响应，
但键名退化成大段 tab/空格，JSON 从未解码成对象。

- `_repair_narrative_blueprint` 本身预算 6 轮、每轮独立预留；
- `format_retry_limit=0` 正确（避免无预留的隐式重试），但没有人接住
  `StructuredFormatError`，于是第 1 轮就抛出，剩余 5 轮预算被丢弃。
- 网关已经区分了这一类：`StructuredFormatError.unparseable`。

**修复**（`app/stages.py`）：`unparseable` 的响应记为"这一轮没送达"，
交给已有的有界轮次继续；解码成功但 schema 非法的答案仍然一次失败。
六轮终态消息追加未送达轮次与最近一次原因。

## Round 2（2026-08-21 21:23）

### 根因 3 — 取消＋删除后该集永久无法启动

EP1 启动直接 503：`BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED: 缺少可绑定的 active revision`。

- 被取消的运行留下 `INTERRUPTED/REQUIRES_EXPLICIT_RETRY` 的 provider call
  （Call 323），构成 unknown-outcome 回执；
- 回执按 `(episode_id, input_fingerprint)` 计，跨 run 存活；
- `delete_screenplay` 把所有 active revision 置为 superseded，而 retry grant
  只能绑定 active revision ⇒ 回执活得比它能绑定的对象更久，此后每次
  Baseline 都走同一条死路。

**修复**：新增 `_abandon_orphaned_blueprint_receipts()`，给这些回执终态
`ABANDONED_BY_SCREENPLAY_DELETE`（`app/stages.py` 的预算读取据此不再计入）。
provider_calls 行、成本与响应全部保留为审计证据，只关闭"未决责任"。

两个调用点，缺一不可：
- `delete_screenplay`：删除即是这些花费的终态处置；
- `_spawn_screenplay_activation`：**全新 Baseline 且没有任何 active revision 时**，
  回执已经孤立——没有任何 grant 能签发、也没有任何批准能替代，所有闸门都不可满足。
  EP1 正是这种状态：它已经是 `pending`，`delete` 直接 409「本集没有可删除的剧本」，
  永远走不到删除侧的清理。resume 不走这条路径，仍然 fail-closed。

作用域必须与预算对"未决回执"的定义完全一致：`status IN ('INTERRUPTED','RUNNING')
AND superseded_by_call_id IS NULL`。第一版误加了 `recovery_disposition IS NULL`，
而未知结果**正常就带着** `REQUIRES_EXPLICIT_RETRY`——那一版对真实数据是空操作。

### 根因 4 — current identity 对"只以称谓出现的已登记人物"没有出路

EP5：`current 已登记身份必须选择 K decision：许清；current named 缺少逐字 owned evidence：许清`。

第 5 集原文只写「许师姐 / 许姓女子」，从未逐字出现「许清」，因此 K 目录为空；
而规则 2 写的是「已登记身份只可选 k」。模型于是把真名写进了 n。

**修复**：prompt 规则 2 补全路由——本批 K 目录没有为人物谱中某人签发
decision_id 时，只能把逐字称谓按规则 4 放入 f，交后续权威绑定认领；
人物谱名单只用于识别，不是可直接书写的名字。硬校验保持不变（仍然 fail-closed）。

### 根因 5 — 0 字节传输卡死会吃掉语义重试预算

- EP2：`screenplay.identity.current.v6` ReadTimeout 303s，`received_chars=0`。
- EP3：shard 13 的 attempt 1、2 产出无效候选，attempt 3 卡死 182.8s、0 字节 ⇒ 整集死亡。

分片路径本就有「0 字节卡死给一次全新尝试」的判断，但它写在**同一个**
attempt 计数里：卡死既消耗一次语义重试，落在最后一次时又不再重试。
身份路径连这一层都没有——`_identity_structured_with_resample` 只接住
`StructuredFormatError`。

**修复**
- `app/stages.py`：新增 `BLUEPRINT_SHARD_MAX_STALL_RETRIES=2`，卡死重放**同一次**
  语义 attempt，用独立预算；`_blueprint_provider_operation_id(stall_epoch=…)`
  让重试拿到不同 operation id（`stall_epoch=0` 时不写入该键，旧 id 不变）。
- `app/portraits.py`：`_identity_structured_with_resample` 同样接住
  `received_chars==0` 且 `failure_kind ∈ {request_outcome_unknown, stream_interrupted}`
  的 `ProviderError`；有字节到达的失败仍然一次失败。
- `app/config.py` / `app/hiagent.py`：身份调用改用独立读超时
  `TIMEOUT_CHAT_IDENTITY_READ=120`（max_tokens 固定 4096，实测最慢 38.0s/4593 字，
  按同网关拟合 latency≈3.7s+chars/170 跑满也只要 ~57s；通用 300s 只是把卡死空等
  拉长到 5 分钟）。

### 根因 6 — 有界 ownership 修复被钉死在 attempt 2

EP4 shard 21：attempt 1 JSON 畸形（无候选）、attempt 2 产出 15 条
state-subject ownership 问题、attempt 3 再次盲重试同样失败。

`repair_only` 的条件写的是 `attempt == 2 and previous_candidate …`：
唯一一次有界 ownership-map 修复机会被一个**从未产出候选**的 attempt 消耗掉，
而真正可修的候选出现在 attempt 2。

**修复**：修复档位跟随「第一份可修候选」而不是固定下标；每次产出候选后都
重算 `ownership_repair_issues`；上一轮已是修复模式且问题仍在时回落整片重写，
避免把剩余预算耗在同一张 map 上。

## Regressions Added
- `tests/test_character_discovery.py`：K 决议可达、N→K 规范化、无后端决议仍
  fail-closed、已登记人物按称谓路由到 f、0 字节卡死重采样、有字节仍一次失败。
- `tests/test_narrative_blueprint.py`：未送达 patch 只消耗一轮、schema 非法仍一次失败。
- `tests/test_screenplay_delete.py`：删除关闭未决责任并可重新启动、不波及其它集。
- `tests/test_blueprint_shard_budget.py`：卡死不吃语义预算、卡死次数有界、
  ownership 修复档位在 attempt 1 丢失后仍可用。
- `tests/test_provider_call_lifecycle.py`：身份调用独立读超时。

## Unrelated Fix
`app/source_facts.py` 的 `quote_open_offset` 是四处只写不读的死赋值，
ruff F821/F841 使 `verify.py --full` 无法通过；已删除，无行为变化。


## Round 3（2026-08-21 07:01）与 Round 4（07:36）

进度显著推进：多集首次越过 CHARACTER_DISCOVERY / BLUEPRINT_GENERATION，
到达 IDENTITY_FREEZE (2/10) 与 SCENE_SHARD_GENERATION (4/10)。新暴露的根因：

### 根因 7 — 关闭回执时没有同步清掉「该 operation 上次结果未知」标记

EP3 缓存分片重放时 `claim()` 仍抛 `BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED`。
`from_durable_calls` 里 `latest_operation_status[operation_id]` 的赋值在
`unresolved_liability` 判断之外，被删除结算过的调用照样把自己登记成
「这个语义 operation 的上一次尝试结果未知」。已一并按 `abandoned_by_delete` 排除。

### 根因 8 — `_FailFastScope` 的失败归属者可能在尚未结束时被登记

`abort_outer_batch()` 从供应商槽位的中止回调里调用 `shard_scope.fail(shard_owner)`，
而此时该任务还在运行。`_gather_fail_fast` 的等待者随后对一个尚无结果的 task 调用
`result()`，抛出 `InvalidStateError: Result is not set`，把真实失败原因彻底掩盖，
整集以一个 asyncio 内部错误结束。

**修复**：登记了失败归属者但它尚未结束时，继续等待它的真实异常，而不是立刻
`result()`，也不是转而报告一个被取消的同伴。回归测试对旧代码确实失败、对新代码通过。

### 根因 9 — 纯称谓被签发成永久人物卡，与真名角色形成身份分裂

EP1 的后续窗口只写出「许师姐」，模型据此签发了 `bible:许师姐`，与人物谱里本来
就有的「许清」构成同一人的两张卡；EP5 的场次身份注册表随后因为「许师姐」同时
指向 `person_0e9f485d40cc` 与 `person_31931b789904` 而 fail-closed。

**用户给定的规则**：真名 > 尊称 > 代称；有真名就不能单独成角色，没有真名才可以。

**方案（零新增模型调用）**：在已有的那一次身份调用里多要一个称谓形态字段，
后端确定性执行阶梯。
- current 合同：`CurrentNewNamedIdentityDecision.name_kind`
  ∈ {personal_name, honorific, referential}；
- future 合同：新增闭合映射 `revealed_name_kinds`；
- 后端规则：只有 `personal_name` 能签发新权威；尊称/代称一律确定性降级为功能身份
  （保留原文逐字称谓，仍是独立身份），等真名真正出现在证据里再由 K 决议认领同一个人；
- 模型从上下文认出已登记人物却写不出逐字姓名时（EP5 的「许清」），该声明被丢弃
  而不是让整集硬失败——结构化身份覆盖审计会用原文真正出现的称谓把这个人补回来。
- 合同版本：identity-discovery v15 / current-identity v12 / future-identity v12。

**数据修复**：已通过人物谱自身的 CAS 写入删除重复角色卡「许师姐」（v3→v4，
影响仅 text_only，无付费资产失效）。

## 根因 10 — content_owner_key 的两套语义互相矛盾

EP2：`SRC0020:unit:008 content owner 未冻结：靠山宗`。

- 蓝图合同明确写着「content_owner_key 可以是文字或物件归属」，所以把一段文字的
  归属写成宗门名是合法的，能通过全部六轮蓝图修复；
- `build_screenplay_scene_shard_plans` 却要求每个 content_owner_key 都能映射到
  冻结的**人物**身份，映射不到就抛裸 `ValueError`，而这一层没有任何修复循环；
- 下游 IR 又把 content_owner_keys 并入 `referenced_identity_keys`，所以「原样保留
  非人物归属」只会把失败推迟到更后面。

身份注册表本身有 `identity_kind="reference"`（offscreen_only、禁止资产）这一类，
正是宗门/机构这种被引用实体应该落的位置——但人物发现只找人，不会登记它们。
需要一个产品判断：非人物归属应当登记为 reference 身份，还是蓝图合同收紧为
「content_owner_key 必须是已登记身份」。


### 根因 10 的定性与取舍

先用真实加载器 `_episode_source_text` 复算了一次源文单元（第一次用手写的章节拼接方式
复算是错的，得到的是一条 prose 单元，据此差点做出反向结论）：

```
SRC0020:unit:008  quoted  quoted_span  '“杂”'
```

这是木牌上刻的那个「杂」字。把它的 content owner 写成「靠山宗」，正是蓝图合同里
白纸黑字允许的那条用法（文字/物件归属），蓝图本身没有错。

因此**不能**收紧蓝图合同——那会禁掉刻字、告示、信物这类完全正当的建模。
错的是场次侧：它要求每个 content_owner_key 都能映射到冻结的**人物**身份。

**采用的方案**：让非人物归属在冻结注册表里有正当位置。注册表本来就有
`identity_kind="reference"` 这一类（`referenced_identity`、`offscreen_only`、
`asset_requirement="forbidden"`），机构/物件归属正该落在这里。

- 新增 `blueprint_referenced_content_owners(blueprint)`：取 picture 节点上
  所有 delivery 的 content_owner_key，**排除任何同时作为 performer_key 出现的 token**；
- `build_frozen_identity_registry(..., referenced_content_owners=...)`：注册表里
  没有的归属 token 登记为 `reference:<token>` 权威；
- 冻结发生在 IDENTITY_FREEZE 步骤内、注册表哈希之前，所以冻结仍然是权威的、哈希仍然正确。

**为什么这样是 fail-safe 而不是 fail-open**：performer 的严格性完全没有放松——
谁在说话必须仍然是冻结的人物身份。被自动登记的只可能是「没有任何人表演的归属」，
而 reference 身份 offscreen_only、禁止资产、不能成为表演者，所以即使模型写错一个
归属 token，它也只会成为一个惰性引用，而不会污染人物卡、定妆照或表演分配。


## Round 5（2026-08-22 12:59）

归属修复生效：EP2 越过了冻结，`靠山宗` 不再拦路。新暴露的失败：

### 根因 11 — 「未送达」的判据太窄

大量 INTERRUPTED 其实是 fail-fast 取消的同伴调用（`流式请求被取消`，几百毫秒）。
真正的源头是 `stream interrupted before [DONE] (received_chars=22)`。

`[DONE]` 是供应商自己的完成标记，`_stream_chat_completion` 在缺它时**直接丢弃**
重建出来的部分内容。所以只要流没走到 `[DONE]`，无论收到多少字节都不存在"已授权
的答案"，字节数根本不是判据。

**修复**：抽出共享判据 `hiagent.provider_answer_undelivered(exc)`
（`stream_interrupted`，或 `request_outcome_unknown` 且 0 字节），
身份重采样与蓝图分片卡死重试都改用它；并给场次写作/语义审查/语义修复三处
加上同样有界的一次重发（`_scene_structured_with_undelivered_retry`，每次重发
带自己的 operation id）。重发**必须写在供应商槽位租约内部**——租约看到异常就会
触发批次中止回调，写在外面第二次尝试根本没机会跑。

### 根因 12 — 严格 enum 的 evidence_ref 仍会收到 `E01`

EP1：目录里是 `E001`，供应商送来 `E01`，整集死在 `current F evidence_ref 越界`。
schema 已经把它钉成闭合枚举，但供应商并不总是遵守 strict 模式。

**修复**：`_resolved_evidence_ref` 只做补零这一种纯格式还原，且**必须唯一命中**
一条已有的后端证据 receipt；有歧义原样返回，继续 fail-closed。

### 一次撤回的改动（记录以免重犯）

同一次响应里「绿袍修士」出现两次（F2/F3、同一条证据）。我一度把它规范化为
同一个身份，但既有用例
`test_current_identity_same_label_multiple_groups_fails_closed_once` 的源文正是
「两名绿袍修士同时开口」——**两个不同的人共用一个称谓**，合并会把两个角色并成
一个。合同要求模型给同段多个无名实体使用可区分的稳定描述，所以这里 fail-closed
是刻意设计。改动已撤回，只保留补零修复。


## Round 6（2026-08-22 13:51）与当前阻塞点

修复全部生效后的分布：EP1/EP3/EP4 都到达 SCENE_SHARD_GENERATION (4/10)，
EP2 到 IDENTITY_FREEZE (2/10)。剩下三类：

### A. 供应商对特定内容的确定性拒绝（当前主要阻塞）

`stream interrupted before [DONE] (received_chars=22)`，约 1s 返回。
用 prompt 内容做哈希比对：**同一份 review prompt 连续 4 次全部以完全相同的方式中断，
没有一次成功**；而同一 `max_tokens=31928` 的其他 review 调用是成功的。
所以这不是传输抖动，也不是输出上限问题，而是供应商对该段内容的确定性拒绝
（22 个字符像是一个被截断的错误信封）。

我加的有界重发确实触发了（可见 `:undelivered:1` 的 operation id），
行为正确，但对确定性拒绝无能为力。

需要产品判断的两个方向：
1. 把「重复出现且完全相同的无 `[DONE]` 中断」重新归类为**内容拒绝**而不是
   传输结果未知——现在的文案让操作者以为"稍后重试"能解决，实际上永远不会成功；
2. 为这一类拒绝启用备用供应商/模型路由。

### B. `SRC0020:008:unit 缺少 single/joint state_subject 结构证据`（EP2）

就是那个「杂」字单元。归属已经能冻结了，但场次写作又要求它有 state_subject
结构证据或显式 environment_only。刻在木牌上的字既不是人物状态，也不完全是环境，
合同里缺少「物件承载的文字」这一类的明确归属，需要与根因 10 一起做完整设计。

### C. 同一称谓对应多个身份组（EP5「韩宗」）

与「绿袍修士」同类，属于刻意的 fail-closed（见上文撤回记录），不应合并。
