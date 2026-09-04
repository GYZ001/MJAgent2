import { describe, expect, it } from 'vitest'
import type { PropItem } from '../api'
import { filterPropItems, propStamp, propStatusBucket } from './PropsPage'

const prop = (over: Partial<PropItem> = {}): PropItem => ({
  name: '旧猫包', appearance: '灰色帆布', aliases: [], image_path: 'a.png', image_url: '/media/a.png', status: 'ready', ...over,
})

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

describe('物件库搜索与筛选（与场景库对齐）', () => {
  const items = [
    prop({ name: '旧猫包', aliases: ['猫包'] }),
    prop({ name: '摄像机', appearance: '哑光黑色机身', image_url: null, status: 'ready' }),
    prop({ name: '聚光灯', status: 'failed', image_url: null }),
  ]
  it('搜索命中名称、别名与外观', () => {
    expect(filterPropItems(items, '猫包', '').map(i => i.name)).toEqual(['旧猫包'])
    expect(filterPropItems(items, '哑光', '').map(i => i.name)).toEqual(['摄像机'])
    expect(filterPropItems(items, '', '').length).toBe(3)
  })
  it('状态筛选与角标判据一致：ready 没图算未出图', () => {
    expect(propStatusBucket(items[1])).toBe('missing')
    expect(filterPropItems(items, '', 'ready').map(i => i.name)).toEqual(['旧猫包'])
    expect(filterPropItems(items, '', 'missing').map(i => i.name)).toEqual(['摄像机'])
    expect(filterPropItems(items, '', 'failed').map(i => i.name)).toEqual(['聚光灯'])
  })
})
