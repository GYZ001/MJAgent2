# 人物谱（世界书）阶段耗时过长 —— 只读调查 + 优化方案

调查对象：`run_8ebe1225aa69`（`workflow_type=character_bible`，`scope_id=proj_195be7df1fd6`，
08-26 22:50 启动）。**只读调查，未改任何代码。**

## 先说结论：这次 run 已经失败，不是"还在跑"

调查过程中反复用只读连接（`file:data/manju.db?mode=ro`，能看到 WAL 里的最新提交）轮询这条
run，它在调查期间跑完了全部生命周期：

```
started_at = 1787809847.5155218
finished_at = 1787810747.6077795   （相差 900.0923 秒，几乎精确等于 900）
status = FAILED
failure_code = RUNTIMEERROR
failure_message = "人物谱解析/修复超时（超过 900 秒），请重新谱写。"
```

这个 900 秒不是巧合，是 `BIBLE_TASK_TIMEOUT_S = 15 * 60`（`app/domain/common.py:55`）——
`app/domain/bible_ops.py:712` 用 `asyncio.wait_for(generate_bible(...), timeout=timeout_s)` 包住
整个世界书生成，超时直接判死。**这条 run 是被这个 900 秒硬顶掐死的，且是这个项目第一次跑世界书
就失败**（`workflow_runs` 里这个项目只有这一条 `character_bible` 记录）。下面的所有耗时分析都是在
回答"为什么 900 秒不够用"，而不是"这条 run 还要跑多久"。

---

## 全流程耗时瀑布（实测，按 `provider_calls`/`run_events` 逐条对账）

| 阶段 | 耗时 | 占比 | 备注 |
|---|---|---|---|
| 副文本净化（`_chapters_without_paratext`） | 120.013s | 13.3% | 硬顶 `BIBLE_PARATEXT_BUDGET_S=120`，31 章目标只净化完 8 章，23 章被取消 |
| 人物点名（`_recurring_character_names` 的模型调用） | 687.861s | 76.4% | **两次串行调用：285.664s（被拒收，纯浪费）+ 402.197s（有效）** |
| 在场裁决闸（`_roster_presence_verdict_call`，逐条 await） | ≥92.0s（被 900s 硬顶打断，未跑完） | 10.2%+ | 14 条候选证据顺序执行，另有 4 条因格式重试各加了一次调用；第 18 次调用被流式取消 |
| 角色圣经正文生成、别名核验、状态事实补录等下游步骤 | 0s（从未开始） | — | 900 秒在第 18 次裁决调用中途耗尽，`generate_bible` 真正产出圣经正文的那次模型调用**从未获得执行机会** |
| **合计** | 899.87s（≈900.09s 实测总时长，含步骤切换开销） | 100% | |

**最刺眼的事实：这次 900 秒，全部花在"准备一份必收角色候选名单"上，圣经正文本身一个字都没
开始生成。** 净化 + 点名 + 裁决三步加起来占了 89.9%的预算，其中点名一步就占 76.4%，而这一步里
又有 41.5%（285.664s / 687.861s）是完全作废的重试。

---

## T1：那两条 ~120000ms 是不是超时

**不是同一件事，两条要分开看，都不是"网络/供应商超时"：**

- **`character_bible_paratext` 汇总记账行（`id=13255`，`latency_ms=120013`）**：这不是一次真实的
  模型请求，是 `log_provider_call("character_bible_paratext", ...)`（`app/stages.py:4541-4551`）的
  记账行，字段里的 `latency_ms` 就是 `_chapters_without_paratext` 函数自己从开始到结束的墙钟时长。
  它的 `meta` 明确写着 `budget_s=120.0, chapters_in_scope=31, chapters_stripped=8, unfinished=23`——
  这是 `BIBLE_PARATEXT_BUDGET_S=120.0`（`app/stages.py:1932`）这个**函数自设的内部预算硬顶**按设计
  触发，不是任何供应商超时常量。它的存在是故意的（"净化本来就是判不出就退回原文的净化步骤而不是
  闸门，超时未完成的章原样进入下游"，`app/stages.py:4497-4498` 原注释），工作符合设计初衷，但
  **代价是 23/31=74% 的目标章节被放弃净化**，这一点在原设计文档里没有被量化过。

- **`chat` 调用 `bible.paratext:4536`（`id=13236`，`latency_ms=119873`，`status=INTERRUPTED`）**：
  这是上面那个 120 秒预算到点时，被 `asyncio.wait(tasks, timeout=BIBLE_PARATEXT_BUDGET_S)`
  取消（`app/stages.py:4520-4529`）的一个仍在进行中的请求。它在被取消时已经等了 119.873 秒，
  逼近但没有触发任何供应商级超时——因为它包着的读超时是 `TIMEOUT_CHAT_PARATEXT_READ=150`
  （`app/config.py:221-223`），比外层的 120 秒预算还宽 30 秒。**这暴露了一个真实的设计矛盾：
  外层预算（120s）比它所包裹的单次调用超时（150s）更紧，意味着外层预算永远等不到内层超时自己
  触发就会先把还健康、仍在正常进行中的请求杀掉**——`INTERRUPTED` 不是这次请求出了问题，是它
  运气不好、活过了 120 秒这条线。

两条都**不是**通用超时常量 `TIMEOUT_CHAT_READ=300`（`app/config.py:162`）或
`TIMEOUT_CHAT_PARATEXT_READ=150`（`app/config.py:221`）本身触发的——是 `BIBLE_PARATEXT_BUDGET_S=120`
这个更早触发的内部预算硬顶造成的连锁效应。这两次调用本身的 HTTP 状态是"被取消"，不是"失败重试"，
没有产生二次付费请求（免费环境下不是关键，但确认一下机制：`INTERRUPTED` 是取消态，不进入
`model_gateway.chat()` 的自动重试分支）。

---

## T2：人物点名为什么要 285 秒（实测是 687.9 秒，比协调方最初看到的数字更严重）

**协调方给出的"该 stage 共 2 次，累计 285.7s"是基于一次中途快照，当时第二次调用还在跑（状态
`RUNNING`，`latency_ms=0`），没有算进去。等这条 run 真正跑完后重新查，两次调用的真实总耗时是
285.664 + 402.197 = 687.861 秒**——这一点必须先纠正，因为它直接决定了"人物点名"在整条 900 秒
预算里的真实占比（76.4%，不是协调方最初估的 32%）。

### 285.7s 和 402.2s 各自发生了什么

两次调用都成功送达（HTTP 200），模型都是 `glm-5.3-flash`，走 OpenRouter 协议。两次请求体
（`request_json`）逐字相同，说明这**不是** `model_gateway.chat()` 那层带 30 秒退避的显式重试
循环（那层重试会在 `run_events` 里留下 `PROVIDER_RETRY_SCHEDULED`/`PROVIDER_RETRY_RESUMED` 事件，
本次 run 的 `run_events` 里只有 5 条：`RUN_CREATED`/`RUN_STARTED`/`STEP_STARTED`/`STEP_SUCCEEDED`/
`RUN_FAILED`，没有任何重试事件，且两次调用之间的时间间隔只有 0.029 秒，远小于
`TEXT_PROVIDER_RETRY_BASE_DELAY=30` 秒的退避）。

真正的机制是 `app/hiagent.py:1228` 的 `_chat_with_reasoning_fallback`（文档字符串原文："封装推理
模型的降级重试逻辑：若首轮因推理过长导致 content 为空，则关闭推理重试一次"），配合
`config.OPENROUTER_TEXT_REASONING_EFFORT` 默认值 `"high"`（`app/config.py:143`）——**每一次经
OpenRouter 协议的文本调用，只要没有显式关闭，都会自动带上 `reasoning: {"effort": "high"}`**。

第一次调用（285.664s）的原始响应（`response_json`）显示 `finish_reason: "length"`——请求在
`max_tokens=4096`（`app/stages.py:2114` 硬编码）这个上限内被截断，且 `reasoning` 字段里确实有
大段推理文字。`content` 字段虽然不是空的（模型确实先输出了一部分 `candidates` JSON），但
`_reject_truncated_chat_response`（`app/hiagent.py:882`）看到 `finish_reason=="length"` 就直接
判定这次响应不可信、必须重来——这是对的，截断的 JSON 不能信任其完整性。这次被拒收的响应
**完全作废，285.664 秒的推理+生成时间没有产出任何可用结果**。第二次调用（402.197s）用同一份
`max_tokens=4096`、同一份提示词重试，这次侥幸以 `finish_reason: "stop"` 正常收尾（更少的角色/
证据被枚举出来，随机性使得这次没有再撞到 4096 token 上限），才拿到可用的候选名单。

### 根因是不是这次改动（任务 #59，commit 696c854）引入的新成本——是的，如实说

旧版 `_recurring_character_names` 只要求模型输出 `{"names": [str]}`，输出量是几十个字符串；
新版（`app/stages.py:2026-2029` 的 `_CharacterRollCall`/`_RosterCandidate`）要求每个候选人物
附带 `primary_appellation`/`formal_name`/`onstage_evidence`（每条证据还要带 `chapter_index` +
最长约 80 字的逐字引句），输出量级直接从"几十个词"变成"每个人物 5~10 条引句、每条引句几十字"，
本次实测两次响应体 `response_json` 分别是 49624 / 60815 字节，是旧版输出量的数十倍。

`max_tokens=4096`（`app/stages.py:2114`）这个上限没有跟着新的输出量级一起调整，而 OpenRouter 的
`reasoning` 字段和 `content` 字段共享同一个 `max_tokens` 预算（`_reasoning_used_all_output_budget`
的注释原文：`OpenRouter 的 reasoning 与 message.content 共用 max_tokens`，`app/hiagent.py:856`）——
新的大输出 + 默认开启的高强度推理，两者一起挤爆了 4096 这个没跟着调整的旧上限。**这是本次改动
引入的新成本，是真实存在的、可复现的结构性浪费，不是这条 run 运气不好撞上的偶然。**

---

## T3：哪些能并行、哪些必须串行

### 净化步骤（`_chapters_without_paratext`）：代码写的是并发 8，实际只跑到 6

`BIBLE_PARATEXT_CONCURRENCY = 8`（`app/stages.py:1931`）是这一步自己的信号量上限，但所有文本
模型调用都要先过一道**进程级全局闸门**：`run_with_provider_call_slot`
（`app/generation_concurrency.py:235`）→ `gate_for("text_provider_calls")`，其并发上限来自
`settings` 表的 `text_generation_concurrency`（`app/generation_concurrency.py:106-107`），
实测当前值是 **6**（`DEFAULT_TEXT_GENERATION_CONCURRENCY=10`、`MAX_TEXT_GENERATION_CONCURRENCY=16`，
但 `settings` 表里显式配了 `6`，覆盖了默认值）。也就是说 `BIBLE_PARATEXT_CONCURRENCY=8` 这个局部
信号量从未真正生效过——全局闸门先在 6 这里卡住了，这和协调方观测到的"已有约 2.8 倍并行度"方向一致
（6 是理论上限，考虑到有格式重试请求错峰插入、任务启动/收尾的非均匀性，实测均值低于 6 完全合理）。

### 人物点名（`_recurring_character_names` 的单次 `model_gateway.chat` 调用）：结构上是一次调用，
不涉及并行问题——见 T2，问题不在并行度，在 `max_tokens` 太小导致的截断重试。

### 在场裁决闸（`_roster_presence_verdict_call`）：**完全串行，这是本次调查确认的最大优化点**

`app/stages.py:2142-2188` 是一个双层 `for` 循环，内层直接 `await _roster_presence_verdict_call(...)`
（第 2172 行），**没有任何 `asyncio.gather`/`asyncio.Semaphore` 包裹**。用真实调用的时间戳可以
逐条验证这是严格串行——每一条调用的起始时间戳和上一条的"起始时间+latency"几乎完全重合（误差
< 0.1 秒）：

```
13258: 6887ms   →（几乎无缝衔接）
13259: 4485ms   →
13260: 5531ms   →
13261: 4180ms   →
13262: 6555ms   →
13263: 6693ms   → ……直到 13275 被 900 秒硬顶取消
```

`_roster_presence_dossier`（`app/stages.py:3303`）和 `_roster_presence_verdict_call`
（`app/stages.py:3344`）都是无共享可变状态的纯函数式调用——每条证据独立判定，互不依赖，
**结构上完全可以并发发起**，安全性上不需要改变"模型只问这一条证据本身"的既有裁决口径
（不涉及降低裁决质量）。这一步在本次 run 里跑了 18 次真实请求（14 条通过结构闸的证据 + 4 次
格式重试），累计约 92 秒仍未跑完就被 900 秒硬顶打断——如果改成受同一个全局闸门（当前上限 6）
约束的并发执行，理论墙钟时间能压到 92/6 ≈ 15~20 秒量级。

### 协调方提到的"25 次 ? 无 stage 标签、累计 839s"——查证：无法复现，如实说明

用完整数据重新核对这条 run 的全部 45 条 `provider_calls`，只有 **1 条**（`id=13255`，
`character_bible_paratext` 汇总记账行）在 `meta` 里没有 `stage_key` 字段，其余全部有明确的
`stage_key`（`screenplay_source_paratext`/`character_roll_call`/`character_roster_presence_verdict`）。
协调方给出的快照时间点（约运行 683 秒时）对应的数据库状态，此时 `character_roster_presence_verdict`
这批调用（真实起始于运行第 808 秒）**根本还没有发生**，与"25 次""839s"这两个数字对不上。没有找到
能解释这个差异的代码路径，怀疑是协调方当时使用的统计口径与本次直接读 `provider_calls.meta` 不同，
或取样时机/工具本身的问题——**如实标注"查不清、无法复现"，不编造一个能自圆其说但没有验证过的
解释**。这不影响后续结论：本次调查的两个真实大头（285.664s 的截断浪费 + 串行裁决闸）已经用
完整数据独立坐实，不依赖协调方那条对不上的数字。

---

## T4：优化方案（按预计收益排序，每条给出依据、预计节省、副作用）

CLAUDE.md 纪律：一个问题最多评估三个方案，选定后不再列多余选项；判据挂产物信号
（"这一步是不是真的完成/正确"），不挂会被正常操作改动的整体状态。以下每条都只給出唯一推荐方案，
不做多选菜单。

### P0-1：把人物点名的 `max_tokens` 从 4096 调大

**位置**：`app/stages.py:2114`（`_recurring_character_names` 里 `model_gateway.chat(...)` 调用的
`max_tokens=4096`）。**依据**：T2 实测 `finish_reason:"length"` 确凿，`response_json` 显示新
schema 单次响应体已达 5~6 万字节量级。映射台主抽取调用面对同量级的结构化输出用的是
`max_tokens=8000`（`app/production/prep_pack.py` `_extract_chunk` 调用处），是一个现成的、已经
在生产环境验证过量级够用的参照值。**预计节省**：本次 run 285.664 秒的截断浪费在预算充足后应该
不会发生（reasoning + content 都有地方放，不会被 4096 这道墙拦腰截断）；不保证在候选人物数量
显著更多的书里绝对不会再撞上限，但把安全边际从"明显不够"改到"贴近映射台已验证过的量级"。
**副作用**：无——判断口径、模型看到的提示词一个字不变，只是给它写完答案的空间更大；如果
`max_tokens` 调大后模型有更多空间反而输出更多候选证据，那是符合"尽量把在场证据都列出来"这条
既有要求的正常结果，不是新增行为。

### P0-2：把在场裁决闸从串行 `await` 改成并发发起

**位置**：`app/stages.py:2142-2188`（`_recurring_character_names` 内层的
`for evidence in candidate.onstage_evidence: ... await _roster_presence_verdict_call(...)`）。
**依据**：T3 实测确认零并发、且每条调用互相独立、无共享状态。**预计节省**：92 秒的已发生部分
（未跑完）理论上可压缩到全局闸门允许的并发上限（当前 6）附近，即 15~20 秒量级；这一步的调用数量
会随书中候选角色和证据条数增长，书越大、角色越多，节省的绝对秒数越可观。**副作用**：
**不改变任何一条证据的裁决逻辑或输入内容**——`_roster_presence_dossier`/`_roster_presence_verdict_call`
的入参和判断标准原样不动，只是把"一条一条等"改成"一起发出去、一起等"，这是纯编排层改动，
不触碰"在场裁决闸的准确性是今晚刚立起来的"这条红线；已经通过的全局并发闸门（`text_provider_calls`
gate）会自然约束这批新增的并发请求，不需要为这一步单独再造一个信号量。

### P1：修正净化步骤"局部并发上限 8 vs 全局闸门 6"的名实不符，并核实 120s 预算是否还合理

**位置**：`app/stages.py:1930-1932`（`BIBLE_PARATEXT_CONCURRENCY=8`、`BIBLE_PARATEXT_BUDGET_S=120.0`）、
`settings.text_generation_concurrency`（当前值 `6`）。**依据**：T1/T3 已经确认这两个数字互相矛盾——
局部代码写着"最多 8 路并发"，实际被全局闸门摁在 6，且即便按 6 路算，31 个目标章节也需要
⌈31/6⌉≈6 轮，每轮实测常见耗时 30~90 秒，理论所需总时长明显超过 120 秒的预算，与实测
"120 秒内只完成 8/31 章"吻合。这里只列一个方向（不在"调大局部并发数"和"调大全局闸门"之间各自
展开成独立方案，避免超过 CLAUDE.md 的"最多评估三个方案"上限，且这两个数字本质是同一个问题的两个
症状）：**核实并对齐 `BIBLE_PARATEXT_CONCURRENCY` 与全局 `text_generation_concurrency` 的实际生效值，
按"时间是唯一约束、多发并发换更短墙钟划算"的既定优先级，评估是否需要把 `text_generation_concurrency`
从 6 调大**（该设置是全局共享闸门，影响世界书/映射台/分镜台等所有经这条路径的文本调用，调整前
需要用户/协调方确认这不会给下游其他并行任务造成资源挤占——这是一个跨环节的系统性设置，不属于
"世界书这一个环节自己就能拍板"的范围，本次只标注问题和数据依据，具体调多少留给用户决策）。
**副作用**：全局闸门调大会同时影响其他正在使用文本模型的工作流（映射台、分镜台等），需要评估
是否有相互挤占的问题；这条不做具体数值建议，只给出问题定位和决策所需的数据。

### P2：`BIBLE_TASK_TIMEOUT_S=900` 是否需要上调，作为兜底安全垫

**位置**：`app/domain/common.py:55`（`BIBLE_TASK_TIMEOUT_S = 15 * 60`），可经 `settings` 表的
`bible_task_timeout_s` 键覆盖（`app/domain/bible_ops.py:712`），当前该键未设置，走的是硬编码默认值。
**依据**：这是兜底安全垫，不是根因——P0-1/P0-2 落地后，按本次实测的三段耗时重新估算
（净化 ≤120s 不变 + 点名从 687.9s 降到大约一次成功调用的量级，参考本次成功那次的 402.197s，
或更短 + 裁决闸从 92s+ 降到 15~20s），三步合计有希望落在 550~650 秒区间，仍然给圣经正文生成
和下游别名核验/状态事实补录留出的余量并不宽裕（900-650=250 秒左右，而这些下游步骤在本次
run 里一次都没跑到，无法从实测数据估计它们各自需要多久）。**不建议把这一条当作首选修复手段**——
单纯调大超时只是让一次本可以更快的 run 允许跑得更久，不解决"点名一步吃掉四分之三预算"这个真实
浪费；只有在 P0-1/P0-2 落地后仍然观测到下游步骤被挤压甚至再次超时，才需要回来动这个数字。

### 观测项（不是行动项，仅记录）

- 在场裁决闸 14 条顶层调用里有 4 条（约 29%）触发了 `:structured-attempt:` 格式重试，说明这个
  裁决 schema 有一定的格式失败率，每次重试额外增加约 3~7 秒。这个比例是否正常、要不要进一步核查
  提示词/schema 本身，需要更多样本才能判断，本次只记录数字，不作为本次优化方案的一部分。

---

## P0/P1/P2 汇总 + 本次明确不做

**P0（本次建议优先做，直接命中已实测坐实的两大浪费源）：**
- `app/stages.py:2114`：`max_tokens` 从 4096 调大（参照映射台 `_extract_chunk` 的 8000）。
- `app/stages.py:2142-2188`：在场裁决闸从串行 `await` 改成并发发起（复用既有全局闸门，不新增
  信号量）。

**P1（需要协调方/用户拍板的系统性设置，本次只定位问题、给数据，不擅自定数值）：**
- `settings.text_generation_concurrency`（当前 6）与 `BIBLE_PARATEXT_CONCURRENCY`（代码写 8）的
  名实不符，是否需要一起上调；`BIBLE_PARATEXT_BUDGET_S`（120s）是否需要跟着并发上限一起重新核算。

**P2（本次明确不做）：**
- `BIBLE_TASK_TIMEOUT_S=900` 的调整——留作 P0/P1 落地后如果仍然不够再处理的兜底手段。
- 在场裁决闸约 29% 的格式重试率——只记录，不在本次方案里改动 schema/提示词。
- 圣经正文生成、别名核验、状态事实补录等下游步骤的耗时优化——本次 run 从未执行到这些步骤，
  没有实测数据支撑任何优化判断，如实标注"查不清"，留待这些步骤真正跑起来后再单独调查。

---

## 风险与已知限制

- **本次分析基于唯一一条 run 的实测数据**（这个项目第一次跑世界书，只有这一次样本），"两次点名
  调用分别 285.664s/402.197s"这类具体秒数会随每次调用的候选人物数量、证据条数、模型侧的随机性
  浮动，不是可以精确复现的常数；但"第一次调用因 max_tokens 不够被截断作废"这个**结构性**问题
  （只要候选/证据数量足够多就会复现）已经有明确的机制性证据（`finish_reason:"length"` +
  `reasoning` 与 `content` 共享预算的官方注释），不依赖这一次的具体秒数才成立。
- **下游步骤（圣经正文生成、别名核验、状态事实补录）的耗时完全没有数据**——本次 run 在这些步骤
  开始前就被 900 秒硬顶打断，P0 修复落地后，如果下游步骤本身也很慢，900 秒仍然可能不够，需要
  等下一次真实 run 的数据来验证。
- **P1 涉及的全局并发闸门调整会影响所有共享该闸门的工作流**（不止世界书），本方案没有评估这个
  调整对映射台/分镜台等其他环节的连带影响，需要在决定调整前单独评估或至少在低峰期验证。
- **协调方提供的"25 次 ? 累计 839s"这条线索本次未能复现**（见 T3 最后一节），如果这个数字来自
  某个我没有查到的其他数据源或统计口径，需要协调方提供具体的查询方式以便交叉验证，本报告不基于
  这条未核实的数字做任何结论。
