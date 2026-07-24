import { describe, expect, it } from 'vitest'
import { emptyTurnState, mergeTurnState, reduceEvents, type TranscriptItem } from './transcript'
import type { AgentStreamEvent } from './types'

function event(event_type: string, payload: Record<string, unknown> = {}, event_id = 1): AgentStreamEvent {
  return { event_id, event_type, payload }
}

describe('reduceEvents', () => {
  it('分别累积思考与正文，并以最终 reply 校正流式结果', () => {
    const state = reduceEvents([
      event('thinking.delta', { text: '先查询' }, 1),
      event('thinking.delta', { text: '证据。' }, 2),
      event('assistant.delta', { text: '结' }, 3),
      event('assistant.delta', { text: '论' }, 4),
      event('plan.updated', { reply: '结论。', tool_calls: [], done: true }, 5),
      event('turn.completed', { reply: '结论。', status: 'completed' }, 6),
    ])

    expect(state.thinking).toBe('先查询证据。')
    expect(state.answer).toBe('结论。')
    expect(state.status).toBe('done')
  })

  it('把工具前叙述沉淀到思考区，不覆盖上一段', () => {
    const state = reduceEvents([
      event('assistant.delta', { text: '我先读取项目。' }, 1),
      event('plan.updated', { reply: '我先读取项目。', tool_calls: [{}], done: false }, 2),
      event('assistant.delta', { text: '再检查分集。' }, 3),
      event('plan.updated', { reply: '再检查分集。', tool_calls: [{}], done: false }, 4),
    ])

    expect(state.thinking).toBe('我先读取项目。\n\n再检查分集。')
    expect(state.answer).toBe('')
  })

  it('不暴露工具状态，但保留 Run、证据和审批收尾逻辑', () => {
    const state = reduceEvents([
      event('tool.proposed', { tool_call_id: 'tc1', tool: 'episode.check', risk: 'R2' }, 1),
      event('approval.required', {
        tool_call_id: 'tc1', tool: 'episode.check', summary: '检查分集', risk: 'R2',
      }, 2),
      event('tool.progress', { tool_call_id: 'tc1', tool: 'episode.check', message: '50%' }, 3),
      event('run.linked', { run_id: 'run1', summary: '处理中' }, 4),
      event('tool.completed', {
        tool_call_id: 'tc1', tool: 'episode.check', status: 'succeeded', summary: '完成',
        resource_uris: ['manju://projects/p1/artifacts/art1'],
      }, 5),
    ])

    expect('tools' in state).toBe(false)
    expect(state.approvals).toEqual([])
    expect(state.runs).toEqual([{ runId: 'run1', summary: '处理中' }])
    expect(state.citations).toEqual(['art1'])
  })

  it('保留失败与取消终态', () => {
    const failed = reduceEvents([
      event('turn.completed', { reply: '模型失败', failure_code: 'model_call_failed' }),
    ])
    expect(failed.status).toBe('failed')
    expect(failed.error).toBe('模型失败')

    const cancelled = reduceEvents([event('turn.cancelled', { reply: '已停止' })])
    expect(cancelled.status).toBe('cancelled')
    expect(cancelled.answer).toBe('已停止')
  })
})

describe('mergeTurnState', () => {
  const history: TranscriptItem[] = [
    { kind: 'user', id: 'u-old', text: '旧问题', createdAt: 1 },
    {
      kind: 'assistant', id: 'a-old', turnId: 'turn-old', createdAt: 2,
      ...emptyTurnState(), status: 'done', answer: '旧答案必须保留',
    },
  ]

  it('新消息 reset SSE 时不用空 streaming 快照覆盖上一轮', () => {
    const merged = mergeTurnState(history, 'turn-old', null, 0, emptyTurnState())

    expect(merged).toBe(history)
    expect(merged[1]).toMatchObject({ status: 'done', answer: '旧答案必须保留' })
  })

  it('仅把有事件且 turn 归属一致的快照写回目标消息', () => {
    const current: TranscriptItem = {
      kind: 'assistant', id: 'a-new', turnId: 'turn-new', createdAt: 3,
      ...emptyTurnState(),
    }
    const state = { ...emptyTurnState(), thinking: '正在查询', answer: '新答案' }
    const merged = mergeTurnState([...history, current], 'turn-new', 'turn-new', 2, state)

    expect(merged[1]).toMatchObject({ status: 'done', answer: '旧答案必须保留' })
    expect(merged[2]).toMatchObject({ status: 'streaming', thinking: '正在查询', answer: '新答案' })
  })
})
