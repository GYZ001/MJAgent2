import { describe, expect, it } from 'vitest'
import type { Bible, Character } from '../api'
import {
  bibleConflictFieldLabel,
  bibleStepStatus,
  characterCompareImages,
  characterIsFitting,
  currentPortrait,
  currentPortraitViews,
  mergeBibleThreeWay,
  portraitAvailability,
} from './BiblePage'

const character = (name: string) => ({ name } as Character)

describe('characterIsFitting', () => {
  it('全量批次在 refs_target 为空时标记所有角色', () => {
    expect(characterIsFitting({ refs_status: 'running', refs_target: null }, character('甲一'))).toBe(true)
    expect(characterIsFitting({ refs_status: 'running', refs_target: null }, character('丙老'))).toBe(true)
  })

  it('批量选角时只标记 refs_target JSON 中的角色', () => {
    const project = { refs_status: 'running', refs_target: JSON.stringify(['甲一', '丙老']) }
    expect(characterIsFitting(project, character('丙老'))).toBe(true)
    expect(characterIsFitting(project, character('熏儿'))).toBe(false)
  })

  it('保持兼容历史单角色 target', () => {
    expect(characterIsFitting(
      { refs_status: 'running', refs_target: '甲一' }, character('甲一'),
    )).toBe(true)
  })
})

const bible = (characters: Character[], style = '国风') => ({
  world: { visual_style_canonical: style },
  characters,
} as Bible)

describe('人物谱步骤状态', () => {
  it('定妆生成中不会把尚未产出的图片误报为有问题', () => {
    expect(bibleStepStatus({
      bible: bible([character('甲一')]),
      bible_status: 'ready',
      refs_status: 'running',
    })).toBe('running')
  })

  it('任务明确失败时仍优先显示有问题', () => {
    expect(bibleStepStatus({
      bible: bible([character('甲一')]),
      bible_status: 'ready',
      refs_status: 'failed',
    })).toBe('problem')
  })

  it('结构完整的低分定妆只提示质量风险，不标记为不可用', () => {
    const lowScoreCharacter = {
      name: '甲一',
      portraits: [{
        id: 'current',
        ep_start: 1,
        ep_end: null,
        pack_status: 'ready',
        image_url: '/front.jpg',
        views: [
          { view_role: 'front_full', status: 'ready', image_url: '/front.jpg' },
          { view_role: 'three_quarter', status: 'ready', image_url: '/three-quarter.jpg' },
          { view_role: 'profile', status: 'ready', image_url: '/profile.jpg' },
        ],
        group_qa: {
          overall: 0.1,
          status: 'failed',
          issues: ['人物一致性偏低'],
          hard_failures: ['watermark'],
        },
      }],
    } as Character

    expect(portraitAvailability(lowScoreCharacter, false)).toBe('warning')
    expect(bibleStepStatus({
      bible: bible([lowScoreCharacter]),
      bible_status: 'ready',
      refs_status: 'ready',
    })).toBe('done')
  })
})

describe('mergeBibleThreeWay', () => {
  it('保留本地字段修订和服务端新角色', () => {
    const base = bible([{ name: '甲一', role: '主角', personality: '冷静' } as Character])
    const local = bible([{ name: '甲一', role: '主角', personality: '坚定' } as Character])
    const server = bible([
      { name: '甲一', role: '主角', personality: '冷静' } as Character,
      { name: '丙老', role: '导师' } as Character,
    ])
    const merged = mergeBibleThreeWay(base, local, server)
    expect(merged.conflicts).toHaveLength(0)
    expect(merged.bible.characters.map(item => item.name)).toEqual(['甲一', '丙老'])
    expect(merged.bible.characters[0].personality).toBe('坚定')
  })

  it('同字段双改时必须显式选择', () => {
    const base = bible([{ name: '甲一', role: '主角', personality: '冷静' } as Character])
    const local = bible([{ name: '甲一', role: '主角', personality: '坚定' } as Character])
    const server = bible([{ name: '甲一', role: '主角', personality: '果断' } as Character])
    const preview = mergeBibleThreeWay(base, local, server)
    expect(preview.conflicts.map(item => item.path)).toContain('characters.甲一.personality')
    expect(preview.bible.characters[0].personality).toBe('果断')
    const resolved = mergeBibleThreeWay(base, local, server, {
      'characters.甲一.personality': 'local',
    })
    expect(resolved.bible.characters[0].personality).toBe('坚定')
  })
})

describe('人物谱冲突字段文案', () => {
  it('把内部字段路径翻译为角色可读名称', () => {
    expect(bibleConflictFieldLabel('characters.甲一.personality')).toBe('甲一 · 性格')
    expect(bibleConflictFieldLabel('world.visual_style_canonical')).toBe('统一画面风格')
  })
})

describe('人物定妆主画廊', () => {
  it('多套历史定妆各有三视角时只展示当前版三张', () => {
    const withPortraitHistory = {
      name: '甲二儿',
      portraits: [
        {
          id: 'history-1',
          ep_start: 1,
          ep_end: 3,
          views: ['profile', 'front_full', 'three_quarter'].map(role => ({
            id: `history-1-${role}`,
            view_role: role,
            image_url: `/history-1-${role}.jpg`,
          })),
        },
        {
          id: 'current',
          ep_start: 7,
          ep_end: null,
          views: ['profile', 'front_full', 'three_quarter'].map(role => ({
            id: `current-${role}`,
            view_role: role,
            image_url: `/current-${role}.jpg`,
          })),
        },
        {
          id: 'history-2',
          ep_start: 4,
          ep_end: 6,
          views: ['front_full', 'three_quarter', 'profile'].map(role => ({
            id: `history-2-${role}`,
            view_role: role,
            image_url: `/history-2-${role}.jpg`,
          })),
        },
      ],
    } as Character

    expect(currentPortrait(withPortraitHistory)?.id).toBe('current')
    expect(currentPortraitViews(withPortraitHistory).map(view => view.id)).toEqual([
      'current-front_full',
      'current-three_quarter',
      'current-profile',
    ])
  })

  it('重复和扩展视角混入时仍只展示当前版三个主视角', () => {
    const withDuplicateViews = {
      name: '甲二儿',
      portraits: [{
        id: 'current',
        ep_start: 1,
        ep_end: null,
        views: [
          { id: 'front', view_role: 'front_full', image_url: '/front.jpg' },
          { id: 'front-duplicate', view_role: 'front_full', image_url: '/front-2.jpg' },
          { id: 'back', view_role: 'back_full', image_url: '/back.jpg' },
          { id: 'profile', view_role: 'profile', image_url: '/profile.jpg' },
          { id: 'closeup', view_role: 'face_closeup', image_url: '/closeup.jpg' },
          { id: 'three-quarter', view_role: 'three_quarter', image_url: '/three-quarter.jpg' },
        ],
      }],
    } as Character

    expect(currentPortraitViews(withDuplicateViews).map(view => view.id)).toEqual([
      'front',
      'three-quarter',
      'profile',
    ])
  })

  it('对比弹窗不会重复展示兼容主图和正面视角的同一文件', () => {
    const withLegacyMainImage = {
      name: '孟浩',
      portraits: [{
        id: 'current',
        ep_start: 1,
        ep_end: null,
        image_url: '/front.jpg',
        views: [
          { id: 'front', view_role: 'front_full', image_url: '/front.jpg' },
          { id: 'three-quarter', view_role: 'three_quarter', image_url: '/three-quarter.jpg' },
          { id: 'profile', view_role: 'profile', image_url: '/profile.jpg' },
        ],
      }],
    } as Character

    expect(characterCompareImages(withLegacyMainImage).map(image => image.src)).toEqual([
      '/front.jpg',
      '/three-quarter.jpg',
      '/profile.jpg',
    ])
  })
})
