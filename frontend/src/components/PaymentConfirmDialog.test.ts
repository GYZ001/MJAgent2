import { describe, expect, it } from 'vitest'
import { paymentPolicyText } from './PaymentConfirmDialog'

describe('付费确认业务文案', () => {
  it('隐藏报价标识与质检内部术语', () => {
    expect(paymentPolicyText('同一 quote_id 重复确认由服务端校验'))
      .toBe('同一报价重复确认由系统校验')
    expect(paymentPolicyText('QA 通过前保留旧图')).toBe('质检通过前保留旧图')
    expect(paymentPolicyText('使用最新角色提示词生成')).toBe('使用最新角色设定生成')
  })
})
