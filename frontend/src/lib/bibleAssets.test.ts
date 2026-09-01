import { describe, expect, it } from 'vitest'

import { AdaptivePoller } from '../adaptivePoller'
import {
  refsBusyPollInterval,
  resolvePortraitPlaceholderKind,
  resolveSceneRefPlaceholderKind,
  portraitPlaceholderText,
  sceneRefPlaceholderText,
} from './bibleAssets'

/**
 * 定妆照/场景图占位四态（用户拍板，2026-08-31）：有卡角色/具卡场景当前无图时，
 * 不许一律显示"无定妆照/无图"——那是撒谎。判据：具名角色 identity_id 恒为
 * bible:{name}（群演/一次性人物没有这个前缀，永远是 extra 态，见
 * app/production/prep_pack/resolve_assets.py）；具名角色/具卡场景当前无图时，
 * 按项目的 refs_status/refs_target（人物）或 scene_refs_status/scene_refs_target
 * （场景）区分"生成中/生成失败/待生成"。
 */
describe('resolvePortraitPlaceholderKind（定妆照占位四态）', () => {
  it('群演/一次性人物（identity_id 没有 bible: 前缀）恒为 extra，不套用下面三态', () => {
    expect(resolvePortraitPlaceholderKind('entity:fdd28fea634a6cdc', { refs_status: 'running' }))
      .toBe('extra')
    expect(portraitPlaceholderText('extra')).toBe('无定妆照')
  })

  it('具名角色 + 当前无图 + 本轮任务正在为它出图 -> generating（不许撒谎成静止态）', () => {
    expect(resolvePortraitPlaceholderKind('bible:张三', { refs_status: 'running', refs_target: null }))
      .toBe('generating')
    expect(resolvePortraitPlaceholderKind('bible:张三', {
      refs_status: 'running', refs_target: JSON.stringify(['张三', '李四']),
    })).toBe('generating')
    expect(portraitPlaceholderText('generating')).toBe('定妆照生成中')
  })

  it('具名角色 + 当前无图 + 出图任务失败 -> failed（如实说失败，界面必须给出路）', () => {
    expect(resolvePortraitPlaceholderKind('bible:张三', { refs_status: 'failed', refs_target: '张三' }))
      .toBe('failed')
    expect(portraitPlaceholderText('failed')).toBe('定妆照生成失败')
  })

  it('具名角色 + 当前无图 + 任务既没跑也没失败 -> pending（不许显示"生成中"）', () => {
    expect(resolvePortraitPlaceholderKind('bible:张三', { refs_status: 'idle' })).toBe('pending')
    expect(resolvePortraitPlaceholderKind('bible:张三', { refs_status: 'ready' })).toBe('pending')
    expect(resolvePortraitPlaceholderKind('bible:张三', null)).toBe('pending')
    expect(portraitPlaceholderText('pending')).toBe('定妆照待生成')
  })

  it('本轮任务在跑，但 refs_target 范围不含这个角色 -> 仍是 pending，不冒充"正在为它出图"', () => {
    expect(resolvePortraitPlaceholderKind('bible:张三', {
      refs_status: 'running', refs_target: JSON.stringify(['李四']),
    })).toBe('pending')
  })
})

describe('resolveSceneRefPlaceholderKind（场景图占位三态，没有群演等价的第四态）', () => {
  it('具卡场景 + 当前无图 + 本轮任务正在为它出图 -> generating', () => {
    expect(resolveSceneRefPlaceholderKind('scene:老宅', '老宅', {
      scene_refs_status: 'running', scene_refs_target: null,
    })).toBe('generating')
    expect(sceneRefPlaceholderText('generating')).toBe('场景图生成中')
  })

  it('具卡场景 + 当前无图 + 出图任务失败 -> failed', () => {
    expect(resolveSceneRefPlaceholderKind('scene:老宅', '老宅', {
      scene_refs_status: 'failed', scene_refs_target: '老宅',
    })).toBe('failed')
    expect(sceneRefPlaceholderText('failed')).toBe('场景图生成失败')
  })

  it('具卡场景 + 当前无图 + 任务既没跑也没失败 -> pending', () => {
    expect(resolveSceneRefPlaceholderKind('scene:老宅', '老宅', { scene_refs_status: 'ready' }))
      .toBe('pending')
    expect(resolveSceneRefPlaceholderKind('scene:老宅', '老宅', null)).toBe('pending')
    expect(sceneRefPlaceholderText('pending')).toBe('场景图待生成')
  })
})

describe('refsBusyPollInterval（出图结束后轮询停止）', () => {
  it('定妆照或场景图任一在跑都算忙，返回非零间隔', () => {
    expect(refsBusyPollInterval({ refs_status: 'running' })).toBe(4000)
    expect(refsBusyPollInterval({ scene_refs_status: 'running' })).toBe(4000)
  })

  it('都不在跑（idle/ready/failed/warning）时返回 0，不轮询', () => {
    expect(refsBusyPollInterval({ refs_status: 'ready', scene_refs_status: 'idle' })).toBe(0)
    expect(refsBusyPollInterval({ refs_status: 'failed' })).toBe(0)
    expect(refsBusyPollInterval(null)).toBe(0)
  })

  it('端到端：AdaptivePoller 在出图期间持续排定下一次轮询，一旦转为 ready 立刻停止排定', async () => {
    // 照抄 adaptivePoller.test.ts 的手动时钟写法：不真的等 4 秒，靠断言 timers
    // 队列里有没有排定下一次回调来证明"轮询启停"，而不是猜测经过的时间。
    type ProjectState = { refs_status: string }
    const responses: ProjectState[] = [
      { refs_status: 'running' },
      { refs_status: 'running' },
      { refs_status: 'ready' },
    ]
    const timers: Array<() => void> = []
    const clock = {
      setTimeout: (callback: () => void) => { timers.push(callback); return callback },
      clearTimeout: (handle: unknown) => {
        const index = timers.indexOf(handle as () => void)
        if (index >= 0) timers.splice(index, 1)
      },
    }
    const received: ProjectState[] = []
    const poller = new AdaptivePoller<ProjectState>(
      async () => responses.shift()!,
      refsBusyPollInterval,
      { onData: state => received.push(state), onError: error => { throw error } },
      clock,
    )

    await poller.start()
    expect(received.at(-1)).toEqual({ refs_status: 'running' })
    expect(timers).toHaveLength(1) // 出图中：已排定下一次轮询

    timers.shift()!()
    await new Promise<void>(resolve => setTimeout(resolve, 0))
    expect(received.at(-1)).toEqual({ refs_status: 'running' })
    expect(timers).toHaveLength(1) // 仍在出图：继续排定

    timers.shift()!()
    await new Promise<void>(resolve => setTimeout(resolve, 0))
    expect(received.at(-1)).toEqual({ refs_status: 'ready' })
    expect(timers).toHaveLength(0) // 出图已结束：不再排定下一次轮询
  })
})
