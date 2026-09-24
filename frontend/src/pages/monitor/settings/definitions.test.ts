import { describe, expect, it } from 'vitest'
import { categorizeSettingKeys, SETTING_FIELD_IMPACTS, SETTING_GROUP_DEFINITIONS } from './definitions'

/**
 * U4b（2026-09-24）新增两个系统设置键：video_reference_audio_enabled（默认
 * 关闭的开关）、video_reference_audio_max_speakers（每段最多传入几个角色的
 * 声音）。字段名与 label 逐字照用后端约定，这里只测前端这一侧的登记——系统
 * 设置页按 schema 自动渲染字段本体（label/type/min/max 等来自后端），但分组
 * 归属与「影响」说明文字必须在 definitions.ts 里逐项登记，不登记就会落进
 * 兜底的「其他系统能力」组，见 SettingsSection.tsx 的 categorizeSettingKeys。
 */
describe('参考声音两个新设置键的分组与说明文字登记', () => {
  it('两个键都在某个分组的 keys 里声明过（不依赖 schema 的其它 key 存在）', () => {
    const keys = SETTING_GROUP_DEFINITIONS.flatMap(group => group.keys)
    expect(keys).toContain('video_reference_audio_enabled')
    expect(keys).toContain('video_reference_audio_max_speakers')
  })

  it('归入参考图与视觉生成分组（视频相关分组），不落进"其他系统能力"兜底组', () => {
    const groups = categorizeSettingKeys([
      'video_reference_audio_enabled',
      'video_reference_audio_max_speakers',
      'use_character_refs',
    ])
    const group = groups.find(g => g.keys.includes('video_reference_audio_enabled'))
    expect(group?.id).toBe('reference-images')
    expect(group?.keys).toContain('video_reference_audio_max_speakers')
    expect(groups.find(g => g.id === 'other')).toBeUndefined()
  })

  it('单独出现（schema 里没有其它 reference-images 分组的键）时也能正确归组', () => {
    const groups = categorizeSettingKeys(['video_reference_audio_enabled', 'video_reference_audio_max_speakers'])
    expect(groups).toHaveLength(1)
    expect(groups[0].id).toBe('reference-images')
  })

  it('开关的影响说明写清"开启后…"与"已生成的视频不受影响"，不是空话', () => {
    const text = SETTING_FIELD_IMPACTS.video_reference_audio_enabled
    expect(text).toContain('开启后')
    expect(text).toContain('已生成的视频不受影响')
  })

  it('每段人数上限的影响说明也已登记，不是兜底的分组 affects 拼接', () => {
    expect(SETTING_FIELD_IMPACTS.video_reference_audio_max_speakers).toBeTruthy()
  })
})
