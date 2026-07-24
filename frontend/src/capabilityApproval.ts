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
    }
  }
}

type Resolver = (approved: boolean) => void

let pending: { payload: WaitingApprovalPayload; resolve: Resolver } | null = null
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

export function requestCapabilityApproval(payload: WaitingApprovalPayload): Promise<boolean> {
  return new Promise(resolve => {
    pending = { payload, resolve }
    notify()
  })
}

export function resolveCapabilityApproval(approved: boolean) {
  if (!pending) return
  const { resolve } = pending
  pending = null
  notify()
  resolve(approved)
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
  for (const w of pf?.warnings ?? []) lines.push(w)
  return lines
}
