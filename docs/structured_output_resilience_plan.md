# 结构化输出偶发失败韧性方案

状态：设计 + 实施。触发案例 `ERR-20260826-93c8e3`（`screenplay.identity.current.v6`，
`run_f8a23b28d098`/`ep_e4b00ccc7db5`，`provider_calls.id=12985/12986`）。根因见任务书，
本文档不复述，只记录设计决策与实施结果。

## 核心原则

**重试必须改变点什么，否则不叫重试叫复读。** 已核实 `_identity_structured_with_resample`
的两次调用 `request_hash` 逐字节相同（messages/temperature/max_tokens/schema 全同），两次
都停在同一决策点、失败方式相同——这不是"多给一次机会"，是把一次失败的成本记了两遍。

## P0/P1/P2 分级

- **P0-A**：让 identity 决议的 resample 真正发出不同的请求（温度 + 定向提示扰动）。
- **P0-B**：`extract_json` 的闭合修复支持一次性补齐 N 个缺失闭合符（栈式回溯），严格
  fail-closed；同时确认并记录"为什么不用在这里区分 finish_reason=length"——因为
  `_reject_truncated_chat_response` 已经在更底层把 truncation 转成 `ProviderError`，
  截断内容永远不会走到这条 extract_json 路径（见下方"P0-B 边界"一节的证据链）。
- **P0-C**：`errors.classify()` 补 `StructuredFormatError` 分支；系统化排查所有自定义异常
  类与 classify 分支的对应关系，把发现的其它真实缺口（不是猜测）一并补上。
- **P1（评估）**：生产路径是否要移植 `yyft_serial10.py` 的离线重试阶梯——评估后**不做**，
  理由见下文。
- **本次不做（P2/超范围）**：
  - 不改 `_identity_structured_with_resample` 之外的其它 `chat_structured` 调用点的重试策略
    （如分镜/成片台的各类结构化调用）——那些不在本次根因证据链里，盲目照搬会扩大变更面。
  - 不把 `errors.py` 的判定机制从"类名字符串匹配"整体换成别的分发框架；只做最小的 MRO 名称
    集合替代（见 P0-C），不引入新抽象。
  - 不改 `_repair_structural_json_delimiters`／`_repair_merged_object_string_entry` 等其它已有
    修复器的内部逻辑，只新增/泛化 P0-B 需要的那一个。
  - 不新增 async 任务队列或轮询基础设施来支撑生产自动重试（P1 决定不做，理由见下文）。

---

## P0-A：让 resample 真的不一样

### 现状

`app/portraits.py:266-321` `_identity_structured_with_resample`：`IDENTITY_UNUSABLE_RESPONSE_
RESAMPLES=1`，在 `StructuredFormatError.unparseable=True`（供应商从未交付可解析对象）或
`ProviderError` 且 `provider_answer_undelivered()`（传输层夭折）时重试一次。三个调用点
（`screenplay.identity.current.v6` 等）把同一个 `messages` 列表和同一个 `temperature` 原样
传给循环里的每一次 `chat_structured` 调用；`operation_id` 虽然按 attempt 变化，但那只是本地
幂等记账用的字段，**不进入发给供应商的请求体**。`app/db.py:provider_request_hash` 只哈希
`request_json`（messages/temperature/max_tokens/response_format 等真实请求字段），因此两次
attempt 的 `request_hash` 必然逐字节相同——这正是 ERR-20260826-93c8e3 里实测到的现象。

### 候选方案（评估 3 个）

1. **只提温度**：resample 时把 `temperature` 提高一个固定增量。实现最简单，改一处
   `request_hash` 必然不同；但不针对性——不保证真的能打破"模型在这个决策点提前结束"这个
   具体故障模式，只是撞大运式的重采样。
2. **续写补全（prefix continuation）**：把上次的残缺输出作为 assistant 消息前缀塞回去，
   只要求模型接着写完剩余的 JSON。理论上最省 token、最不改变已生成的判断内容；但 HiAgent
   是多供应商网关，不是所有底层 provider 都支持"assistant 消息末尾不闭合、模型接续"这种
   prefill 语义（这不是 Anthropic 式的显式续写接口，是 OpenAI 兼容 `/chat/completions`
   通用格式，多数实现会把这当一条完整的历史轮次，而不是"继续写你自己"）。生产上验证成本高、
   一旦某个供应商不支持会静默产出胡乱衔接的文本，风险与收益不成正比，本次不采用。
3. **温度提升 + 针对失败模式的定向提示**：在方案 1 基础上，resample 请求追加一段简短提示，
   明确指出"上次输出没有交付语法闭合的完整 JSON，本次必须让所有 `{ }[ ]` 严格配对闭合"，
   不改变身份判定规则本身的任何一个字。

**选定方案 3。** 理由：
- **确定性因何被打破**：温度改变会改变采样分布（哪怕原温度已经很低），而追加的提示文本本身
  就改变了 prompt 的 token 序列——两者任一都足以让 `request_hash` 不同，叠加使用是双重保险，
  不是叠床架屋（各自解决不同的失败面：温度解决"模型可能再次做出同样选择"，提示解决"模型
  可能压根没意识到自己被期待写完整闭合的 JSON"）。
- **为什么不损害正确性**：这次重试只发生在**第一次尝试已经确认没有交付任何可用判断**之后
  （`unparseable=True` 或传输层夭折）——`_identity_structured_with_resample` 顶部注释本身就
  说明了这个前提："there was no identity judgement to preserve"。既然第一次的结果已经是
  "零信息"，第二次多大的采样扰动都不构成"把一个已经给出的正确答案重新摇骰子直到它变錯"，
  因为压根没有第一个答案存在。温度提升幅度也刻意保守（+0.2，封顶不超过 1.0），仍处在原温度
  0.05~0.1 量级之上、远未进入"胡乱发挥"的区间；追加的提示只谈 JSON 语法闭合，一个字都没有
  松动任何身份判定规则、证据要求或输出 schema 本身。
- 追加提示只加在**最后一条消息**上（通常就是唯一的 user 消息），不改变消息角色结构，不影响
  下游对 `messages[0]` 内容做散列/审计的任何既有假设（未发现有调用方假设 resample 时
  `messages` 不变）。

### 实施

`app/portraits.py`：
- 新增两个常量 `IDENTITY_RESAMPLE_TEMPERATURE_BUMP`（+0.2）、
  `IDENTITY_RESAMPLE_TEMPERATURE_CAP`（1.0）与一段追加提示文案常量
  `IDENTITY_RESAMPLE_FORMAT_REMINDER`。
- `_identity_structured_with_resample` 在 `attempt > 0` 时：把追加提示拼进最后一条消息的
  `content`，把 `kwargs["temperature"]` 提升到 `min(cap, base + bump)`；`attempt == 0`
  时完全不变（不影响首次调用的既有行为/既有测试）。

### 如何验证"确实不一样"（独立观察点）

新增测试 `test_resample_attempt_actually_differs_from_first_attempt`
（`tests/test_character_discovery.py`）：monkeypatch `model_gateway.chat_structured` 为一个
记录每次实际收到的 `messages`/`temperature` 的假函数（不是复用被测函数自己的任何内部状态），
第一次抛 `StructuredFormatError(unparseable=True)`，断言：
- 两次调用记录到的 `messages` 不相等；
- 两次调用记录到的 `temperature` 不相等，且第二次 > 第一次。

这是从"调用方实际收到了什么"这个独立观察点验证的，不是复用生产代码自己的判断。

---

## P0-B：修复库支持补 N 个闭合符

### 现状缺口

`app/schemas.py:1890 _close_missing_root_object`：只在整个文本走完之后**剩余未闭合容器栈
恰好是 `["}"]`**（只缺根对象自己的收尾括号）时才补；本次真实故障需要依次补 `]` 和 `}`
两层，不满足这个条件，函数直接原样返回，最终 `extract_json` 整体失败。

`app/schemas.py:1927 _repair_trailing_container_closure` 处理的是另一种情形：文本**末尾确实
有一个闭合符，但类型错了**（栈顶要 `]` 却写了 `}`），用整段 `reversed(expected_closers)`
替换掉那一个错误字符——它已经支持多字符替换，只是触发条件要求"错误闭合符就是最后一个
字符"。本次故障是**没有任何闭合符**、文本直接在字符串值写完后中止（报错位置 `char 8418 ==
len(content)`），命中的是 `_close_missing_root_object` 那条路径，不是这条。

### 方案

对 `_close_missing_root_object` 做**唯一的泛化**：把成功条件从"栈恰好是 `["}"]`"放宽为
"栈非空"，其余逐字符扫描逻辑（字符串状态跟踪、转义跟踪、遇到不匹配闭合符立即放弃）完全不变。
重命名为 `_close_missing_trailing_containers` 以准确反映新语义（不再局限于"根对象"）。这是
纯语法判据：整段文本只被扫描一次，唯一放宽的是"最多补几个字符"，触发所需要满足的**歧义排除
条件一个没有变松**：
- 扫描中途任何一个闭合符与栈顶类型不符 → 立即返回原文，不猜；
- 结束时仍处于字符串内部（`in_string`）或转义悬空（`escaped`）→ 返回原文，不猜（这正是
  "内容真被从字符串中间截断"的情形，不能靠拼括号掩盖）；
- 栈为空（本来就是合法 JSON，没有缺闭合符）→ 返回原文，不动它。

### 为什么不需要在这个函数里单独判断 finish_reason=length

`app/hiagent.py:882 _reject_truncated_chat_response`：任何 `finish_reason=='length'` 的响应，
在 `chat()`（非流式 `:1271/:1577/:1631`，流式 `:2739`，覆盖 `chat()` 的所有出口）返回内容
字符串之前就会被转成 `ProviderError(failure_kind=OUTPUT_TRUNCATED)` 抛出——`chat_structured`
拿到的 `last_raw`（进而喂给 `extract_json` 的所有文本）**结构上不可能是一次 token 预算耗尽
截断的产物**；如果真被截断，异常会在 `await chat(...)` 那一步就抛出，根本进不到
`extract_json` 这一层，走的是完全不同的 `ProviderError` → `errors.classify()` "provider"
分类（已有独立处理，`ERR-20260825-497eea` 那次真截断走的正是这条路）。这个不变式不是本次
新增的，是仓库已有并在多处依赖的既定设计（`hiagent.py:164-182` 关于
`_cached_successful_provider_response`/`_durable_operation_proven_undelivered` 的注释明确
点名了这一点）。因此 P0-B 只需要处理"模型自己选择提前结束（`finish_reason=stop`）、但语法
上唯一确定的收尾方式"这一种情形，不需要（也不应该）在 `extract_json` 内部重新引入
`finish_reason` 参数——那会是给一个已经在更底层被强制满足的不变式重复交叉验证，徒增维护面。

即便这个不变式将来被破坏（比如新增了某条绕过 `chat()` 的路径），泛化后的函数仍然只在**语法
上唯一确定**时才补，补出来的对象还要再过 pydantic schema 校验和业务 `validate()` 回调
两道关——一个被真正截断、语义不完整的候选，闭合泛化最多让它从"语法失败"变成"语义/schema
失败"，不会让一个真正残缺的答案被当成正确答案通过。

### fail-closed 测试对齐

读了 `tests/test_extract_json.py` 里所有闭合相关的 fail-closed 用例：
- `test_extract_json_closes_only_missing_root_brace_at_eof`（单层，本来就通过，泛化后行为
  不变——单层是多层的特例）。
- `test_extract_json_does_not_guess_multiple_missing_closers`：这条测试**当前**断言"缺
  `]}` 两层时必须拒绝"。逐字符模拟后确认：这段文本 `'{"episode_no": 9, "events": [{"event_id":
  "E1"'` 走到结尾时栈是 `["}", "]", "}"]`、没有中途不匹配、没有停在字符串内部——按泛化后的
  规则，这正是"语法上唯一确定"的合法补齐对象，补出 `{"episode_no": 9, "events":
  [{"event_id": "E1"}]}`。这条测试的断言与本次任务要修的生产故障是同一个故障模式（只是层数
  更少），继续要求它失败等于继续放着这个真故障不修。**已按新预期更新该测试**（保留测试名
  释义、改断言为"应当成功补齐并解析出正确结构"），并在测试里写明这是有意的行为变更，指向
  `ERR-20260826-93c8e3`。
- 新增 `test_extract_json_refuses_multi_closer_repair_inside_unterminated_string`：文本在
  多层容器打开后于**字符串内部**（缺右引号）直接截止，断言仍然拒绝——这是泛化后唯一需要
  新补的边界用例，保证"截断在字符串中间"这类真正有信息缺失风险的情形不会被新逻辑误接受。
- `test_extract_json_does_not_repair_non_eof_closer_mismatch`：不受影响（它在 EOF 之前就
  遇到不匹配闭合符，两个函数都在那一步提前放弃，与本次改动的触发条件无关，已用逐字符模拟
  核实）。

---

## P0-C：`errors.classify()` 补分支 + 兄弟异常排查

### 直接要求：`StructuredFormatError`

`app/harness/model_gateway.py` 定义的 `StructuredFormatError` 完全没有出现在
`classify()` 里，`type(exc).__name__=="StructuredFormatError"` 不命中任何分支，落到
`_FALLBACK`（"system"/"SYS"，提示语"服务器内部错误，请把错误码反馈给技术人员"）——这正是
用户在 ERR-20260826-93c8e3 里看到的、与真实情况不符的提示。新增分支：归入既有
`"generation"/"JSON"`（与 `StageError` + "JSON 解析失败" 同类），提示语已经是"内容生成未
通过格式或业务校验，可点击重试"，准确且可操作。

### 排查方法

用脚本枚举 `app/` 下所有继承自 `Exception`/`ValueError`/`RuntimeError`/... 的自定义类
（含间接继承），得到 57 个；逐一用 `grep` 确认是否被上游任何 `except <Name>` 捕获并转换。
从未被捕获、且会经由某个 `except Exception as exc:` 泛捕获点（`app/domain/*_ops.py` 里一系列
`errors.record_and_format(exc, ...)` 调用，最终兜底是 `app/main.py:223` 的全局
`@app.exception_handler(Exception)`）原样冒泡到 `classify()` 的，才算真缺口；MCP 协议错误
（`app/mcp/*.py`）与 `media_exec/run_job.py` 内部的调度类控制流异常（各类 lease/fence 信号）
走的是完全不同的错误面，不在这套 REST `errors.classify()` 体系内，本次不动。

### 发现的真实缺口（全部已核实"从未被捕获"）

| 异常类 | 定义位置 | 判定 | 依据 |
|---|---|---|---|
| `StructuredFormatError` | model_gateway.py | generation/JSON | 任务书给定根因 |
| `StructuredProviderRejection` | model_gateway.py | provider/LLM | 与 `StructuredFormatError`/`StructuredSemanticError` 同一函数 `chat_structured` 抛出；`media_exec/run_job.py:4318` 已经把它显式转成 `ProviderError` 再记日志，说明其"本质是供应商拒答"的判断在本仓库里已有先例——但只在这一条调用路径生效。`_identity_structured_with_resample` 完全没有捕获它，会从身份判定一路冒泡到 `screenplay_generate` 的兜底处理器，此时走的是原始类型，命中不到任何分支。 |
| `ScreenplayIdentityGateError` | production/screenplay_repair.py | quality_gate/QA | 与已覆盖的同文件同模式兄弟类 `ScreenplayNarrativeGateError`（专用注释里明确讨论过 `PrepPackGateError` 同族）完全同构——都是"硬门禁未通过，供应商调用本身成功"。 |
| `ScreenplaySceneMergeError` | screenplay_scene_shards.py | generation_contract/GEN-CONTRACT | 与已覆盖的同文件兄弟类 `ScreenplaySceneShardError` 是同一套场次分片生成合同的分片阶段/合并阶段两半，理应同一分类。 |
| `ScreenplayIRFidelityError` | screenplay_ir.py | generation/GEN | 与已覆盖的同文件兄弟类 `ScreenplayIRIdentityConflictError` 同属 IR 构建期的业务保真度校验，现有测试 `test_screenplay_ir.py:2292` 已锁定后者是 generation/GEN，前者理应同类。 |
| `SceneAssetQualityError` | scenes.py | quality_gate/QA（通过 MRO 自动命中，见下） | 显式继承 `ContentGenerationError`，但 `classify()` 原来只做 `type(exc).__name__` 精确匹配，子类完全绕过父类已有的分类——这是本次发现的最有共性的一类缺口："分类表按名字枚举，继承关系形同虚设"。 |

### 结构性修复：从"精确类名匹配"改为"MRO 名称集合匹配"

除了给上表逐条补分支，还把 `classify()` 的匹配机制从 `type(exc).__name__ == "X"`
改成 `"X" in {klass.__name__ for klass in type(exc).__mro__}`（等价查询，保留原有的字符串
判定风格以避免新增 import/环依赖，只是判定对象从"自己的类名"换成"自己以及所有基类的类名
集合"）。这一步不是新增一条判据，是修好判据本身的一个结构性缺陷：**只要某个异常类未来
选择继承一个已分类的基类**（`SceneAssetQualityError(ContentGenerationError)` 就是活的
先例），旧的精确匹配机制会让这种继承形同虚设，子类静默漏判；换成 MRO 集合匹配后，这类
继承关系天然生效，不需要每加一个子类就手工补一行。经检查，本仓库目前没有任何两个不相关的
自定义异常类共享同一个类名（57 个类名互不重复），所以这个改动不会引入误判风险。

`StoryboardOutlineAuthorityError` 的子类 `StoryboardOutlineMigrationRequired`
（storyboard_authority.py）同样从未被捕获；因为 MRO 匹配已经生效，这里只需要给基类
`StoryboardOutlineAuthorityError` 补一条分支（quality_gate/QA，理由与 `PrepPackGateError`
同构——都是"持久化的权威快照缺失或内部分裂"这类结构性前置条件未满足），子类自动一并覆盖，
不用单独再列一行。

### 评估过但判定"当前不构成缺口"的类（有证据，不是没查）

`IdentityContractError`／`DialogueSceneBindingError`／`GrantValidationError`／
`ProviderTasksNotTerminalError`／`VideoBudgetAuthorizationError`／`ReplanActiveWorkError`：
逐一 grep 确认每一个 `raise` 点对应的调用方（`validators.py`/`stages.py`/`portraits.py`/
`compiler.py`/`orchestration/api.py`/`media_exec/run_job.py`/`planning.py` 等）都有**局部**
`except` 捕获并转换成别的信号（追加进校验错误列表、转成 `CompileError`、转成
`HTTPException`），未发现任何一条会以原始类型冒泡到 `classify()`。

`IdentityAuthorityConflictError`／`BlueprintSourceOwnershipError`／
`BlueprintSourceOccurrenceError`：grep 未找到任何 `except` 捕获点，理论上和上面补的几个一样
是真缺口（语义也匹配 quality_gate：identity_authority.py/narrative_blueprint.py 里都是"业务
一致性冲突，供应商调用本身没问题"这类信息）。**本次一并补上**（quality_gate/QA），因为
判定方式与已修的几条完全同构，风险可控；不属于"评估过判定不做"，是同批修复的一部分。

MCP 相关四个类（`AuthError`/`ForbiddenError`/`McpError`/`PromptError`/`ResourceError`）与
`media_exec/run_job.py`/`completion_grant.py` 内部一批用于调度控制流的信号类
（`LeaseLost`/`_ContinuityWait`/`ReviewDependencyFence`/`ProviderCreateUnresolved`/
`VideoInflightAdmissionDeferred`/`VideoPlanStaleFence`/`VideoInputRepairRequired`/
`ConcatOperationConflict`/`ConcatOperationInProgress`/`VideoCommandOperation*`/
`ProductionRevisionOwnershipLost`/`ScreenplaySceneShardOwnershipLost`/`StateConflict`/
`SceneCandidateReviewRequired`/`AgentLoopFailure`）：这些要么走独立的 MCP JSON-RPC 错误面
（不经过 `errors.classify()`），要么是模块内部的调度/续跑控制流信号，各自都有专门的调用点
捕获处理（本次逐个抽查了其中 6 个的捕获点，均命中局部 `except`），判定"不在本次 REST 错误
分类体系覆盖范围内"，不动。这批没有做到 100% 逐一穷举捕获点核实（超出本次时间预算），标注
为"低置信度未继续深挖"，不是"确认无问题"。

---

## P1：生产路径要不要自动重试

**评估结论：不做。**

理由：
1. `app/domain/screenplay_ops.py` 里 `screenplay_generate` 已经是"轻量流程一次性生成到
   原子发布"（该文件自己的注释原话），**没有可续跑的分片 checkpoint**——对它做自动重试
   意味着重新跑一遍整集剧本生成的全部供应商调用，不是本次 P0-A 那种"只重采一次失败的那个
   决策点"。cost 会成倍放大，且当前 identity 调用点显式设置了
   `"reuse_successful_operation": False`，不会去重复用旧调用。
2. 这个函数已经有相当复杂的 `run_id` 归属围栏逻辑（`active_screenplay_run_id`、
   `asyncio.CancelledError` 与"已被恢复任务替代的旧协程"分支），是当前仓库里在途改动最密集
   的文件之一（`app/stages.py` 已知有并发 agent 在改）。在这层再叠加一个"阶梯退避 + 自动
   重启整个流程"的状态机，属于 CLAUDE.md 明令的"为了炫技加入复杂状态机"，且会显著增加这个
   本已高风险文件的变更面与验证成本，与"可运行性 > 稳定性"的优先级冲突。
3. `yyft_serial10.py` 的阶梯（60s→120s→300s）是为**无人值守、以集为单位、拥有全部时间预算
   的离线回归**设计的；生产路径是用户发起、期望及时反馈的动作，机械照搬会让用户等待却看不到
   任何进度（该 stage 本来就是后台任务，轮询 UI 能显示"进行中"，但连续 8 分钟"进行中"对一次
   用户主动发起的生成来说体验很差，且没有任何取消入口）。
4. **"拦住用户时必须给出路"已经通过 P0-C 满足**：修复前，这条失败会显示"系统内部错误，请把
   错误码反馈给技术人员"——把用户晾在原地；修复后，走 `generation/JSON` 分类，提示语明确是
   "可点击重试"，且这次重试是用户的真实新一次点击，产生的是一次genuinely 新的请求（不是
   本文档 P0-A 修复前那种"重试等于复读"的情形，因为**每一次用户手动点击重试都会重新执行整个
   `screenplay_generate`**，其内部的 identity 决议 attempt-0 本身就是一次全新请求，不存在
   "自动重试"要解决的"同一份请求打两次"问题——那个问题只存在于同一次 run 内部的
   `_identity_structured_with_resample` 循环里，P0-A 已经解决）。
5. 残余风险有界：P0-A 让 resample 更可能一次内部消化掉这次故障，P0-B 让更多"缺闭合符"的响应
   在 `extract_json` 层面直接被救活，两者叠加后这类失败到达用户可见层的概率应显著下降
   （`error_logs` 里该 schema 相关 36 次失败中，`StructuredFormatError` 16 次——这批里
   有多少本来就会被 P0-B 直接挽回、有多少即使挽回不了也会被 P0-A 的 resample 用不同请求
   救回来，缺少能否复现历史响应文本的手段核实，不编造具体比例)。若后续观察到 P0-A+P0-B
   之后残余失败仍然频繁到需要自动重试，应该在**独立的异步任务/轮询基础设施**上做（这本身是
   一个不小的项目，需要与轮询 UI、取消入口一起设计），不属于本次任务范围，留给后续单独立项。

---

## 验证记录

见任务报告正文（测试真实运行数字、P0-A 的独立观察点验证结果等），此处只记录设计决策。
