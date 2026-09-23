import type { VideoModelSwitchResult } from '../api/storyboard/status'

/**
 * 切换视频模型成功后的提示文案。
 *
 * 切换接口只改 episodes.target_video_model，不碰已持久化的分镜段
 * prompt_text（app/domain/storyboard_ops/video_model.py::_dialect_stale_fields）。
 * 之前的响应类型只有 changed/cleared_videos/target_video_model，这条「分镜
 * 提示词还是旧方言写的」信息在界面上完全消失——BoardPage.tsx::submitVideoModel
 * 只读了前三个字段就 toast 了"已切换"，用户以为万事俱备，实际下一次生成会把
 * 语法不兼容的旧方言提示词发给新供应商。
 */
export function videoModelSwitchToast(
  result: VideoModelSwitchResult,
  videoModelLabel: (value: string) => string,
): string {
  const base = result.cleared_videos
    ? `已切换为 ${videoModelLabel(result.target_video_model)}，清空了 ${result.cleared_videos} 个旧方言视频`
    : `已切换为 ${videoModelLabel(result.target_video_model)}`
  return result.storyboard_dialect_stale && result.message
    ? `${base}；${result.message}`
    : base
}
