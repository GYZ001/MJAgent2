import { useEffect, useState } from 'react'
import { api, CharacterPortraitCandidate, Portrait } from '../api'
import { useNav } from '../App'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { statusLabel } from '../lib/statusLabels'
import DecisionDialog from './DecisionDialog'
import OperationError from './OperationError'

const VIEW_LABELS: Record<string, string> = {
  front_full: '正面全身',
  three_quarter: '3/4 面',
  profile: '侧面',
  back_full: '背面全身',
  face_closeup: '面部特写',
}

export function characterQaMessage(value: string): string {
  return Object.entries(VIEW_LABELS).reduce(
    (text, [key, label]) => text.replaceAll(key, label),
    value,
  ).replaceAll('警告', '质检提示')
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
  portrait?: Portrait | null
  onChanged?: () => void
  onClose: () => void
}) {
  const { toast } = useNav()
  const qa = portrait?.group_qa
  const hard = qa?.hard_failures ?? []
  const soft = qa?.issues ?? []
  const [candidates, setCandidates] = useState<CharacterPortraitCandidate[]>([])
  const [loadingCandidates, setLoadingCandidates] = useState(true)
  const [candidateError, setCandidateError] = useState<string | null>(null)
  const [candidateBusy, setCandidateBusy] = useState<string | null>(null)
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [bypassSoft, setBypassSoft] = useState<Record<string, boolean>>({})
  const [pendingDecision, setPendingDecision] = useState<{
    candidate: CharacterPortraitCandidate
    action: 'adopt' | 'rollback'
  } | null>(null)
  const trapRef = useFocusTrap(!pendingDecision, onClose)

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
    void api.post('/system/monitor/events', {
      name: 'portrait_qa_review', object_id: projectId,
      dimensions: { action: 'open', result: portrait?.pack_status || 'candidate_only' },
    }).catch(() => undefined)
    loadCandidates()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, characterName, portrait?.pack_status])

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
      <section ref={trapRef} className="impact-dialog character-qa-panel" role="dialog" aria-modal="true" aria-label="人物质检详情">
        <h3>{characterName} · 定妆质检</h3>
        <ul>
          <li>定妆包状态：{portrait
            ? <span>{statusLabel(portrait.pack_status)}</span>
            : '暂无已采用定妆包'}</li>
          <li>整体验收：{qa?.status ? <span>{statusLabel(qa.status)}</span> : '待质检'}
            {typeof qa?.overall === 'number' ? ` · ${qa.overall.toFixed(2)}` : ''}</li>
          <li>脸一致性：{qa?.face_consistency ?? '—'}</li>
          <li>发型一致性：{qa?.hair_consistency ?? '—'}</li>
          <li>服装一致性：{qa?.outfit_consistency ?? '—'}</li>
          <li>体型一致性：{qa?.body_consistency ?? '—'}</li>
        </ul>
        {!!hard.length && (
          <>
            <h4>未通过的必检项</h4>
            <ul>{hard.map(item => <li key={item}>{characterQaMessage(item)}</li>)}</ul>
          </>
        )}
        {!!soft.length && (
          <>
            <h4>质量需复核</h4>
            <ul>{soft.map(item => <li key={item}>{characterQaMessage(item)}</li>)}</ul>
          </>
        )}
        <h4>视角级结果</h4>
        <ul>
          {(qa?.views ?? portrait?.views ?? []).map((view, index) => {
            const role = ('view_role' in view ? view.view_role : undefined) || `view-${index}`
            const overall = 'overall' in view ? view.overall : ('qa_overall' in view ? view.qa_overall : null)
            const issues = ('issues' in view ? view.issues : undefined) || []
            const fails = ('hard_failures' in view ? view.hard_failures : undefined) || []
            return (
              <li key={role}>
                {VIEW_LABELS[role || ''] || role}
                {typeof overall === 'number' ? ` · ${overall.toFixed(2)}` : ''}
                {fails?.length ? ` · 必检项未通过：${fails.map(characterQaMessage).join('；')}` : ''}
                {issues?.length ? ` · ${issues.slice(0, 2).map(characterQaMessage).join('；')}` : ''}
              </li>
            )
          })}
        </ul>
        <h4>候选定妆包</h4>
        {loadingCandidates && <p>正在读取候选包…</p>}
        {candidateError && <OperationError
          title="候选定妆包加载失败"
          message={candidateError}
          guidance="当前采用的定妆包没有改变。可重试加载候选列表。"
        >
          <button type="button" className="btn small ghost" onClick={loadCandidates}>重试加载</button>
        </OperationError>}
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
              const isSingleImage = candidate.candidate_kind === 'single_image'
              const adoptable = candidate.adoptable !== false
              const canAdopt = adoptable && !isCurrent
              const adoptDisabledReason = candidateBusy
                ? '正在处理上一项候选操作'
                : (reasons[id] || '').trim().length < 4
                  ? '请填写至少 4 个字的采纳原因'
                  : warnings.length && !bypassSoft[id]
                    ? '请先确认已阅读质检提示'
                    : ''
              return (
                <article key={id} className="portrait-candidate-item">
                  {candidate.image_url && <img src={candidate.image_url} alt={`${characterName} 候选定妆`} loading="lazy" decoding="async" />}
                  <div>
                    <div className="portrait-candidate-head">
                      <b>{isCurrent ? '当前采用' : candidate.historical ? '历史候选' : isSingleImage ? '失败单图候选' : '候选包'}</b>
                      <span>{statusLabel(candidate.pack_status || candidate.status)}</span>
                      {typeof qa?.overall === 'number' && <span>质检分 {qa.overall.toFixed(2)}</span>}
                      {candidate.attempt != null && <span>第 {candidate.attempt} 次</span>}
                    </div>
                    <p>
                      {(qa?.hard_failures ?? []).length
                        ? `必检项未通过：${(qa?.hard_failures ?? []).map(characterQaMessage).join('；')}`
                        : warnings.length
                          ? `质检提示：${warnings.slice(0, 3).map(characterQaMessage).join('；')}`
                          : '质检未报告明显问题'}
                    </p>
                    <p className="hint">
                      {isSingleImage
                        ? '阶段：正面单图候选，尚未进入三视角整包'
                        : `适用范围：${(candidate.ep_start ?? 0) <= 0
                          ? '历史初始版本（曾适用第 1 集起）'
                          : `第 ${candidate.ep_start} 集起${candidate.ep_end != null ? ` 至第 ${candidate.ep_end} 集` : '至今'}`}`}
                      {candidate.change?.adoption_reason ? ` · 采纳原因：${candidate.change.adoption_reason}` : ''}
                    </p>
                    {candidate.artifact_id && <details className="portrait-candidate-technical"><summary>技术证据</summary><code>{candidate.artifact_id}</code></details>}
                    {!adoptable && <p className="error-banner">{candidate.blocked_reason || '该候选未通过生产必检项，不能直接采纳。'}</p>}
                    {canAdopt && <><label className="f">采纳原因</label>
                    <input
                      aria-label={`${characterName}候选 ${index + 1} 的采纳原因`}
                      value={reasons[id] || ''}
                      onChange={event => setReasons(current => ({ ...current, [id]: event.target.value }))}
                      placeholder="说明为什么采纳此候选包"
                    /></>}
                    {canAdopt && !!warnings.length && (
                      <label className="portrait-candidate-bypass">
                        <input
                          type="checkbox"
                          checked={!!bypassSoft[id]}
                          onChange={event => setBypassSoft(current => ({ ...current, [id]: event.target.checked }))}
                        />
                        已阅读这些质检提示，仍采用此候选
                      </label>
                    )}
                    {(canAdopt || isCurrent) && <div className="dialog-actions">
                      {canAdopt && (
                      <button
                        type="button"
                        className="btn small primary"
                        disabled={Boolean(adoptDisabledReason)}
                        aria-label={adoptDisabledReason ? `采纳候选定妆包，暂不可用：${adoptDisabledReason}` : '采纳候选定妆包；下一步确认影响'}
                        onClick={() => setPendingDecision({ candidate, action: 'adopt' })}
                      >
                        {candidateBusy === `adopt:${id}` ? '采纳中…' : '采纳'}
                      </button>
                      )}
                      {isCurrent && (
                      <button
                        type="button"
                        className="btn small"
                        disabled={candidateBusy === `rollback:${id}`}
                        aria-label={candidateBusy ? '回滚当前定妆包，暂不可用：正在处理候选操作' : '回滚当前定妆包；下一步确认影响'}
                        onClick={() => setPendingDecision({ candidate, action: 'rollback' })}
                      >
                        {candidateBusy === `rollback:${id}` ? '回滚中…' : '回滚'}
                      </button>
                      )}
                    </div>}
                  </div>
                </article>
              )
            })}
          </div>
        )}
        <p className="hint">采用规则：三视角文件齐全并可读取后即可采用；质量分数与问题仅供评审，不阻止采用。技术失败不会替换下游正在使用的版本。</p>
        <div className="dialog-actions">
          <button type="button" className="btn primary" onClick={onClose}>关闭</button>
        </div>
        {pendingDecision && (
          <DecisionDialog
            title={pendingDecision.action === 'adopt'
              ? `采纳「${characterName}」候选定妆包？`
              : `回滚「${characterName}」当前定妆包？`}
            summary={pendingDecision.action === 'adopt' ? '候选将成为当前采用版本' : '将恢复上一可用定妆包'}
            message={pendingDecision.action === 'adopt'
              ? '确认后，后续新生成内容会使用该候选；当前采用版本会保留为历史记录。'
              : '确认后，后续新生成内容会改用上一可用版本；当前版本和历史记录不会删除。'}
            details={[
              '不会重新生成图片，也不会产生生成费用',
              '已完成的历史视频不会被自动删除',
            ]}
            confirmLabel={pendingDecision.action === 'adopt' ? '确认采纳候选' : '确认回滚版本'}
            cancelLabel="返回检查"
            danger={pendingDecision.action === 'rollback'}
            onClose={() => setPendingDecision(null)}
            onConfirm={() => {
              const decision = pendingDecision
              setPendingDecision(null)
              void mutateCandidate(decision.candidate, decision.action)
            }}
          />
        )}
      </section>
    </div>
  )
}
