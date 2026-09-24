// useProjectVoices 的模块级缓存 + 订阅：多个组件实例同一个 projectId 只发一次
// 请求；items 里有 generating 才安排 3 秒后的下一次刷新，没有就不安排。
// 每个用例用不同的 projectId，互相之间不共享缓存条目，不需要额外的重置钩子。

const { mockGetProjectVoices } = vi.hoisted(() => ({ mockGetProjectVoices: vi.fn() }))
vi.mock('../../api/voices', () => ({ getProjectVoices: mockGetProjectVoices }))

import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ProjectVoices } from '../../api/voices'
// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import { GENERATING_POLL_MS, useProjectVoices } from './useProjectVoices'

function voicesPayload(generating: boolean): ProjectVoices {
  return {
    voice_model_configured: true,
    auto_generate: true,
    items: [{ character_name: '张三', anchor_key: '', current: null, candidates: [], generating }],
  }
}

function Probe({ projectId, calls }: { projectId: string; calls: unknown[] }) {
  const state = useProjectVoices(projectId)
  calls.push(state.data)
  return null
}

async function mountProbes(count: number, projectId: string) {
  const calls: unknown[] = []
  const els = Array.from({ length: count }, (_, i) => React.createElement(Probe, { key: i, projectId, calls }))
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(React.Fragment, null, ...els))
  })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  return { renderer, calls }
}

describe('useProjectVoices', () => {
  afterEach(() => { mockGetProjectVoices.mockReset() })

  it('同一页面多个订阅者共用一次请求：3 个面板同一个 projectId 只发一次网络调用', async () => {
    mockGetProjectVoices.mockResolvedValue(voicesPayload(false))
    const { renderer } = await mountProbes(3, 'proj-shared')
    expect(mockGetProjectVoices).toHaveBeenCalledTimes(1)
    expect(mockGetProjectVoices).toHaveBeenCalledWith('proj-shared')
    renderer.unmount()
  })

  it('items 里有角色 generating 时安排 GENERATING_POLL_MS 之后的下一次刷新', async () => {
    const scheduled: Array<() => void> = []
    const realSetTimeout = global.setTimeout
    const spy = vi.spyOn(global, 'setTimeout').mockImplementation(((fn: () => void, ms?: number) => {
      if (ms === GENERATING_POLL_MS) { scheduled.push(fn); return 0 as unknown as ReturnType<typeof setTimeout> }
      return realSetTimeout(fn, ms)
    }) as typeof setTimeout)

    mockGetProjectVoices.mockResolvedValue(voicesPayload(true))
    const { renderer } = await mountProbes(1, 'proj-generating')
    expect(scheduled).toHaveLength(1)

    // 手动触发已安排的那次刷新，不真的等 3 秒（照抄 adaptivePoller.test.ts 的手动时钟写法）。
    await act(async () => { scheduled[0](); await Promise.resolve(); await Promise.resolve() })
    expect(mockGetProjectVoices).toHaveBeenCalledTimes(2)

    renderer.unmount()
    spy.mockRestore()
  })

  it('没有角色 generating 时不安排下一次刷新', async () => {
    const scheduled: Array<() => void> = []
    const realSetTimeout = global.setTimeout
    const spy = vi.spyOn(global, 'setTimeout').mockImplementation(((fn: () => void, ms?: number) => {
      if (ms === GENERATING_POLL_MS) { scheduled.push(fn); return 0 as unknown as ReturnType<typeof setTimeout> }
      return realSetTimeout(fn, ms)
    }) as typeof setTimeout)

    mockGetProjectVoices.mockResolvedValue(voicesPayload(false))
    const { renderer } = await mountProbes(1, 'proj-idle')
    expect(scheduled).toHaveLength(0)

    renderer.unmount()
    spy.mockRestore()
  })
})
