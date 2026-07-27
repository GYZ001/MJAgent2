/** 服务端 Capability 批准：展示 Impact 后由用户确认（PRD §8）。 */

export type WaitingApprovalPayload = {
  status: 'waiting_approval'
  summary?: string
  command?: string
  approval_id?: string
  approval_token?: string
  expires_at?: number
  preflight?: {
    summary?: string
    risk?: string
    estimated_cost_cny?: number | null
    warnings?: string[]
    affected?: {
      shot_count?: number
      invalidated_artifacts?: number
      episodes?: string[]
      projects?: string[]
      packages?: string[]
      extra?: {
        diff?: string[]
        active_runs?: number
        reference_images?: number
        media_versions?: number
        rerun_scope?: string
        stop_downstream_first?: boolean
        unchanged?: boolean
      }
    }
  }
}

type Resolver = (approved: boolean) => void

type PendingApproval = { payload: WaitingApprovalPayload; resolve: Resolver }

let pending: PendingApproval | null = null
const queue: PendingApproval[] = []
const listeners = new Set<() => void>()

export function subscribeApprovalPrompt(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function getPendingApproval() {
  return pending?.payload ?? null
}

function notify() {
  listeners.forEach(fn => fn())
}

function promoteNext() {
  pending = queue.shift() ?? null
  notify()
}

export function requestCapabilityApproval(payload: WaitingApprovalPayload): Promise<boolean> {
  return new Promise(resolve => {
    const item: PendingApproval = { payload, resolve }
    if (!pending) {
      pending = item
      notify()
    } else {
      queue.push(item)
    }
  })
}

export function resolveCapabilityApproval(approved: boolean) {
  if (!pending) return
  const { resolve } = pending
  pending = null
  resolve(approved)
  promoteNext()
}

export function describeImpact(payload: WaitingApprovalPayload): string[] {
  const pf = payload.preflight
  const lines: string[] = []
  lines.push(pf?.summary || payload.summary || '需要确认后才能执行')
  if (pf?.risk) lines.push(`风险等级：${pf.risk}`)
  if (typeof pf?.estimated_cost_cny === 'number') {
    lines.push(`预计费用：¥${pf.estimated_cost_cny.toFixed(2)}`)
  }
  const affected = pf?.affected
  if (affected?.shot_count) lines.push(`影响镜头数：${affected.shot_count}`)
  if (affected?.invalidated_artifacts) {
    lines.push(`预计失效产物：${affected.invalidated_artifacts}`)
  }
  if (affected?.packages?.length) lines.push(`涉及交付包：${affected.packages.join(', ')}`)
  const extra = affected?.extra
  if (extra?.diff?.length) lines.push(`变更字段：${extra.diff.join('、')}`)
  if (extra?.active_runs) lines.push(`需先停止的下游运行：${extra.active_runs} 个`)
  if (extra?.reference_images) lines.push(`受影响参考图：${extra.reference_images} 个`)
  if (extra?.media_versions) lines.push(`受影响媒体版本：${extra.media_versions} 个`)
  if (extra?.rerun_scope) lines.push(`重跑范围：${extra.rerun_scope}`)
  for (const w of pf?.warnings ?? []) lines.push(w)
  return lines
}
