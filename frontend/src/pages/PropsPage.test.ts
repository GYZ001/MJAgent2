import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { PropItem } from '../api'
import { filterPropItems, propStamp, propStatusBucket } from './PropsPage'

const source = readFileSync(fileURLToPath(new URL('./PropsPage.tsx', import.meta.url)), 'utf-8')

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

// useProject 内部走 usePoll，已有 p 后再轮询失败不清空 p；error 此前只喂给早退的
// `<QueryState hasData={false}>` 分支，p 到手后就再没人看（2026-09-23 补丁，同
// orgs/resources 各面板 2c96b89c）。本页无组件渲染测试基建（同 BiblePage.test.ts
// 顶部注释），用源码静态扫描守住接线不回归。
describe('物件库——已有数据时后台轮询刷新失败不得被吞', () => {
  it('QueryState 早退分支之外单独渲染 StaleRefreshBanner，接的是同一个 error/refresh', () => {
    expect(source).toMatch(/import StaleRefreshBanner from '\.\.\/components\/StaleRefreshBanner'/)
    expect(source).toMatch(/<StaleRefreshBanner error=\{error\} onRetry=\{refresh\} objectName="物件库" \/>/)
  })
})
