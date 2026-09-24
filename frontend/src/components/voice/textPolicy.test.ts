import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * 界面不许出现任何人民币金额（CLAUDE.md/U4 派单：金额概念 2026-09-01 已退场）。
 * 扫描整份源码（含注释）而不是只挑渲染出来的文案：这样即使某条分支没被其它
 * 测试覆盖到，也不会漏查；副作用是本文件自己的注释也不能出现"元"/"¥"，属于
 * 更严格而不是更松的判据。
 */
const FILES = [
  '../../api/voices.ts',
  './voicePlayerStore.ts',
  './useProjectVoices.ts',
  './VoicePlayButton.tsx',
  './VoiceChip.tsx',
  './CharacterVoicePanel.tsx',
  './VoiceRosterActions.tsx',
]

describe('角色固定音色界面文案不含人民币金额', () => {
  it.each(FILES)('%s 不出现"元"或"¥"', (relativePath) => {
    const source = readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf-8')
    expect(source).not.toMatch(/[元¥]/)
  })
})
