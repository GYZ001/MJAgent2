import { describe, expect, it } from 'vitest'
import { screenplayTaskNotice, storyboardTaskNotice } from './productionNotices'

describe('生产任务提示语义', () => {
  it('剧本运行进度文本不会被误报为错误', () => {
    expect(screenplayTaskNotice({
      screenplay_status: 'running',
      screenplay_error: '首次整版 Baseline 生成中；本次完成后只允许局部 Patch',
      screenplay_production: { task_active: true, can_resume_repair: false },
    })).toBeNull()

    expect(screenplayTaskNotice({
      screenplay_status: 'repairing',
      screenplay_error: '局部修复中',
      screenplay_production: { task_active: true, can_resume_repair: false },
    })).toBeNull()

    expect(screenplayTaskNotice({
      screenplay_status: 'failed',
      screenplay_error: '上一轮失败文本',
      screenplay_production: { task_active: true, can_resume_repair: false },
    })).toBeNull()
  })

  it('剧本只在真实失败时显示错误，可续修时显示提醒', () => {
    expect(screenplayTaskNotice({
      screenplay_status: 'failed',
      screenplay_error: '生成服务不可用',
    })?.severity).toBe('error')

    expect(screenplayTaskNotice({
      screenplay_status: 'repairing',
      screenplay_error: '恢复点已保留',
      screenplay_production: { task_active: false, can_resume_repair: true },
    })?.severity).toBe('warning')

    expect(screenplayTaskNotice({
      screenplay_status: 'ready',
      screenplay_error: '历史文本',
    })).toBeNull()
  })

  it('分镜文本也由工作流状态决定展示级别', () => {
    const episode = { status: 'scripting', script_error: '当前正在处理第 2 镜' }
    expect(storyboardTaskNotice(episode, 'running')).toBeNull()
    expect(storyboardTaskNotice({ ...episode, status: 'scripted' }, 'paused')?.severity)
      .toBe('warning')
    expect(storyboardTaskNotice({ ...episode, status: 'script_failed' }, 'failed')?.severity)
      .toBe('error')
    expect(storyboardTaskNotice({ ...episode, status: 'confirmed' }, 'confirmed'))
      .toBeNull()
    expect(storyboardTaskNotice({ ...episode, status: 'script_failed' }, 'syncing'))
      .toBeNull()
  })

  it('兼容旧响应中 scripted 加处理信息的暂停语义', () => {
    expect(storyboardTaskNotice({
      status: 'scripted',
      script_error: '暂停待处理',
    })?.severity).toBe('warning')
  })
})
