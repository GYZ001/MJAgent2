// StoryboardPackSegment.target_model 用的是冻结契约自己的词表（"seedance_2" |
// "minimax_h3"，见 app/production/storyboard_pack.py _dialect_for_target_video_model），
// 与上面 VIDEO_MODEL_OPTIONS 的供应商 key（"hiagent" | "minimax_h3"）不是同一套
// 词表——两者恰好共享 "minimax_h3" 这一个值是巧合，不能假设通用，不能复用
// videoModelLabel 给段落记录查标签。
const STORYBOARD_PACK_TARGET_MODEL_LABELS: Record<string, string> = {
  seedance_2: 'Seedance 2.0',
  minimax_h3: 'MiniMax H3',
}
export function storyboardPackTargetModelLabel(targetModel: string): string {
  return STORYBOARD_PACK_TARGET_MODEL_LABELS[targetModel] ?? targetModel
}

