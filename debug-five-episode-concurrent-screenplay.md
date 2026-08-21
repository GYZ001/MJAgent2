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

**修复**：`delete_screenplay` 给这些回执一个终态
`ABANDONED_BY_SCREENPLAY_DELETE`（`app/stages.py` 的预算读取据此不再计入）。
provider_calls 行、成本与响应全部保留为审计证据，只关闭"未决责任"。

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
