import { useEffect, useState } from 'react'
import { api, CharacterPortraitCandidate, Portrait } from '../api'
import { useNav } from '../App'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { statusLabel, statusTitle } from '../lib/statusLabels'

const VIEW_LABELS: Record<string, string> = {
  front_full: '正面全身',
  three_quarter: '3/4 面',
  profile: '侧面',
  back_full: '背面全身',
  face_closeup: '面部特写',
}

export default function CharacterQaPanel({
  projectId,
  characterName,
  portrait,
  onChanged,
  onClose,
}: {
  projectId: string
  characterName: string
  portrait: Portrait
  onChanged?: () => void
  onClose: () => void
}) {
  const { toast } = useNav()
  const trapRef = useFocusTrap(true, onClose)
  const qa = portrait.group_qa
  const hard = qa?.hard_failures ?? []
  const soft = qa?.issues ?? []
  const [candidates, setCandidates] = useState<CharacterPortraitCandidate[]>([])
  const [loadingCandidates, setLoadingCandidates] = useState(true)
  const [candidateError, setCandidateError] = useState<string | null>(null)
  const [candidateBusy, setCandidateBusy] = useState<string | null>(null)
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [bypassSoft, setBypassSoft] = useState<Record<string, boolean>>({})

  const loadCandidates = () => {
    setLoadingCandidates(true)
    setCandidateError(null)
    api.listPortraitCandidates(projectId, characterName)
      .then(result => {
        const items = Array.isArray(result) ? result : (result.items ?? result.candidates ?? [])
        setCandidates(items)
      })
      .catch(error => setCandidateError((error as Error).message))
      .finally(() => setLoadingCandidates(false))
  }

  useEffect(() => {
    loadCandidates()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, characterName])

  const candidateId = (candidate: CharacterPortraitCandidate) => candidate.portrait_id || candidate.id || ''
  const candidateQa = (candidate: CharacterPortraitCandidate) => candidate.group_qa || candidate.qa || null
  const candidateSoftWarnings = (candidate: CharacterPortraitCandidate) => {
    const qa = candidateQa(candidate)
    return [...(candidate.soft_warnings ?? []), ...(qa?.issues ?? [])]
  }
  const mutateCandidate = async (candidate: CharacterPortraitCandidate, action: 'adopt' | 'rollback') => {
    const id = candidateId(candidate)
    if (!id) return
    setCandidateBusy(`${action}:${id}`)
    try {
      if (action === 'adopt') {
        const reason = (reasons[id] || '').trim()
        if (!reason) {
          toast('请先填写采纳原因', true)
          return
        }
        await api.adoptPortraitCandidate(projectId, characterName, id, {
          reason,
          bypass_soft: !!bypassSoft[id],
        })
        toast(`已采纳「${characterName}」候选定妆`)
      } else {
        await api.rollbackPortraitCandidate(projectId, characterName, id)
        toast(`已回滚「${characterName}」定妆候选`)
      }
      onChanged?.()
      loadCandidates()
    } catch (error) {
      toast((error as Error).message, true)
    } finally {
      setCandidateBusy(null)
    }
  }

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={e => {
      if (e.currentTarget === e.target) onClose()
    }}>
      <section ref={trapRef} className="impact-dialog character-qa-panel" role="dialog" aria-modal="true" aria-label="人物 QA 详情">
        <h3>{characterName} · 定妆 QA</h3>
        <ul>
          <li>包状态：<span title={statusTitle(portrait.pack_status || '')}>{statusLabel(portrait.pack_status)}</span></li>
          <li>整包结论：{qa?.status ? <span title={statusTitle(qa.status)}>{statusLabel(qa.status)}</span> : '未验证'}
            {typeof qa?.overall === 'number' ? ` · ${qa.overall.toFixed(2)}` : ''}</li>
          <li>脸一致性：{qa?.face_consistency ?? '—'}</li>
          <li>发型一致性：{qa?.hair_consistency ?? '—'}</li>
          <li>服装一致性：{qa?.outfit_consistency ?? '—'}</li>
        </ul>
        {!!hard.length && (
          <>
            <h4>硬失败</h4>
            <ul>{hard.map(item => <li key={item}>{item}</li>)}</ul>
          </>
        )}
        {!!soft.length && (
          <>
            <h4>警告</h4>
            <ul>{soft.map(item => <li key={item}>{item}</li>)}</ul>
          </>
        )}
        <h4>视角级结果</h4>
        <ul>
          {(qa?.views ?? portrait.views ?? []).map((view, index) => {
            const role = ('view_role' in view ? view.view_role : undefined) || `view-${index}`
            const overall = 'overall' in view ? view.overall : ('qa_overall' in view ? view.qa_overall : null)
            const issues = ('issues' in view ? view.issues : undefined) || []
            const fails = ('hard_failures' in view ? view.hard_failures : undefined) || []
            return (
              <li key={role}>
                {VIEW_LABELS[role || ''] || role}
                {typeof overall === 'number' ? ` · ${overall.toFixed(2)}` : ''}
                {fails?.length ? ` · 硬失败：${fails.join('；')}` : ''}
                {issues?.length ? ` · ${issues.slice(0, 2).join('；')}` : ''}
              </li>
            )
          })}
        </ul>
        <h4>候选定妆包</h4>
        {loadingCandidates && <p>正在读取候选包…</p>}
        {candidateError && <p className="error-banner">{candidateError}</p>}
        {!loadingCandidates && !candidateError && !candidates.length && (
          <p className="hint">暂无可采纳或回滚的候选包。</p>
        )}
        {!!candidates.length && (
          <div className="portrait-candidate-list">
            {candidates.map((candidate, index) => {
              const id = candidateId(candidate) || `candidate-${index}`
              const qa = candidateQa(candidate)
              const warnings = candidateSoftWarnings(candidate)
              const isCurrent = candidate.current || candidate.is_current || candidate.adopted
              return (
                <article key={id} className="portrait-candidate-item">
                  {candidate.image_url && <img src={candidate.image_url} alt={`${characterName} 候选定妆`} />}
                  <div>
                    <div className="portrait-candidate-head">
                      <b>{isCurrent ? '当前采用' : candidate.historical ? '历史候选' : '候选包'}</b>
                      <span title={statusTitle(candidate.pack_status || candidate.status || '')}>
                        {statusLabel(candidate.pack_status || candidate.status)}
                      </span>
                      {typeof qa?.overall === 'number' && <span>QA {qa.overall.toFixed(2)}</span>}
                    </div>
                    <p>
                      {(qa?.hard_failures ?? []).length
                        ? `硬失败：${(qa?.hard_failures ?? []).join('；')}`
                        : warnings.length
                          ? `警告：${warnings.slice(0, 3).join('；')}`
                          : 'QA 未报告明显问题'}
                    </p>
                    <label className="f">采纳原因</label>
                    <input
                      value={reasons[id] || ''}
                      onChange={event => setReasons(current => ({ ...current, [id]: event.target.value }))}
                      placeholder="说明为什么采纳此候选包"
                    />
                    {!!warnings.length && (
                      <label className="portrait-candidate-bypass">
                        <input
                          type="checkbox"
                          checked={!!bypassSoft[id]}
                          onChange={event => setBypassSoft(current => ({ ...current, [id]: event.target.checked }))}
                        />
                        允许越过软警告
                      </label>
                    )}
                    <div className="dialog-actions">
                      <button
                        type="button"
                        className="btn small primary"
                        disabled={candidateBusy === `adopt:${id}`}
                        onClick={() => { void mutateCandidate(candidate, 'adopt') }}
                      >
                        {candidateBusy === `adopt:${id}` ? '采纳中…' : '采纳'}
                      </button>
                      <button
                        type="button"
                        className="btn small"
                        disabled={candidateBusy === `rollback:${id}`}
                        onClick={() => { void mutateCandidate(candidate, 'rollback') }}
                      >
                        {candidateBusy === `rollback:${id}` ? '回滚中…' : '回滚'}
                      </button>
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
        )}
        <p className="hint">采用原因：硬门禁通过后自动采用当前包；失败包不切换下游引用。人工特批默认不可越过硬门禁。</p>
        <div className="dialog-actions">
          <button type="button" className="btn primary" onClick={onClose}>关闭</button>
        </div>
      </section>
    </div>
  )
}
