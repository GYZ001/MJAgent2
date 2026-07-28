import { describe, expect, it } from 'vitest'
import { impactBusinessText } from './ImpactDialog'

describe('影响预览业务文案', () => {
  it('隐藏质检与必检项的内部术语', () => {
    expect(impactBusinessText('需要重新通过分镜人工门禁')).toBe('需要重新通过分镜人工确认')
    expect(impactBusinessText('QA 硬门禁未通过')).toBe('质检 必检项未通过')
  })
})
