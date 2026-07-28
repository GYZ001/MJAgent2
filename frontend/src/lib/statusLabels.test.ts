import { describe, expect, it } from 'vitest'
import {
  artifactTypeLabel,
  statusLabel,
  statusTitle,
} from './statusLabels'

describe('面向用户的状态文案', () => {
  it('把质检、必检项和验证等级写成完整中文', () => {
    expect(statusLabel('qa_pending')).toBe('质检中')
    expect(statusLabel('hard_failure')).toBe('未通过必检项')
    expect(statusLabel('T4')).toBe('人工已确认')
    expect(statusLabel('T5')).toBe('交付已验证')
  })

  it('未知状态不直接显示内部码', () => {
    expect(statusLabel('new_backend_state')).toBe('未知状态')
    expect(statusTitle('new_backend_state')).toBe('未识别的系统状态码：new_backend_state')
  })

  it('翻译产物枚举', () => {
    expect(artifactTypeLabel('episode_screenplay')).toBe('剧本')
    expect(artifactTypeLabel('unknown_asset')).toBe('其他产物')
  })
})
