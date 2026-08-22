import { describe, expect, it } from 'vitest'
import { agentContextRequestPaths } from './ContextChips'

describe('Agent 上下文标签的取数路径', () => {
  it('只取 picker 投影，不再拉整份项目', () => {
    const paths = agentContextRequestPaths({
      project_id: 'p1',
      episode_id: 'e1',
    })

    // 千集项目的整份投影是 4.8 MB；picker 投影 1.4 KB 就给出项目名与当前分集。
    expect(paths.project).toBe(
      '/projects/p1?view=picker&episode_limit=1&episode_cursor=e1',
    )
    expect(paths.project).not.toMatch(/^\/projects\/[^?]+$/)
  })

  it('没有选中镜头时完全不取分镜投影', () => {
    expect(agentContextRequestPaths({ project_id: 'p1', episode_id: 'e1' }).shot)
      .toBeNull()
  })

  it('选中镜头时才按分集取一次分镜投影', () => {
    expect(
      agentContextRequestPaths({
        project_id: 'p1',
        episode_id: 'e1',
        selected_shot_id: 's1',
      }).shot,
    ).toBe('/episodes/e1?view=board')
  })

  it('没有分集时不带 cursor，也不取分镜', () => {
    const paths = agentContextRequestPaths({ project_id: 'p1' })
    expect(paths.project).toBe('/projects/p1?view=picker&episode_limit=1')
    expect(paths.shot).toBeNull()
  })
})
