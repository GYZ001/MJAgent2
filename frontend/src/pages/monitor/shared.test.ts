// 观测台运行类型中文名映射：角色声音生成接入观测后补的回归锁，避免这张表
// 以后被误删或误改成未翻译的英文 key（用户曾直接反馈"点了批量生成配音，
// 观测里什么都看不到"，根因之一就是缺这类翻译登记）。
import { describe, expect, it } from 'vitest'
import { WORKFLOW_LABELS } from './shared'

describe('WORKFLOW_LABELS', () => {
  it('角色声音生成运行类型有中文名', () => {
    expect(WORKFLOW_LABELS.character_voices).toBe('角色声音生成')
  })
})
