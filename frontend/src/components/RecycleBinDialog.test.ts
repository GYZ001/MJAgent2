import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RecycleBinDialog, type RecycleBinDialogProps } from './RecycleBinDialog'
import type { DeletedProject } from '../api'

// useRecycleBin 用 usePoll 轮询回收站列表（见 hooks/useRecycleBin.ts），轮询失败
// 不清空 deletedProjects——已有列表时再失败，deletedError 必须单独给出信号，不能
// 被 QueryState 的 hasData 分支吞掉（同 orgs/resources 各面板，2c96b89c）。
// RecycleBinDialog 是纯展示组件（props 直接传入，不自己调用 hooks/api），可以
// 直接挂载真实渲染，不需要 mock '../App' 或 '../api'；但组件内部有个 Esc 关闭的
// useEffect 会摸 window.addEventListener，测试环境是 node（无 window），补一个
// 最小存根（同 adminLoadingStates.test.ts 的处理方式）。
beforeEach(() => { vi.stubGlobal('window', { addEventListener: () => {}, removeEventListener: () => {} }) })
afterEach(() => { vi.unstubAllGlobals() })

const project = (overrides: Partial<DeletedProject> = {}): DeletedProject => ({
  id: 'p1', name: '测试项目', chapter_count: 3, episode_count: 5,
  retention_seconds_remaining: 3600, ...overrides,
} as DeletedProject)

function baseProps(overrides: Partial<RecycleBinDialogProps> = {}): RecycleBinDialogProps {
  return {
    deletedProjects: [project()], deletedCount: 1, deletedLoading: false, deletedError: null,
    busyId: null, purgingAll: false,
    onRestore: vi.fn(), onPurge: vi.fn(), onPurgeAll: vi.fn(), onClose: vi.fn(), onRefresh: vi.fn(),
    ...overrides,
  }
}

function mount(props: RecycleBinDialogProps) {
  let renderer!: TestRenderer.ReactTestRenderer
  act(() => { renderer = TestRenderer.create(React.createElement(RecycleBinDialog, props)) })
  return renderer
}

describe('RecycleBinDialog——已有列表后台轮询刷新失败不得被吞', () => {
  it('已有数据时 deletedError 非空：显示「刷新失败」横幅，旧列表仍在，不显示 QueryState 的空态/首屏失败态', () => {
    const onRefresh = vi.fn()
    const renderer = mount(baseProps({ deletedError: '网络连接异常', onRefresh }))
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('回收站项目刷新失败')
    expect(serialized).toContain('网络连接异常')
    expect(serialized).toContain('测试项目') // 反向断言：旧数据没有被清空/被替换成失败态
    expect(serialized).not.toContain('暂无回收站项目')
    const retryBtn = renderer.root.findAll(n => n.type === 'button' && n.props.children === '重试刷新')[0]
    act(() => { retryBtn.props.onClick() })
    expect(onRefresh).toHaveBeenCalledTimes(1)
    act(() => { renderer.unmount() })
  })

  it('deletedError 清空后横幅消失，列表照常展示', () => {
    const renderer = mount(baseProps({ deletedError: null }))
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).not.toContain('刷新失败')
    expect(serialized).toContain('测试项目')
    act(() => { renderer.unmount() })
  })

  it('首屏就没有数据（deletedProjects 为 null）且报错：走 QueryState 首屏失败态，不重复展示横幅', () => {
    const renderer = mount(baseProps({
      deletedProjects: null, deletedCount: 0, deletedError: '加载失败测试',
    }))
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('回收站项目加载失败')
    // 首屏失败态由 QueryState 自己的横幅呈现，StaleRefreshBanner 不应该重复渲染一份。
    expect(serialized).not.toContain('回收站项目刷新失败')
    act(() => { renderer.unmount() })
  })
})
