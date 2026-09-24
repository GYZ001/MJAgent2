// VoiceChip 四态：当前声音（播放按钮）/未配置/生成中/生成失败+原因；以及
// readOnly（映射台人物谱）非当前声音三态点击跳回人物谱。

const { mockGetProjectVoices } = vi.hoisted(() => ({ mockGetProjectVoices: vi.fn() }))
vi.mock('../../api/voices', () => ({ getProjectVoices: mockGetProjectVoices }))

import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ProjectVoices } from '../../api/voices'
// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import VoiceChip from './VoiceChip'

let seq = 0
function nextProject(): string { seq += 1; return `proj-chip-${seq}` }

async function mount(projectId: string, characterName: string, readOnly = false) {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(VoiceChip, { projectId, characterName, readOnly }))
  })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  return renderer
}

function treeText(renderer: TestRenderer.ReactTestRenderer): string {
  return JSON.stringify(renderer.toJSON())
}

describe('VoiceChip', () => {
  afterEach(() => { mockGetProjectVoices.mockReset() })

  it('有当前声音：渲染可播放按钮，不显示"未配置"文案', async () => {
    const projectId = nextProject()
    mockGetProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [{
        character_name: '张三', anchor_key: '', generating: false, candidates: [],
        current: {
          id: 'v1', status: 'current', source: 'design', voice_prompt: 'p', preview_text: 't',
          audio_url: 'https://x/a.mp3', clip_url: 'https://x/a-clip.mp3', clip_duration_s: 4.2,
          check_status: 'passed', check_reason: null, asr_text: null, error: null,
          created_at: 1, adopted_at: 1,
        },
      }],
    } satisfies ProjectVoices)
    const renderer = await mount(projectId, '张三')
    const text = treeText(renderer)
    expect(text).toContain('播放张三的声音')
    expect(text).not.toContain('未配置声音')
    renderer.unmount()
  })

  it('未配置：没有 current、没有候选、也没在生成中，显示"未配置声音"', async () => {
    const projectId = nextProject()
    mockGetProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [{ character_name: '李四', anchor_key: '', generating: false, candidates: [], current: null }],
    } satisfies ProjectVoices)
    const renderer = await mount(projectId, '李四')
    expect(treeText(renderer)).toContain('未配置声音')
    renderer.unmount()
  })

  it('生成中：item.generating 为 true 时显示"声音生成中…"', async () => {
    const projectId = nextProject()
    mockGetProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [{ character_name: '王五', anchor_key: '', generating: true, candidates: [], current: null }],
    } satisfies ProjectVoices)
    const renderer = await mount(projectId, '王五')
    expect(treeText(renderer)).toContain('声音生成中')
    renderer.unmount()
  })

  it('生成失败：最近一条候选是 failed 时显示"声音生成失败"，原因进 title', async () => {
    const projectId = nextProject()
    mockGetProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [{
        character_name: '赵六', anchor_key: '', generating: false, current: null,
        candidates: [{
          id: 'v2', status: 'failed', source: 'design', voice_prompt: 'p', preview_text: 't',
          audio_url: '', clip_url: '', clip_duration_s: null, check_status: 'unchecked',
          check_reason: null, asr_text: null, error: '供应商拒绝：内容涉嫌违规', created_at: 5, adopted_at: null,
        }],
      }],
    } satisfies ProjectVoices)
    const renderer = await mount(projectId, '赵六')
    const text = treeText(renderer)
    expect(text).toContain('声音生成失败')
    expect(text).toContain('供应商拒绝：内容涉嫌违规')
    renderer.unmount()
  })

  it('readOnly 且非当前声音状态：渲染指向人物谱的可点击链接，不把用户晾在原地', async () => {
    const projectId = 'proj_9'
    mockGetProjectVoices.mockResolvedValue({
      voice_model_configured: true, auto_generate: true,
      items: [{ character_name: '孙七', anchor_key: '', generating: false, candidates: [], current: null }],
    } satisfies ProjectVoices)
    const renderer = await mount(projectId, '孙七', true)
    const links = renderer.root.findAllByType('a')
    expect(links).toHaveLength(1)
    expect(links[0].props.href).toBe('/projects/proj_9/bible')
    renderer.unmount()
  })

  it('characterName 为空时不渲染任何内容（projectId 的共享请求仍正常发出，不影响同页其它面板）', async () => {
    const projectId = nextProject()
    mockGetProjectVoices.mockResolvedValue({ voice_model_configured: true, auto_generate: true, items: [] } satisfies ProjectVoices)
    const renderer = await mount(projectId, '')
    expect(renderer.toJSON()).toBeNull()
    renderer.unmount()
  })
})
