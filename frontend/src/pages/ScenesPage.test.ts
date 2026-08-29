import { describe, expect, it } from 'vitest'
import type { Scene } from '../api'
import { sceneUsability } from '../lib/sceneUsability'
import {
  handoffGapSelectionToPayment,
  readScenePreviewDraft,
  scenePrepStatus,
  scenePreviewStorageKey,
  writeScenePreviewDraft,
} from './ScenesPage'

describe('场景库步骤状态', () => {
  it('生成期间优先显示进行中，不把待生成缺口误报为有问题', () => {
    expect(scenePrepStatus(true, true, 11)).toBe('running')
    expect(scenePrepStatus(true, false, 11)).toBe('problem')
  })
})

describe('场景缺口扫描弹窗交接', () => {
  it('先关闭扫描结果，再打开费用确认，避免确认窗被挡在背后', async () => {
    const events: string[] = []

    await handoffGapSelectionToPayment(
      ['甲家广场'],
      () => { events.push('close-gap') },
      async scenes => { events.push(`open-payment:${scenes.join(',')}`) },
    )

    expect(events).toEqual(['close-gap', 'open-payment:甲家广场'])
  })
})

describe('场景主图与附加视角状态', () => {
  it('反打包失败不再把可展示主图标成不可用', () => {
    const scene = {
      name: '后山小树林',
      scene_canonical: '树林',
      scene_refs: [{
        ep_start: 1,
        ep_end: null,
        image_url: '/media/forest.jpg',
        pack_status: 'failed',
        views: [
          { id: 'front', view_role: 'establishing', image_url: '/media/forest.jpg' },
          { id: 'reverse', view_role: 'reverse_angle', image_url: '/media/forest-reverse.jpg' },
        ],
      }],
    }

    expect(sceneUsability(scene, false)).toBe('available')
  })

  it('整包模式缺少必需视角文件时不可用', () => {
    const scene = {
      name: '半包场景',
      scene_canonical: '室内',
      required_views: ['establishing', 'reverse_angle'],
      scene_refs: [{
        ep_start: 1,
        ep_end: null,
        image_url: '/media/room.jpg',
        pack_status: 'failed',
        views: [
          { id: 'front', view_role: 'establishing', image_url: '/media/room.jpg' },
        ],
      }],
    }

    expect(sceneUsability(scene, false)).toBe('unavailable')
  })
})

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
    has: (key: string) => values.has(key),
  }
}

const previewScenes: Scene[] = [{
  name: '甲家测验广场',
  scene_canonical: '室外开阔青石广场，中央立有测验魔石碑，四周为家族看台，日光明亮且空间规整。',
}]

describe('场景清单预览恢复', () => {
  it('恢复同一人物谱版本的场景预览', () => {
    const storage = memoryStorage()
    writeScenePreviewDraft(storage, 'project-1', 3, previewScenes)

    expect(readScenePreviewDraft(storage, 'project-1', 3)).toEqual(previewScenes)
  })

  it('丢弃旧人物谱版本的场景预览', () => {
    const storage = memoryStorage()
    writeScenePreviewDraft(storage, 'project-1', 2, previewScenes)

    expect(readScenePreviewDraft(storage, 'project-1', 3)).toBeNull()
    expect(storage.has(scenePreviewStorageKey('project-1'))).toBe(false)
  })

  it('丢弃结构损坏的场景预览', () => {
    const storage = memoryStorage()
    storage.setItem(scenePreviewStorageKey('project-1'), '{"bibleVersion":3,"scenes":[{}]}')

    expect(readScenePreviewDraft(storage, 'project-1', 3)).toBeNull()
    expect(storage.has(scenePreviewStorageKey('project-1'))).toBe(false)
  })
})
