import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type Project } from '../api'
import ProjectSettingsPanel from './ProjectSettingsPanel'

vi.mock('../api', () => ({ api: { updateProjectSettings: vi.fn(), getAspectRatioImpact: vi.fn() } }))

/** 项目测试环境是 node（未装 jsdom）。画幅确认弹窗复用 DecisionDialog，其
 *  useFocusTrap 在 useEffect 里摸 document.activeElement / document.body.style /
 *  window.addEventListener，需要一个最小宿主存根——同 ScriptPage.render.test.ts
 *  的 installHostStubs 先例。react-test-renderer 不产生真实 DOM，containerRef.current
 *  始终为 null，故 focusables() 提前返回空数组，不需要更完整的 querySelectorAll 实现。 */
function installHostStubs() {
  const passthrough = { addEventListener: () => {}, removeEventListener: () => {} }
  ;(globalThis as { window?: unknown }).window = { ...passthrough }
  ;(globalThis as { document?: unknown }).document = { ...passthrough, activeElement: null, body: { style: {} } }
}
function uninstallHostStubs() {
  delete (globalThis as { window?: unknown }).window
  delete (globalThis as { document?: unknown }).document
}
beforeEach(installHostStubs)
afterEach(() => { vi.resetAllMocks(); uninstallHostStubs() })

const BASE_PROJECT: Project = {
  id: 'proj-1', name: '测试项目', status: 'planned', novel_chars: 1000,
  bible_status: 'ready', plan_status: 'ready',
  adaptation_mode: 'short_drama', aspect_ratio: '9:16', ai_label_enabled: 0,
}

async function mount(project: Project = BASE_PROJECT) {
  const toast = vi.fn()
  const onSaved = vi.fn()
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => {
    view = TestRenderer.create(React.createElement(ProjectSettingsPanel, { project, toast, onSaved }))
  })
  return { view, toast, onSaved }
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === 'string' ? c : textOf(c))).join('')
}

describe('ProjectSettingsPanel：默认值与提交字段', () => {
  it('按 project 上的原始字段渲染三项当前值（ai_label_enabled 用 !! 从 0/1 转布尔）', async () => {
    const { view } = await mount()
    const selects = view.root.findAllByType('select')
    expect(selects[0].props.value).toBe('short_drama')
    expect(selects[1].props.value).toBe('9:16')
    const checkbox = view.root.findAllByType('input').find(n => n.props.type === 'checkbox')
    expect(checkbox!.props.checked).toBe(false)
    view.unmount()
  })

  it('字段缺失时按忠实原著/9:16/关闭兜底，不假装读到了值', async () => {
    const { view } = await mount({ ...BASE_PROJECT, adaptation_mode: undefined, aspect_ratio: undefined, ai_label_enabled: undefined })
    const selects = view.root.findAllByType('select')
    expect(selects[0].props.value).toBe('faithful')
    expect(selects[1].props.value).toBe('9:16')
    view.unmount()
  })

  it('切换改编强度直接提交 PUT，不弹确认框', async () => {
    vi.mocked(api.updateProjectSettings).mockResolvedValue({ project_id: 'proj-1', adaptation_mode: 'faithful', aspect_ratio: '9:16', ai_label_enabled: false })
    const { view, onSaved } = await mount()
    await act(async () => {
      view.root.findAllByType('select')[0].props.onChange({ target: { value: 'faithful' } })
      await Promise.resolve()
    })
    expect(api.updateProjectSettings).toHaveBeenCalledWith('proj-1', { adaptation_mode: 'faithful' })
    expect(onSaved).toHaveBeenCalledOnce()
    view.unmount()
  })

  it('开启 AI 标识直接提交 PUT', async () => {
    vi.mocked(api.updateProjectSettings).mockResolvedValue({ project_id: 'proj-1', adaptation_mode: 'short_drama', aspect_ratio: '9:16', ai_label_enabled: true })
    const { view, onSaved } = await mount()
    const checkbox = view.root.findAllByType('input').find(n => n.props.type === 'checkbox')
    await act(async () => { checkbox!.props.onChange({ target: { checked: true } }); await Promise.resolve() })
    expect(api.updateProjectSettings).toHaveBeenCalledWith('proj-1', { ai_label_enabled: true })
    expect(onSaved).toHaveBeenCalledOnce()
    view.unmount()
  })
})

describe('ProjectSettingsPanel：保存失败展示后端 detail', () => {
  it('409 报错原样展示在面板里，也 toast 出来', async () => {
    vi.mocked(api.updateProjectSettings).mockRejectedValue(new Error('改编强度不合法：xyz'))
    const { view, toast, onSaved } = await mount()
    await act(async () => {
      view.root.findAllByType('select')[0].props.onChange({ target: { value: 'faithful' } })
      await Promise.resolve()
    })
    const alert = view.root.findAll(n => n.props.role === 'alert')
    expect(alert.some(n => textOf(n).includes('改编强度不合法：xyz'))).toBe(true)
    expect(toast).toHaveBeenCalledWith('改编强度不合法：xyz', true)
    expect(onSaved).not.toHaveBeenCalled()
    view.unmount()
  })
})

describe('ProjectSettingsPanel：切换画幅先确认', () => {
  it('有统计数据时如实展示确认文案，确认后才真正提交', async () => {
    vi.mocked(api.getAspectRatioImpact).mockResolvedValue({
      project_id: 'proj-1', current_aspect_ratio: '9:16', target_aspect_ratio: '16:9',
      adopted_videos_total: 10, adopted_videos_mismatched: 4,
      scene_images_total: 6, scene_images_mismatched: 2,
    })
    vi.mocked(api.updateProjectSettings).mockResolvedValue({ project_id: 'proj-1', adaptation_mode: 'short_drama', aspect_ratio: '16:9', ai_label_enabled: false })
    const { view, onSaved } = await mount()
    await act(async () => {
      view.root.findAllByType('select')[1].props.onChange({ target: { value: '16:9' } })
      await Promise.resolve()
    })
    expect(api.getAspectRatioImpact).toHaveBeenCalledWith('proj-1', '16:9')
    const dialogText = textOf(view.root)
    expect(dialogText).toContain('4 个视频仍是旧画幅')
    expect(dialogText).toContain('场景图 2 张为旧画幅（共 6 张）')
    expect(api.updateProjectSettings).not.toHaveBeenCalled()
    const confirmBtn = view.root.findAllByType('button').find(b => textOf(b) === '确认切换画幅')
    await act(async () => { confirmBtn!.props.onClick(); await Promise.resolve() })
    expect(api.updateProjectSettings).toHaveBeenCalledWith('proj-1', { aspect_ratio: '16:9' })
    expect(onSaved).toHaveBeenCalledOnce()
    view.unmount()
  })

  it('scene_images_mismatched 为 null 时如实写「画幅未记录」', async () => {
    vi.mocked(api.getAspectRatioImpact).mockResolvedValue({
      project_id: 'proj-1', current_aspect_ratio: '9:16', target_aspect_ratio: '16:9',
      adopted_videos_total: 1, adopted_videos_mismatched: 0,
      scene_images_total: 5, scene_images_mismatched: null,
    })
    const { view } = await mount()
    await act(async () => {
      view.root.findAllByType('select')[1].props.onChange({ target: { value: '16:9' } })
      await Promise.resolve()
    })
    expect(textOf(view.root)).toContain('场景图 5 张，画幅未记录')
    view.unmount()
  })

  it('impact 接口失败时仍展示确认框并允许切换，如实提示统计不到', async () => {
    vi.mocked(api.getAspectRatioImpact).mockRejectedValue(new Error('HTTP 404'))
    vi.mocked(api.updateProjectSettings).mockResolvedValue({ project_id: 'proj-1', adaptation_mode: 'short_drama', aspect_ratio: '16:9', ai_label_enabled: false })
    const { view } = await mount()
    await act(async () => {
      view.root.findAllByType('select')[1].props.onChange({ target: { value: '16:9' } })
      await Promise.resolve()
    })
    expect(textOf(view.root)).toContain('暂时无法统计受影响的视频数')
    const confirmBtn = view.root.findAllByType('button').find(b => textOf(b) === '确认切换画幅')
    expect(confirmBtn).toBeDefined()
    await act(async () => { confirmBtn!.props.onClick(); await Promise.resolve() })
    expect(api.updateProjectSettings).toHaveBeenCalledWith('proj-1', { aspect_ratio: '16:9' })
    view.unmount()
  })

  it('取消确认框不会提交切换', async () => {
    vi.mocked(api.getAspectRatioImpact).mockResolvedValue({
      project_id: 'proj-1', current_aspect_ratio: '9:16', target_aspect_ratio: '16:9',
      adopted_videos_total: 0, adopted_videos_mismatched: 0, scene_images_total: 0, scene_images_mismatched: 0,
    })
    const { view } = await mount()
    await act(async () => {
      view.root.findAllByType('select')[1].props.onChange({ target: { value: '16:9' } })
      await Promise.resolve()
    })
    const cancelBtn = view.root.findAllByType('button').find(b => textOf(b) === '取消（保持当前画幅）')
    await act(async () => { cancelBtn!.props.onClick(); await Promise.resolve() })
    expect(api.updateProjectSettings).not.toHaveBeenCalled()
    expect(view.root.findAllByType('button').some(b => textOf(b) === '确认切换画幅')).toBe(false)
    view.unmount()
  })
})
