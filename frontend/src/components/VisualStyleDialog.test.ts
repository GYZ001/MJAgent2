import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const source = readFileSync(fileURLToPath(new URL('./VisualStyleDialog.tsx', import.meta.url)), 'utf-8')

// 真实案例（2026-08-31）：同一项目同一摄影类画风下 8/10 集视频阶段被供应商
// 隐私政策拒收（InputImageSensitiveContentDetected.PrivacyInformation）。
// 本仓库无组件渲染测试基建（见 Studio.test.ts / BiblePage.test.ts 同类注
// 释），改用源码静态扫描守住：摄影类选项必须可见提示，非摄影类不得跟着一起
// 提示（判据是每个选项自带的 photographic 标记，不是新造的画风名单）。
describe('画风弹窗按每个选项的 photographic 标记显示隐私拒收提示', () => {
  it('提示门控在 option.photographic 上，不是无条件对每个选项都展示', () => {
    expect(source).toMatch(/\{option\.photographic && \(/)
    expect(source).toContain('视频阶段较高概率因疑似真人被供应商隐私政策拒收')
  })

  it('提示文案给出具体出路（仅出图可用 / 改选其它画风），不是纯警告', () => {
    expect(source).toContain('仅出图可放心选用')
    expect(source).toContain('需要出视频建议改选其它画风')
  })

  it('VisualStyleOption 类型携带 photographic，不依赖调用方另传', () => {
    expect(source).toMatch(/photographic: boolean/)
  })

  it('提示复用既有 warning-banner 样式，没有新造一套未定义的 className', () => {
    expect(source).toMatch(/<small className="warning-banner">/)
  })
})
