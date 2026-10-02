import { useEffect } from 'react'
import type { MixStatus } from '../../api'

/**
 * 后台合成的收尾判定（2026-09-15：合成改为后台任务，POST 立即返回 202，成片台按 mix-status 轮询）。
 *
 * 判据挂在「是否还挂着一个未收尾的幂等键」上，不挂在「本次组件生命周期里是否亲眼看过
 * running=true 这一帧」——2026-10-01 生产事故：旧版本靠对比上一次/这一次轮询结果
 * （true→false 边沿）判断收尾，用户离开页面再回来时组件重新挂载、`previous` 状态
 * 清零，第一次轮询看到的已经是 concat_in_progress=false，边沿被永久错过，localStorage
 * 里的幂等键从此变成孤儿：下一次点击复用这个键，后端幂等缓存/CAS 冲突拦死请求，
 * 用户感觉"点了合成也没用"。`hasPendingKey` 来自调用方持久化的 localStorage 键是否
 * 还在，与是否曾经观测到中间状态无关，刷新页面、切换集数再切回来都能收尾。
 *
 * 2026-10-01 对抗式复查 #0/#1：上面这版判据只看 `concat_in_progress`/`concat_last_error`
 * ——两者都是进程内存（`app/media_exec/concat_state.py`），后端重启就清零；而
 * `hasPendingKey` 却是跨重启持久化的。若后台合成恰好在服务端重启时被腰斩
 * （`task_registry.stop_all()` 取消在途任务），新进程第一次轮询看到的就是
 * "不在执行 + 无错误 + 幂等键还在"——会被误判成本轮成功，弹出虚假的"合成完成"提示，
 * 但实际上旧成片原封未动。改为读 `concat_receipt`（persisted，来自
 * `concat_operation_receipts.status`，见 `app/media_exec/concat_receipt_status.py`）：
 * 只有它落到 `succeeded`/`failed` 终态才算"有定论"；停在 `running`（含缺失）说明
 * 这条记录"还没有定论"——多半是上一个进程的任务被腰斩，receipt 没能走到终态——
 * 既不能判"完成"（没有新成片），也不能让按钮永远锁死，归为第三态 `unknown`：
 * 解除忙碌锁、不清幂等键（同一个键后端已允许重新认领，见
 * `claim_concat_operation` 的 CAS），让用户自己核对或重新点击。
 */
export type ConcatOutcome = { finished: boolean; error: string | null; unknown?: boolean }

export function concatOutcome(next: MixStatus | null, hasPendingKey: boolean): ConcatOutcome {
  if (!next || next.concat_in_progress || !hasPendingKey) return { finished: false, error: null }
  const receiptStatus = next.concat_receipt?.status
  if (receiptStatus === 'succeeded') return { finished: true, error: null }
  if (receiptStatus === 'failed') {
    return { finished: true, error: next.concat_last_error || next.concat_receipt?.error || '合成失败，原因未知' }
  }
  return { finished: true, error: null, unknown: true }
}

/** 202 受理响应（后台合成已开始）与同步完成响应的区分。 */
export function isConcatAccepted(result: unknown): boolean {
  const record = result as { status?: string; concat_in_progress?: boolean } | null
  return Boolean(record && (record.status === 'accepted' || record.concat_in_progress === true))
}

/** 成片台接线：轮询看到「不在后台执行 + 仍挂着未清理的幂等键」时收尾；
 *  刷新页面回来、切集数再切回来都能接上，不依赖本次挂载期间亲眼见过 running=true。*/
export function useConcatWatch(
  polledMix: MixStatus | null,
  mixBusy: boolean,
  storageKey: string,
  handlers: { setMixBusy: (busy: boolean) => void; onFinished: (outcome: ConcatOutcome) => void },
): void {
  useEffect(() => {
    if (!polledMix) return
    if (polledMix.concat_in_progress && !mixBusy) handlers.setMixBusy(true)
    const outcome = concatOutcome(polledMix, Boolean(localStorage.getItem(storageKey)))
    if (outcome.finished) handlers.onFinished(outcome)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polledMix])
}
