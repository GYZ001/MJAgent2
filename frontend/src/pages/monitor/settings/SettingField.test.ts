import React from 'react'
import TestRenderer from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'
import type { SettingSchema } from '../../../api'
import SettingField from './SettingField'

/**
 * U4b（2026-09-24）新增两个系统设置键：video_reference_audio_enabled（开关，
 * 默认关闭）、video_reference_audio_max_speakers（1~3 的整数）。系统设置页
 * 按 schema 自动渲染，每个 key 最终都落到这个组件（SettingsSection.tsx 按
 * categorizeSettingKeys 分组后逐个丢给 SettingField，见 definitions.test.ts
 * 覆盖分组归属那一半）。
 *
 * 这里直接测 SettingField 而不整体挂载 SettingsSection：后者的
 * useLayoutEffect 会访问 window（beforeunload 监听），而 vitest 全局跑在
 * node 环境（vite.config.ts test.environment='node'，没有 jsdom/window），
 * 整体挂载会在与本测试无关的副作用上抛 ReferenceError。SettingField 本身
 * 是无副作用的纯展示组件，直接测更贴近"这两个字段能不能正确渲染"这件事。
 */
function render(settingKey: string, spec: SettingSchema, current: string, editable = true) {
  const onChange = vi.fn()
  const onReset = vi.fn()
  const renderer = TestRenderer.create(React.createElement(SettingField, {
    settingKey, spec, current, editable, hasDraft: false, groupAffects: ['视频生成'], onChange, onReset,
  }))
  return { renderer, onChange, onReset }
}

const enabledSpec: SettingSchema = {
  label: '生成视频时传入角色声音参考', type: 'boolean', default: 'false', immediate: true, experimental: false,
}
const maxSpeakersSpec: SettingSchema = {
  label: '每段最多传入几个角色的声音', type: 'integer', default: '3', min: 1, max: 3, step: 1, immediate: true, experimental: false,
}

describe('SettingField 渲染参考声音两个新设置键', () => {
  it('开关：中文 label、复选框、且带"开启后…已生成的视频不受影响"的说明文字', () => {
    const { renderer } = render('video_reference_audio_enabled', enabledSpec, 'false')
    const text = JSON.stringify(renderer.toJSON())
    expect(text).toContain('生成视频时传入角色声音参考')
    expect(text).toContain('开启后')
    expect(text).toContain('已生成的视频不受影响')
    const checkbox = renderer.root.findByProps({ id: 'setting-video_reference_audio_enabled' })
    expect(checkbox.props.type).toBe('checkbox')
    expect(checkbox.props.checked).toBe(false)
    expect(checkbox.props.disabled).toBeFalsy()
  })

  it('开关能修改：勾选后 onChange 收到 "true"', () => {
    const { renderer, onChange } = render('video_reference_audio_enabled', enabledSpec, 'false')
    const checkbox = renderer.root.findByProps({ id: 'setting-video_reference_audio_enabled' })
    checkbox.props.onChange({ target: { checked: true } })
    expect(onChange).toHaveBeenCalledWith('true')
  })

  it('人数上限：中文 label、数字输入框、范围 1~3', () => {
    const { renderer } = render('video_reference_audio_max_speakers', maxSpeakersSpec, '3')
    const text = JSON.stringify(renderer.toJSON())
    expect(text).toContain('每段最多传入几个角色的声音')
    expect(text).toContain('1~3')
    const input = renderer.root.findByProps({ id: 'setting-video_reference_audio_max_speakers' })
    expect(input.props.type).toBe('number')
    expect(input.props.min).toBe(1)
    expect(input.props.max).toBe(3)
    expect(input.props.disabled).toBeFalsy()
  })

  it('人数上限能修改：改成 2 时 onChange 收到 "2"', () => {
    const { renderer, onChange } = render('video_reference_audio_max_speakers', maxSpeakersSpec, '3')
    const input = renderer.root.findByProps({ id: 'setting-video_reference_audio_max_speakers' })
    input.props.onChange({ target: { value: '2' } })
    expect(onChange).toHaveBeenCalledWith('2')
  })

  it('只读态（editable=false）：两个字段的输入控件都被禁用，不能修改', () => {
    const disabledSwitch = render('video_reference_audio_enabled', enabledSpec, 'false', false)
    const disabledInput = render('video_reference_audio_max_speakers', maxSpeakersSpec, '3', false)
    expect(disabledSwitch.renderer.root.findByProps({ id: 'setting-video_reference_audio_enabled' }).props.disabled).toBe(true)
    expect(disabledInput.renderer.root.findByProps({ id: 'setting-video_reference_audio_max_speakers' }).props.disabled).toBe(true)
  })
})
