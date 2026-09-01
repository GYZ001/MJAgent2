import { useEffect, useRef } from 'react'
import { refsBusyPollInterval } from '../lib/bibleAssets'

/**
 * 后台定妆照/场景图跑完的那一刻，把分集 payload 再拉一次（映射台/分镜台共用）。
 *
 * 为什么单独需要这一步：缩略图取的 current_portrait_image_url /
 * current_scene_image_url 是后端在 GET /episodes/{id} 时按当前人物谱/场景库现算
 * 的，字段挂在**分集** payload 上；而 refsBusyPollInterval 那轮轮询刷的是**项目**
 * payload（refs_status/scene_refs_status，占位四态要用）。两者是两条独立的数据
 * 通道——2026-08-31 接上项目轮询后，出图完成时占位文案会从"生成中"变回"待生成"，
 * 图却始终不出现，因为分集 payload 从进页面起就没再拉过（useEpisode 的轮询间隔
 * 是 0，只在剧本状态变化时才刷）。用户 2026-09-01 的原话：图像生成出来后，应该
 * 补充刷新这些历史占位图。
 *
 * 判据挂"这一轮出图任务从忙碌变回不忙碌"这个跳变，只在跳变那一次刷新，不是按
 * 固定间隔轮询分集（分集 payload 1MB+，见 App.tsx::episodeBusy 的注释）。
 */
export function useRefsSettledRefresh(
  project: { refs_status?: string; scene_refs_status?: string } | null,
  refresh: () => unknown,
): void {
  const busy = refsBusyPollInterval(project) > 0
  const wasBusy = useRef(busy)
  useEffect(() => {
    const settled = wasBusy.current && !busy
    wasBusy.current = busy
    if (settled) void refresh()
  }, [busy, refresh])
}
