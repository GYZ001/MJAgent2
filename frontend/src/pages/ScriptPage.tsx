import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  DialogueOccurrence,
  EpisodeScreenplay,
  PlotSpine,
  PlotSpineBeat,
  ScreenplayState,
  ScriptScene,
  numToCn,
} from '../api'
import { useNav, useScriptEpisode } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import DecisionDialog from '../components/DecisionDialog'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import { EpisodeStatusStamp, ScreenplayStatusStamp } from '../components/ProductionStatusStamp'
import QueryState from '../components/QueryState'
import OperationError from '../components/OperationError'
import { useFocusTrap } from '../hooks/useFocusTrap'

type EditorSection = 'spine' | 'body' | 'scenes' | 'evidence'
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

type ActionPreview = {
  kind: 'screenplay' | 'storyboard-create' | 'storyboard-resume' | 'screenplay-save'
  title: string
  data: Record<string, any>
  idempotencyKey: string
}

type DropWizard = {
  item: string
  reason: string
  rewrite: string
  targetType: 'beat' | 'scene'
  targetIndex: number
  step: 1 | 2
}

const cloneScript = (script: EpisodeScreenplay | null | undefined): EpisodeScreenplay | null =>
  script ? JSON.parse(JSON.stringify(script)) : null

const emptySpine = (): PlotSpine => ({
  episode_premise: '',
  spine_beats: [],
  must_keep_ending: '',
  drop_list: [],
})

const sourceRangeText = (chapters: number[]) => chapters.length <= 1
  ? `第 ${chapters[0] ?? '-'} 章`
  : `第 ${chapters[0]}-${chapters[chapters.length - 1]} 章`

const splitLines = (text: string) => text.split('\n').map(value => value.trim()).filter(Boolean)

const moveItem = <T,>(items: T[], index: number, direction: -1 | 1): T[] => {
  const target = index + direction
  if (target < 0 || target >= items.length) return items
  const next = [...items]
  ;[next[index], next[target]] = [next[target], next[index]]
  return next
}

const stableKey = (prefix: string) => `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`
const TARGET_DURATION_CHOICES = [40, 50, 60, 70, 80, 90] as const

function StructuredListActions({
  index,
  length,
  onMove,
  onDelete,
}: {
  index: number
  length: number
  onMove: (direction: -1 | 1) => void
  onDelete: () => void
}) {
  return (
    <div className="structured-row-actions">
      <button type="button" className="btn small ghost" disabled={index === 0} onClick={() => onMove(-1)}
        aria-label={index === 0 ? '上移，暂不可用：已是第一项' : '上移'}
        title={index === 0 ? '已是第一项' : '上移一项'}>↑</button>
      <button type="button" className="btn small ghost" disabled={index === length - 1} onClick={() => onMove(1)}
        aria-label={index === length - 1 ? '下移，暂不可用：已是最后一项' : '下移'}
        title={index === length - 1 ? '已是最后一项' : '下移一项'}>↓</button>
      <button type="button" className="btn small ghost danger" onClick={onDelete}>删除</button>
    </div>
  )
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const needle = query.trim()
  if (!needle) return <>{text}</>
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = text.split(new RegExp(`(${escaped})`, 'gi'))
  return <>{parts.map((part, index) => part.toLowerCase() === needle.toLowerCase()
    ? <mark key={index}>{part}</mark>
    : part)}</>
}

export default function ScriptPage() {
  const { episodeId, projectId, go, toast, registerNavigationGuard } = useNav()
  const { data: ep, refresh, error, loading } = useScriptEpisode(episodeId!)
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState<EpisodeScreenplay | null>(null)
  const [baselineVersion, setBaselineVersion] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [draftSaveState, setDraftSaveState] = useState<SaveState>('idle')
  const [recoverable, setRecoverable] = useState<{ content?: EpisodeScreenplay; constraints?: { occurrence_ids?: string[] }; baseline?: string | null } | null>(null)
  const [selectedOccurrenceIds, setSelectedOccurrenceIds] = useState<string[] | null>(null)
  const [manuscriptExpanded, setManuscriptExpanded] = useState(false)
  const [detailsExpanded, setDetailsExpanded] = useState(false)
  const [editorSection, setEditorSection] = useState<EditorSection>('spine')
  const [manuscriptSearch, setManuscriptSearch] = useState('')
  const [preview, setPreview] = useState<ActionPreview | null>(null)
  const [dropWizard, setDropWizard] = useState<DropWizard | null>(null)
  const [conflict, setConflict] = useState<Record<string, any> | null>(null)
  const [discardDraftOpen, setDiscardDraftOpen] = useState(false)
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false)
  const [targetDurationDraft, setTargetDurationDraft] = useState(50)
  const historyRef = useRef<EpisodeScreenplay[]>([])
  const redoRef = useRef<EpisodeScreenplay[]>([])
  const restoredRef = useRef(false)
  const manuscriptRef = useRef<HTMLDivElement>(null)
  const conflictTrapRef = useFocusTrap(Boolean(conflict), () => setConflict(null))
  const previewTrapRef = useFocusTrap(Boolean(preview), () => setPreview(null))
  const dropTrapRef = useFocusTrap(Boolean(dropWizard), () => setDropWizard(null), {
    dirty: Boolean(dropWizard?.reason || dropWizard?.rewrite),
    onDirtyClose: () => toast('恢复向导尚未写入草稿，请先完成或点击取消'),
  })

  const screenplayTimer = useTaskTimer(
    `episode.${episodeId}.screenplay`,
    ep?.screenplay_production?.task_active ?? ep?.screenplay_status === 'running',
  )
  const storyboardTimer = useTaskTimer(`episode.${episodeId}.storyboard`, ep?.status === 'scripting')

  const occurrences = ep?.source_dialogue_occurrences ?? []
  const serverOccurrenceIds = ep?.required_dialogue_occurrence_ids ?? []
  const requiredOccurrenceIds = selectedOccurrenceIds ?? serverOccurrenceIds
  const selectedSet = useMemo(() => new Set(requiredOccurrenceIds), [requiredOccurrenceIds])
  const selectedOccurrences = useMemo(
    () => occurrences.filter(item => selectedSet.has(item.id)),
    [occurrences, selectedSet],
  )
  const selectedSeconds = useMemo(
    () => selectedOccurrences.reduce((sum, item) => sum + item.estimated_seconds, 0),
    [selectedOccurrences],
  )
  const targetDuration = ep?.target_duration_s ?? 50
  const performanceReserve = targetDuration - selectedSeconds
  const suggestedTargetDuration = TARGET_DURATION_CHOICES.find(
    value => value >= selectedSeconds / 0.8,
  )
  const averageSeconds = occurrences.length
    ? occurrences.reduce((sum, item) => sum + item.estimated_seconds, 0) / occurrences.length
    : 2.5
  const dynamicLimit = Math.max(1, Math.floor(targetDuration * 0.8 / Math.max(averageSeconds, 0.5)))
  const effectiveLimit = Math.max(dynamicLimit, requiredOccurrenceIds.length)
  const hardBudgetExceeded = selectedSeconds > targetDuration
  const allDialogueSelected = occurrences.length > 0 && occurrences.every(item => selectedSet.has(item.id))

  const screenplayTaskActive = ep?.screenplay_production?.task_active ?? ep?.screenplay_status === 'running'
  const canResumeRepair = ep?.screenplay_production?.can_resume_repair
    ?? (ep?.screenplay_status === 'repairing' || ep?.screenplay_status === 'warning')
  const legacyDialoguePolicyRecovery = Boolean(
    ep?.screenplay_production?.legacy_dialogue_policy_recovery_available,
  )

  const script = draft ?? ep?.screenplay ?? null
  const editing = draft !== null

  const localDraftKey = ep ? `manju:screenplay-draft:${projectId}:${ep.id}` : ''
  const draftEpisodeId = ep?.id
  const draftEpisodeArtifactId = ep?.screenplay_artifact_id ?? null

  useEffect(() => {
    if (ep) setTargetDurationDraft(ep.target_duration_s)
  }, [ep?.id, ep?.target_duration_s])

  const applyTargetDuration = async () => {
    if (!ep || targetDurationDraft === targetDuration) return
    setBusy(true)
    try {
      await api.put(`/episodes/${ep.id}/target-duration`, {
        target_duration_s: targetDurationDraft,
      })
      await refresh()
      toast(`本集目标时长已调整为 ${targetDurationDraft} 秒`)
    } catch (reason: unknown) {
      setTargetDurationDraft(targetDuration)
      toast((reason as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (!ep || restoredRef.current) return
    restoredRef.current = true
    let cancelled = false
    const fromLocal = localStorage.getItem(localDraftKey)
    if (fromLocal) {
      try {
        const parsed = JSON.parse(fromLocal)
        setRecoverable(parsed)
      } catch { localStorage.removeItem(localDraftKey) }
    }
    api.get(`/episodes/${ep.id}/screenplay/draft`).then((result: any) => {
      if (cancelled || !result?.draft) return
      const server = result.draft
      setRecoverable(current => current ?? {
        content: server.content,
        constraints: server.constraints,
        baseline: server.baseline_artifact_id,
      })
    }).catch(() => { /* 本地草稿仍可恢复 */ })
    return () => { cancelled = true }
  }, [ep, localDraftKey])

  useEffect(() => {
    if (!draftEpisodeId || !dirty) return
    const payload = {
      content: draft ?? undefined,
      constraints: { occurrence_ids: requiredOccurrenceIds },
      baseline: baselineVersion ?? draftEpisodeArtifactId,
      saved_at: Date.now(),
    }
    localStorage.setItem(localDraftKey, JSON.stringify(payload))
    setDraftSaveState('saving')
    const timer = window.setTimeout(() => {
      api.put(`/episodes/${draftEpisodeId}/screenplay/draft`, {
        content: draft ?? undefined,
        constraints: { occurrence_ids: requiredOccurrenceIds },
        baseline_artifact_id: baselineVersion ?? draftEpisodeArtifactId,
      }).then(() => setDraftSaveState('saved')).catch(() => setDraftSaveState('error'))
    }, 650)
    return () => window.clearTimeout(timer)
  }, [baselineVersion, dirty, draft, draftEpisodeArtifactId, draftEpisodeId, localDraftKey, requiredOccurrenceIds])

  useLayoutEffect(() => {
    if (!dirty) {
      registerNavigationGuard(null, false)
      return
    }
    registerNavigationGuard(
      {
        title: '保留工作草稿并离开？',
        summary: '当前剧本修改尚未发布',
        message: '系统会保留本地工作草稿；云端草稿保存成功后，也可在其他页面返回继续编辑。',
        details: [
          draftSaveState === 'error'
            ? '云端草稿保存失败，本机草稿仍保留'
            : '离开不会改动当前已发布剧本',
          '未发布修改不会进入分镜或视频流程',
        ],
        confirmLabel: '保留草稿并离开',
        cancelLabel: '继续编辑',
      },
      true,
    )
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      registerNavigationGuard(null, false)
    }
  }, [dirty, draftSaveState, registerNavigationGuard])

  const mutateDraft = (updater: (current: EpisodeScreenplay) => EpisodeScreenplay) => {
    setDraft(current => {
      if (!current) return current
      historyRef.current.push(cloneScript(current)!)
      if (historyRef.current.length > 80) historyRef.current.shift()
      redoRef.current = []
      return updater(cloneScript(current)!)
    })
    setDirty(true)
  }

  const undo = () => {
    const previous = historyRef.current.pop()
    if (!previous || !draft) return
    redoRef.current.push(cloneScript(draft)!)
    setDraft(previous)
    setDirty(true)
  }

  const redo = () => {
    const next = redoRef.current.pop()
    if (!next || !draft) return
    historyRef.current.push(cloneScript(draft)!)
    setDraft(next)
    setDirty(true)
  }

  const updateScript = (patch: Partial<EpisodeScreenplay>) => mutateDraft(current => ({ ...current, ...patch }))
  const updateSpine = (patch: Partial<PlotSpine>) => mutateDraft(current => ({
    ...current,
    plot_spine: { ...(current.plot_spine ?? emptySpine()), ...patch },
  }))

  const beginEditing = (value = ep?.screenplay ?? null, baseline = ep?.screenplay_artifact_id ?? null) => {
    if (!value) return
    setDraft(cloneScript(value))
    setBaselineVersion(baseline)
    setEditorSection('spine')
    historyRef.current = []
    redoRef.current = []
    setDirty(false)
    setConflict(null)
  }

  const clearWorkingDraft = async () => {
    setDraft(null)
    setBaselineVersion(null)
    setDirty(false)
    setSelectedOccurrenceIds(null)
    setRecoverable(null)
    setConflict(null)
    historyRef.current = []
    redoRef.current = []
    if (localDraftKey) localStorage.removeItem(localDraftKey)
    if (ep) await api.del(`/episodes/${ep.id}/screenplay/draft`).catch(() => undefined)
  }

  const run = async (fn: () => Promise<any>, done?: string) => {
    setBusy(true)
    try {
      const result = await fn()
      if (done) toast(done)
      await refresh()
      return result
    } catch (unknownError: unknown) {
      const apiError = unknownError as Error & { status?: number; detail?: any }
      if (apiError.status === 403 && apiError.message.includes('已取消操作')) {
        toast('未执行，数据保持不变')
      } else {
        toast(apiError.message, true)
      }
      throw unknownError
    } finally {
      setBusy(false)
    }
  }

  const publishDraft = async () => {
    if (!ep || !draft) return
    setBusy(true)
    try {
      const result = await api.put(`/episodes/${ep.id}/screenplay`, {
        screenplay: draft,
        expected_version: baselineVersion,
      })
      toast(result.unchanged ? '内容无变化，未创建新版本' : '剧本已原子发布')
      await clearWorkingDraft()
      await refresh()
    } catch (saveError: unknown) {
      const typed = saveError as Error & { status?: number; detail?: any }
      if (typed.status === 409 && ['screenplay_version_conflict', 'version_conflict'].includes(typed.detail?.code)) {
        setConflict(typed.detail)
      } else if (typed.status === 403 && typed.message.includes('已取消操作')) {
        toast('未执行发布，工作草稿已保留')
      } else {
        toast(typed.message, true)
      }
    } finally { setBusy(false) }
  }

  const openScreenplayPreview = async () => {
    if (!ep || hardBudgetExceeded || canResumeRepair) return
    setBusy(true)
    try {
      const data = await api.post(`/episodes/${ep.id}/screenplay/preflight`, {
        occurrence_ids: requiredOccurrenceIds,
      })
      setPreview({
        kind: 'screenplay',
        title: '首次生成剧本预检',
        data,
        idempotencyKey: stableKey(`screenplay:${ep.id}`),
      })
    } catch (previewError) {
      toast((previewError as Error).message, true)
    } finally { setBusy(false) }
  }

  const openStoryboardPreview = async (mode: 'create' | 'resume') => {
    if (!ep || dirty) return
    setBusy(true)
    try {
      const data = await api.post(`/episodes/${ep.id}/storyboard/preflight`, { mode })
      setPreview({
        kind: mode === 'resume' ? 'storyboard-resume' : 'storyboard-create',
        title: mode === 'resume' ? '继续生成分镜预检' : '首次生成分镜预检',
        data,
        idempotencyKey: stableKey(`storyboard:${ep.id}:${mode}`),
      })
    } catch (previewError) {
      toast((previewError as Error).message, true)
    } finally { setBusy(false) }
  }

  const executePreview = async () => {
    if (!preview || !ep) return
    const current = preview
    setPreview(null)
    if (current.kind === 'screenplay-save') {
      await publishDraft()
      return
    }
    if (current.kind === 'screenplay') {
      screenplayTimer.start()
      const result = await run(() => api.post(`/episodes/${ep.id}/screenplay`, {
        required_dialogue_occurrence_ids: requiredOccurrenceIds,
        required_dialogue_lines: selectedOccurrences.map(item => item.text),
        idempotency_key: current.idempotencyKey,
      }), '首版剧本任务已受理').catch(() => screenplayTimer.clear())
      if (result) {
        setDirty(false)
        localStorage.removeItem(localDraftKey)
        void api.del(`/episodes/${ep.id}/screenplay/draft`).catch(() => undefined)
      }
      return
    }
    storyboardTimer.start()
    const resume = current.kind === 'storyboard-resume'
    await run(() => api.post(`/episodes/${ep.id}/storyboard${resume ? '/resume' : ''}`, {
      preflight_token: current.data.preview_token,
      idempotency_key: current.idempotencyKey,
    }), resume ? '已从安全恢复点继续分镜' : '分镜生成任务已受理')
      .catch(() => storyboardTimer.clear())
  }

  const resumeRepair = async () => {
    if (!ep) return
    screenplayTimer.start()
    await run(() => api.post(`/episodes/${ep.id}/screenplay/resume`, {
      idempotency_key: stableKey(`screenplay-resume:${ep.id}`),
    }), '已使用任务详情中的锁定约束版本继续修复')
      .catch(() => screenplayTimer.clear())
  }

  const stopScreenplay = async () => {
    if (!ep) return
    const result = await run(() => api.post(`/episodes/${ep.id}/screenplay/cancel`, {}))
      .catch(() => null)
    if (result?.status === 'cancelling') toast('正在取消，尚未宣称已停止')
    else if (result) toast(`任务已终止；${result.resume_available ? '可从工作副本恢复' : '可重新发起'}`)
  }

  const savePublished = async () => {
    if (!ep || !draft) return
    setBusy(true)
    try {
      const result = await api.post(`/episodes/${ep.id}/screenplay/impact-preview`, {
        screenplay: draft,
        expected_version: baselineVersion,
      })
      if (result.requires_server_approval) {
        // 有下游时不再叠加本地弹窗；PUT 会进入统一 Capability 影响卡。
        await publishDraft()
        return
      }
      setPreview({
        kind: 'screenplay-save',
        title: result.unchanged ? '发布前检查' : '剧本发布差异预览',
        data: result,
        idempotencyKey: stableKey(`screenplay-save:${ep.id}`),
      })
    } catch (previewError: unknown) {
      const typed = previewError as Error & { status?: number; detail?: any }
      if (typed.status === 409 && ['screenplay_version_conflict', 'version_conflict'].includes(typed.detail?.code)) {
        setConflict(typed.detail)
      } else {
        toast(typed.message, true)
      }
    } finally { setBusy(false) }
  }

  const deleteCurrentScreenplay = async () => {
    if (!ep) return
    try {
      const result = await run(() => api.del(`/episodes/${ep.id}/screenplay`))
      if (result) {
        await clearWorkingDraft()
        screenplayTimer.clear()
        storyboardTimer.clear()
        toast('当前剧本及下游已删除；必保留台词已保留')
      }
    } catch { /* run 已呈现结果 */ }
  }

  const validateDraft = (value: EpisodeScreenplay | null) => {
    const sections: Record<EditorSection, string[]> = { spine: [], body: [], scenes: [], evidence: [] }
    if (!value) return sections
    if (!value.title?.trim()) sections.body.push('标题不能为空')
    if (!value.full_script_text?.trim()) sections.body.push('完整剧本正文不能为空')
    if ((value.full_script_text?.length ?? 0) < 100) sections.body.push('剧本正文过短')
    ;(value.plot_spine?.spine_beats ?? []).forEach((beat, index) => {
      if (!beat.who?.trim() || !beat.does?.trim() || !beat.turn?.trim()) sections.spine.push(`节拍 ${index + 1} 未写完`)
    })
    ;(value.scene_outline ?? []).forEach((scene, index) => {
      if (!scene.scene_heading.trim() || !scene.story_function.trim() || !scene.summary.trim()) sections.scenes.push(`场次 ${index + 1} 未写完`)
    })
    ;(value.key_lines ?? []).forEach((line, index) => {
      if (line && !value.full_script_text?.includes(line.replace(/^.{1,12}[：:]/, ''))) sections.evidence.push(`主线台词 ${index + 1} 在正文中不可追溯`)
    })
    return sections
  }

  const validation = useMemo(() => validateDraft(draft), [draft])
  const totalErrors = Object.values(validation).reduce((sum, items) => sum + items.length, 0)

  const exportScript = () => {
    if (!script || !ep) return
    const blob = new Blob([script.full_script_text ?? ''], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `第${ep.episode_no}集-${script.title || ep.title}-剧本.txt`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const applyDropWizard = () => {
    if (!dropWizard || !draft || !dropWizard.rewrite.trim() || !dropWizard.reason.trim()) return
    const wizard = dropWizard
    mutateDraft(current => {
      const next = cloneScript(current)!
      const nextSpine = { ...(next.plot_spine ?? emptySpine()) }
      nextSpine.drop_list = (nextSpine.drop_list ?? []).filter(item => item !== wizard.item)
      if (wizard.targetType === 'beat') {
        const beats = [...(nextSpine.spine_beats ?? [])]
        const target = beats[wizard.targetIndex]
        if (target) beats[wizard.targetIndex] = {
          ...target,
          does: `${target.does || ''}；${wizard.rewrite}`.replace(/^；/, ''),
        }
        nextSpine.spine_beats = beats
      } else {
        const scenes = [...(next.scene_outline ?? [])]
        const target = scenes[wizard.targetIndex]
        if (target) scenes[wizard.targetIndex] = {
          ...target,
          summary: `${target.summary || ''}；${wizard.rewrite}`.replace(/^；/, ''),
        }
        next.scene_outline = scenes
      }
      next.plot_spine = nextSpine
      next.approved_adaptations = [
        ...(next.approved_adaptations ?? []),
        `恢复原因：${wizard.reason}；可拍化：${wizard.rewrite}`,
      ]
      return next
    })
    setDropWizard(null)
    toast('恢复项已进入工作草稿，尚未影响下游')
  }

  if (!ep) {
    return (
      <QueryState
        loading={loading}
        error={error}
        hasData={false}
        objectName="剧本台"
        loadingText="正在加载剧本、本集状态与分镜进度…"
        emptyText="未找到可展示的剧本数据，请刷新后重试。"
        onRetry={() => void refresh()}
      >
        {null}
      </QueryState>
    )
  }

  const state: Pick<ScreenplayState, 'code' | 'message' | 'recommended_action' | 'publish_blocked' | 'storyboard_running' | 'reason' | 'checkpoint_shot'> = ep.screenplay_state ?? {
    code: 'unknown',
    message: '状态同步中',
    recommended_action: 'refresh',
    publish_blocked: true,
    storyboard_running: false,
    reason: '',
    checkpoint_shot: null,
  }
  const screenplayGenerateDisabledReason = busy
    ? '正在处理上一项操作'
    : hardBudgetExceeded
      ? '所选对白估算已超出本集目标时长，请减少选择或提高目标时长'
      : ''
  const publishDisabledReason = busy
    ? '正在处理上一项操作'
    : totalErrors > 0
      ? `工作草稿还有 ${totalErrors} 项需要修正`
      : ''
  const storyboardGenerateDisabledReason = busy
    ? '正在处理上一项操作'
    : dirty
      ? '当前有未发布修改，请先发布或放弃工作草稿'
      : ''
  const dialogueSelectionDisabledReason = busy
    ? '正在处理上一项操作'
    : canResumeRepair
      ? '安全恢复点已锁定台词约束，继续修复不会读取本地改动'
      : !occurrences.length
        ? '本集原文未识别到显式台词'
        : ''
  const targetDurationDisabledReason = busy
    ? '正在处理上一项操作'
    : canResumeRepair
      ? '安全恢复点已锁定本次目标时长'
      : ''
  const applyTargetDurationDisabledReason = targetDurationDisabledReason
    || (targetDurationDraft === targetDuration ? '所选时长与当前目标一致' : '')

  const primaryAction = () => {
    if (dirty && editing) return <button className="btn primary" disabled={Boolean(publishDisabledReason)}
      aria-label={publishDisabledReason ? `预览影响并发布，暂不可用：${publishDisabledReason}` : '预览影响并发布'}
      title={publishDisabledReason || '提交前先预览对下游的影响'} onClick={savePublished}>预览影响并发布</button>
    switch (state.recommended_action) {
      case 'generate_screenplay':
        return <button className="btn primary" disabled={Boolean(screenplayGenerateDisabledReason)}
          aria-label={screenplayGenerateDisabledReason ? `首次生成剧本，暂不可用：${screenplayGenerateDisabledReason}` : '首次生成剧本'}
          title={screenplayGenerateDisabledReason || '生成前将展示范围、约束和费用'} onClick={openScreenplayPreview}>首次生成剧本</button>
      case 'stop_screenplay':
        return <button className="btn ghost danger" disabled={busy}
          aria-label={busy ? '停止剧本任务，暂不可用：正在处理上一项操作' : '停止剧本任务'}
          title={busy ? '正在处理上一项操作' : '停止前会说明费用和保留范围'} onClick={() => setStopConfirmOpen(true)}>停止剧本任务</button>
      case 'resume_screenplay':
        return <button className="btn primary" disabled={busy}
          aria-label={busy ? '继续局部修复，暂不可用：正在处理上一项操作' : '继续局部修复'}
          title={busy ? '正在处理上一项操作' : undefined} onClick={resumeRepair}>
          {legacyDialoguePolicyRecovery ? '按当前规则恢复并继续' : '继续局部修复'}
        </button>
      case 'generate_storyboard':
        return <button className="btn primary" disabled={Boolean(storyboardGenerateDisabledReason)}
          aria-label={storyboardGenerateDisabledReason ? `首次生成分镜，暂不可用：${storyboardGenerateDisabledReason}` : '首次生成分镜'}
          title={storyboardGenerateDisabledReason || '生成前会预览范围和费用'} onClick={() => openStoryboardPreview('create')}>首次生成分镜</button>
      case 'resume_storyboard':
        return <button className="btn primary" disabled={Boolean(storyboardGenerateDisabledReason)}
          aria-label={storyboardGenerateDisabledReason ? `继续生成分镜，暂不可用：${storyboardGenerateDisabledReason}` : `继续生成分镜，从第 ${(state.checkpoint_shot ?? ep.shot_count ?? 0) + 1} 镜开始`}
          title={storyboardGenerateDisabledReason || '继续前会预览恢复范围'} onClick={() => openStoryboardPreview('resume')}>继续生成分镜（从第 {(state.checkpoint_shot ?? ep.shot_count ?? 0) + 1} 镜）</button>
      case 'view_storyboard':
        return <button className="btn primary" onClick={() => go('board', projectId, ep.id)}>查看分镜进度</button>
      default:
        return <button className="btn primary" disabled={busy} onClick={() => refresh()}>刷新状态</button>
    }
  }

  const structureItems = [
    ['开端', script?.opening], ['发展', script?.development], ['冲突', script?.conflict],
    ['高潮', script?.climax], ['结尾钩子', script?.ending_hook],
  ].filter(([, value]) => Boolean(value?.trim()))

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="剧本台" view="script" episodeNo={ep.episode_no} />
        <h1>剧本台 <span className="sub">《{ep.title}》 · 先完成可拍剧本，再进入镜头设计</span></h1>
        <hr className="rule" />
      </header>

      <section className="card script-toolbar">
        <div className="screenplay-primary-row">
          <div className="screenplay-state-copy">
            <div><ScreenplayStatusStamp status={ep.screenplay_status} /> <EpisodeStatusStamp status={ep.status} /></div>
            <strong>{state.message}</strong>
            {state.reason && <small>{state.reason}</small>}
          </div>
          <div className="screenplay-primary-actions">
            {primaryAction()}
            {ep.screenplay_status === 'ready' && state.recommended_action !== 'view_storyboard' && (
              <button className="btn ghost" type="button" onClick={() => go('board', projectId, ep.id)}>查看分镜台 →</button>
            )}
          </div>
        </div>

        <div className="screenplay-secondary-row">
          {ep.screenplay && !editing && (
            <button className="btn" disabled={busy} onClick={() => beginEditing()}>手工编辑全文</button>
          )}
          {editing && (
            <>
              <button className="btn ghost" disabled={!historyRef.current.length}
                title={!historyRef.current.length ? '当前草稿还没有可撤销的修改' : '撤销上一步修改'}
                onClick={undo}>撤销</button>
              <button className="btn ghost" disabled={!redoRef.current.length}
                title={!redoRef.current.length ? '撤销修改后才可重做' : '恢复上一步已撤销的修改'}
                onClick={redo}>重做</button>
              <button className="btn ghost" disabled={busy} onClick={() => setDiscardDraftOpen(true)}>放弃工作草稿</button>
              <span className={`draft-state ${draftSaveState}`}>
                {draftSaveState === 'saving' ? '草稿保存中…'
                  : draftSaveState === 'saved' ? '草稿已自动保存'
                    : draftSaveState === 'error' ? '草稿云端保存失败（本地已保留）' : ''}
              </span>
            </>
          )}
          {!screenplayTaskActive && (ep.screenplay || canResumeRepair) && (
            <button className="btn ghost danger" disabled={busy} onClick={deleteCurrentScreenplay}>
              {ep.screenplay ? '删除当前剧本' : '删除失败剧本'}
            </button>
          )}
          <span className="screenplay-row-spacer" />
          {ep.screenplay_evidence && <EvidenceDrawer evidence={ep.screenplay_evidence} label="剧本证据" />}
          <TaskTimer label="剧本" timer={screenplayTimer} />
          <TaskTimer label="分镜" timer={storyboardTimer} />
        </div>

        {recoverable && !editing && (
          <div className="draft-recovery-banner" role="status">
            <span>发现未发布的工作草稿，基线 {recoverable.baseline || '空版本'}。</span>
            <div>
              <button className="btn small" onClick={() => {
                if (recoverable.content) beginEditing(recoverable.content, recoverable.baseline ?? null)
                if (recoverable.constraints?.occurrence_ids) setSelectedOccurrenceIds(recoverable.constraints.occurrence_ids)
                setDirty(true)
                setRecoverable(null)
              }}>恢复草稿</button>
              <button className="btn small ghost" onClick={() => setDiscardDraftOpen(true)}>放弃</button>
            </div>
          </div>
        )}

        {!script && !screenplayTaskActive && (
          <div className="screenplay-dialogue-picker">
            <div className="screenplay-dialogue-picker-head">
              <div>
                <b>必保留原文台词（按出现位置）</b>
                <span>已选 {requiredOccurrenceIds.length} / {occurrences.length} 处 · 估算 {selectedSeconds.toFixed(1)}s / 目标 {targetDuration}s · 差值 {(targetDuration - selectedSeconds).toFixed(1)}s</span>
              </div>
              <button type="button" className="btn ghost" disabled={Boolean(dialogueSelectionDisabledReason)}
                aria-label={dialogueSelectionDisabledReason
                  ? `${allDialogueSelected ? '取消全选' : '全选'}，暂不可用：${dialogueSelectionDisabledReason}`
                  : allDialogueSelected ? '取消全选' : '全选'}
                title={dialogueSelectionDisabledReason || (allDialogueSelected ? '取消选择全部原文台词' : '选择全部原文台词')}
                onClick={() => {
                  setSelectedOccurrenceIds(allDialogueSelected ? [] : occurrences.map(item => item.id))
                  setDirty(true)
                }}>
                {allDialogueSelected ? '取消全选' : '全选'}
              </button>
            </div>
            <div className="target-duration-control">
              <div className="target-duration-copy">
                <b>本集目标时长</b>
                <span>这是包含对白、动作、反应和转场的整集节奏预算，不要求成片精确卡到该秒数。</span>
              </div>
              <div className="target-duration-actions">
                <label>
                  <select
                    aria-label="本集目标时长"
                    value={targetDurationDraft}
                    disabled={Boolean(targetDurationDisabledReason)}
                    title={targetDurationDisabledReason || '选择整集对白、动作、反应和转场的节奏预算'}
                    onChange={event => setTargetDurationDraft(Number(event.target.value))}
                  >
                    {TARGET_DURATION_CHOICES.map(value => <option key={value} value={value}>{value} 秒</option>)}
                  </select>
                </label>
                <button
                  type="button"
                  className="btn small"
                  disabled={Boolean(applyTargetDurationDisabledReason)}
                  aria-label={applyTargetDurationDisabledReason
                    ? `应用目标，暂不可用：${applyTargetDurationDisabledReason}`
                    : `应用 ${targetDurationDraft} 秒目标时长`}
                  title={applyTargetDurationDisabledReason || `将本集目标时长调整为 ${targetDurationDraft} 秒`}
                  onClick={() => void applyTargetDuration()}
                >应用目标</button>
              </div>
            </div>
            <div className={`dialogue-budget ${hardBudgetExceeded ? 'hard' : selectedSeconds > targetDuration * 0.8 ? 'soft' : 'ok'}`}>
              <b>时长预算参考：约 {dynamicLimit} 处</b>
              <span>
                这不是对白条数上限；当前选择按口播时长折算约可容纳 {effectiveLimit} 处，所选对白后
                {performanceReserve >= 0 ? `约剩 ${performanceReserve.toFixed(1)}s` : `已超 ${Math.abs(performanceReserve).toFixed(1)}s`} 表演空间。
              </span>
              <small>
                口径：约 4.2 字/秒；通常建议至少留出 20% 给动作、反应和转场。
                {selectedSeconds > targetDuration * 0.8 && suggestedTargetDuration
                  ? ` 按此余量建议选择 ${suggestedTargetDuration} 秒。`
                  : ''}
              </small>
            </div>
            {canResumeRepair && <div className="screenplay-dialogue-warning">当前安全恢复点锁定了约束版本，台词选择只读；继续修复不会使用尚未提交的本地改动。</div>}
            <div className="screenplay-dialogue-options">
              {occurrences.map(item => (
                <DialogueOption
                  key={item.id}
                  item={item}
                  checked={selectedSet.has(item.id)}
                  disabled={Boolean(canResumeRepair)}
                  onChange={checked => {
                    const next = new Set(requiredOccurrenceIds)
                    if (checked) next.add(item.id)
                    else next.delete(item.id)
                    setSelectedOccurrenceIds(occurrences.filter(value => next.has(value.id)).map(value => value.id))
                    setDirty(true)
                  }}
                />
              ))}
              {!occurrences.length && <div className="screenplay-dialogue-empty">本集原文未识别到显式台词，可直接首次生成剧本。</div>}
            </div>
            {hardBudgetExceeded && (
              <div className="screenplay-dialogue-warning hard">
                对白估算已超出整集节奏预算 {Math.abs(targetDuration - selectedSeconds).toFixed(1)}s；请减少选择、拆分对话组，或在上方提高目标时长后再生成。
              </div>
            )}
          </div>
        )}

        <button type="button" className="script-details-toggle" onClick={() => setDetailsExpanded(value => !value)} aria-expanded={detailsExpanded}>
          {detailsExpanded ? '收起详情' : '查看计时、来源与技术详情'}
        </button>
        {detailsExpanded && (
          <div className="screenplay-detail-grid">
            <div className="kv"><b>当前分集</b>第{numToCn(ep.episode_no)}集</div>
            <div className="kv"><b>原文来源范围</b>{script?.source_text_range || sourceRangeText(ep.source_chapters)}{!script?.source_text_range && <em>推断显示</em>}</div>
            <div className="kv"><b>目标时长</b>{ep.target_duration_s}s</div>
            <div className="kv"><b>状态快照</b>v{ep.screenplay_state?.version ?? 0} · {ep.screenplay_state?.code ?? 'unknown'}</div>
            {ep.screenplay_production?.operation === 'repair' && (
              <div className="kv"><b>修复统计</b>
                已启动 {ep.screenplay_production.activation_count ?? 0} 轮 ·
                已应用 {ep.screenplay_production.patch_count ?? 0} 个补丁 ·
                待处理 {ep.screenplay_production.open_issue_count ?? 0} 项
              </div>
            )}
            {legacyDialoguePolicyRecovery && (
              <div className="kv"><b>兼容恢复</b>将恢复旧数量上限裁掉的对白链，再按当前时长规则复验</div>
            )}
          </div>
        )}
        {ep.screenplay_error && (
          <OperationError
            title="剧本生成有待处理信息"
            message={ep.screenplay_error}
            guidance="已发布剧本和工作草稿会保留。请按顶部主操作继续修复或重新生成。"
            detailLabel="查看剧本错误详情"
          />
        )}
        {ep.script_error && (
          <OperationError
            title="分镜生成有待处理信息"
            message={ep.script_error}
            guidance="已有镜头与安全恢复点会保留。请到分镜台继续修复或重新生成。"
            detailLabel="查看分镜错误详情"
          />
        )}
      </section>

      <div className="workspace-gap" />

      {!script ? (
        <div className="empty screenplay-mobile-summary"><div className="big">剧</div>{state.message}<br />请使用顶部唯一主操作</div>
      ) : editing ? (
        <ScreenplayEditor
          draft={draft!}
          section={editorSection}
          setSection={setEditorSection}
          validation={validation}
          updateScript={updateScript}
          updateSpine={updateSpine}
          sourceFallback={sourceRangeText(ep.source_chapters)}
          restoreDrop={item => setDropWizard({ item, reason: '', rewrite: '', targetType: 'beat', targetIndex: 0, step: 1 })}
        />
      ) : (
        <ScreenplayReader
          script={script}
          epTitle={ep.title}
          expanded={manuscriptExpanded}
          setExpanded={setManuscriptExpanded}
          search={manuscriptSearch}
          setSearch={setManuscriptSearch}
          manuscriptRef={manuscriptRef}
          exportScript={exportScript}
          toast={toast}
          structureItems={structureItems}
        />
      )}

      {discardDraftOpen && (
        <DecisionDialog
          title="永久放弃工作草稿？"
          summary="未发布修改将无法恢复"
          message="本机草稿和云端工作草稿都会删除；当前已发布剧本及其下游产物不受影响。"
          details={[
            draft ? `当前草稿“${draft.title || '未命名剧本'}”将被移除` : '待恢复的工作草稿将被移除',
            '此操作不会删除已发布剧本',
          ]}
          confirmLabel="确认放弃草稿"
          cancelLabel="保留草稿"
          danger
          onClose={() => setDiscardDraftOpen(false)}
          onConfirm={() => {
            setDiscardDraftOpen(false)
            void clearWorkingDraft().then(() => toast('工作草稿已放弃'))
          }}
        />
      )}

      {stopConfirmOpen && (
        <DecisionDialog
          title="停止本集剧本任务？"
          summary={`第 ${ep.episode_no} 集《${ep.title}》仍在生成`}
          message="系统会停止当前剧本生成或局部修复；已写入的工作副本会保留，尚未发布的内容不会进入分镜。"
          details={[
            '停止可能需要等待当前模型请求返回，界面不会提前宣称已终止',
            '已经发生的模型调用费用不会退回；停止后可从工作副本恢复或重新发起',
          ]}
          confirmLabel="确认停止剧本任务"
          cancelLabel="继续生成"
          danger
          onClose={() => setStopConfirmOpen(false)}
          onConfirm={() => {
            setStopConfirmOpen(false)
            void stopScreenplay()
          }}
        />
      )}

      {conflict && (
        <div className="evidence-backdrop" role="presentation">
          <section ref={conflictTrapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label="剧本版本冲突">
            <h3>当前剧本已被更新</h3>
            <p>我的工作草稿仍完整保留，没有覆盖新发布版。</p>
            <p><code>{conflict.expected_version || '空基线'}</code> → <code>{conflict.current_version || '空版本'}</code></p>
            <ul>{(conflict.diff ?? []).map((item: any) => <li key={item.field}>{item.section} / {item.field}</li>)}</ul>
            <div className="dialog-actions">
              <button className="btn" onClick={() => setConflict(null)}>继续保留我的草稿</button>
              <button className="btn primary" onClick={async () => {
                const latest = await refresh()
                if (latest?.screenplay) beginEditing(latest.screenplay, latest.screenplay_artifact_id ?? null)
                setConflict(null)
              }}>重新加载发布版</button>
            </div>
          </section>
        </div>
      )}

      {preview && (
        <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
          if (event.currentTarget === event.target) setPreview(null)
        }}>
          <section ref={previewTrapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label={preview.title}>
            <h3>{preview.title}</h3>
            <p>{preview.kind === 'screenplay-save'
              ? '此预览只读，尚未发布。确认后才会写入新版本。'
              : '预检不会创建任务；只有点击下方执行按钮才会发起。'}</p>
            <ul>
              {preview.kind === 'screenplay' ? (
                <>
                  <li>原文 {preview.data.input?.source_chars ?? '—'} 字，选中 {preview.data.selected_count ?? 0} 处台词</li>
                  <li>口播估算 {preview.data.selected_seconds ?? 0}s / 目标 {preview.data.target_duration_s}s</li>
                  <li>{preview.data.estimate_note}</li>
                </>
              ) : preview.kind === 'screenplay-save' ? (
                <>
                  <li>{preview.data.unchanged ? '可编辑内容没有变化，确认后也不会创建新版本' : `变更 ${preview.data.diff?.length ?? 0} 个字段`}</li>
                  {(preview.data.diff ?? []).map((item: any) => (
                    <li key={item.field}>{item.section}：{item.before_chars} 字符 → {item.after_chars} 字符</li>
                  ))}
                  <li>{preview.data.impact}</li>
                </>
              ) : (
                <>
                  <li>安全恢复点：{preview.data.checkpoint?.available ? `从第 ${preview.data.checkpoint.resume_from_shot} 镜继续` : '无'}</li>
                  <li>安全恢复点：已保留前 {preview.data.kept_validated_shots ?? 0} 镜</li>
                  <li>{preview.data.impact}</li>
                  <li>{preview.data.estimate_note}</li>
                </>
              )}
            </ul>
            <div className="dialog-actions">
              <button className="btn" onClick={() => setPreview(null)}>取消（不执行）</button>
              <button className="btn primary" disabled={Boolean(preview.data.hard_exceeded)} onClick={executePreview}>
                {preview.kind === 'screenplay'
                  ? '启动首版剧本生成'
                  : preview.kind === 'storyboard-resume'
                    ? '继续生成分镜'
                    : preview.kind === 'screenplay-save'
                      ? (preview.data.unchanged ? '确认无变更' : '确认发布')
                      : '首次生成分镜'}
              </button>
            </div>
          </section>
        </div>
      )}

      {dropWizard && draft && (
        <div className="evidence-backdrop" role="presentation">
          <section ref={dropTrapRef} className="impact-dialog drop-wizard" role="dialog" aria-modal="true" aria-label="恢复为可拍内容">
            <h3>恢复“{dropWizard.item}”</h3>
            {dropWizard.step === 1 ? (
              <>
                <label className="f">恢复原因
                  <textarea rows={3} value={dropWizard.reason} onChange={event => setDropWizard({ ...dropWizard, reason: event.target.value })} />
                </label>
                <label className="f">改写为可见 / 可听的内容
                  <textarea rows={4} value={dropWizard.rewrite} onChange={event => setDropWizard({ ...dropWizard, rewrite: event.target.value })} />
                </label>
                <div className="dialog-actions">
                  <button className="btn" onClick={() => setDropWizard(null)}>取消</button>
                  <button className="btn primary" disabled={!dropWizard.reason.trim() || !dropWizard.rewrite.trim()} onClick={() => setDropWizard({ ...dropWizard, step: 2 })}>选择落点</button>
                </div>
              </>
            ) : (
              <>
                <label className="f">落入结构
                  <select value={dropWizard.targetType} onChange={event => setDropWizard({ ...dropWizard, targetType: event.target.value as 'beat' | 'scene', targetIndex: 0 })}>
                    <option value="beat">主线节拍</option><option value="scene">场次</option>
                  </select>
                </label>
                <select aria-label="选择具体落点" value={dropWizard.targetIndex} onChange={event => setDropWizard({ ...dropWizard, targetIndex: Number(event.target.value) })}>
                  {(dropWizard.targetType === 'beat' ? draft.plot_spine?.spine_beats ?? [] : draft.scene_outline ?? []).map((item: any, index: number) => (
                    <option key={index} value={index}>{dropWizard.targetType === 'beat' ? item.beat_id || `节拍 ${index + 1}` : item.scene_heading || `场 ${index + 1}`}</option>
                  ))}
                </select>
                <div className="drop-diff-preview"><b>草稿 diff</b><del>{dropWizard.item}</del><ins>{dropWizard.rewrite}</ins></div>
                <div className="dialog-actions">
                  <button className="btn" onClick={() => setDropWizard({ ...dropWizard, step: 1 })}>上一步</button>
                  <button className="btn primary" onClick={applyDropWizard}>写入工作草稿</button>
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </>
  )
}

function DialogueOption({ item, checked, disabled, onChange }: {
  item: DialogueOccurrence
  checked: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
}) {
  const [contextOpen, setContextOpen] = useState(false)
  return (
    <div className="screenplay-dialogue-option">
      <label>
        <input type="checkbox" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} />
        <span><em>D{String(item.order).padStart(3, '0')}</em>{item.text}</span>
      </label>
      <div className="dialogue-occurrence-meta">
        <span>{item.chapter ? `第 ${item.chapter} 章` : '本集原文'} · 段落 {item.paragraph} · 约 {item.estimated_seconds}s</span>
        {item.group_id && <span>建议上下文组 {item.group_id}</span>}
        <button type="button" className="btn small ghost" onClick={() => setContextOpen(value => !value)}>{contextOpen ? '收起上下文' : '查看上下文'}</button>
      </div>
      {contextOpen && <p>{item.context}</p>}
    </div>
  )
}

function ScreenplayEditor({
  draft,
  section,
  setSection,
  validation,
  updateScript,
  updateSpine,
  sourceFallback,
  restoreDrop,
}: {
  draft: EpisodeScreenplay
  section: EditorSection
  setSection: (section: EditorSection) => void
  validation: Record<EditorSection, string[]>
  updateScript: (patch: Partial<EpisodeScreenplay>) => void
  updateSpine: (patch: Partial<PlotSpine>) => void
  sourceFallback: string
  restoreDrop: (item: string) => void
}) {
  const tabs: [EditorSection, string][] = [['spine', '主线'], ['body', '正文'], ['scenes', '场次'], ['evidence', '依据与状态']]
  const beats = draft.plot_spine?.spine_beats ?? []
  const scenes = draft.scene_outline ?? []
  const stateChanges = draft.character_state_changes ?? []
  const panelId = 'screenplay-editor-panel'
  const focusSection = (next: EditorSection) => {
    setSection(next)
    window.requestAnimationFrame(() => {
      document.getElementById(`screenplay-editor-tab-${next}`)?.focus()
    })
  }
  const onTabKeyDown = (event: React.KeyboardEvent, index: number) => {
    let nextIndex = index
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = tabs.length - 1
    else return
    event.preventDefault()
    focusSection(tabs[nextIndex][0])
  }
  return (
    <section className="screenplay-editor-shell">
      <nav className="screenplay-editor-tabs" role="tablist" aria-label="剧本编辑目录">
        {tabs.map(([key, label], index) => (
          <button id={`screenplay-editor-tab-${key}`} key={key} type="button" role="tab"
            aria-selected={section === key} aria-controls={panelId} tabIndex={section === key ? 0 : -1}
            className={section === key ? 'active' : ''} onClick={() => setSection(key)}
            onKeyDown={event => onTabKeyDown(event, index)}>
            {label}{validation[key].length > 0 && <span>{validation[key].length}</span>}
          </button>
        ))}
      </nav>
      <div id={panelId} className="screenplay-editor-panel" role="tabpanel" aria-labelledby={`screenplay-editor-tab-${section}`}>
      {validation[section].length > 0 && <div className="editor-validation"><b>本区待修复</b>{validation[section].map(item => <span key={item}>{item}</span>)}</div>}

      {section === 'spine' && (
        <div className="card screenplay-editor-section">
          <label className="f">本集前提</label>
          <textarea aria-label="本集前提" rows={2} value={draft.plot_spine?.episode_premise ?? ''} onChange={event => updateSpine({ episode_premise: event.target.value })} />
          <div className="structured-section-head"><b>主线节拍</b><button className="btn small" type="button" onClick={() => updateSpine({ spine_beats: [...beats, { beat_id: `S${String(beats.length + 1).padStart(2, '0')}`, who: '', does: '', turn: '', must_keep: true }] })}>新增节拍</button></div>
          <div className="structured-list">
            {beats.map((beat, index) => (
              <article className="structured-row" key={`${beat.beat_id}-${index}`}>
                <header><b>{beat.beat_id || `S${index + 1}`}</b><StructuredListActions index={index} length={beats.length} onMove={direction => updateSpine({ spine_beats: moveItem(beats, index, direction) })} onDelete={() => updateSpine({ spine_beats: beats.filter((_, itemIndex) => itemIndex !== index) })} /></header>
                <div className="structured-fields">
                  <label>谁<input value={beat.who ?? ''} onChange={event => updateSpine({ spine_beats: beats.map((item, itemIndex) => itemIndex === index ? { ...item, who: event.target.value } : item) })} /></label>
                  <label>做了什么<textarea rows={2} value={beat.does ?? ''} onChange={event => updateSpine({ spine_beats: beats.map((item, itemIndex) => itemIndex === index ? { ...item, does: event.target.value } : item) })} /></label>
                  <label>局势变化<textarea rows={2} value={beat.turn ?? ''} onChange={event => updateSpine({ spine_beats: beats.map((item, itemIndex) => itemIndex === index ? { ...item, turn: event.target.value } : item) })} /></label>
                  <label className="check"><input type="checkbox" checked={beat.must_keep !== false} onChange={event => updateSpine({ spine_beats: beats.map((item, itemIndex) => itemIndex === index ? { ...item, must_keep: event.target.checked } : item) })} />必保留</label>
                </div>
              </article>
            ))}
          </div>
          <label className="f">必须收束</label>
          <textarea aria-label="必须收束" rows={2} value={draft.plot_spine?.must_keep_ending ?? ''} onChange={event => updateSpine({ must_keep_ending: event.target.value })} />
          <div className="structured-section-head"><b>本集不拍（默认排除）</b></div>
          <div className="drop-restore-list">
            {(draft.plot_spine?.drop_list ?? []).map((item, index) => (
              <div key={`${item}-${index}`}><textarea aria-label={`默认不拍内容 ${index + 1}`} rows={2} value={item} onChange={event => updateSpine({ drop_list: (draft.plot_spine?.drop_list ?? []).map((value, itemIndex) => itemIndex === index ? event.target.value : value) })} /><button type="button" className="btn small ghost" onClick={() => restoreDrop(item)}>改写为可拍内容</button></div>
            ))}
          </div>
        </div>
      )}

      {section === 'body' && (
        <div className="card script-editor editing">
          <div className="full"><label className="f">标题<input value={draft.title ?? ''} onChange={event => updateScript({ title: event.target.value })} /></label></div>
          <div className="full"><label className="f">原文来源范围<input value={draft.source_text_range ?? sourceFallback} onChange={event => updateScript({ source_text_range: event.target.value })} /></label><small>{!draft.source_text_range && '当前为本集章节范围的推断显示'}</small></div>
          <div className="full"><label className="f">本集一句话梗概<textarea rows={2} value={draft.logline ?? ''} onChange={event => updateScript({ logline: event.target.value })} /></label></div>
          <div><label className="f">本集戏剧问题<textarea rows={2} value={draft.dramatic_question ?? ''} onChange={event => updateScript({ dramatic_question: event.target.value })} /></label></div>
          <div><label className="f">主角目标<textarea rows={2} value={draft.protagonist_goal ?? ''} onChange={event => updateScript({ protagonist_goal: event.target.value })} /></label></div>
          <div><label className="f">阻力<textarea rows={2} value={draft.obstacle ?? ''} onChange={event => updateScript({ obstacle: event.target.value })} /></label></div>
          <div><label className="f">失败代价<textarea rows={2} value={draft.stakes ?? ''} onChange={event => updateScript({ stakes: event.target.value })} /></label></div>
          <div className="full"><label className="f">完整剧本正文 · {(draft.full_script_text ?? '').length.toLocaleString()} 字<textarea rows={24} value={draft.full_script_text ?? ''} onChange={event => updateScript({ full_script_text: event.target.value })} /></label></div>
          <div className="full"><label className="f">主线台词（每行一条）<textarea rows={5} value={(draft.key_lines ?? []).join('\n')} onChange={event => updateScript({ key_lines: splitLines(event.target.value) })} /></label></div>
          <div className="full"><label className="f">主线剧情点（每行一条）<textarea rows={5} value={(draft.key_plot_points ?? []).join('\n')} onChange={event => updateScript({ key_plot_points: splitLines(event.target.value) })} /></label></div>
        </div>
      )}

      {section === 'scenes' && (
        <div className="card screenplay-editor-section">
          <div className="structured-section-head"><b>场次结构</b><span>每个字段独立存储；正文中的 | 只是普通字符。</span><button className="btn small" type="button" onClick={() => updateScript({ scene_outline: [...scenes, { scene_no: scenes.length + 1, scene_heading: '', story_function: '', summary: '', conflict: '', turn: '', source_basis: '', characters: [] }] })}>新增场次</button></div>
          <div className="structured-list">
            {scenes.map((scene, index) => (
              <article className="structured-row" key={`${scene.scene_no}-${index}`}>
                <header><b>场 {index + 1}</b><StructuredListActions index={index} length={scenes.length} onMove={direction => updateScript({ scene_outline: moveItem(scenes, index, direction).map((item, itemIndex) => ({ ...item, scene_no: itemIndex + 1 })) })} onDelete={() => updateScript({ scene_outline: scenes.filter((_, itemIndex) => itemIndex !== index).map((item, itemIndex) => ({ ...item, scene_no: itemIndex + 1 })) })} /></header>
                <SceneFields scene={scene} update={patch => updateScript({ scene_outline: scenes.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item) })} />
              </article>
            ))}
          </div>
        </div>
      )}

      {section === 'evidence' && (
        <div className="card script-editor editing">
          <div className="full"><label className="f">原文依据<textarea rows={5} value={draft.source_basis ?? ''} onChange={event => updateScript({ source_basis: event.target.value })} /></label></div>
          <div className="full">
            <div className="structured-section-head"><b>主要人物状态变化</b><button className="btn small" type="button" onClick={() => updateScript({ character_state_changes: [...stateChanges, ''] })}>新增状态</button></div>
            <div className="structured-list compact">
              {stateChanges.map((value, index) => (
                <article className="structured-row" key={`state-${index}`}>
                  <header><b>状态 {index + 1}</b><StructuredListActions index={index} length={stateChanges.length} onMove={direction => updateScript({ character_state_changes: moveItem(stateChanges, index, direction) })} onDelete={() => updateScript({ character_state_changes: stateChanges.filter((_, itemIndex) => itemIndex !== index) })} /></header>
                  <textarea rows={2} aria-label={`人物状态变化 ${index + 1}`} value={value} onChange={event => updateScript({ character_state_changes: stateChanges.map((item, itemIndex) => itemIndex === index ? event.target.value : item) })} />
                </article>
              ))}
            </div>
          </div>
          <div><label className="f">情绪曲线<textarea rows={3} value={draft.emotional_curve ?? ''} onChange={event => updateScript({ emotional_curve: event.target.value })} /></label></div>
          <div><label className="f">结尾钩子<textarea rows={3} value={draft.ending_hook ?? ''} onChange={event => updateScript({ ending_hook: event.target.value })} /></label></div>
          <div className="full"><label className="f">改编方向<textarea rows={3} value={draft.adaptation_direction ?? ''} onChange={event => updateScript({ adaptation_direction: event.target.value })} /></label></div>
          <div><label className="f">开端摘要<textarea rows={2} value={draft.opening ?? ''} onChange={event => updateScript({ opening: event.target.value })} /></label></div>
          <div><label className="f">发展摘要<textarea rows={2} value={draft.development ?? ''} onChange={event => updateScript({ development: event.target.value })} /></label></div>
          <div><label className="f">冲突摘要<textarea rows={2} value={draft.conflict ?? ''} onChange={event => updateScript({ conflict: event.target.value })} /></label></div>
          <div><label className="f">高潮摘要<textarea rows={2} value={draft.climax ?? ''} onChange={event => updateScript({ climax: event.target.value })} /></label></div>
        </div>
      )}
      </div>
    </section>
  )
}

function SceneFields({ scene, update }: { scene: ScriptScene; update: (patch: Partial<ScriptScene>) => void }) {
  return (
    <div className="structured-fields scene-fields">
      <label>场次标题<input value={scene.scene_heading} onChange={event => update({ scene_heading: event.target.value })} /></label>
      <label>本场功能<input value={scene.story_function} onChange={event => update({ story_function: event.target.value })} /></label>
      <label className="wide">本场内容<textarea rows={3} value={scene.summary} onChange={event => update({ summary: event.target.value })} /></label>
      <label>冲突<textarea rows={2} value={scene.conflict ?? ''} onChange={event => update({ conflict: event.target.value })} /></label>
      <label>转折 / 交接<textarea rows={2} value={scene.turn ?? ''} onChange={event => update({ turn: event.target.value })} /></label>
      <label className="wide">原文依据<textarea rows={2} value={scene.source_basis ?? ''} onChange={event => update({ source_basis: event.target.value })} /></label>
      <label className="wide">角色（顿号或逗号分隔）<input value={(scene.characters ?? []).join('、')} onChange={event => update({ characters: event.target.value.split(/[、,，/]/).map(value => value.trim()).filter(Boolean) })} /></label>
    </div>
  )
}

function ScreenplayReader({
  script,
  epTitle,
  expanded,
  setExpanded,
  search,
  setSearch,
  manuscriptRef,
  exportScript,
  toast,
  structureItems,
}: {
  script: EpisodeScreenplay
  epTitle: string
  expanded: boolean
  setExpanded: (value: boolean) => void
  search: string
  setSearch: (value: string) => void
  manuscriptRef: React.RefObject<HTMLDivElement>
  exportScript: () => void
  toast: (message: string, error?: boolean) => void
  structureItems: Array<(string | undefined)[]>
}) {
  const spine = script.plot_spine
  const matches = search.trim() ? (script.full_script_text ?? '').toLowerCase().split(search.trim().toLowerCase()).length - 1 : 0
  return (
    <>
      {spine && (
        <section className="card spine-card" id="script-spine">
          <details open>
            <summary><b>主线骨架</b><span>保留本集故事主线；排除内容默认不拍</span></summary>
            <div className="shot-body">
              {spine.episode_premise && <div className="kv full"><b>本集前提</b>{spine.episode_premise}</div>}
              {!!spine.spine_beats?.length && <div className="kv full"><b>主线节拍</b><ol className="spine-beat-list">{spine.spine_beats.map((beat: PlotSpineBeat, index: number) => <li key={beat.beat_id || index}><code>{beat.beat_id || `S${index + 1}`}</code><span>{beat.who}｜{beat.does}→{beat.turn}</span>{beat.must_keep === false && <em className="spine-optional">可删过渡</em>}</li>)}</ol></div>}
              {spine.must_keep_ending && <div className="kv full"><b>必须收束</b>{spine.must_keep_ending}</div>}
              {!!spine.drop_list?.length && <div className="kv full"><b>本集不拍</b><ul className="key-list drop-list">{spine.drop_list.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
            </div>
          </details>
        </section>
      )}
      <div className="workspace-gap" />
      <section className="card script-editor">
        <div className="kv full"><b>标题</b>{script.title || epTitle}</div>
        <div className="kv full"><b>本集一句话梗概</b>{script.logline}</div>
        {!!script.key_lines?.length && <div className="kv full"><b>主线台词</b><ul className="key-list">{script.key_lines.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
        <div className={`kv full script-manuscript-section ${expanded ? 'expanded' : 'collapsed'}`}>
          <div className="script-manuscript-head">
            <div><b>完整剧本文本</b><span>{(script.full_script_text ?? '').length.toLocaleString()} 字 · {(script.full_script_text ?? '').split('\n').filter(Boolean).length} 行</span></div>
            <div className="manuscript-tools">
              <input type="search" value={search} onChange={event => { setSearch(event.target.value); setExpanded(true) }} placeholder="页内搜索" aria-label="搜索剧本正文" />
              {search && <span>{matches} 处</span>}
              <button className="btn small ghost" type="button" onClick={() => navigator.clipboard.writeText(script.full_script_text ?? '').then(() => toast('剧本正文已复制'))}>复制</button>
              <button className="btn small ghost" type="button" onClick={exportScript}>导出</button>
              <button type="button" className="script-manuscript-toggle" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起全文 ↑' : '展开全文 ↓'}</button>
            </div>
          </div>
          {expanded ? <div ref={manuscriptRef} className="script-manuscript"><HighlightedText text={script.full_script_text || '暂无完整剧本文本'} query={search} /></div> : <button type="button" className="script-manuscript-collapsed" onClick={() => setExpanded(true)}><span>正文已收起</span><small>点击展开并阅读完整剧本</small></button>}
        </div>
        <div className="kv"><b>情绪曲线说明</b>{script.emotional_curve}</div>
        <div className="kv"><b>结尾钩子</b>{script.ending_hook}</div>
        <div className="kv full"><b>原文依据</b>{script.source_basis}</div>
      </section>

      {!!script.scene_outline?.length && <><div className="workspace-gap" /><section className="card"><details open><summary><b>场次结构</b><span>可跳转到正文对应场次</span></summary><div className="scene-outline-grid">{script.scene_outline.map(scene => <article key={scene.scene_no} className="scene-outline-card"><div className="scene-outline-head"><span className="sn">场{scene.scene_no}</span><span className="meta">{scene.scene_heading}</span><button className="btn small ghost" onClick={() => { setExpanded(true); setSearch(scene.scene_heading); window.setTimeout(() => manuscriptRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0) }}>跳转正文</button></div><div className="scene-outline-body"><div className="kv full"><b>本场功能</b>{scene.story_function}</div><div className="kv full"><b>本场内容</b>{scene.summary}</div>{scene.conflict && <div className="kv"><b>冲突</b>{scene.conflict}</div>}{scene.turn && <div className="kv"><b>转折 / 交接</b>{scene.turn}</div>}{scene.source_basis && <div className="kv full"><b>原文依据</b>{scene.source_basis}</div>}</div></article>)}</div></details></section></>}

      {structureItems.length > 0 && <><div className="workspace-gap" /><section className="card auxiliary-structure"><details><summary><b>辅助结构</b><span>与主线 / 场次重复的内容默认折叠</span></summary><div className="shot-body">{structureItems.map(([label, value]) => <div key={label} className="kv full"><b>{label}</b>{value}</div>)}</div></details></section></>}
    </>
  )
}
