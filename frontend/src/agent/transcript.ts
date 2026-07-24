/**
 * 案头助手对话流归约器（纯函数，便于单测）。
 *
 * 把一个 turn 的 SSE 事件序列折叠成「一条 assistant 消息」的展示状态：
 * - thinking：模型思考过程（reasoning 流 + 调用工具前的中间叙述），分段追加，不覆盖。
 * - answer：最终答复（流式逐 token，结束时以后端 reply 为权威覆盖，修正任何流式漂移）。
 * - approvals / runs / citations：归属本轮的内联卡片。
 *
 * 设计要点：reduceEvents 每次都从完整事件数组重算，天然幂等、断线续传安全，也让归约逻辑
 * 独立于 React 组件，可直接单测。
 */
import type { AgentStreamEvent, ApprovalCardData, UiIntent } from './types'

export interface RunCardState {
  runId: string
  summary?: string
}

export type TurnStatus = 'streaming' | 'done' | 'failed' | 'cancelled'

export interface AssistantTurnState {
  thinking: string
  answer: string
  approvals: ApprovalCardData[]
  runs: RunCardState[]
  citations: string[]
  intent: UiIntent | null
  status: TurnStatus
  error: string | null
}

export interface UserTranscriptItem {
  kind: 'user'
  id: string
  text: string
  createdAt: number
}

export interface AssistantTranscriptItem extends AssistantTurnState {
  kind: 'assistant'
  id: string
  turnId: string | null
  createdAt: number
}

export type TranscriptItem = UserTranscriptItem | AssistantTranscriptItem

export function emptyTurnState(): AssistantTurnState {
  return {
    thinking: '',
    answer: '',
    approvals: [],
    runs: [],
    citations: [],
    intent: null,
    status: 'streaming',
    error: null,
  }
}

/**
 * 只把确实属于目标 turn 的非空事件快照写回对话。
 *
 * 发送新消息时 SSE 会先 reset。如果用这份空快照去更新上一个 turn，
 * 会把已完成答案清空并改回 streaming。这里作为纯函数门禁，防止该类竞态。
 */
export function mergeTurnState(
  items: TranscriptItem[],
  targetTurnId: string | null,
  streamTurnId: string | null,
  eventCount: number,
  turnState: AssistantTurnState,
): TranscriptItem[] {
  if (!targetTurnId || streamTurnId !== targetTurnId || eventCount === 0) return items
  let hit = false
  const next = items.map(item => {
    if (item.kind !== 'assistant' || item.turnId !== targetTurnId) return item
    hit = true
    return { ...item, ...turnState }
  })
  return hit ? next : items
}

const str = (v: unknown): string => (v == null ? '' : String(v))

function appendSegment(existing: string, addition: string): string {
  const add = addition.trim()
  if (!add) return existing
  if (!existing) return add
  return `${existing}\n\n${add}`
}

/** 把一个 turn 的完整事件序列折叠成 assistant 展示状态。 */
export function reduceEvents(events: AgentStreamEvent[]): AssistantTurnState {
  const state = emptyTurnState()
  // 已到达终态（completed/failed/rejected）的工具调用：用于把对应审批卡从待办中移除。
  const resolvedToolCalls = new Set<string>()

  for (const ev of events) {
    const p = (ev.payload || {}) as Record<string, unknown>
    switch (ev.event_type) {
      case 'assistant.delta': {
        const delta = str(p.text ?? p.delta ?? p.reply)
        if (delta) state.answer += delta
        break
      }
      case 'thinking.delta': {
        const delta = str(p.text ?? p.delta ?? p.reasoning)
        if (delta) state.thinking += delta
        break
      }
      case 'plan.updated': {
        const reply = str(p.reply)
        const done = p.done === true
        if (done) {
          // 最终迭代：以后端 reply 为权威答复（覆盖流式缓冲，修正漂移）。
          if (reply) state.answer = reply
        } else {
          // 中间迭代（模型将调用工具）：把这段叙述沉淀进思考区，清空正文缓冲。
          const narration = state.answer.trim() || reply
          state.thinking = appendSegment(state.thinking, narration)
          state.answer = ''
        }
        break
      }
      case 'tool.proposed':
      case 'tool.started':
      case 'tool.progress': {
        // 工具过程是内部执行细节，不进入用户对话展示。
        break
      }
      case 'tool.completed':
      case 'tool.failed': {
        const id = str(p.tool_call_id ?? p.id)
        const failed = ev.event_type === 'tool.failed'
        if (id) {
          resolvedToolCalls.add(id)
        }
        if (Array.isArray(p.resource_uris)) {
          for (const uri of p.resource_uris) {
            const s = str(uri)
            if (s.includes('/artifacts/')) {
              const aid = s.split('/artifacts/')[1]
              if (aid && !state.citations.includes(aid)) state.citations.push(aid)
            }
          }
        }
        if (failed && p.error_id) {
          state.error = `工具失败 · ${str(p.error_code)} · ${str(p.error_id)}`
        }
        break
      }
      case 'approval.required': {
        const toolCallId = str(p.tool_call_id)
        if (toolCallId && !state.approvals.some(a => a.tool_call_id === toolCallId)) {
          state.approvals.push({
            tool_call_id: toolCallId,
            command: str(p.command ?? p.tool),
            title: str(p.title ?? p.command ?? p.tool) || '操作',
            summary: str(p.summary),
            risk: str(p.risk) || 'R2',
            estimated_cost_cny: typeof p.estimated_cost_cny === 'number' ? p.estimated_cost_cny : null,
            warnings: Array.isArray(p.warnings) ? p.warnings.map(str) : [],
            approval_token: p.approval_token != null ? str(p.approval_token) : undefined,
            expires_at: typeof p.expires_at === 'number' ? p.expires_at : undefined,
            affected: typeof p.affected === 'object' && p.affected ? p.affected as Record<string, unknown> : undefined,
          })
        }
        break
      }
      case 'run.linked': {
        const runId = str(p.run_id)
        if (runId && !state.runs.some(r => r.runId === runId)) {
          state.runs.push({ runId, summary: p.summary != null ? str(p.summary) : undefined })
        }
        break
      }
      case 'ui.intent': {
        const intent = (p.intent ?? p.ui_intent) as UiIntent | undefined
        if (intent) state.intent = intent
        break
      }
      case 'turn.completed': {
        const reply = str(p.reply ?? p.message)
        if (reply) state.answer = reply
        state.status = p.failure_code ? 'failed' : 'done'
        if (p.failure_code && !state.error) state.error = str(p.reply ?? p.failure_code)
        break
      }
      case 'turn.cancelled': {
        const reply = str(p.reply ?? p.message)
        if (reply) state.answer = reply
        state.status = 'cancelled'
        break
      }
      default:
        break
    }
  }

  // 已执行/已拒绝的工具，其审批卡不再展示为待办。
  if (resolvedToolCalls.size) {
    state.approvals = state.approvals.filter(a => !resolvedToolCalls.has(a.tool_call_id))
  }
  return state
}
