import { describe, expect, it } from 'vitest'
import { pageSizeForWidth, SINGLE_ROW_ASSET_PAGE } from './useFillPageSize'

describe('照片库单行分页容量', () => {
  it('桌面宽度按一行可容纳的卡片数分页', () => {
    expect(pageSizeForWidth({ ...SINGLE_ROW_ASSET_PAGE, available: 1480 })).toBe(5)
    expect(pageSizeForWidth({ ...SINGLE_ROW_ASSET_PAGE, available: 1380 })).toBe(4)
  })

  it('窄屏按实际列数缩减，不再被最少八张撑成多行', () => {
    expect(pageSizeForWidth({ ...SINGLE_ROW_ASSET_PAGE, available: 620 })).toBe(2)
    expect(pageSizeForWidth({ ...SINGLE_ROW_ASSET_PAGE, available: 320 })).toBe(1)
  })
})
