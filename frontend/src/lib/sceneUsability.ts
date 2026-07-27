import type { Scene } from '../api'

export type SceneAvailability = 'generating' | 'passed' | 'warning' | 'failed' | 'unverified' | 'missing'
export type SceneUsability = 'available' | 'unavailable'

const DEFAULT_SCENE_REQUIRED_VIEWS = ['establishing', 'reverse_angle']

export function sceneAvailability(scene: Scene, fitting: boolean): SceneAvailability {
  if (fitting) return 'generating'
  const ref = activeSceneRef(scene)
  if (!primaryImageUrl(scene, ref)) return 'missing'
  if (!ref) return 'unverified'
  if (ref && !requiredViewsAvailable(scene, ref)) return 'missing'
  if (ref.pack_status === 'generating' || ref.pack_status === 'qa_pending') return 'generating'
  const primaryQa = ref.qa
  const gate = ref.group_qa
  if (
    primaryQa?.status === 'failed'
    || (primaryQa?.hard_failures ?? []).length
    || ref.pack_status === 'failed'
    || gate?.status === 'failed'
    || (gate?.hard_failures ?? []).length
  ) return 'warning'
  // Extra angles may fail without invalidating the establishing image used by video generation.
  if (!gate?.policy_version || ['unverified', 'pending'].includes(gate.status || '')) return 'unverified'
  if (gate.status === 'warning' || (gate.warnings ?? gate.issues ?? []).length) return 'warning'
  return ref.pack_status === 'ready' ? 'passed' : 'unverified'
}

/** 主流程只回答一个问题：这张主图现在能否进入视频生成。 */
export function sceneUsability(scene: Scene, fitting: boolean): SceneUsability {
  void fitting
  const ref = activeSceneRef(scene)
  if (!primaryImageUrl(scene, ref)) return 'unavailable'
  if (ref && !requiredViewsAvailable(scene, ref)) return 'unavailable'
  return 'available'
}

function activeSceneRef(scene: Scene) {
  const refs = scene.scene_refs ?? []
  return refs.find(item => item.ep_end == null)
    ?? refs.find(item => item.image_url === scene.ref_image_url)
    ?? refs.at(-1)
}

function primaryImageUrl(scene: Scene, ref: ReturnType<typeof activeSceneRef>): string | null {
  return ref?.image_url || scene.ref_image_url || null
}

function requiredViewsAvailable(scene: Scene, ref: NonNullable<ReturnType<typeof activeSceneRef>>): boolean {
  const required = [
    ...(ref.group_qa?.required_views ?? []),
    ...(!(ref.group_qa?.required_views ?? []).length ? (scene.required_views ?? []) : []),
  ].filter(Boolean)
  const roles = required.length
    ? required
    : (ref.views?.length ? DEFAULT_SCENE_REQUIRED_VIEWS : [])
  if (!roles.length) return true
  const present = new Set(
    (ref.views ?? [])
      .filter(view => !!view.image_url)
      .map(view => view.view_role)
      .filter(Boolean),
  )
  if (primaryImageUrl(scene, ref)) present.add('establishing')
  return roles.every(role => present.has(role))
}
