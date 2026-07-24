# 案头助手（Agent Drawer）UI 交互整改方案

> 状态：已定稿 · 范围锁定 **P0 + P1 + P2（真·逐 token 流式，重写 provider 层）**
> 日期：2026-07-24
> 关联代码：`frontend/src/agent/*`、`app/agent/*`、`app/hiagent.py`

---

## 0. 背景与结论

用户反馈案头助手体感差，集中在三点：**发送的消息不显示、看不到历史对话、思考过程展示拉垮**。

排查结论一句话：**后端该给的数据基本都给了（用户消息、历史消息接口都在），是前端把它们丢了；而"思考过程"更是踩了死代码坑——前端在监听一个后端根本不发的事件。** 同时，模型真正的"思考"（`reasoning`）在 `hiagent` 层被显式丢弃。本方案彻底重做前端消息流，并把后端调用层改成逐 token 流式，让"思考过程"成为真实、渐进、可折叠的展示。

---

## 1. 问题诊断（定位到代码）

### 1.1 发送的消息不显示
- `send()` 在 `frontend/src/agent/AgentDrawer.tsx:157` 里 `setInput('')` 清空输入框后，**从未把用户这句话放进任何列表**。transcript 区域（`AgentDrawer.tsx:241`）只渲染 `assistantText`/工具卡/审批卡，没有"用户气泡"。
- 后端 `POST …/messages` 已返回落库的用户消息 `message`（`app/agent/api.py:64`），前端只取 `turn_id`（`AgentDrawer.tsx:172`），`message` 直接丢弃。

### 1.2 看不到历史对话
- 每次 `send()` 把 `assistantText / planSteps / toolCards / linkedRuns / citations` 全部清空（`AgentDrawer.tsx:161-167`）。**新一轮 = 抹掉上一轮**，界面上永远只有"当前轮"。
- 后端 `GET /agent/conversations/{id}` 会返回完整 `messages` 历史（`app/agent/api.py:32-40`），前端**从来没调用过**。抽屉关掉再开、组件重挂载，历史归零。
- 所有内容都是易失的组件 state，无加载、无回填。

### 1.3 思考过程展示拉垮（最严重）
- 前端一直等 `assistant.delta`（`useAgentStream.ts:69`、`AgentDrawer.tsx:72`），但 **orchestrator 从未 emit `assistant.delta`**。助手文本实际来自 `plan.updated.reply` 和 `turn.completed.reply`（`orchestrator.py:349`、`:253`）。
- `plan.updated` 处理器（`AgentDrawer.tsx:76-80`）去找后端根本不发的 `steps` 数组（后端发 `reply/tool_calls/done`），于是 `PlanCard` 永远空着（死组件），且每轮循环 `setAssistantText(reply)` **覆盖**上一轮——中间推理一闪而过。
- 全部文本塞进一个裸 `<p className="agent-assistant-text">`（`AgentDrawer.tsx:247`）：无 Markdown、无分段、无"思考 vs 结论"区分、无流式光标。
- `tool.progress` 在 SSE 订阅了（`useAgentStream.ts:70`）却**没有 reducer 处理**，进度全丢。
- Header 直接把枚举 `streamStatus`（`connecting/open/closed`）当文案显示（`AgentDrawer.tsx:214`）。
- 模型真正的"思考" `reasoning`/`reasoning_content` 在 `app/hiagent.py:324`（"推理字段一律丢弃"）被扔掉，`:347` 取出却未用。

---

## 2. 目标 UI 模型

把"单轮覆盖式面板"改成**统一消息流（message-list transcript）**：

```
user 气泡
  └─ assistant 轮次
       ├─ 💭 思考过程（reasoning，默认折叠，流式追加，带光标）
       ├─ 正文（Markdown）
       └─ 内联卡片：审批卡 / Run 进度卡 / 证据引用（归属本轮）
user 气泡
  └─ assistant 轮次 …
```

原则：
- **按 turn 累积，永不覆盖**；轮结束沉淀为历史项。
- 发送即本地插入 user 气泡（乐观更新），不等后端。
- 开抽屉时 `GET /conversations/{id}` 回填历史。
- 思考 = 真实 `reasoning` 流；正文 = `assistant.delta` 流；两者独立分段。

---

## 3. 事件契约（前后端对齐）

| 事件 | 载荷 | 用途 | 现状 |
|---|---|---|---|
| `turn.started` | `{conversation_id}` | 轮开始，前端建"当前 assistant 轮" | 有 |
| `thinking.delta` | `{text}` | **新增**：reasoning 逐 token | **新增** |
| `assistant.delta` | `{text}` | 正文逐 token | 前端已监听，后端将补发 |
| `plan.updated` | `{reply, tool_calls, done}` | 每次循环的边界/兼容标记 | 有，保留 |
| `tool.proposed/started/completed/failed` | `{tool_call_id, ...}` | 内部执行事件，不向用户展示技术卡片 | 有 |
| `tool.progress` | `{tool_call_id, ...}` | 内部进度，仅用于收尾对账 | 有 |
| `approval.required` | approval payload | 审批卡 | 有 |
| `run.linked` | `{run_id}` | Run 进度卡 | 有 |
| `ui.intent` | `{intent}` | 定位建议 | 有 |
| `turn.completed/cancelled` | `{reply, status}` | 收尾，沉淀历史 | 有 |

---

## 4. 分阶段实施

### P0 · 前端救急（半天，纯前端，低风险）
1. **用户气泡**：`send()` 里立即 push 一条 user 项到 transcript；输入内容作气泡文本。
2. **删清空逻辑**：移除 `AgentDrawer.tsx:161-167` 的 7 行 `setXxx([])`，改为"新增当前轮"而非"抹掉"。
3. **思考不覆盖**：`plan.updated` 的 `reply` 改为 append 到当前轮的思考分段，不再 `setAssistantText` 覆盖。
4. **状态文案**：Header 把 `connecting/open/closed` 映射为 `连接中/思考中/已完成`。

### P1 · 前端重构（约 1 天，中风险）
5. **新增 `frontend/src/agent/transcript.ts`**：纯函数归约器 `reduceEvent(state, ev)`，把散在 `AgentDrawer` 的 `useEffect`（`:69-155`）搬入，按 turn 累积、思考/正文分段 append；工具事件仅保留审批收尾和证据归属。
   - 状态模型：`messages: TranscriptItem[]`，`TranscriptItem = UserMsg | AssistantTurn`；`AssistantTurn = { turnId, thinking: string, answer: string, approvals[], runs[], citations[], status }`。
6. **重写 `AgentDrawer.tsx`**：用 `messages` 取代 `assistantText/planSteps/toolCards/…` 一堆并列 state；开抽屉/拿到 `conversationId` 后 `GET /conversations/{id}` 回填历史。
7. **新增组件**：`MessageBubble.tsx`（用户气泡）、`ThinkingBlock.tsx`（💭 可折叠、流式展开、结束折叠、打字光标）、`AssistantTurn.tsx`（正文 Markdown + 内联卡片）。
8. **正文 Markdown**：轻量渲染（段落/列表/代码/加粗），替换裸 `<p>`。
9. **自动滚动到底**（新消息时），滚动容器样式复用 `useScrollContainment`。
10. **CSS**：`index.css` 的 `.agent-*` 段（`:2282-2348`）补气泡、思考折叠区、流式光标、消息间距。

### P2 · 后端真·逐 token 流式（重写 provider 层，较高成本）
11. **`app/hiagent.py`**：
    - 新增 `_post_json_stream(...)`：`stream: true` + `stream_options:{include_usage:true}`，逐块解析 SSE `data:`，累积 `choices[0].delta.content`（正文）、`delta.reasoning`/`reasoning_content`（思考）、`delta.tool_calls[]`（按 `index` 重组 `id`/`function.name`/分片 `arguments`），并对每个增量调用回调 `on_token(kind, text)`。
    - `chat_with_tools(...)` 增加 `stream: bool` 与 `on_token` 参数；非 bailian 路径走流式，bailian 与 json-protocol fallback 至少流式回传正文，`_parse_assistant_turn` 复用于收尾重组，**保证返回同一个 `AssistantTurn`，编排器语义不变**。
    - reasoning 不再丢弃：`AssistantTurn` 增加 `reasoning: str`。
    - 新增配置开关 `agent_stream_tokens`（默认开，可一键回退到非流式，安全灰度）。
12. **`app/agent/orchestrator.py`**：`chat_with_tools` 调用处（`:337`）传入 `on_token` 回调，回调内 `events.append_event(turn_id, "assistant.delta"/"thinking.delta", {"text": ...})`；`plan.updated` 保留为循环边界；`_finish_turn` 不变。
13. **`frontend/src/agent/useAgentStream.ts`**：订阅列表补 `thinking.delta`（`assistant.delta` 已在）。

---

## 5. 涉及文件清单

**前端**
- 改：`frontend/src/agent/AgentDrawer.tsx`、`useAgentStream.ts`、`AgentComposer.tsx`、`types.ts`、`frontend/src/index.css`
- 新：`frontend/src/agent/transcript.ts`、`MessageBubble.tsx`、`ThinkingBlock.tsx`、`AssistantTurn.tsx`

**后端**
- 改：`app/hiagent.py`、`app/agent/orchestrator.py`
- 测：`tests/test_agent_api.py`、`tests/test_chat_with_tools.py`（新增流式解析用例）

---

## 6. 验收标准
1. 发送后立即看到自己的气泡；重开抽屉/刷新后历史仍在。
2. 多轮按序堆叠，旧轮不被新轮抹掉。
3. 思考过程是真实 `reasoning`，独立可折叠、逐 token 追加不覆盖；正文 Markdown 正常。
4. 审批/Run/证据卡归属对应轮次；工具名与技术状态不进入用户对话。
5. 正文与思考逐字流式（打字机）；Header 状态为人话。
6. `agent_stream_tokens` 关闭时自动回退非流式且功能正常；SSE 断线续传（Last-Event-ID）行为不回退。

---

## 7. 风险与回退
- **provider 流式差异**：各网关 SSE 分片/`reasoning` 字段命名不一 → 用 `agent_stream_tokens` 开关灰度，异常自动降级非流式。
- **tool_calls 增量重组**：分片 `arguments` 拼接错误会导致工具参数损坏 → 单测覆盖多分片、多工具、乱序 `index`；收尾仍用 `_parse_tool_arguments` 兜底校验。
- **前端重构回归**：审批/Run/证据卡链路 → P0 与 P1 分开合入，P1 保留归约器纯函数便于单测。

---

## 8. 实施顺序建议
P0（快速止血，可先合入上线）→ P1（前端体感彻底改观）→ P2（后端流式，带开关灰度）。
