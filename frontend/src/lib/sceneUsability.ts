import type { Scene } from '../api'

export type SceneAvailability = 'generating' | 'passed' | 'warning' | 'failed' | 'unverified' | 'missing'
export type SceneUsability = 'available' | 'unavailable'

export function sceneAvailability(scene: Scene, fitting: boolean): SceneAvailability {
  if (fitting) return 'generating'
  const refs = scene.scene_refs ?? []
  const ref = refs.find(item => item.ep_end == null) ?? refs.at(-1)
  if (!ref?.image_url) return scene.ref_image_url ? 'unverified' : 'missing'
  if (ref.pack_status === 'generating' || ref.pack_status === 'qa_pending') return 'generating'
  const primaryQa = ref.qa
  if (primaryQa?.status === 'failed' || (primaryQa?.hard_failures ?? []).length) return 'failed'
  const gate = ref.group_qa
  // Extra angles may fail without invalidating the establishing image used by video generation.
  if (ref.pack_status === 'failed' || gate?.status === 'failed' || (gate?.hard_failures ?? []).length) return 'warning'
  if (!gate?.policy_version || gate.hard_gate_passed !== true || ['unverified', 'pending'].includes(gate.status || '')) return 'unverified'
  if (gate.status === 'warning' || (gate.warnings ?? gate.issues ?? []).length) return 'warning'
  return ref.pack_status === 'ready' ? 'passed' : 'unverified'
}

/** 主流程只回答一个问题：这张主图现在能否进入视频生成。 */
export function sceneUsability(scene: Scene, fitting: boolean): SceneUsability {
  const state = sceneAvailability(scene, fitting)
  return state === 'passed' || state === 'warning' ? 'available' : 'unavailable'
}
