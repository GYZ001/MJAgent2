import { describe, expect, it } from 'vitest'
import { characterQaMessage } from './CharacterQaPanel'

describe('人物定妆质检文案', () => {
  it('翻译视角内部码并统一质检提示术语', () => {
    expect(characterQaMessage('front_full：存在警告')).toBe('正面全身：存在质检提示')
    expect(characterQaMessage('three_quarter 与 profile 不一致')).toBe('3/4 面 与 侧面 不一致')
  })
})
