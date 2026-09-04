import { describe, expect, it } from 'vitest'
import { propStamp } from './PropsPage'

// 物件库卡片角标只看两件事：库表状态 + 是否真有图。ready 但没图（登记行说 ready、
// 图文件却不在盘上，image_url 为 null）不能标成「已出图」，那会让用户以为分镜拿得到参考图。
describe('物件库卡片角标', () => {
  it('ready 且有图才算已出图', () => {
    expect(propStamp('ready', true)).toEqual({ color: 'green', label: '已出图' })
    expect(propStamp('ready', false)).toEqual({ color: 'grey', label: '未出图' })
  })
  it('失败与出图中各有独立角标', () => {
    expect(propStamp('failed', false).label).toBe('出图失败')
    expect(propStamp('running', false).label).toBe('出图中')
  })
})
