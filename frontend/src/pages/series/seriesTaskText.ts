// 纯函数集合：连播任务台的文案映射、判据与选择集运算。不含 JSX，供
// SeriesPage.test.ts 直接单测，也供各渲染组件复用，避免同一份判据散落两份。

import { SERIES_MAX_SPAN } from '../../api'
import type {
  SeriesQueueState,
  SeriesTaskPlanResponse,
  SeriesTaskStatus,
  SeriesTaskSummary,
} from '../../api'
import { SERIES_STAGE_LABEL } from './SeriesProgressBoard'

/** 任务标题兜底：空串（未命名）按「第 X-Y 集」展示，与后端契约的默认标题规则一致。 */
export function seriesTaskTitle(task: { title: string; episode_from: number; episode_to: number }): string {
  return task.title || `第 ${task.episode_from}-${task.episode_to} 集`
}

const STATUS_LABEL: Record<SeriesTaskStatus, string> = {
  idle: '未开始',
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const STATUS_TONE: Record<SeriesTaskStatus, 'grey' | 'gold' | 'green' | 'red'> = {
  idle: 'grey',
  queued: 'gold',
  running: 'gold',
  succeeded: 'green',
  failed: 'red',
  cancelled: 'grey',
}

export function seriesTaskStatusLabel(status: SeriesTaskStatus): string {
  return STATUS_LABEL[status] ?? '状态未知'
}

export function seriesTaskStatusTone(status: SeriesTaskStatus): 'grey' | 'gold' | 'green' | 'red' {
  return STATUS_TONE[status] ?? 'grey'
}

/** 进度百分比：steps_total<=0（进度树尚未展开）按 0 处理，不产出 NaN/Infinity；
 *  结果夹在 [0,100] 之间，竞态下 done 短暂超过 total 也不展示畸形数字。 */
export function seriesTaskProgressPercent(stepsDone: number, stepsTotal: number): number {
  if (stepsTotal <= 0) return 0
  return Math.max(0, Math.min(100, Math.round((stepsDone / stepsTotal) * 100)))
}

/** 进度定位文案：正在跑第几集第几步 / 排第几位 / 终态归类，列表与详情共用。 */
export function seriesTaskProgressLabel(
  task: Pick<SeriesTaskSummary, 'status' | 'current_episode_no' | 'current_stage' | 'queue_position'>
    & { running_episode_nos?: number[] },
): string {
  if (task.status === 'running' && task.current_episode_no != null) {
    const stage = task.current_stage ? SERIES_STAGE_LABEL[task.current_stage] : '处理中'
    const running = task.running_episode_nos ?? []
    // 多集并行时列出全部在跑的集；步骤只标最靠前那集的（进度树里 current_* 就是它）。
    if (running.length > 1) return `第 ${running.join('、')} 集并行 · 第 ${task.current_episode_no} 集${stage}`
    return `第 ${task.current_episode_no} 集 · ${stage}`
  }
  if (task.status === 'queued') {
    return task.queue_position != null ? `排队中（第 ${task.queue_position} 位）` : '排队中'
  }
  if (task.status === 'succeeded') return '已完成'
  if (task.status === 'cancelled') return '已取消'
  if (task.status === 'failed') return '有失败的集（已跳过，其余已跑完）'
  return '尚未开始'
}

export interface SeriesBatchAvailability {
  enqueueDisabled: boolean
  cancelDisabled: boolean
  exportDisabled: boolean
}

/** 批量操作按钮可用性：无选中一律禁用；取消只在选中里有排队/执行中任务时才有
 *  意义；导出只在选中里至少有一个已出片时才有意义（其余会被后端计入 skipped）。 */
export function seriesBatchAvailability(selected: SeriesTaskSummary[]): SeriesBatchAvailability {
  const hasSelection = selected.length > 0
  const hasActive = selected.some(t => t.status === 'queued' || t.status === 'running')
  const hasFilm = selected.some(t => t.film != null)
  return {
    enqueueDisabled: !hasSelection,
    cancelDisabled: !hasSelection || !hasActive,
    exportDisabled: !hasSelection || !hasFilm,
  }
}

/** 队列状态条文案：连续失败停队 > 手动暂停 > 正在跑 > 空闲，优先级从高到低。 */
export function seriesQueueStatusText(queue: SeriesQueueState): string {
  if (queue.stop_reason) return `已连续失败自动暂停：${queue.stop_reason}`
  if (queue.paused) return '队列已暂停'
  if (queue.running_task_id) {
    return queue.queued_count > 0
      ? `正在执行 ${queue.running_task_id}，还有 ${queue.queued_count} 个排队`
      : `正在执行 ${queue.running_task_id}`
  }
  return queue.queued_count > 0 ? `队列中还有 ${queue.queued_count} 个待执行` : '队列空闲'
}

export function validateGroupSize(groupSize: number): { ok: boolean; reason?: string } {
  if (!Number.isInteger(groupSize)) return { ok: false, reason: '每组集数必须是整数' }
  if (groupSize < 1 || groupSize > SERIES_MAX_SPAN) {
    return { ok: false, reason: `每组集数必须在 1–${SERIES_MAX_SPAN} 之间` }
  }
  return { ok: true }
}

export function seriesPlanSummaryText(plan: SeriesTaskPlanResponse): string {
  return `将新建 ${plan.new_groups} 组、已存在 ${plan.existing_groups} 组（共 ${plan.total_groups} 组）`
}

/** 跨页勾选：单个切换。 */
export function toggleTaskSelection(selected: Set<string>, taskId: string): Set<string> {
  const next = new Set(selected)
  if (next.has(taskId)) next.delete(taskId)
  else next.add(taskId)
  return next
}

/** 跨页勾选：批量加入（本页全选）。 */
export function selectTasks(selected: Set<string>, taskIds: string[]): Set<string> {
  const next = new Set(selected)
  taskIds.forEach(id => next.add(id))
  return next
}

/** 跨页勾选：批量移除（本页取消全选 / 操作完成后清空已处理项）。 */
export function deselectTasks(selected: Set<string>, taskIds: string[]): Set<string> {
  const next = new Set(selected)
  taskIds.forEach(id => next.delete(id))
  return next
}

export function formatFilmSize(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(gb < 10 ? 2 : 1)} GB`
  const mb = bytes / 1024 ** 2
  if (mb >= 1) return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`
  const kb = bytes / 1024
  if (kb >= 1) return `${kb.toFixed(0)} KB`
  return `${Math.round(bytes)} B`
}

/** 导出面板总量固定用 GB 呈现（契约要求的文案口径：「共 N 个文件、合计 X GB」），
 *  不像 formatFilmSize 那样按量级自适应单位。 */
export function formatGB(bytes: number): string {
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}
