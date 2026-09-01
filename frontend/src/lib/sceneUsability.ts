import type { Scene } from '../api'

export type SceneUsability = 'available' | 'unavailable'

/**
 * 这个场景现在能不能进视频生成——只看主图有没有（用户拍板 2026-09-01：有图就是可用）。
 *
 * 曾经还要求 establishing+reverse_angle 两个必需视角齐全，于是"主图明明在、只差侧
 * 视角"的场景在场景库显示"不可用"，出图流程也把它当成"还没有图"反复重烧主图
 * （真实事故：赵国大青山山顶堆到 8 张候选、界面一直红着）。后端同一天用同一判据
 * （app.multiview.scene_primary_is_usable，pack 版判据已退场），两侧不留第二套。
 */
export function sceneUsability(scene: Scene, fitting: boolean): SceneUsability {
  void fitting
  return primaryImageUrl(scene) ? 'available' : 'unavailable'
}

function primaryImageUrl(scene: Scene): string | null {
  const refs = scene.scene_refs ?? []
  const ref = refs.find(item => item.ep_end == null)
    ?? refs.find(item => item.image_url === scene.ref_image_url)
    ?? refs.at(-1)
  return ref?.image_url || scene.ref_image_url || null
}
