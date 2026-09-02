/** 面向普通用户的中文状态映射；未知值不伪装成功。 */
const STATUS_MAP: Record<string, string> = {
  ready: '已就绪',
  approved: '已批准',
  validated: '已校验',
  candidate: '待验证',
  generating: '生成中',
  qa_pending: '质检中',
  failed: '失败',
  legacy_partial: '历史资料不完整',
  unverified: '未验证',
  running: '运行中',
  idle: '空闲',
  warning: '有问题',
  stale: '已失效',
  rejected: '已拒绝',
  pending: '等待中',
  succeeded: '已完成',
  cancelled: '已取消',
  partial: '部分成功',
  accepted: '已受理',
  stopped: '已停止',
  pending_review: '待审核',
  waiting_provider: '等待生成服务',
  waiting_retry: '等待重试',
  needs_revision: '待修订',
  ineligible: '不可用',
  available: '可用',
  active: '使用中',
  archived: '已归档',
  completed: '已完成',
  open: '待处理',
  in_progress: '处理中',
  resolved: '已解决',
  hard_failed: '未通过必检项',
  passed: '已通过',
  missing: '缺失',
  hard_failure: '未通过必检项',
  interrupted: '任务中断',
  T0: '仅生成，尚未验证',
  T1: '结构已校验',
  T2: '规则已验证',
  T3: '独立评估通过',
  T4: '人工已确认',
  T5: '交付已验证',
}

const ARTIFACT_TYPE_MAP: Record<string, string> = {
  character: '人物',
  character_bible: '人物谱',
  character_portrait: '人物定妆照',
  character_references: '人物参考图',
  scene_bible: '场景设定',
  scene: '场景',
  scene_reference: '场景参考图',
  scene_references: '场景参考图',
  episode_screenplay: '剧本',
  screenplay: '剧本',
  storyboard: '分镜',
  reference_image: '镜头参考图',
  shot_video: '镜头视频',
  video: '视频',
  delivery_package: '交付包',
}

export function statusLabel(raw: string | null | undefined): string {
  if (!raw) return '未知状态'
  if (STATUS_MAP[raw]) return STATUS_MAP[raw]
  if (STATUS_MAP[raw.toUpperCase()]) return STATUS_MAP[raw.toUpperCase()]
  if (STATUS_MAP[raw.toLowerCase()]) return STATUS_MAP[raw.toLowerCase()]
  return '未知状态'
}

export function statusTitle(raw: string | null | undefined): string | undefined {
  if (!raw) return undefined
  const label = statusLabel(raw)
  return label === '未知状态' ? `未识别的系统状态码：${raw}` : `系统状态码：${raw}`
}

export function artifactTypeLabel(raw: string | null | undefined): string {
  if (!raw) return '其他产物'
  return ARTIFACT_TYPE_MAP[raw] || (/[\u3400-\u9fff]/.test(raw) ? raw : '其他产物')
}

export function artifactTypeTitle(raw: string | null | undefined): string | undefined {
  if (!raw) return undefined
  return `系统产物类型：${raw}`
}

export type PrepStepStatus = 'idle' | 'running' | 'problem' | 'done'

export function prepStepLabel(status: PrepStepStatus): string {
  switch (status) {
    case 'running': return '进行中'
    case 'problem': return '有问题'
    case 'done': return '已完成'
    default: return '未开始'
  }
}
