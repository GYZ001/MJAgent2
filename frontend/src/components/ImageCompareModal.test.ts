import { describe, expect, it } from 'vitest'
import { imageCompareDisabledReason } from './ImageCompareModal'

describe('图片对比禁用原因', () => {
  it('说明单图与首尾边界', () => {
    expect(imageCompareDisabledReason('compare', 1, 0, 1)).toContain('只有一张')
    expect(imageCompareDisabledReason('previous', 3, 0, 1)).toContain('第一张')
    expect(imageCompareDisabledReason('next', 3, 2, 1)).toContain('最后一张')
  })

  it('说明缩放上下限与重置状态', () => {
    expect(imageCompareDisabledReason('reset', 3, 0, 1)).toContain('100%')
    expect(imageCompareDisabledReason('zoomIn', 3, 0, 3)).toContain('300%')
    expect(imageCompareDisabledReason('zoomOut', 3, 0, 0.5)).toContain('50%')
  })
})
