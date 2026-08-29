import type { Bible } from '../api'
import { sceneUsability } from './sceneUsability'
import type { PrepStepStatus } from './statusLabels'

export interface SceneStepProject {
  bible?: Bible | null
  bible_status?: string
  refs_status?: string
  scene_refs_status?: string
}

/**
 * 场景库这一步的状态：人物谱页与场景库页的顶部步骤卡共用同一份判据，不允许
 * 两页各自维护一份互相漂移——人物谱/定妆照仍在跑时，场景库这一步已经在管线
 * 里被安排执行（后端人物谱生成成功后无条件依次触发定妆照、场景清单、
 * 场景图，参见 ScenesPage `startBibleAndSceneLibrary` 的注释），不能显示
 * 「未开始」，那是在说谎（CLAUDE.md：界面承诺必须与实际行为一致）。
 */
export function sceneStepStatus(project: SceneStepProject): PrepStepStatus {
  if (project.bible_status === 'running' || project.refs_status === 'running') return 'running'
  const status = project.scene_refs_status
  const scenes = project.bible?.scenes ?? []
  if (status === 'running') return 'running'
  const hasUnavailable = scenes.some(scene => (scene.scene_refs ?? []).length > 0
    ? sceneUsability(scene, false) === 'unavailable'
    : !scene.ref_image_url)
  if (hasUnavailable) return 'problem'
  if (scenes.length > 0) return 'done'
  if (status === 'failed' || status === 'warning') return 'problem'
  if (status && ['ready', 'done', 'succeeded'].includes(status)) return 'done'
  return 'idle'
}
