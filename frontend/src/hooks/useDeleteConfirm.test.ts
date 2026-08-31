import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'
import { ApprovalRequiredError } from '../api/client'
import type { ApprovalPreflight } from '../api/client'

// 弹窗焦点圈定要摸 document.body，vitest 跑在 environment:'node' 下没有 DOM
// （照抄 modalLayering.render.test.ts / AccountAdminPage.render.test.ts 的既有解法）。
vi.mock('./useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import DeleteConfirmDialog from '../components/DeleteConfirmDialog'
// eslint-disable-next-line import/first
import { useDeleteConfirm } from './useDeleteConfirm'

/**
 * 用户实测反馈（2026-08-31）：点"清空回收站"确认后界面卡住转圈一分钟不放。
 * 根因是后端批准后的真实清理请求是一次同步跑到底的 HTTP 请求（没有队列/
 * 后台任务），旧的 useDeleteConfirm.run() 语义会把调用方一路 await 到那时候。
 * runBackground() 改成"确认即放行"：这里直接钉住这个新方法的关键行为，不重复
 * 测已有的 run()（那条路径行为未变，各页面既有测试已经覆盖它）。
 *
 * 断言尽量直接 await 真实 Promise 而不是数固定的 microtask 轮数——后者在
 * Promise 链条变化时会变脆；只在需要观察 React 重渲染副作用时才用 act() 包一层
 * `await Promise.resolve()`。
 */

function Harness({ hookRef }: { hookRef: { current: ReturnType<typeof useDeleteConfirm> | null } }) {
  const hook = useDeleteConfirm()
  hookRef.current = hook
  return hook.dialog
}

async function mount(): Promise<{
  renderer: TestRenderer.ReactTestRenderer
  hookRef: { current: ReturnType<typeof useDeleteConfirm> | null }
}> {
  const hookRef: { current: ReturnType<typeof useDeleteConfirm> | null } = { current: null }
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(Harness, { hookRef }))
  })
  return { renderer, hookRef }
}

describe('useDeleteConfirm.runBackground（清空回收站不再卡住转圈的根因修复）', () => {
  it('①用户点"确认删除"后立刻放行调用方，不等真正的删除请求跑完', async () => {
    const { renderer, hookRef } = await mount()
    const preflight: ApprovalPreflight = { summary: '将清空回收站' }

    let resolveRealDelete!: (v: { purged_count: number }) => void
    const realDeletePromise = new Promise<{ purged_count: number }>(resolve => {
      resolveRealDelete = resolve
    })
    const action = vi.fn(async () => {
      throw new ApprovalRequiredError(preflight, () => realDeletePromise)
    })
    const onSettled = vi.fn()

    let outerPromise!: Promise<'submitted' | undefined>
    await act(async () => {
      outerPromise = hookRef.current!.runBackground(action, onSettled)
      await Promise.resolve()
      await Promise.resolve()
    })

    // 批准弹窗出现，但用户还没点确认——调用方理应还在等，这不是本次要修的卡顿。
    const dialogEl = renderer.root.findByType(DeleteConfirmDialog)
    expect(dialogEl.props.pending).toEqual(preflight)

    // 用户点"确认删除"。
    await act(async () => {
      dialogEl.props.onConfirm()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    // 关键断言：确认后立刻放行——即使真正的删除请求（realDeletePromise）此刻仍未 resolve。
    await expect(outerPromise).resolves.toBe('submitted')
    expect(onSettled).not.toHaveBeenCalled()
    // 弹窗必须已经关闭，不是停在"删除中…"的模态里出不来。
    expect(renderer.root.findByType(DeleteConfirmDialog).props.pending).toBeNull()

    // 现在后端才真正跑完，真实结果异步交付给 onSettled。
    resolveRealDelete({ purged_count: 3 })
    await Promise.resolve()
    await Promise.resolve()
    expect(onSettled).toHaveBeenCalledWith({ ok: true, value: { purged_count: 3 } })
  })

  it('③真正的删除请求失败时如实通过 onSettled 交付，不吞异常', async () => {
    const { renderer, hookRef } = await mount()
    const preflight: ApprovalPreflight = { summary: '将彻底删除项目' }
    let rejectRealDelete!: (err: unknown) => void
    const realDeletePromise = new Promise<never>((_resolve, reject) => {
      rejectRealDelete = reject
    })
    const action = vi.fn(async () => {
      throw new ApprovalRequiredError(preflight, () => realDeletePromise)
    })
    const onSettled = vi.fn()

    await act(async () => {
      void hookRef.current!.runBackground(action, onSettled)
      await Promise.resolve()
      await Promise.resolve()
    })
    const dialogEl = renderer.root.findByType(DeleteConfirmDialog)
    await act(async () => {
      dialogEl.props.onConfirm()
      await Promise.resolve()
      await Promise.resolve()
    })

    const failure = new Error('供应商任务未到终态，无法彻底删除')
    // .catch 是必须的：这条 realDeletePromise 本身也被测试断言之外的默认拒绝
    // 处理器观察到，Node 会对"无人处理的 rejection"报警，这里显式接住即可。
    realDeletePromise.catch(() => {})
    rejectRealDelete(failure)
    await Promise.resolve()
    await Promise.resolve()
    expect(onSettled).toHaveBeenCalledWith({ ok: false, error: failure })
  })

  it('用户取消批准弹窗：不调用 onSettled，也不视为已提交', async () => {
    const { renderer, hookRef } = await mount()
    const preflight: ApprovalPreflight = { summary: '将彻底删除项目' }
    const action = vi.fn(async () => {
      throw new ApprovalRequiredError(preflight, () => Promise.resolve({ ok: true }))
    })
    const onSettled = vi.fn()

    let outerPromise!: Promise<'submitted' | undefined>
    await act(async () => {
      outerPromise = hookRef.current!.runBackground(action, onSettled)
      await Promise.resolve()
      await Promise.resolve()
    })
    const dialogEl = renderer.root.findByType(DeleteConfirmDialog)
    await act(async () => {
      dialogEl.props.onCancel()
    })

    await expect(outerPromise).resolves.toBeUndefined()
    expect(onSettled).not.toHaveBeenCalled()
    expect(renderer.root.findByType(DeleteConfirmDialog).props.pending).toBeNull()
  })
})
