import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { sceneUsability } from '../lib/sceneUsability'
import { handoffGapSelectionToGenerate } from './ScenesPage'

const source = readFileSync(fileURLToPath(new URL('./ScenesPage.tsx', import.meta.url)), 'utf-8')

// 2026-08-31 用户拍板：场景库不再持有「模型驱动」的批量生成入口，画风选择挪到
// 导入项目时一次性选定，场景清单不再批量扫描全文生成。同一套静态扫描守法见
// BiblePage.test.ts 顶部注释（本仓库无组件渲染测试基建）。
//
// 同日晚些用户进一步拍板：「给用户一个动手添加的能力，图像描述都让用户自己填写
// 上传」——手动新增/替换与「模型驱动生成」是两回事，不受上面这条纪律约束，
// 见下方新增的正向断言。
describe('场景库页不再持有产生新内容的入口', () => {
  it('世界观/画风与批量场景清单生成入口已移除', () => {
    expect(source).not.toContain('选择画风并确定世界观')
    expect(source).not.toContain('配置统一画风')
    expect(source).not.toContain('准备场景清单')
    expect(source).not.toMatch(/VisualStyleDialog/)
    expect(source).not.toMatch(/useVisualStyleDialog/)
    expect(source).not.toMatch(/ScenePreviewDialog/)
  })

  it('场景空态给出指向映射台（分集页）的可点击入口', () => {
    expect(source).toContain('场景在映射台按需发现')
    expect(source).toMatch(/onGoEpisodes=\{\(\) => go\('episodes', p\.id\)\}/)
  })

  it('保留对已有场景的维护操作：补图缺口扫描、场景设定编辑', () => {
    expect(source).toContain('扫描场景图缺口')
    expect(source).toContain('场景设定与重绘')
  })
})

describe('场景库页保留手动新增/替换（用户自己填写，不走模型）', () => {
  it('挂了手动新增场景入口', () => {
    expect(source).toMatch(/import ManualSceneDialog from '..\/components\/bible\/ManualSceneDialog'/)
    expect(source).toContain('<ManualSceneDialog')
  })

  it('场景设定弹窗挂了替换场景图入口', () => {
    expect(source).toMatch(/import ReplaceSceneImageControl from '..\/components\/bible\/ReplaceSceneImageControl'/)
    expect(source).toContain('<ReplaceSceneImageControl')
  })
})

describe('人物谱世界观写入失败时的场景库出路', () => {
  it('不再让用户去联系管理员或重新导入，改成可点的重试', () => {
    expect(source).not.toContain('联系管理员')
    expect(source).not.toContain('不再提供重新发起入口')
    expect(source).toContain('重新生成人物谱')
    expect(source).toContain('retryBibleGenerationAction')
    expect(source).toMatch(/guidance="[^"]*映射台[^"]*(手动添加|手动新增)/)
  })
})

describe('场景缺口扫描弹窗交接', () => {
  it('先关闭扫描结果，再触发生成，避免弹窗被挡在背后', async () => {
    const events: string[] = []

    await handoffGapSelectionToGenerate(
      ['甲家广场'],
      () => { events.push('close-gap') },
      async scenes => { events.push(`generate:${scenes.join(',')}`) },
    )

    expect(events).toEqual(['close-gap', 'generate:甲家广场'])
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

  it('只出了主图、侧视角还没补上，照样是可用', () => {
    // 用户拍板 2026-09-01：有图就是可用。旧判据要求 required_views 齐全，实测把
    // 「赵国大青山山顶」（主图在、reverse_angle 没成）判成不可用，还连带让出图流程
    // 反复重烧主图堆到 8 张候选。
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

    expect(sceneUsability(scene, false)).toBe('available')
  })

  it('一张图都没有才是不可用', () => {
    const scene = { name: '空场景', scene_canonical: '室内', scene_refs: [] }
    expect(sceneUsability(scene, false)).toBe('unavailable')
  })
})
