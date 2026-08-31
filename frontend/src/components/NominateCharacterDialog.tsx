import { useId, useState } from 'react'
import { api, ApiError } from '../api'
import { useFocusTrap } from '../hooks/useFocusTrap'

type NominateResult = {
  status: string
  label?: string
  message?: string
  owner?: string
  owners?: string[]
  alias_registered?: boolean
  alias_reason?: string | null
  reason?: string
}

const REJECTED_STATUSES = new Set([
  'skipped_minor', 'card_incomplete', 'skipped_not_person', 'error',
])

function NominateDialog({
  projectId,
  onClose,
  onFocusCharacter,
}: {
  projectId: string
  onClose: () => void
  onFocusCharacter: (name: string) => void
}) {
  const titleId = useId()
  const trapRef = useFocusTrap(true, onClose)
  const [label, setLabel] = useState('')
  const [fromEpisodeNo, setFromEpisodeNo] = useState('1')
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<NominateResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    const trimmed = label.trim()
    if (!trimmed || submitting) return
    setSubmitting(true)
    setError(null)
    setResult(null)
    try {
      const episodeNo = Number(fromEpisodeNo)
      const outcome = await api.nominateCharacter(
        projectId, trimmed, Number.isFinite(episodeNo) && episodeNo > 0 ? episodeNo : undefined,
      )
      setResult(outcome as NominateResult)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '提名失败，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  const owner = result?.owner
  const rejected = !!result && REJECTED_STATUSES.has(result.status)

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={event => {
        if (event.currentTarget === event.target) onClose()
      }}
    >
      <section ref={trapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>提名角色</h3>
        <p>没被自动选上的角色，在这里手动提名——系统仍按原文证据检索、人物硬闸、外观生成的既有判据处理，不是让你手写一张卡。</p>
        <div className="review-impact">
          <label>
            原文称呼
            <input
              type="text" value={label} disabled={submitting}
              onChange={event => setLabel(event.target.value)}
              placeholder="例如：小胖子、靠山宗掌门"
            />
          </label>
          <label>
            从第几集开始检索原文（默认第 1 集，称呼在更靠后章节才出现时请调整）
            <input
              type="number" min={1} value={fromEpisodeNo} disabled={submitting}
              onChange={event => setFromEpisodeNo(event.target.value)}
            />
          </label>
        </div>
        {error && <p className="review-impact danger">{error}</p>}
        {result && (
          <div className={`review-impact${rejected ? ' danger' : ''}`}>
            <p>{result.message || result.reason || '已处理'}</p>
            {result.status === 'exists' && owner && (
              <button type="button" className="btn" onClick={() => onFocusCharacter(owner)}>
                查看「{owner}」
              </button>
            )}
            {result.status === 'added' && result.label && (
              <button type="button" className="btn" onClick={() => onFocusCharacter(result.label!)}>
                查看「{result.label}」
              </button>
            )}
          </div>
        )}
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose}>关闭</button>
          <button type="button" className="btn primary" disabled={!label.trim() || submitting} onClick={submit}>
            {submitting ? '提交中…' : '提名'}
          </button>
        </div>
      </section>
    </div>
  )
}

/** 人物谱页"角色没被选上时"的手动提名入口：工具栏按钮 + 弹窗一起挂载，
 * BiblePage.tsx（1934 行棘轮基线）只需要挂一行，判据全部在 NominateDialog
 * 内部——提交一个原文称呼，后端按既有建卡判据处理（命中已有角色就登记别名，
 * 冲突就 fail closed，都没命中就走建卡流程），被拒时原样显示后端给的真实
 * 原因，不在前端改写成笼统话术。 */
export default function NominateCharacterEntry({
  projectId,
  onFocusCharacter,
}: {
  projectId: string
  onFocusCharacter: (name: string) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="btn" onClick={() => setOpen(true)}>提名没被选上的角色</button>
      {open && (
        <NominateDialog
          projectId={projectId}
          onClose={() => setOpen(false)}
          onFocusCharacter={name => { onFocusCharacter(name); setOpen(false) }}
        />
      )}
    </>
  )
}
