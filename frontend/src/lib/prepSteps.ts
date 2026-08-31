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
 * 两页各自维护一份互相漂移。架构转向（2026-08-31）后场景清单/场景图不再随
 * 人物谱谱写自动级联（generate_scene_bible 退出首版流程，场景改为
 * app.scenes.assess_new_scene 反应式发现/场景库页手动准备），因此这一步的
 * 状态只看它自己的产物信号（scene_refs_status/scenes），不再把
 * bible_status/refs_status 的「运行中」借用成本步骤的「运行中」——那样会在
 * 场景库实际什么都没开始时显示「进行中」，同样是在说谎。
 */
export function sceneStepStatus(project: SceneStepProject): PrepStepStatus {
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
