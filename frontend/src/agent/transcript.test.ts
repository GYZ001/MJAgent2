import { describe, expect, it } from 'vitest'
import { reduceEvents } from './transcript'
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

  it('合并工具进度、Run 和证据，工具终态后移除待审批卡', () => {
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

    expect(state.tools).toEqual([expect.objectContaining({
      id: 'tc1', name: 'episode.check', status: 'succeeded', summary: '完成', risk: 'R2',
    })])
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
