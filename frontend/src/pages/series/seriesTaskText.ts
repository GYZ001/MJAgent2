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

/** 进度定位文案：正在跑第几集第几步 / 排第几位 / 终态归类，列表与详情共用。
 *  merge（合成连播成片）单独判：orchestrator.py 在全部集跑完后把
 *  current_episode_no 置 null、current_stage 置 'merge'（见 orchestrator.py
 *  102-113 行）——不加这一支的话会落进最后的 return '尚未开始'，跟同屏的
 *  「执行中」状态灯、接近 100% 的步骤计数正面矛盾（P1-7）。 */
export function seriesTaskProgressLabel(
  task: Pick<SeriesTaskSummary, 'status' | 'current_episode_no' | 'current_stage' | 'queue_position'>
    & { running_episode_nos?: number[] },
): string {
  if (task.status === 'running' && task.current_stage === 'merge') {
    return SERIES_STAGE_LABEL.merge
  }
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

/** 队列状态条文案：连续失败停队 > 手动暂停 > 正在跑 > 空闲，优先级从高到低。
 *  2026-09-04 起同项目可并行跑多个任务（见 app/domain/series_ops/queue.py
 *  queue_concurrency，缺省 3）；running_task_ids 是后端已下发的并行任务 id
 *  列表，缺失（老响应）时退回只看 running_task_id 单值，行为与此前一致。 */
export function seriesQueueStatusText(queue: SeriesQueueState): string {
  if (queue.stop_reason) return `已连续失败自动暂停：${queue.stop_reason}`
  if (queue.paused) return '队列已暂停'
  const runningIds = queue.running_task_ids ?? (queue.running_task_id ? [queue.running_task_id] : [])
  if (runningIds.length > 0) {
    const running = runningIds.length > 1
      ? `正在并行执行 ${runningIds.length} 个任务`
      : `正在执行 ${runningIds[0]}`
    return queue.queued_count > 0 ? `${running}，还有 ${queue.queued_count} 个排队` : running
  }
  return queue.queued_count > 0 ? `队列中还有 ${queue.queued_count} 个待执行` : '队列空闲'
}

/** 批量执行提示：真实并行数取自 queue.concurrency（设置台配置与账号配额上限
 *  取更紧的一个，后端已算好生效值——见 queue.py::queue_snapshot 的注释「界面
 *  显示的并行数必须是真正生效的那个值」）。旧文案「按勾选顺序串行执行，一次
 *  只跑一个任务」与实际默认并行 3 不符（P1-6）。concurrency 缺失（老响应）或
 *  ≤1 时退回不宣称具体数字的中性表述，不编造并行数。 */
export function seriesBatchEnqueueHint(concurrency: number | undefined): string {
  const parallelText = concurrency != null && concurrency > 1
    ? `最多同时执行 ${concurrency} 个任务，其余按勾选顺序排队`
    : '按勾选顺序排队执行'
  return `${parallelText}。已完成的任务会被跳过——它们的成片已经在盘上；`
    + '要重做请先去成片台/生成台重跑对应的集，成片一变这里就会重新判为可执行。'
}

/** 页头副标题里的并行提示（<span className="sub"> 放不下 seriesBatchEnqueueHint
 *  那句长文案，另给一个短语）；判据与 seriesBatchEnqueueHint 一致——concurrency
 *  缺失或 ≤1 时不编造具体数字。旧副标题「勾选后批量串行执行」与实际默认并行 3
 *  不符（P1-6 续，SeriesPage.tsx 页头 + SeriesTaskBar.tsx 按钮两处同一措辞）。 */
export function seriesConcurrencyPhrase(concurrency: number | undefined): string {
  return concurrency != null && concurrency > 1
    ? `最多同时执行 ${concurrency} 个任务`
    : '按顺序执行'
}

export interface SeriesTaskStartAvailability {
  disabled: boolean
  reason: string | null
}

/** 单任务「开始」按钮可用性（P2-5）：运行中/排队中/区间缺集时禁用（沿用既有
 *  判据）；新增一档——已完成且成片未过期时同样禁用：这种情况点击后端会判定
 *  skipped 静默返回 200（SeriesTaskEnqueueResult.skipped），此前按钮不禁用、
 *  用户点了却没有任何反馈。film_stale=true 时成片已过期，仍允许重新执行。 */
export function seriesTaskStartAvailability(
  task: Pick<SeriesTaskSummary, 'status' | 'missing_episode_nos' | 'film_stale'>,
): SeriesTaskStartAvailability {
  if (task.status === 'running') return { disabled: true, reason: '任务正在执行中' }
  if (task.status === 'queued') return { disabled: true, reason: '任务已在队列中排队' }
  if (task.missing_episode_nos.length > 0) return { disabled: true, reason: '区间内缺集，需先补齐分集规划' }
  if (task.status === 'succeeded' && !task.film_stale) {
    return { disabled: true, reason: '已完成且成片未过期，无需重新执行' }
  }
  return { disabled: false, reason: null }
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

/** 入队响应里的 skipped 列表 → 中文提示（P2-5 续）：此前批量/单任务入队接口
 *  对已完成且未过期的任务返回 skipped，静默 200，界面从不读这个字段，用户点了
 *  却什么反馈都看不到。空数组返回 null，调用方据此判断要不要弹 toast；多个
 *  任务命中同一原因（如都是"已完成，成片未过期"）时去重，不重复念叨同一句话。 */
export function seriesSkippedToastMessage(
  skipped: { task_id: string; reason: string }[],
): string | null {
  if (skipped.length === 0) return null
  const reasons = Array.from(new Set(skipped.map(s => s.reason)))
  return `已跳过 ${skipped.length} 个任务：${reasons.join('；')}`
}
