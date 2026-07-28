import { describe, expect, it } from 'vitest'
import { formatFileSize, projectEntry, validateNovelFile } from './studioImport'

describe('validateNovelFile', () => {
  it('accepts non-empty TXT files regardless of extension case', () => {
    expect(validateNovelFile({ name: 'story.txt', size: 12 })).toBeNull()
    expect(validateNovelFile({ name: 'STORY.TXT', size: 12 })).toBeNull()
  })

  it('rejects unsupported and empty files before upload', () => {
    expect(validateNovelFile({ name: 'story.pdf', size: 12 })).toContain('不是 TXT 文件')
    expect(validateNovelFile({ name: 'story.txt', size: 0 })).toContain('没有正文内容')
  })
})

describe('formatFileSize', () => {
  it('uses readable units', () => {
    expect(formatFileSize(128)).toBe('128 B')
    expect(formatFileSize(1536)).toBe('1.5 KB')
    expect(formatFileSize(2 * 1024 * 1024)).toBe('2.0 MB')
  })
})

describe('projectEntry', () => {
  it('routes users to the stage that needs attention', () => {
    expect(projectEntry({
      bible_status: 'failed',
      plan_status: 'ready',
      episode_count: 8,
    })).toEqual({ view: 'bible', label: '处理人物谱问题' })
    expect(projectEntry({
      bible_status: 'ready',
      plan_status: 'failed',
      episode_count: 0,
    })).toEqual({ view: 'episodes', label: '处理分集问题' })
    expect(projectEntry({
      bible_status: 'ready',
      plan_status: 'ready',
      episode_count: 8,
    })).toEqual({ view: 'episodes', label: '继续分集制作' })
  })
})
