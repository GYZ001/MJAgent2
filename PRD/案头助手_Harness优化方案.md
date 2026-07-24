# 漫剧 Agent 2.0：案头助手 Harness 优化方案

> 版本：v0.1（方案稿）
> 日期：2026-07-24
> 状态：待评审
> 适用范围：`app/agent/`（对话编排器）、`app/hiagent.py`（模型网关）、`app/capabilities/`（能力层）、前端 `frontend/src/agent/`
> 关联文档：[`AGENT_MCP_CAPABILITY_PRD.md`](AGENT_MCP_CAPABILITY_PRD.md)、[`../docs/HARNESS_RUNBOOK.md`](../docs/HARNESS_RUNBOOK.md)、[`../docs/PROMPT_SPEC.md`](../docs/PROMPT_SPEC.md)

---

## 0. 执行结论

当前案头助手（对话 Agent）「能力不强大」的根因**不在安全执行层**，而在其之上手写的编排循环本身。

- **应保留（已是一流实现）**：`app/capabilities/` 的 Command Bus + Registry + Policy —— R0–R4 风险分级、preflight（影响范围与成本预检）、以 `args_hash` + `state_fingerprint` 绑定的一次性批准令牌、幂等键、MCP 露出；以及 `orchestrator.py` 的「批准中断→恢复」可复原循环、SSE 事件日志、进程重启安全。这一层作为「安全执行 harness」完成度很高。
- **应重构（能力瓶颈所在）**：模型推理、计划与工具调用部分 —— 采用手写文本 JSON 工具协议、单轮单工具、无流式、朴素上下文管理、无计划分解。

**方向判断（已定）**：不整体引入外部框架（LangGraph / CrewAI / Agent SDK），因为整套栈均为 OpenAI 兼容网关且存在独有的批准/幂等/preflight 不变量，外部框架的执行模型会与之冲突并带来供应商锁定。**采用规范的工具调用*协议*（OpenAI 原生 function calling），并打磨自建 harness；工具执行仍委托现有 Command Bus。**

**决定性有利条件**：所有领域命令已具备 `input_model`（Pydantic 模型），且 `app/mcp/tools.py:26` 已用 `input_model.model_json_schema()` 生成 JSON Schema。原生 tool calling 的 `tools` 数组可直接复用这段既有代码。

---

## 1. 现状诊断

### 1.1 应保留的部分（本方案的不变量）

| 能力 | 位置 | 说明 |
|------|------|------|
| Command Bus + Registry + Policy | `app/capabilities/` | 风险分级、preflight、批准令牌、幂等、MCP 露出 |
| 批准中断→恢复的可复原循环 | `orchestrator.py` `_PAUSED_LOOPS` | `WAITING_APPROVAL` 挂起、批准/拒绝后续跑、进程重启兜底 |
| SSE 事件日志 | `app/agent/events.py`、`schemas.py` | `turn.*` / `tool.*` / `approval.required` 等事件类型齐备 |
| 供应商路由 | `app/hiagent.py` | HiAgent / OpenRouter / 百炼 / DeepSeek / 智谱 / custom，均 OpenAI 兼容 |
| Prompt 注入防御 | `orchestrator.py` `_SYSTEM_PROMPT_HEADER` | 素材内文本一律视为内容而非指令 |

### 1.2 束缚能力的 7 个根本原因

| # | 问题 | 位置 | 症状 |
|---|------|------|------|
| 1 | **文本 JSON 工具协议**：要求模型输出 `{reply, tool_calls, done}` 生 JSON，再以正则＋花括号匹配抽取 | `orchestrator.py:108` `_extract_json` | 推理模型在生成正文前用尽输出预算导致 content 空；解析失败消耗工具预算；不使用原生 `tool_calls` |
| 2 | **单轮单工具**：只执行 `tool_calls[0]`，硬上限 8 次/轮 | `orchestrator.py:345` | 无法并行只读、多段任务浅而慢 |
| 3 | **无流式**：`hiagent.chat` 阻塞到全文完成，`assistant.delta` 仅定义未发火 | `hiagent.py` `chat` / `schemas.py` | 体感「卡住」 |
| 4 | **朴素上下文管理**：直近 20 条截断，工具结果全文 verbatim 追加 | `orchestrator.py:99` | 长任务溢出，无要约/压缩 |
| 5 | **无计划/分解/子代理**：仅扁平 ReAct 循环 | `orchestrator.py` `_run_loop` | 剧本/世界观圣经/分镜等多段案头作业难以贯穿到底 |
| 6 | **检索与 grounding 弱**：仅 `resource.read(uri)` 模板 | `orchestrator.py:65` | 模型靠猜测指定 URI，无法对语料检索 |
| 7 | **每轮把整份目录连进 system prompt** | `orchestrator.py:65` `_tool_catalog_text` | Token 膨胀，随命令数线性恶化 |

---

## 2. 目标与非目标

**目标**
1. 用 OpenAI 原生 function calling 取代脆弱的文本 JSON 协议（消除 #1、#7）。
2. 支持单轮多工具与只读并行、恢复流式输出（#2、#3）。
3. 上下文按摘要＋引用保留并自动压缩（#4）。
4. 引入计划相与领域子代理，贯穿多段案头任务（#5、#6）。
5. 建立案头 Agent 的回归评估 harness。

**非目标**
- 不重写 Command Bus / Policy / 批准 / 幂等 / preflight 逻辑。
- 不引入外部 Agent 框架作为主路径（Pydantic-AI 仅作备选，见 §5）。
- 不改动媒体生成路径（video / image / vlm）。

---

## 3. 分阶段实施计划

### Phase 1 — 迁移到原生工具调用协议 ★最优先、收益最大

**① `app/hiagent.py`：新增支持工具的 chat（现有 `chat` 保持不变）**
- 新增 `chat_with_tools(messages, tools, tool_choice="auto", ...)`：payload 携带 `tools`，从响应解析 `message.tool_calls`（`id` / `function.name` / `function.arguments`）返回轻量结果类型。
- 复用既有 `_post_json` / 重试 / 供应商路由。仿照 `hiagent.py` 的 `response_format` 回退写法（`_chat_with_reasoning_fallback`），以能力标志实现「`tools` 不支持的 provider → 回退现行 JSON 协议」。

**② 工具定义生成 —— 抽到共用小模块**
- 将 `app/mcp/tools.py:26` 的 `_tool_definition`（`input_model.model_json_schema()`）抽为共用函数，供 MCP 与 Agent 复用。
- 由 `registry.list_mcp_tools()` 加 `resource.read` 转为 OpenAI `tools` 数组；从 schema 中剔除 `StandardCommandInput` 的内部字段（`approval_token`、`request_id` 等），不暴露给模型。

**③ `app/agent/orchestrator.py`：替换循环内核（外壳不变）**
- 删除 `_extract_json`、`{reply, tool_calls, done}` 协议、以及把整份目录连进 `_system_prompt` 的逻辑；system prompt 仅保留 `_SYSTEM_PROMPT_HEADER`（含注入防御）。
- `_run_loop` 改为调用 `chat_with_tools`，遍历 `message.tool_calls`；为空则以 `message.content` 作为最终 reply 走 `_finish_turn(completed)`。
- 每个 tool_call 接到现有 `_execute_domain_command` / `_execute_resource_read`（`create_tool_call`→Bus→`WAITING_APPROVAL` 则 `_pause_for_approval`→恢复）；工具结果以 `role:"tool", tool_call_id:...` 消息回填。
- 撤除 `resource.read` 特殊分支（`orchestrator.py:351`），按普通工具处理。
- **必须死守的不变量**：批准恢复时不得改写 args（见 `orchestrator.py:458` 关于 `args_hash` 绑定的注释）、一次性令牌、`_PAUSED_LOOPS` 复原。

**④ 测试**
- 将 `tests/test_agent_api.py` / `tests/test_command_dispatch.py` 更新到原生 tool calling 路径。保留：批准中断→恢复、注入无效化、预算上限用例。

> Phase 1 单独即可消除「脆、浅、慢」的主因（#1、#7），且大量复用既有 Command Bus / MCP schema，实现量小于直觉。

### Phase 2 — 并行工具 + 流式
- 允许单轮多个 `tool_calls`：**R0（只读）并行执行**，写入类逐次并保留批准门禁。
- 实现 `hiagent` 流式版本，真实发火 `assistant.delta` SSE（事件类型已在 `schemas.py` 定义）；前端 `frontend/src/agent/useAgentStream.ts` 适配增量渲染。

### Phase 3 — 上下文管理
- 工具结果不再 verbatim，改为**摘要＋引用**（`run_id` / `resource_uri`）保留。
- 历史超阈值自动**压缩**（旧 turn 折叠为摘要）。system prompt 不再内联目录，工具经原生 `tools` 数组下发，消除重复。

### Phase 4 — 计划与子代理
- 「计划→执行」两相化：复杂任务由 planner 产出 TODO，executor 分步执行（把当前 8 次/轮预算改为任务级预算）。
- 拆分案头领域子代理（剧本 / 世界观圣经 / 分镜）为经 Bus 调用的工具，基础为既有 `app/capabilities/handlers/{bible,screenplay,storyboard}.py`。

### Phase 5 — 检索/grounding + 评估
- 新增对项目语料的 `search` 工具，取代 URI 猜测。
- 建立**案头 Agent 回归评估 harness**：以代表性任务集在 CI 度量工具选择、批准发火、完成率（扩展 `tests/test_agent_api.py`）。

---

## 4. 影响与风险

- **后向兼容**：`hiagent.chat` 签名保持不变，新增 `chat_with_tools`；对媒体生成（video/image/vlm）无影响。
- **供应商差异**：部分网关可能不支持 `tools` → 保留现行 JSON 协议作为**回退**，按 provider 能力标志切换（同 `hiagent.py` `_chat_with_reasoning_fallback` 模式）。
- **安全**：素材内文本不视为指令的注入防御（`_SYSTEM_PROMPT_HEADER`）与批准令牌绑定，在全阶段**不变**。
- **回归面**：批准中断→恢复链路、进程重启兜底、SSE 续传是高风险区，Phase 1 需以现有测试全绿为准入。

---

## 5. 备选方案：Pydantic-AI

若后续决定引入框架，**Pydantic-AI** 是唯一易与 Command Bus 共存的候选：供应商无关、运行于 OpenAI 兼容端点、可直接复用 Pydantic 工具 schema。做法是把它作为薄的循环层套在 Bus 的 executor 之上，而非取代 Bus。标准化程度更高，但引入结合成本，故列为备选而非主路径。

---

## 6. 里程碑建议

| 阶段 | 交付 | 准入标准 |
|------|------|----------|
| Phase 1 | 原生 tool calling 迁移 | 现有 Agent 测试全绿；批准/注入/预算用例保留 |
| Phase 2 | 并行只读 + 流式 | `assistant.delta` 端到端可见；写入类仍逐次门禁 |
| Phase 3 | 上下文压缩 | 长任务不溢出；目录不再内联 prompt |
| Phase 4 | 计划相 + 子代理 | 多段案头任务端到端贯穿 |
| Phase 5 | 检索 + 评估 harness | CI 度量工具选择与完成率 |

**优先级**：Phase 1 为最高优先，单点收益最大，且实现量最小。
