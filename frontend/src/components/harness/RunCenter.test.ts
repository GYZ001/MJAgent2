import { describe, expect, it } from 'vitest'
import { runFailureGuidance, shouldFocusRunRow } from './RunCenter'

describe('运行失败恢复建议', () => {
  it('按暂停与失败状态给出业务恢复路径', () => {
    expect(runFailureGuidance('PAUSED_BUDGET')).toContain('预算不足')
    expect(runFailureGuidance('PAUSED_EXTERNAL')).toContain('安全检查点')
    expect(runFailureGuidance('WAITING_RETRY')).toContain('自动重试')
    expect(runFailureGuidance('WAITING_HUMAN')).toContain('人工确认')
    expect(runFailureGuidance('PARTIAL')).toContain('受控重试')
    expect(runFailureGuidance('FAILED')).toContain('错误详情')
  })
})

describe('运行中心后台刷新定位', () => {
  it('同一定位令牌只滚动一次，新令牌仍可重新定位', () => {
    expect(shouldFocusRunRow('focus-1', '', true)).toBe(true)
    expect(shouldFocusRunRow('focus-1', 'focus-1', true)).toBe(false)
    expect(shouldFocusRunRow('focus-2', 'focus-1', true)).toBe(true)
    expect(shouldFocusRunRow('focus-2', 'focus-1', false)).toBe(false)
  })
})
