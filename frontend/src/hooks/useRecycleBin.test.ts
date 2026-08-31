import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApprovalRequiredError } from '../api/client'
import type { ApprovalPreflight } from '../api/client'

// vi.mock 工厂里立即用到 mock* 变量，必须用 vi.hoisted 让声明真正提到文件顶部
// （照抄 AccountAdminPage.render.test.ts 的既有解法，理由同上：避免 TDZ）。
const { mockApi, mockUsePoll } = vi.hoisted(() => ({
  mockApi: {
    listDeletedProjects: vi.fn(async () => []),
    restoreProject: vi.fn(async () => ({ restored: 'p1' })),
    purgeProject: vi.fn(),
    purgeAllDeletedProjects: vi.fn(),
  },
  mockUsePoll: vi.fn(() => ({ data: [], error: null, loading: false, refresh: vi.fn() })),
}))

vi.mock('./useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))
vi.mock('../api', () => ({ api: mockApi }))
vi.mock('../App', () => ({ usePoll: mockUsePoll }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import { useRecycleBin } from './useRecycleBin'

/**
 * 用户实测反馈（2026-08-31）：清空回收站点确认后卡住转圈一分钟，且完不成后
 * 没有任何提醒。这里钉住 Studio.tsx 现在依赖的 useRecycleBin：
 *   ②真正完成时用全站 toast 报告成功/失败计数；
 *   ③失败必须如实透出（含后端给的每条错误原文），不能被"清空回收站失败"
 *     这种笼统文案盖过去。
 * "①确认后立刻放行"已经在 useDeleteConfirm.test.ts 钉过 runBackground 本身，
 * 这里补的是 Studio 实际接线（purgingAll 状态、toast 调用）没有悄悄绕开它。
 */

function Harness(props: {
  hookRef: { current: ReturnType<typeof useRecycleBin> | null }
  toast: (msg: string, isErr?: boolean) => void
  refreshProjects: () => void
}) {
  const hook = useRecycleBin(props.toast, props.refreshProjects)
  props.hookRef.current = hook
  return hook.dialog
}

async function mount(toast: (msg: string, isErr?: boolean) => void) {
  const hookRef: { current: ReturnType<typeof useRecycleBin> | null } = { current: null }
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(Harness, { hookRef, toast, refreshProjects: vi.fn() }),
    )
  })
  return { renderer, hookRef }
}

describe('useRecycleBin（清空回收站/彻底删除改成后台执行）', () => {
  beforeEach(() => {
    mockApi.purgeProject.mockReset()
    mockApi.purgeAllDeletedProjects.mockReset()
  })
  afterEach(() => vi.clearAllMocks())

  it('②清空回收站真正完成后用 toast 报告成功计数，purgingAll 回落', async () => {
    const preflight: ApprovalPreflight = { summary: '将清空回收站' }
    let resolveReal!: (v: unknown) => void
    const realPromise = new Promise(resolve => { resolveReal = resolve })
    mockApi.purgeAllDeletedProjects.mockImplementation(async () => {
      throw new ApprovalRequiredError(preflight, () => realPromise)
    })

    const toast = vi.fn()
    const { renderer, hookRef } = await mount(toast)

    await act(async () => {
      hookRef.current!.purgeAll()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(hookRef.current!.purgingAll).toBe(true)
    const confirmProps = hookRef.current!.dialog.props as { pending: ApprovalPreflight | null; onConfirm: () => void }
    expect(confirmProps.pending).toEqual(preflight)

    await act(async () => {
      confirmProps.onConfirm()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    // 确认后立刻放行：后台仍在跑（purgingAll 还是 true），还没 toast。
    expect(hookRef.current!.purgingAll).toBe(true)
    expect(toast).not.toHaveBeenCalled()

    await act(async () => {
      resolveReal({ purged: ['p1', 'p2'], purged_count: 2, failed: [] })
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(toast).toHaveBeenCalledTimes(1)
    const [msg, isErr] = toast.mock.calls[0]
    expect(msg).toContain('2')
    expect(isErr).toBeFalsy()
    expect(hookRef.current!.purgingAll).toBe(false)
    void renderer
  })

  it('③部分失败必须如实透出后端给的错误原文，不能被笼统文案吞掉', async () => {
    const preflight: ApprovalPreflight = { summary: '将清空回收站' }
    let resolveReal!: (v: unknown) => void
    const realPromise = new Promise(resolve => { resolveReal = resolve })
    mockApi.purgeAllDeletedProjects.mockImplementation(async () => {
      throw new ApprovalRequiredError(preflight, () => realPromise)
    })

    const toast = vi.fn()
    const { hookRef } = await mount(toast)
    await act(async () => {
      hookRef.current!.purgeAll()
      await Promise.resolve()
      await Promise.resolve()
    })
    const confirmProps = hookRef.current!.dialog.props as { onConfirm: () => void }
    await act(async () => {
      confirmProps.onConfirm()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    await act(async () => {
      resolveReal({
        purged: ['p1'],
        purged_count: 1,
        failed: [{ project_id: 'p2', error_id: 'err_9', error: '供应商任务尚未到终态' }],
      })
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(toast).toHaveBeenCalledTimes(1)
    const [msg, isErr] = toast.mock.calls[0]
    expect(msg).toContain('1')
    expect(msg).toContain('供应商任务尚未到终态')
    expect(isErr).toBe(true)
  })

  it('单个项目彻底删除失败时也如实报告，不静默吞掉', async () => {
    const preflight: ApprovalPreflight = { summary: '将彻底删除项目' }
    let rejectReal!: (err: unknown) => void
    const realPromise = new Promise((_resolve, reject) => { rejectReal = reject })
    mockApi.purgeProject.mockImplementation(async () => {
      throw new ApprovalRequiredError(preflight, () => realPromise)
    })

    const toast = vi.fn()
    const { hookRef } = await mount(toast)
    const target = { id: 'p9', name: '测试项目' } as Parameters<ReturnType<typeof useRecycleBin>['purge']>[0]
    await act(async () => {
      hookRef.current!.purge(target)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(hookRef.current!.busyId).toBe('p9')
    const confirmProps = hookRef.current!.dialog.props as { onConfirm: () => void }
    await act(async () => {
      confirmProps.onConfirm()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(toast).not.toHaveBeenCalled()

    const failure = new Error('磁盘产物删除失败')
    realPromise.catch(() => {})
    await act(async () => {
      rejectReal(failure)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('磁盘产物删除失败'), true)
    expect(hookRef.current!.busyId).toBeNull()
  })
})
