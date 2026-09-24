import type { AspectRatioImpact } from '../api'

const ASPECT_RATIO_LABEL: Record<string, string> = {
  '9:16': '9:16（竖屏）',
  '16:9': '16:9（横屏）',
}

export function aspectRatioLabel(value: string): string {
  return ASPECT_RATIO_LABEL[value] ?? value
}

export type AspectRatioImpactDialogContent = {
  title: string
  summary: string
  message: string
  details: string[]
  danger: boolean
}

/**
 * 画幅切换前确认弹窗的文案（2026-09-23 用户拍板）：如实列出已采用视频与场景图
 * 受影响的数量；impact 接口 404/失败时（impact 为 null 且 impactError 非空）
 * 照样允许切换，只是把"统计不到"如实说清楚——不能因为统计接口挂了就拦死整个
 * 切换（CLAUDE.md「拦住用户时必须给出路」）。纯函数，不碰组件状态，便于独立测试
 * 「mismatched=null」与「impact 失败」这两种形态而不必渲染整个面板。
 */
export function buildAspectRatioImpactDialog(
  target: string,
  impact: AspectRatioImpact | null,
  impactError: string | null,
): AspectRatioImpactDialogContent {
  const title = `切换画幅为 ${aspectRatioLabel(target)}？`
  if (!impact) {
    return {
      title,
      summary: impactError ? '暂时无法统计受影响的视频数' : '正在核对受影响的视频与场景图…',
      message: impactError
        ? `统计接口未能返回结果（${impactError}）；仍可以切换画幅，已采用的视频若是旧画幅，会在成片合成时被等比放大裁切进新画布，可能裁掉部分画面。`
        : '仍可以切换画幅；已采用的视频若是旧画幅，会在成片合成时被等比放大裁切进新画布，可能裁掉部分画面。',
      details: ['之后新生成的视频和场景图按新画幅', '需要的话可以到生成台重新生成'],
      danger: false,
    }
  }
  const sceneLine = impact.scene_images_mismatched == null
    ? `场景图 ${impact.scene_images_total} 张，画幅未记录`
    : `场景图 ${impact.scene_images_mismatched} 张为旧画幅（共 ${impact.scene_images_total} 张）`
  return {
    title,
    summary: `已采用的 ${impact.adopted_videos_mismatched} 个视频仍是旧画幅，成片合成时会等比放大裁切，可能裁掉画面`,
    message: `当前项目共采用 ${impact.adopted_videos_total} 个视频（当前画幅 ${aspectRatioLabel(impact.current_aspect_ratio)}）。`,
    details: [sceneLine, '之后新生成的按新画幅', '需要的话到生成台重新生成'],
    danger: impact.adopted_videos_mismatched > 0,
  }
}
