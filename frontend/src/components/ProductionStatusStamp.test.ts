import { describe, expect, it } from 'vitest'
import { episodeStatusMeta, screenplayStatusMeta } from './ProductionStatusStamp'

describe('制作状态文案', () => {
  it('使用完整、跨页面一致的映射包状态', () => {
    expect(screenplayStatusMeta('pending').label).toBe('映射包待生成')
    expect(screenplayStatusMeta('ready').label).toBe('映射包已就绪')
    expect(screenplayStatusMeta('failed').label).toBe('映射包生成失败')
  })

  it('覆盖当前与兼容分集状态', () => {
    expect(episodeStatusMeta('scripted').label).toBe('分镜待确认')
    expect(episodeStatusMeta('scripted', '等待人工修正')).toEqual({
      label: '分镜待处理',
      tone: 'gold',
      known: true,
    })
    expect(episodeStatusMeta('mixed').label).toBe('视频处理中')
    expect(episodeStatusMeta('done').label).toBe('本集已成片')
  })

  it('未知状态不向普通界面泄漏内部状态码', () => {
    expect(screenplayStatusMeta('new_backend_state')).toEqual({
      label: '映射包状态待确认',
      tone: 'grey',
      known: false,
    })
    expect(episodeStatusMeta('new_backend_state').label).toBe('制作状态待确认')
  })
})
