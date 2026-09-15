import { useEffect, useRef } from 'react'
import type { MixStatus } from '../../api'

/**
 * 后台合成的收尾判定（2026-09-15：合成改为后台任务，POST 立即返回 202，成片台按 mix-status 轮询）。
 * 上一次轮询 concat_in_progress 为真、这一次为假 → 本轮合成结束；有 concat_last_error 就是失败。
 */
export type ConcatOutcome = { finished: boolean; error: string | null }

export function concatOutcome(previous: MixStatus | null, next: MixStatus | null): ConcatOutcome {
  if (!next) return { finished: false, error: null }
  const wasRunning = Boolean(previous?.concat_in_progress)
  const nowRunning = Boolean(next.concat_in_progress)
  if (!wasRunning || nowRunning) return { finished: false, error: null }
  return { finished: true, error: next.concat_last_error ? String(next.concat_last_error) : null }
}

/** 202 受理响应（后台合成已开始）与同步完成响应的区分。 */
export function isConcatAccepted(result: unknown): boolean {
  const record = result as { status?: string; concat_in_progress?: boolean } | null
  return Boolean(record && (record.status === 'accepted' || record.concat_in_progress === true))
}

/** 成片台接线：轮询到「进行中 → 结束」时收尾；刷新页面回来时接上进行中的合成。 */
export function useConcatWatch(
  polledMix: MixStatus | null,
  mixBusy: boolean,
  handlers: { setMixBusy: (busy: boolean) => void; onFinished: (error: string | null) => void },
): void {
  const previous = useRef<MixStatus | null>(null)
  useEffect(() => {
    if (!polledMix) return
    const outcome = concatOutcome(previous.current, polledMix)
    previous.current = polledMix
    if (polledMix.concat_in_progress && !mixBusy) handlers.setMixBusy(true)
    if (outcome.finished) handlers.onFinished(outcome.error)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polledMix])
}
