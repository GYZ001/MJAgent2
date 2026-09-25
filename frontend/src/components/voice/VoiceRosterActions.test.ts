// VoiceRosterActions：未配置声音生成模型时显示横幅 + 去模型中心的出路；已配置
// 且存在缺口时显示批量生成按钮，确认框写明数量与付费措辞；缺口清空时不渲染。

const { mockApi } = vi.hoisted(() => ({
  mockApi: { getProjectVoices: vi.fn(), generateMissingVoices: vi.fn() },
}))
vi.mock('../../api/voices', () => mockApi)

import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectVoices } from '../../api/voices'
// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import VoiceRosterActions from './VoiceRosterActions'

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

const emptyVoiceItem = (name: string, generating = false) => (
  { character_name: name, anchor_key: '', current: null, candidates: [], generating }
)

async function mount(projectId = 'proj-roster') {
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => { view = TestRenderer.create(React.createElement(VoiceRosterActions, { projectId })) })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  return view
}

function treeText(view: TestRenderer.ReactTestRenderer): string {
  return JSON.stringify(view.toJSON())
}

describe('VoiceRosterActions', () => {
  it('未配置声音生成模型：显示横幅并给出去模型中心的入口，不显示批量按钮', async () => {
    mockApi.getProjectVoices.mockResolvedValue({
      voice_model_configured: false, auto_generate: true, items: [emptyVoiceItem('张三')],
    } satisfies ProjectVoices)
    const view = await mount()
    expect(treeText(view)).toContain('还没有配置声音生成模型')
    const links = view.root.findAllByType('a')
    expect(links).toHaveLength(1)
    expect(links[0].props.href).toBe('/system/models')
    expect(view.root.findAllByType('button')).toHaveLength(0)
    view.unmount()
  })

  it('已配置且有缺口：按钮显示缺口数量，确认框写明数量与付费措辞，确认后才调用接口', async () => {
    mockApi.getProjectVoices.mockResolvedValueOnce({
      voice_model_configured: true, auto_generate: true,
      items: [emptyVoiceItem('张三'), emptyVoiceItem('李四'), emptyVoiceItem('王五', true)],
    } satisfies ProjectVoices)
    mockApi.generateMissingVoices.mockResolvedValue({ accepted: 2, characters: ['张三', '李四'] })
    mockApi.getProjectVoices.mockResolvedValueOnce({
      voice_model_configured: true, auto_generate: true,
      items: [emptyVoiceItem('张三', true), emptyVoiceItem('李四', true), emptyVoiceItem('王五', true)],
    } satisfies ProjectVoices)
    const view = await mount()

    const button = view.root.findAllByType('button').find(b => b.children.join('').includes('为未配置声音的角色生成'))
    expect(button?.children.join('')).toBe('为未配置声音的角色生成（2 个）') // 王五在生成中，不计入缺口
    await act(async () => { button!.props.onClick(); await Promise.resolve() })
    const text = treeText(view)
    expect(text).toContain('将调用 2 次付费的声音生成接口')
    expect(mockApi.generateMissingVoices).not.toHaveBeenCalled()

    const confirmBtn = view.root.findAllByType('button').find(b => b.children.join('') === '确认生成')
    await act(async () => { confirmBtn!.props.onClick(); await Promise.resolve(); await Promise.resolve() })
    expect(mockApi.generateMissingVoices).toHaveBeenCalledWith('proj-roster')
    view.unmount()
  })

  it('受理成功后提示可在观测查看进度，并给出跳到该运行的链接', async () => {
    // 用独立 projectId：useProjectVoices 的缓存按 projectId 分槽且模块级持久，
    // 与相邻用例共用默认 id 会让本用例点击后触发的 refresh() 竞态污染下一个
    // 用例读到的缓存数据（曾实测导致"没有缺口时不渲染任何内容"误报有缺口）。
    mockApi.getProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [emptyVoiceItem('张三'), emptyVoiceItem('李四')],
    } satisfies ProjectVoices)
    mockApi.generateMissingVoices.mockResolvedValue({
      accepted: 2, characters: ['张三', '李四'], run_id: 'run_voice_abc123',
    })
    const view = await mount('proj-roster-accepted')

    const button = view.root.findAllByType('button')[0]
    await act(async () => { button.props.onClick(); await Promise.resolve() })
    const confirmBtn = view.root.findAllByType('button').find(b => b.children.join('') === '确认生成')
    await act(async () => {
      confirmBtn!.props.onClick()
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    })

    expect(treeText(view)).toContain('已开始为 2 个角色生成声音')
    expect(treeText(view)).toContain('可在')
    expect(treeText(view)).toContain('查看进度')
    const runLink = view.root.findAllByType('a').find(a => a.children.join('') === '观测')
    expect(runLink?.props.href).toBe(
      '/projects/proj-roster-accepted/observability/runs?run_id=run_voice_abc123',
    )
    view.unmount()
  })

  it('没有缺口时不渲染任何内容', async () => {
    mockApi.getProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [emptyVoiceItem('张三', true)],
    } satisfies ProjectVoices)
    const view = await mount()
    expect(view.toJSON()).toBeNull()
    view.unmount()
  })

  it('批量生成失败时就地显示中文错误', async () => {
    mockApi.getProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true, items: [emptyVoiceItem('张三')],
    } satisfies ProjectVoices)
    mockApi.generateMissingVoices.mockRejectedValue(new Error('供应商暂不可用，请稍后重试'))
    const view = await mount()
    const button = view.root.findAllByType('button')[0]
    await act(async () => { button.props.onClick(); await Promise.resolve() })
    const confirmBtn = view.root.findAllByType('button').find(b => b.children.join('') === '确认生成')
    await act(async () => { confirmBtn!.props.onClick(); await Promise.resolve(); await Promise.resolve() })
    expect(treeText(view)).toContain('供应商暂不可用，请稍后重试')
    view.unmount()
  })
})
