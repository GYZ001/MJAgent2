import { describe, expect, it } from 'vitest'
import type { Bible, Character } from '../api'
import { characterIsFitting, mergeBibleThreeWay } from './BiblePage'

const character = (name: string) => ({ name } as Character)

describe('characterIsFitting', () => {
  it('全量批次在 refs_target 为空时标记所有角色', () => {
    expect(characterIsFitting({ refs_status: 'running', refs_target: null }, character('萧炎'))).toBe(true)
    expect(characterIsFitting({ refs_status: 'running', refs_target: null }, character('药老'))).toBe(true)
  })

  it('批量选角时只标记 refs_target JSON 中的角色', () => {
    const project = { refs_status: 'running', refs_target: JSON.stringify(['萧炎', '药老']) }
    expect(characterIsFitting(project, character('药老'))).toBe(true)
    expect(characterIsFitting(project, character('熏儿'))).toBe(false)
  })

  it('保持兼容历史单角色 target', () => {
    expect(characterIsFitting(
      { refs_status: 'running', refs_target: '萧炎' }, character('萧炎'),
    )).toBe(true)
  })
})

const bible = (characters: Character[], style = '国风') => ({
  world: { visual_style_canonical: style },
  characters,
} as Bible)

describe('mergeBibleThreeWay', () => {
  it('保留本地字段修订和服务端新角色', () => {
    const base = bible([{ name: '萧炎', role: '主角', personality: '冷静' } as Character])
    const local = bible([{ name: '萧炎', role: '主角', personality: '坚定' } as Character])
    const server = bible([
      { name: '萧炎', role: '主角', personality: '冷静' } as Character,
      { name: '药老', role: '导师' } as Character,
    ])
    const merged = mergeBibleThreeWay(base, local, server)
    expect(merged.conflicts).toHaveLength(0)
    expect(merged.bible.characters.map(item => item.name)).toEqual(['萧炎', '药老'])
    expect(merged.bible.characters[0].personality).toBe('坚定')
  })

  it('同字段双改时必须显式选择', () => {
    const base = bible([{ name: '萧炎', role: '主角', personality: '冷静' } as Character])
    const local = bible([{ name: '萧炎', role: '主角', personality: '坚定' } as Character])
    const server = bible([{ name: '萧炎', role: '主角', personality: '果断' } as Character])
    const preview = mergeBibleThreeWay(base, local, server)
    expect(preview.conflicts.map(item => item.path)).toContain('characters.萧炎.personality')
    expect(preview.bible.characters[0].personality).toBe('果断')
    const resolved = mergeBibleThreeWay(base, local, server, {
      'characters.萧炎.personality': 'local',
    })
    expect(resolved.bible.characters[0].personality).toBe('坚定')
  })
})
