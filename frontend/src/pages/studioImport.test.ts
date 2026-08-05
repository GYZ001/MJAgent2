import { describe, expect, it } from 'vitest'
import { formatFileSize, novelTitleFromFilename, projectEntry, validateNovelFile } from './studioImport'

describe('validateNovelFile', () => {
  it('accepts non-empty TXT and EPUB files regardless of extension case', () => {
    expect(validateNovelFile({ name: 'story.txt', size: 12 })).toBeNull()
    expect(validateNovelFile({ name: 'STORY.TXT', size: 12 })).toBeNull()
    expect(validateNovelFile({ name: 'story.epub', size: 12 })).toBeNull()
    expect(validateNovelFile({ name: 'STORY.EPUB', size: 12 })).toBeNull()
  })

  it('rejects unsupported and empty files before upload', () => {
    expect(validateNovelFile({ name: 'story.pdf', size: 12 })).toContain('目前可导入 TXT 或 EPUB')
    expect(validateNovelFile({ name: 'story.txt', size: 0 })).toContain('没有正文内容')
    expect(validateNovelFile({ name: 'story.epub', size: 0 })).toContain('没有正文内容')
  })

  it('derives the default title from either supported extension', () => {
    expect(novelTitleFromFilename('长夜.txt')).toBe('长夜')
    expect(novelTitleFromFilename('长夜.EPUB')).toBe('长夜')
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
