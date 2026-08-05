import type { Project } from '../api'

export interface NovelFileDescriptor {
  name: string
  size: number
}

const SUPPORTED_NOVEL_EXTENSIONS = ['.txt', '.epub']

export function validateNovelFile(file: NovelFileDescriptor): string | null {
  const filename = file.name.trim()
  if (!SUPPORTED_NOVEL_EXTENSIONS.some(extension => filename.toLowerCase().endsWith(extension))) {
    return `“${filename || '未命名文件'}”格式不支持，目前可导入 TXT 或 EPUB`
  }
  if (file.size <= 0) {
    return `“${filename}”没有正文内容，请重新选择`
  }
  return null
}

export function novelTitleFromFilename(filename: string): string {
  return filename.replace(/\.(?:txt|epub)$/i, '')
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

type ProjectEntry = {
  view: 'bible' | 'episodes'
  label: string
}

export function projectEntry(project: Pick<Project, 'bible_status' | 'plan_status' | 'episode_count'>): ProjectEntry {
  if (project.bible_status === 'failed') {
    return { view: 'bible', label: '处理人物谱问题' }
  }
  if (project.plan_status === 'failed') {
    return { view: 'episodes', label: '处理分集问题' }
  }
  if (project.bible_status !== 'ready') {
    return {
      view: 'bible',
      label: project.bible_status === 'running' ? '查看人物谱进度' : '完善人物谱',
    }
  }
  if (project.plan_status !== 'ready') {
    return {
      view: 'episodes',
      label: project.plan_status === 'running' ? '查看分集进度' : '查看分集规划',
    }
  }
  return {
    view: 'episodes',
    label: (project.episode_count ?? 0) > 0 ? '继续分集制作' : '查看分集规划',
  }
}
