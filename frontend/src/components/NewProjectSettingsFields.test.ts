import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'
import NewProjectSettingsFields, { DEFAULT_NEW_PROJECT_SETTINGS, type NewProjectSettingsValue } from './NewProjectSettingsFields'

function mount(value: NewProjectSettingsValue, onChange = vi.fn(), disabled = false) {
  let view!: TestRenderer.ReactTestRenderer
  act(() => {
    view = TestRenderer.create(React.createElement(NewProjectSettingsFields, { value, onChange, disabled }))
  })
  return { view, onChange }
}

describe('NewProjectSettingsFields：默认值与变更回调', () => {
  it('默认值是短剧节奏 / 9:16 / AI 标识关闭', () => {
    expect(DEFAULT_NEW_PROJECT_SETTINGS).toEqual({ adaptation_mode: 'short_drama', aspect_ratio: '9:16', ai_label_enabled: false })
  })

  it('按传入 value 渲染三个控件的当前值', () => {
    const { view } = mount(DEFAULT_NEW_PROJECT_SETTINGS)
    const selects = view.root.findAllByType('select')
    expect(selects[0].props.value).toBe('short_drama')
    expect(selects[1].props.value).toBe('9:16')
    const checkbox = view.root.findAllByType('input').find(n => n.props.type === 'checkbox')
    expect(checkbox!.props.checked).toBe(false)
    view.unmount()
  })

  it('改动任一字段都通过 onChange 回传完整对象，不丢其余两个字段', () => {
    const { view, onChange } = mount(DEFAULT_NEW_PROJECT_SETTINGS)
    act(() => { view.root.findAllByType('select')[0].props.onChange({ target: { value: 'faithful' } }) })
    expect(onChange).toHaveBeenLastCalledWith({ adaptation_mode: 'faithful', aspect_ratio: '9:16', ai_label_enabled: false })

    act(() => { view.root.findAllByType('select')[1].props.onChange({ target: { value: '16:9' } }) })
    expect(onChange).toHaveBeenLastCalledWith({ adaptation_mode: 'short_drama', aspect_ratio: '16:9', ai_label_enabled: false })

    const checkbox = view.root.findAllByType('input').find(n => n.props.type === 'checkbox')
    act(() => { checkbox!.props.onChange({ target: { checked: true } }) })
    expect(onChange).toHaveBeenLastCalledWith({ adaptation_mode: 'short_drama', aspect_ratio: '9:16', ai_label_enabled: true })
    view.unmount()
  })

  it('disabled 时三个控件都不可操作', () => {
    const { view } = mount(DEFAULT_NEW_PROJECT_SETTINGS, vi.fn(), true)
    expect(view.root.findAllByType('select').every(s => s.props.disabled)).toBe(true)
    const checkbox = view.root.findAllByType('input').find(n => n.props.type === 'checkbox')
    expect(checkbox!.props.disabled).toBe(true)
    view.unmount()
  })
})
