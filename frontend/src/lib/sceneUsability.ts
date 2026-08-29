import type { Scene } from '../api'

export type SceneUsability = 'available' | 'unavailable'

const DEFAULT_SCENE_REQUIRED_VIEWS = ['establishing', 'reverse_angle']

/** 主流程只回答一个问题：这张主图现在能否进入视频生成。只看文件是否存在（技术产物），不再有质检评分。 */
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
  const required = (scene.required_views ?? []).filter(Boolean)
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
