import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { CharacterLooksStatus } from '../api/storyboard/characterLooks'
import CharacterLooksPanel from './CharacterLooksPanel'

vi.mock('../api', () => ({
  api: { getCharacterLooks: vi.fn(), startCharacterLooks: vi.fn() },
  ApiError: class ApiError extends Error {},
}))
afterEach(() => vi.resetAllMocks())

async function mount(episodeId = 'ep-1') {
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => {
    view = TestRenderer.create(React.createElement(CharacterLooksPanel, { episodeId }))
    await Promise.resolve()
  })
  return view
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === 'string' ? c : textOf(c))).join('')
}

const EMPTY: CharacterLooksStatus = { items: [], summary: { ready: 0, generating: 0, failed: 0, missing: 0 } }

describe('CharacterLooksPanel：无需求时不渲染', () => {
  it('全部计数为零时返回 null', async () => {
    vi.mocked(api.getCharacterLooks).mockResolvedValue(EMPTY)
    const view = await mount()
    expect(view.root.findAllByType('div')).toHaveLength(0)
    view.unmount()
  })
})

describe('CharacterLooksPanel：有缺口', () => {
  it('展示就绪/生成中/失败/待生成计数与补齐按钮', async () => {
    vi.mocked(api.getCharacterLooks).mockResolvedValue({
      items: [], summary: { ready: 2, generating: 0, failed: 1, missing: 3 },
    })
    const view = await mount()
    const text = textOf(view.root)
    expect(text).toContain('就绪')
    expect(text).toContain('待生成')
    const button = view.root.findByType('button')
    expect(textOf(button)).toBe('补齐造型照')
    view.unmount()
  })

  it('点击补齐按钮会调用 startCharacterLooks 并带上 episodeId', async () => {
    vi.mocked(api.getCharacterLooks).mockResolvedValue({
      items: [], summary: { ready: 0, generating: 0, failed: 0, missing: 1 },
    })
    vi.mocked(api.startCharacterLooks).mockResolvedValue({ status: 'accepted' })
    const view = await mount('ep-9')
    const button = view.root.findByType('button')
    await act(async () => {
      button.props.onClick()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(api.startCharacterLooks).toHaveBeenCalledWith('ep-9')
    view.unmount()
  })

  it('全部就绪时不展示补齐按钮', async () => {
    vi.mocked(api.getCharacterLooks).mockResolvedValue({
      items: [], summary: { ready: 4, generating: 0, failed: 0, missing: 0 },
    })
    const view = await mount()
    expect(view.root.findAllByType('button')).toHaveLength(0)
    view.unmount()
  })
})
