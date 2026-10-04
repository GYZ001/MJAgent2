import { useState } from 'react'
import { api, type PropAuditDoubt } from '../api'

/**
 * 道具卡复核「待你确认」列表：两次独立判定不一致 / 归属没有自己的卡 / 模型
 * 自述拿不准——这三类都不会被自动删除，交给人工看过原文再决定（CLAUDE.md
 * 「拦住用户时必须给出路」）。从 PropsPage.tsx 拆出以守住前端单文件 ≤300
 * 行的红线。
 */

/** 存疑原因的中文说明——纯函数，便于直接单测，不在 JSX 里散落三元表达式。 */
export function doubtReasonLabel(doubt: PropAuditDoubt): string {
  if (doubt.doubt_type === 'owner_without_card') {
    return `判定属于「${doubt.owner || '另一件物件'}」，但本项目没有它的道具卡，删掉会让这条外观信息无处可寄`
  }
  if (doubt.doubt_type === 'model_self_doubt') {
    return `模型自己也拿不准：${doubt.reason_a || doubt.reason_b || '未说明理由'}`
  }
  if (doubt.doubt_type === 'invalid_category') {
    return `模型判定该删，但给出的类别不是约定的三类之一，无法自动采信，需要人工核对`
  }
  if (doubt.doubt_type === 'owner_not_cooccurring') {
    return `判定属于「${doubt.owner || '另一件物件'}」，但这件道具与它在分镜里从未同时出现过，可能不是同一件实物，需要人工确认`
  }
  if (doubt.doubt_type === 'keep_fragment_mismatch') {
    return `两次判定都认为这句该删，但各自要保留的片段不一致（一次保留：${doubt.keep_fragment_a || '（整句删除）'}；另一次保留：${doubt.keep_fragment_b || '（整句删除）'}），需要人工确认该保留哪一部分——下面的「确认删除」会把这句整句删除、不会自动采纳其中任一片段，拿不准先点「保留」`
  }
  return `两次独立判定不一致（一次：${doubt.reason_a || '未判删'}；另一次：${doubt.reason_b || '未判删'}）`
}

export function doubtSubjectLabel(doubt: PropAuditDoubt): string {
  return doubt.kind === 'alias' ? `别名「${doubt.alias}」` : `外观「${doubt.text}」`
}

interface Props {
  projectId: string
  propName: string
  doubts: PropAuditDoubt[]
  onResolved: (doubtKey: string) => void
  toast: (message: string) => void
}

export default function PropAuditDoubts({ projectId, propName, doubts, onResolved, toast }: Props) {
  const [busyKey, setBusyKey] = useState<string | null>(null)
  if (!doubts.length) return null

  const resolve = async (doubtKey: string, action: 'confirm' | 'keep') => {
    if (busyKey) return
    setBusyKey(doubtKey)
    try {
      const call = action === 'confirm' ? api.confirmPropAuditDoubt : api.keepPropAuditDoubt
      await call(projectId, propName, doubtKey)
      toast(action === 'confirm' ? '已确认删除，参考图已更新' : '已保留，这条不会再提示')
      onResolved(doubtKey)
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyKey(null)
    }
  }

  return (
    <div className="prop-audit-doubts" role="group" aria-label={`${propName} 待确认的复核存疑`}>
      <small className="hint"><b>待你确认（{doubts.length}）：</b></small>
      {doubts.map(doubt => {
        const doubtKey = doubt.kind === 'alias' ? `alias:${doubt.alias}` : `clause:${doubt.text}`
        const busy = busyKey === doubtKey
        return (
          <div key={doubtKey} className="prop-audit-doubt-item">
            <small className="hint">{doubtSubjectLabel(doubt)}：{doubtReasonLabel(doubt)}</small>
            <div>
              <button type="button" className="btn small" disabled={busy}
                onClick={() => void resolve(doubtKey, 'confirm')}>
                {busy ? '处理中…' : '确认删除'}
              </button>
              <button type="button" className="btn small ghost" disabled={busy}
                onClick={() => void resolve(doubtKey, 'keep')}>保留</button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
