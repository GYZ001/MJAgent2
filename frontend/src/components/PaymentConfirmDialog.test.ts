import { describe, expect, it } from 'vitest'
import type { RefsCostPrecheck } from '../api'
import { paymentPolicyText, paymentSelectionSummary } from './PaymentConfirmDialog'

describe('付费确认业务文案', () => {
  it('隐藏报价标识与质检内部术语', () => {
    expect(paymentPolicyText('同一 quote_id 重复确认由服务端校验'))
      .toBe('同一报价重复确认由系统校验')
    expect(paymentPolicyText('QA 通过前保留旧图')).toBe('质检通过前保留旧图')
    expect(paymentPolicyText('使用最新角色提示词生成')).toBe('使用最新角色设定生成')
  })
})

describe('付费范围选择', () => {
  it('按当前勾选角色实时计算确认范围和费用', () => {
    const precheck = {
      quote_id: 'quote',
      computed_at: 1,
      quote_expires_at: 2,
      project_id: 'project',
      action: 'regenerate',
      character_count: 2,
      views_per_character: 3,
      image_count: 6,
      unit_price_cny: 0.2,
      estimated_cost_cny: 1.2,
      max_retry_budget_cny: 1.8,
      budget_cap_cny: 1.8,
      scope: [
        { character: '孟浩', views: ['front_full', 'three_quarter', 'profile'] },
        { character: '李富贵', views: ['front_full', 'three_quarter', 'profile'] },
      ],
    } satisfies RefsCostPrecheck

    expect(paymentSelectionSummary(precheck, { characters: ['孟浩'] })).toEqual({
      itemCount: 1,
      imageCount: 3,
      estimatedCostCny: 0.6,
      maxRetryBudgetCny: 0.9,
      budgetCapCny: 0.9,
    })
  })
})
