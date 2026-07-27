/** 面向普通用户的中文状态映射；未知值不伪装成功。 */
const STATUS_MAP: Record<string, string> = {
  ready: '已就绪',
  approved: '已批准',
  validated: '已校验',
  generating: '生成中',
  qa_pending: '验收中',
  failed: '失败',
  legacy_partial: '历史半包',
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
  hard_failed: '硬失败',
  passed: '已通过',
  missing: '缺失',
  hard_failure: '硬门禁失败',
  interrupted: '任务中断',
  T0: '信任 T0',
  T1: '信任 T1',
  T2: '信任 T2',
  T3: '信任 T3',
  T4: '信任 T4',
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
  return label === '未知状态' ? `原始值：${raw}` : `技术值：${raw}`
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
