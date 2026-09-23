import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'
import StaleRefreshBanner from './StaleRefreshBanner'

// 详情页（人物谱/物件库/场景库/生成台/分镜台/成片台/映射台/连播台等）复用的
// 「已有数据后台刷新失败」横幅：见组件顶部注释。这里只单测组件本身的三条
// 行为——各页面接线的红/绿验证见各自 test 文件。
describe('StaleRefreshBanner', () => {
  it('error 为空时不渲染任何内容', () => {
    let renderer!: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(React.createElement(StaleRefreshBanner, {
        error: null, onRetry: vi.fn(), objectName: '人物谱',
      }))
    })
    expect(renderer.toJSON()).toBeNull()
    act(() => { renderer.unmount() })
  })

  it('error 非空时渲染「{objectName}刷新失败」标题、原始错误文案与重试按钮', () => {
    let renderer!: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(React.createElement(StaleRefreshBanner, {
        error: '网络连接异常', onRetry: vi.fn(), objectName: '人物谱',
      }))
    })
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('人物谱刷新失败')
    expect(serialized).toContain('网络连接异常')
    expect(serialized).toContain('当前仍展示上次成功加载的内容')
    const retryBtn = renderer.root.findAll(n => n.type === 'button')[0]
    expect(retryBtn.props.children).toBe('重试刷新')
    act(() => { renderer.unmount() })
  })

  it('点击重试按钮调用 onRetry', () => {
    const onRetry = vi.fn()
    let renderer!: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(React.createElement(StaleRefreshBanner, {
        error: '服务器错误', onRetry, objectName: '场景库',
      }))
    })
    const retryBtn = renderer.root.findAll(n => n.type === 'button')[0]
    act(() => { retryBtn.props.onClick() })
    expect(onRetry).toHaveBeenCalledTimes(1)
    act(() => { renderer.unmount() })
  })
})
