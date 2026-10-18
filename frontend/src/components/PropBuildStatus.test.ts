import { act, create } from 'react-test-renderer'
import { createElement } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { retryPropBuild, type PropBuildRecord } from '../api/bible/props'
import PropBuildStatus from './PropBuildStatus'

vi.mock('../api/bible/props', () => ({ retryPropBuild: vi.fn() }))

const failed: PropBuildRecord = {
  label: '铜镜', status: 'failed', attempts: 3,
  error: '模型鉴权失败（HTTP 401），请在模型中心检查密钥',
  episodes: [{ id: 'ep4', episode_no: 4 }],
}

describe('道具补建失败恢复', () => {
  it('没有卡片也显示失败原因，点击重试传递项目、分集和道具', async () => {
    vi.mocked(retryPropBuild).mockResolvedValue({ accepted: true })
    const refresh = vi.fn().mockResolvedValue(undefined)
    const toast = vi.fn()
    const tree = create(createElement(PropBuildStatus, { projectId: 'p1', builds: [failed], onRefresh: refresh, toast }))
    expect(JSON.stringify(tree.toJSON())).toContain('HTTP 401')
    expect(JSON.stringify(tree.toJSON())).toContain('补建失败')
    const retry = tree.root.findAllByType('button').find(button => button.children.includes('重试补建'))!
    await act(async () => { await retry.props.onClick() })
    expect(retryPropBuild).toHaveBeenCalledWith('p1', 'ep4', '铜镜')
    expect(refresh).toHaveBeenCalledOnce()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('已受理'))
    tree.unmount()
  })

  it('运行中不提供重复重试入口，任务清空后面板消失', () => {
    const tree = create(createElement(PropBuildStatus, {
      projectId: 'p1', builds: [{ ...failed, status: 'running', error: null }], onRefresh: vi.fn(), toast: vi.fn(),
    }))
    expect(JSON.stringify(tree.toJSON())).toContain('补建中')
    expect(tree.root.findAllByType('button')).toHaveLength(1)
    tree.update(createElement(PropBuildStatus, { projectId: 'p1', builds: [], onRefresh: vi.fn(), toast: vi.fn() }))
    expect(tree.toJSON()).toBeNull()
    tree.unmount()
  })
})
