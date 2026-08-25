import { useMemo, useState } from 'react'
import {
  api,
  Bible,
  EpisodePrepPack,
  PrepPackCoverageEntry,
  PrepPackCoverageLedger,
  PrepPackEvent,
  numToCn,
} from '../api'
import { useNav, useProject, useScriptEpisode } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import DecisionDialog from '../components/DecisionDialog'
import { ServerTaskTimer } from '../components/TaskTimer'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import { ScreenplayStatusStamp } from '../components/ProductionStatusStamp'
import QueryState from '../components/QueryState'
import OperationError from '../components/OperationError'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { screenplayTaskNotice } from '../lib/productionNotices'

type ScreenplayProduction = NonNullable<
  NonNullable<ReturnType<typeof useScriptEpisode>['data']>['screenplay_production']
>

type ActionPreview = {
  title: string
  data: Record<string, any>
  idempotencyKey: string
}

const sourceRangeText = (chapters: number[]) => chapters.length <= 1
  ? `第 ${chapters[0] ?? '-'} 章`
  : `第 ${chapters[0]}-${chapters[chapters.length - 1]} 章`

const stableKey = (prefix: string) => `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`

export function screenplayResumeActionLabel(
  production: ScreenplayProduction | null | undefined,
): string {
  if (production?.mode_label) return production.mode_label
  return production?.can_resume_baseline
    ? '继续首版场次生成'
    : '继续完整剧本校验'
}

export function screenplayResumeOutcomeSummary(
  outcome: { summary?: string; mode?: ScreenplayProduction['operation'] },
): string {
  if (outcome.summary?.trim()) return outcome.summary
  if (outcome.mode === 'baseline_rebuild') return '已按当前合同启动剧本基线重建'
  if (outcome.mode === 'baseline') return '已从安全检查点继续首版场次生成'
  return '完整剧本工作副本已继续执行结构校验、评分与发布'
}

export function screenplayGeneratePayload(
  idempotencyKey: string,
  blueprintBudget?: {
    requires_fresh_retry_grant?: boolean
    unknown_receipts?: unknown
  } | null,
): Record<string, unknown> {
  const payload: Record<string, unknown> = { idempotency_key: idempotencyKey }
  // 上次供应商结果未知时，后端围栏同一语义 operation 的重付；经用户在预检弹窗二次
  // 确认后，显式携带授权与期望的未知收据，解开成本保护闸门。非该场景绝不带授权字段。
  if (blueprintBudget?.requires_fresh_retry_grant) {
    payload.authorize_blueprint_retry = true
    payload.expected_blueprint_unknown_receipts = Array.isArray(
      blueprintBudget.unknown_receipts,
    )
      ? blueprintBudget.unknown_receipts
      : []
  }
  return payload
}

/** 旧产物（转型前的重型剧本）没有这个字段；出现即说明后端还没换成准备包，前端绝不按旧形状渲染。 */
export function isPrepPack(value: unknown): value is EpisodePrepPack {
  if (!value || typeof value !== 'object') return false
  const version = (value as { prep_pack_version?: unknown }).prep_pack_version
  return typeof version === 'string' && version.length > 0
}

export function sortedEventChain(events: PrepPackEvent[] | null | undefined): PrepPackEvent[] {
  return [...(events ?? [])].sort((a, b) => (Number(a?.order) || 0) - (Number(b?.order) || 0))
}

/** 覆盖账本条目的确切子形状后端未冻结（示例只给了空数组）；数字或 {segment_index} 都按原文段号解析。 */
function coverageEntryLabel(entry: PrepPackCoverageEntry): string {
  if (typeof entry === 'number') return String(entry)
  if (entry && typeof entry === 'object' && 'segment_index' in entry && entry.segment_index != null) {
    return String((entry as { segment_index: number | string }).segment_index)
  }
  return JSON.stringify(entry)
}

export function coverageGateSummary(ledger: PrepPackCoverageLedger | null | undefined) {
  const uncovered = ledger?.uncovered ?? []
  const delivered = ledger?.delivered ?? []
  const merged = ledger?.merged ?? []
  const retained = ledger?.retained_as_context ?? []
  const duplicates = ledger?.proven_duplicates ?? []
  // 第五账（1.4.0+，1.3.0 及更早产物没有它）：副文本是合法覆盖，并入"已覆盖"总数，
  // 不算未覆盖——uncovered 数组本身已经是权威来源，这里不需要、也不应该从 uncovered
  // 里减去 paratext，只是把它计进展示用的覆盖计数。
  const paratext = ledger?.paratext ?? []
  return {
    ok: uncovered.length === 0,
    uncoveredCount: uncovered.length,
    uncoveredLabels: uncovered.map(coverageEntryLabel),
    deliveredCount: delivered.length,
    mergedCount: merged.length,
    retainedCount: retained.length,
    duplicateCount: duplicates.length,
    paratextCount: paratext.length,
    paratextLabels: paratext.map(coverageEntryLabel),
    totalSegments: ledger?.total_segments ?? 0,
  }
}

/** 复用 BiblePage 展示 character_portraits 的口径：在项目人物谱的 portraits[] 里按 id 查图。 */
export function findPortraitImage(bible: Bible | null | undefined, portraitId: string | null | undefined): string | null {
  if (!portraitId) return null
  for (const character of bible?.characters ?? []) {
    const match = (character.portraits ?? []).find(portrait => portrait.id === portraitId)
    if (match?.image_url) return match.image_url
  }
  return null
}

/** 复用 ScenesPage 展示 scene_references 的口径：在项目场景库的 scene_refs[] 里按 id 查图。 */
export function findSceneReferenceImage(bible: Bible | null | undefined, sceneReferenceId: string | null | undefined): string | null {
  if (!sceneReferenceId) return null
  for (const scene of bible?.scenes ?? []) {
    const match = (scene.scene_refs ?? []).find(ref => ref.id === sceneReferenceId)
    if (match?.image_url) return match.image_url
  }
  return null
}

/**
 * 画面与字幕分离（1.7.0+，见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.3）：
 * display_appellation 是本集原文对这个角色的称呼（不提前剧透 display_name 这个
 * 全局规范名）。两者不同时才需要单独标出本集称谓；相同、或字段缺失（旧产物 /
 * 未绑定成功前）时都退回 null——调用方据此只显示 canonicalName，不重复、不空渲染。
 */
export function characterAppellationTag(
  character: { display_appellation?: string | null },
  canonicalName: string,
): string | null {
  const appellation = (character.display_appellation || '').trim()
  if (!appellation || appellation === canonicalName.trim()) return null
  return appellation
}

/** provenance.method 已知取值 -> 低调中文提示；未识别的取值原样透传，不吞信息。 */
const PROVENANCE_METHOD_LABELS: Record<string, string> = {
  direct: '直接匹配',
  alias: '别名匹配',
  resolution: '本集消歧',
  resolution_forward: '前瞻章节判定',
  candidate_verdict: '候选判别',
  discovery: '新角色发现',
  alias_inherited: '跨集别名继承',
}

/** 供角色卡 title 悬浮提示用；method 缺失（旧产物 1.6.0 之前）时返回 null，不渲染空提示。 */
export function provenanceMethodHint(method: string | null | undefined): string | null {
  const trimmed = (method || '').trim()
  if (!trimmed) return null
  return `绑定依据：${PROVENANCE_METHOD_LABELS[trimmed] ?? trimmed}`
}

// 轻量流程的真实阶段列表由后端下发（目标形状 {key, display_name, state}，
// state: pending/active/done/blocked）。实测后端落地是渐进的：当前仍在发旧形状
// {key, label, status}（十步重型流水线遗留），若只读 display_name/state 会读到
// undefined、渲出空文本——这正是"十个框还在但字没了"的成因。normalizeStage 做
// 三级文本回退 + state/status 双读，新旧两种形状、以及字段整个缺失，都不会空。
const STAGE_STATE_LABELS: Record<string, string> = {
  pending: '待开始',
  queued: '待开始',
  active: '进行中',
  in_progress: '进行中',
  running: '进行中',
  blocked: '门禁未通过',
  failed: '异常中断',
  paused: '已暂停',
  done: '已完成',
  completed: '已完成',
}

/** 未识别的 state 原样显示，不假装认识、不吞掉信息——state 枚举确定前的安全带。 */
export function stageStateLabel(state: string): string {
  return STAGE_STATE_LABELS[state] ?? state
}

export type StageTone = 'done' | 'active' | 'blocked' | 'pending'

export function stageStateTone(state: string): StageTone {
  if (state === 'done' || state === 'completed') return 'done'
  if (state === 'active' || state === 'in_progress' || state === 'running') return 'active'
  if (state === 'blocked' || state === 'failed') return 'blocked'
  return 'pending'
}

export type RawStage = {
  key: string
  display_name?: string
  label?: string
  state?: string
  status?: string
}

export type NormalizedStage = {
  key: string
  text: string
  tone: StageTone
  stateLabel: string
}

/** display_name ?? label ?? key 三级回退（空字符串也视为缺失，继续往下退）；
 *  state ?? status 同理，缺失时按 pending 处理。任何输入形状都保证 text 非空。 */
export function normalizeStage(stage: RawStage, index: number): NormalizedStage {
  const text = (stage.display_name || '').trim()
    || (stage.label || '').trim()
    || (stage.key || '').trim()
    || `阶段 ${index + 1}`
  const rawState = (stage.state || '').trim() || (stage.status || '').trim() || 'pending'
  return {
    key: (stage.key || '').trim() || String(index),
    text,
    tone: stageStateTone(rawState),
    stateLabel: stageStateLabel(rawState),
  }
}

/**
 * 阶段列表选源：只读 prep_pack_stages（已定稿的轻量流程真实阶段，2026-08-24 后端
 * 上线，4-5 步），不再回退旧 stages（十步重型流水线遗留）。
 *
 * 用户报告过首屏闪现旧十步阶段带——根因是曾经的"prep_pack_stages 缺失/为空时
 * 回退渲染旧 stages"逻辑：后端集详情投影统一到新阶段单源的过程中有短暂窗口两个
 * 字段状态不一致，回退分支就把旧十步顺带渲了出来。这里直接不读旧字段，让这种
 * 闪现在物理上不可能发生——不是"概率更低"，是这条代码路径已经不存在。
 * PrepStepper 内部仍对 prep_pack_stages 自身的字段（display_name/label/key、
 * state/status）做三级防御，那是防这个字段自己漏子字段，跟旧 stages 无关。
 */
export function resolveStages(production: {
  prep_pack_stages?: RawStage[]
} | null | undefined): RawStage[] {
  return production?.prep_pack_stages ?? []
}

/** 紧凑步进器：小号数字圆点 + 短标签，单行排布可换行；不管后端发来几步都不占大面积。 */
export function PrepStepper({ stages }: { stages: RawStage[] }) {
  return (
    <ol className="prep-stepper" aria-label="剧本制作阶段">
      {stages.map((stage, index) => {
        const normalized = normalizeStage(stage, index)
        return (
          <li key={normalized.key} className="prep-stepper-item" data-tone={normalized.tone}>
            <span className="prep-stepper-dot" aria-hidden="true">{normalized.tone === 'done' ? '✓' : index + 1}</span>
            <span className="prep-stepper-label" title={normalized.stateLabel}>{normalized.text}</span>
          </li>
        )
      })}
    </ol>
  )
}

/** 事件原文段区间（1.1.0 起下发，更早的产物没有该字段）；单段与多段分别措辞，缺失时返回 null。 */
export function formatSourceSpan(span: { from_segment: number; to_segment: number } | null | undefined): string | null {
  if (!span || span.from_segment == null || span.to_segment == null) return null
  return span.from_segment === span.to_segment
    ? `覆盖原文段 ${span.from_segment}`
    : `覆盖原文段 ${span.from_segment}-${span.to_segment}`
}

export function ScreenplayResumeButton({
  production,
  busy,
  onResume,
}: {
  production: ScreenplayProduction | null | undefined
  busy: boolean
  onResume: () => void
}) {
  const label = screenplayResumeActionLabel(production)
  return (
    <button type="button" className="btn primary" disabled={busy}
      aria-label={busy ? `${label}，暂不可用：正在处理上一项操作` : label}
      title={busy ? '正在处理上一项操作' : undefined} onClick={onResume}>
      {label}
    </button>
  )
}

export default function ScriptPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep, refresh, error, status, loading } = useScriptEpisode(episodeId!)
  const { data: project } = useProject(projectId!, 0, 'bible')
  const [busy, setBusy] = useState(false)
  const [detailsExpanded, setDetailsExpanded] = useState(false)
  const [preview, setPreview] = useState<ActionPreview | null>(null)
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false)
  const previewTrapRef = useFocusTrap(Boolean(preview), () => setPreview(null))

  const screenplayTaskActive = ep?.screenplay_production?.task_active
    ?? ['queued', 'running'].includes(ep?.screenplay_status ?? '')
  const canResumeBaseline = ep?.screenplay_production?.can_resume_baseline ?? false
  // 信任后端状态机：repairing 但无兼容 checkpoint 时后端会给 generate_screenplay，
  // 前端不再用 `status==='repairing'` 猜测续跑，否则会误挡首次生成主操作。
  const canResumeRepair = ep?.screenplay_production?.can_resume_repair ?? false
  const canResumeFlow = canResumeBaseline || canResumeRepair
  const screenplayNotice = ep ? screenplayTaskNotice(ep) : null

  // React #310 事故教训：这个 useMemo 曾经写在下面 `if (!ep) return` 之后——首次渲染
  // ep 为空时提前返回、这个 hook 从未被调用；数据到达后的下一次渲染会走到这里，
  // hook 调用数比上一次渲染多了一个，直接违反 Rules of Hooks 炸出 #310。
  // 所有 hooks（含 useMemo/useState/自定义 hook）必须无条件出现在组件顶部、
  // 出现在任何 return 之前；条件渲染只允许发生在 JSX/返回值层面，这里用 `ep?.` 可选链
  // 而不是等 `ep` 确定非空后再读，就是为了让这个 useMemo 能安全地留在提前 return 之前。
  // 选源逻辑见 resolveStages：prep_pack_stages 存在且非空优先，否则回退旧 stages。
  const stages = resolveStages(ep?.screenplay_production)
  const normalizedStages = useMemo(() => stages.map(normalizeStage), [stages])
  const activeStage = normalizedStages.find(item => item.tone === 'active')
  const generatingHint = activeStage ? `正在${activeStage.text}…` : '正在生成准备包…'

  const run = async (fn: () => Promise<any>, done?: string) => {
    setBusy(true)
    try {
      const result = await fn()
      if (done) toast(done)
      await refresh({ force: true })
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

  const openScreenplayPreview = async () => {
    if (!ep || canResumeFlow) return
    setBusy(true)
    try {
      const data = await api.post(`/episodes/${ep.id}/screenplay/preflight`, {})
      setPreview({
        title: '首次生成准备包预检',
        data,
        idempotencyKey: stableKey(`screenplay:${ep.id}`),
      })
    } catch (previewError) {
      toast((previewError as Error).message, true)
    } finally { setBusy(false) }
  }

  const executePreview = async () => {
    if (!preview || !ep) return
    const current = preview
    setPreview(null)
    await run(() => api.post(
      `/episodes/${ep.id}/screenplay`,
      screenplayGeneratePayload(current.idempotencyKey, current.data?.blueprint_budget),
    ), '准备包生成任务已受理').catch(() => undefined)
  }

  const resumeRepair = async () => {
    if (!ep) return
    const result = await run(() => api.post(`/episodes/${ep.id}/screenplay/resume`, {
      idempotency_key: stableKey(`screenplay-resume:${ep.id}`),
    })).catch(() => null)
    if (!result) return
    toast(screenplayResumeOutcomeSummary(result))
  }

  const stopScreenplay = async () => {
    if (!ep) return
    const result = await run(() => api.post(`/episodes/${ep.id}/screenplay/cancel`, {}))
      .catch(() => null)
    if (result?.status === 'cancelling') toast('正在取消，尚未宣称已停止')
    else if (result) toast(`任务已终止；${result.resume_available ? '可从工作副本恢复' : '可重新发起'}`)
  }

  const deleteCurrentScreenplay = async () => {
    if (!ep) return
    try {
      const result = await run(() => api.del(`/episodes/${ep.id}/screenplay`))
      if (result) toast('当前准备包及下游已删除')
    } catch { /* run 已呈现结果 */ }
  }

  if (!ep) {
    return (
      <QueryState
        loading={loading}
        error={error}
        status={status}
        hasData={false}
        objectName="剧本台"
        loadingText="正在加载剧本与本集状态…"
        emptyText="未找到可展示的剧本数据，请刷新后重试。"
        onRetry={() => void refresh()}
      >
        {null}
      </QueryState>
    )
  }

  const state = ep.screenplay_state ?? {
    message: '状态同步中',
    recommended_action: 'refresh' as const,
  }
  // 直接透传后端已压好的一句话状态；后端已区分"准备包已交付｜分镜生成中/停在第 N 镜/待人工确认"等细节。
  const screenplayStateMessage = state.message
  const screenplayGenerateDisabledReason = busy ? '正在处理上一项操作' : ''

  const primaryAction = () => {
    switch (state.recommended_action) {
      case 'generate_screenplay':
        return <button className="btn primary" disabled={Boolean(screenplayGenerateDisabledReason)}
          aria-label={screenplayGenerateDisabledReason ? `首次生成准备包，暂不可用：${screenplayGenerateDisabledReason}` : '首次生成准备包'}
          title={screenplayGenerateDisabledReason || '生成前将展示输入范围'} onClick={openScreenplayPreview}>首次生成准备包</button>
      case 'stop_screenplay':
        return <button className="btn ghost danger" disabled={busy}
          aria-label={busy ? '停止准备包任务，暂不可用：正在处理上一项操作' : '停止准备包任务'}
          title={busy ? '正在处理上一项操作' : '停止前会说明费用和保留范围'} onClick={() => setStopConfirmOpen(true)}>停止准备包任务</button>
      case 'resume_screenplay':
        return <ScreenplayResumeButton
          production={ep.screenplay_production}
          busy={busy}
          onResume={() => void resumeRepair()}
        />
      case 'generate_storyboard':
      case 'resume_storyboard':
      case 'view_storyboard':
        return <button className="btn primary"
          title="分镜的生成、续跑和确认统一在分镜台完成"
          onClick={() => go('board', projectId, ep.id)}>进入分镜台</button>
      case 'view_save_progress':
        return <button className="btn primary" disabled>正在安全停止下游…</button>
      case 'view_cancel_progress':
        return <button className="btn primary" disabled>正在等待任务停止…</button>
      default:
        return <button className="btn primary" disabled={busy} onClick={() => refresh()}>刷新状态</button>
    }
  }

  // 后端把两种产物形状投影到不同字段（见 Episode.prep_pack 注释）：新形状在 prep_pack，
  // screenplay 此时为 null；旧形状仍在 screenplay。两个字段都要看，不能只看其中一个。
  const packRaw = ep.prep_pack ?? null

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="剧本台" view="script" episodeNo={ep.episode_no} />
        <h1>剧本台 <span className="sub">《{ep.title}》 · 先备齐本集事件链与资源，再进入镜头设计</span></h1>
        <hr className="rule" />
      </header>

      <section className="card script-toolbar">
        <div className="screenplay-primary-row">
          <div className="screenplay-state-copy">
            <div><ScreenplayStatusStamp status={ep.screenplay_status} /></div>
            <strong>{screenplayStateMessage}</strong>
          </div>
          <div className="screenplay-primary-actions">
            {primaryAction()}
            {ep.screenplay_status === 'ready' && state.recommended_action !== 'view_storyboard' && (
              <button className="btn ghost" type="button" onClick={() => go('board', projectId, ep.id)}>查看分镜台 →</button>
            )}
          </div>
        </div>

        {/* 紧凑步进器只读 prep_pack_stages 单源（见 resolveStages）；旧十步重型流水线
            的大灰框不会再作为回退出现——prep_pack_stages 缺失/为空时要么渲染与
            步进器同高的占位骨架（production 存在、阶段数据在路上），要么什么都不渲染
            （连 production 都没有，没有阶段概念可言）。 */}
        {ep.screenplay_production && (
          <div className="prep-stepper-block">
            {screenplayTaskActive && ep.screenplay_production?.task_started_at && (
              <p className="prep-stepper-status" role="status">
                <ServerTaskTimer
                  label={activeStage ? `正在${activeStage.text}` : '生成中'}
                  startedAt={ep.screenplay_production.task_started_at}
                  finishedAt={ep.screenplay_production.task_finished_at}
                  running={screenplayTaskActive}
                />
              </p>
            )}
            {stages.length > 0
              ? <PrepStepper stages={stages} />
              : <div className="prep-stepper-skeleton" aria-label="阶段信息加载中" />}
          </div>
        )}

        <div className="screenplay-secondary-row">
          {!screenplayTaskActive && (ep.screenplay || packRaw || canResumeFlow) && (
            <button className="btn ghost danger" disabled={busy} onClick={deleteCurrentScreenplay}>
              {(ep.screenplay || packRaw) ? '删除当前准备包' : '删除失败准备包'}
            </button>
          )}
          <span className="screenplay-row-spacer" />
          {ep.screenplay_evidence && <EvidenceDrawer evidence={ep.screenplay_evidence} label="准备包证据" />}
          <ServerTaskTimer
            label="准备包"
            startedAt={ep.screenplay_production?.task_started_at}
            finishedAt={ep.screenplay_production?.task_finished_at}
            running={screenplayTaskActive}
          />
        </div>

        <button type="button" className="script-details-toggle" onClick={() => setDetailsExpanded(value => !value)} aria-expanded={detailsExpanded}>
          {detailsExpanded ? '收起详情' : '查看计时、来源与技术详情'}
        </button>
        {detailsExpanded && (
          <div className="screenplay-detail-grid">
            <div className="kv"><b>当前分集</b>第{numToCn(ep.episode_no)}集</div>
            <div className="kv"><b>原文来源范围</b>{isPrepPack(packRaw)
              ? `${sourceRangeText(packRaw.episode_scope?.chapter_indexes ?? ep.source_chapters)} · 原文段 ${packRaw.episode_scope?.source_segment_count ?? '—'} 段`
              : sourceRangeText(ep.source_chapters)}</div>
            <div className="kv"><b>准备包状态</b>{ep.screenplay_status ?? 'unknown'}</div>
            {ep.screenplay_production && (
              <div className="kv"><b>当前阶段</b>
                {ep.screenplay_production.phase_label ?? ep.screenplay_production.phase} ·
                第 {(ep.screenplay_production.stage_index ?? 0) + 1}/
                {ep.screenplay_production.stage_count ?? ep.screenplay_production.stages?.length ?? 1} 段
              </div>
            )}
          </div>
        )}
        {screenplayNotice && (
          <OperationError
            title={screenplayNotice.severity === 'error' ? '剧本流程未完成' : '剧本流程等待继续'}
            message={screenplayNotice.message}
            guidance={screenplayNotice.severity === 'error'
              ? '已发布准备包和工作草稿会保留。请按顶部主操作重新生成。'
              : '这不是失败结果；工作副本和安全恢复点已保留，可按顶部主操作继续流程。'}
            variant={screenplayNotice.severity}
            detailLabel={screenplayNotice.severity === 'error' ? '查看剧本错误详情' : '查看剧本处理详情'}
          />
        )}
      </section>

      <div className="workspace-gap" />

      {isPrepPack(packRaw) ? (
        <PrepPackView pack={packRaw} bible={project?.bible} sourceFallback={sourceRangeText(ep.source_chapters)} />
      ) : ep.screenplay ? (
        <section className="card">
          <div className="empty">
            <div className="big">旧</div>
            旧版产物，转型后需重新生成
            <br />
            <small>本集内容仍是转型前的格式，界面无法解析展示；请使用顶部主操作重新生成准备包。</small>
          </div>
        </section>
      ) : screenplayTaskActive ? (
        // 生成中还没有可展示的准备包内容：不摆大面积空框，主视觉就是上面的步进器 + 计时 + 这一句。
        <section className="card prep-generating-hint" role="status">
          <p>{generatingHint}</p>
        </section>
      ) : (
        <div className="empty screenplay-mobile-summary"><div className="big">备</div>{screenplayStateMessage}<br />请使用顶部唯一主操作</div>
      )}

      {stopConfirmOpen && (
        <DecisionDialog
          title="停止本集剧本任务？"
          summary={`第 ${ep.episode_no} 集《${ep.title}》仍在生成`}
          message="系统会停止当前准备包生成或局部修复；已写入的工作副本会保留，尚未发布的内容不会进入分镜。"
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

      {preview && (
        <div className="evidence-backdrop" role="presentation" onMouseDown={event => {
          if (event.currentTarget === event.target) setPreview(null)
        }}>
          <section ref={previewTrapRef} className="impact-dialog" role="dialog" aria-modal="true" aria-label={preview.title}>
            <h3>{preview.title}</h3>
            <p>预检不会创建任务；只有点击下方执行按钮才会发起。</p>
            <ul>
              <li>
                原文 {preview.data.input?.source_chars ?? '—'} 字，
                覆盖 {preview.data.input?.source_chapters?.length ?? '—'} 个源章节
              </li>
              {preview.data.blueprint_budget?.requires_fresh_retry_grant && (
                <li className="danger">
                  上次生成的模型调用被中断、结果未知（常见于服务重启或网络波动）。
                  为避免重复扣费，系统已暂停自动重试；确认继续将授权对同一环节重新发起一次付费调用。
                </li>
              )}
            </ul>
            <div className="dialog-actions">
              <button className="btn" onClick={() => setPreview(null)}>取消（不执行）</button>
              <button className="btn primary" onClick={executePreview}>
                {preview.data.blueprint_budget?.requires_fresh_retry_grant
                  ? '授权并重试（可能重新计费）'
                  : '启动首版准备包生成'}
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  )
}

export function PrepPackView({
  pack,
  bible,
  sourceFallback,
}: {
  pack: EpisodePrepPack
  bible: Bible | null | undefined
  sourceFallback: string
}) {
  const events = useMemo(() => sortedEventChain(pack.event_chain), [pack.event_chain])
  const gate = useMemo(() => coverageGateSummary(pack.coverage_ledger), [pack.coverage_ledger])
  const characters = pack.asset_manifest?.characters ?? []
  const scenes = pack.asset_manifest?.scenes ?? []
  const functionalExtras = pack.asset_manifest?.functional_extras ?? []
  const scopeText = pack.episode_scope?.chapter_indexes?.length
    ? sourceRangeText(pack.episode_scope.chapter_indexes)
    : sourceFallback
  const coveredSegments = gate.totalSegments
    || gate.deliveredCount + gate.mergedCount + gate.retainedCount + gate.duplicateCount + gate.paratextCount

  return (
    <>
      {/* 门禁状态灯：全宽细条带，不占大块版面 */}
      <div className={`prep-gate-strip ${gate.ok ? 'ok' : 'blocked'}`} role="status">
        <span className="prep-gate-strip-icon" aria-hidden="true">{gate.ok ? '✓' : '!'}</span>
        <strong>{gate.ok
          ? `全部原文段已覆盖（${coveredSegments}/${coveredSegments} 段）`
          : `原文覆盖门禁未通过 · 缺失 ${gate.uncoveredCount} 段`}</strong>
        <span className="prep-gate-strip-chips">
          <span className="prep-gate-chip">已交付 {gate.deliveredCount}</span>
          <span className="prep-gate-chip">已合并 {gate.mergedCount}</span>
          <span className="prep-gate-chip">保留上下文 {gate.retainedCount}</span>
          <span className="prep-gate-chip">判定重复 {gate.duplicateCount}</span>
          {/* 第五账（1.4.0+）：副文本，仅非空时显示；没有展开交互，用 title 承载段号列表。 */}
          {gate.paratextCount > 0 && (
            <span className="prep-gate-chip" title={`原文段：${gate.paratextLabels.join('、')}`}>
              副文本 {gate.paratextCount} 段
            </span>
          )}
        </span>
      </div>
      {!gate.ok && (
        <p className="prep-gate-missing">缺失原文段索引：{gate.uncoveredLabels.join('、') || '未知'}</p>
      )}

      {/* 主体两栏：左 2/3 事件链（含末尾 hook/cliffhanger），右 1/3 粘性侧栏 */}
      <div className="prep-pack-layout">
        <div className="prep-pack-main">
          <section className="card">
            <div className="card-heading-row"><h3>事件链<span className="hint">{events.length} 个事件 · 按剧情顺序</span></h3></div>
            {events.length ? (
              <ol className="prep-timeline">
                {events.map(event => {
                  const span = formatSourceSpan(event.source_span)
                  return (
                    <li key={event.event_id || event.order} className="prep-timeline-item">
                      <span className="prep-timeline-marker" aria-hidden="true">{event.order}</span>
                      <details className="prep-timeline-details">
                        <summary className="prep-timeline-summary">
                          <span className="prep-timeline-headline">{event.summary || '（未填写事件概述）'}</span>
                          {span && <span className="prep-timeline-span">{span}</span>}
                        </summary>
                        <div className="prep-timeline-body">
                          <div>
                            <b>原文依据</b>
                            {event.source_evidence?.length ? (
                              <ul className="prep-timeline-quotes">
                                {event.source_evidence.map((item, index) => (
                                  <li key={index}>“{item.quote}”<span className="prep-timeline-idx">#{item.segment_index}</span></li>
                                ))}
                              </ul>
                            ) : <span>无</span>}
                          </div>
                          <div>
                            <b>关键台词</b>
                            {event.key_lines?.length ? (
                              <ul className="prep-timeline-lines">
                                {event.key_lines.map((line, index) => (
                                  <li key={index}>{line.speaker}：{line.line}<span className="prep-timeline-idx">#{line.segment_index}</span></li>
                                ))}
                              </ul>
                            ) : <span>无</span>}
                          </div>
                        </div>
                      </details>
                    </li>
                  )
                })}
              </ol>
            ) : <p className="prep-empty-hint">本集准备包尚无事件</p>}
          </section>

          <section className="card">
            <div className="card-heading-row"><h3>Hook / Cliffhanger<span className="hint">集级叙事钩子</span></h3></div>
            <div className="prep-quote-grid">
              <div className={`prep-quote-card ${pack.hook ? '' : 'empty'}`}>
                <b>Hook</b>
                <p>{pack.hook || '（空）'}</p>
              </div>
              <div className={`prep-quote-card ${pack.cliffhanger ? '' : 'empty'}`}>
                <b>Cliffhanger</b>
                <p>{pack.cliffhanger || '（空）'}</p>
              </div>
            </div>
          </section>
        </div>

        <aside className="prep-pack-sidebar">
          <section className="card prep-scope-card">
            <h3 className="prep-sidebar-heading">本集范围</h3>
            <p>{scopeText}</p>
            <p className="prep-sidebar-meta">原文段 {pack.episode_scope?.source_segment_count ?? '—'} 段</p>
          </section>

          <section className="card">
            <h3 className="prep-sidebar-heading">出场角色 · {characters.length}</h3>
            <div className="prep-roster">
              {characters.map(character => {
                const imageUrl = findPortraitImage(bible, character.portrait_id)
                const name = character.display_name || character.identity_id || '未命名角色'
                // 本集称谓（display_appellation）单独标出；旧的 aliases 小签保留，但
                // 去掉与本集称谓重复的那一条，避免同一句话在同一行出现两遍。
                const appellation = characterAppellationTag(character, name)
                const aliases = (character.aliases?.filter(alias => alias.trim()) ?? [])
                  .filter(alias => alias !== appellation)
                const provenanceHint = provenanceMethodHint(character.provenance?.method)
                return (
                  <div className="prep-roster-item" key={character.identity_id || character.display_name}>
                    {imageUrl
                      ? <img className="prep-roster-thumb" src={imageUrl} alt={name} loading="lazy" decoding="async" />
                      : <div className="prep-roster-thumb-empty" aria-hidden="true">无图</div>}
                    <div className="prep-roster-body">
                      <span className="prep-roster-name">
                        <span className="prep-roster-name-text">{name}</span>
                        {appellation && (
                          <span className="prep-roster-alias" title="本集原文称谓；谱内正名见前">本集：{appellation}</span>
                        )}
                        {!!aliases.length && (
                          <span className="prep-roster-alias" title="本集称谓">{aliases.join('、')}</span>
                        )}
                      </span>
                      <span className="prep-roster-meta" title={provenanceHint ?? undefined}>覆盖 {character.event_ids?.length ?? 0} 个事件</span>
                    </div>
                  </div>
                )
              })}
              {!characters.length && <p className="prep-empty-hint">未列出出场角色</p>}
            </div>
          </section>

          {!!functionalExtras.length && (
            <section className="card">
              <h3 className="prep-sidebar-heading">群演 / 一次性人物 · {functionalExtras.length}</h3>
              <div className="prep-roster">
                {functionalExtras.map((extra, index) => (
                  <div className="prep-roster-item" key={`${extra.label || 'extra'}-${index}`}>
                    {/* 群演没有定妆照是设计使然（不进人物谱身份体系），不是数据缺失，
                        用统一占位图标而不是"无图"——那个措辞是给真正缺图的具名角色用的。 */}
                    <div className="prep-roster-icon" aria-hidden="true">👤</div>
                    <div className="prep-roster-body">
                      <span className="prep-roster-name"><span className="prep-roster-name-text">{extra.label || '未命名群演'}</span></span>
                      <span className="prep-roster-meta">覆盖 {extra.event_ids?.length ?? 0} 个事件</span>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="card">
            <h3 className="prep-sidebar-heading">出场场景 · {scenes.length}</h3>
            <div className="prep-roster">
              {scenes.map(scene => {
                const imageUrl = findSceneReferenceImage(bible, scene.scene_reference_id)
                const name = scene.display_name || scene.scene_id || '未命名场景'
                return (
                  <div className="prep-roster-item" key={scene.scene_id || scene.display_name}>
                    {imageUrl
                      ? <img className="prep-roster-thumb" src={imageUrl} alt={name} loading="lazy" decoding="async" />
                      : <div className="prep-roster-thumb-empty" aria-hidden="true">无图</div>}
                    <div className="prep-roster-body">
                      <span className="prep-roster-name"><span className="prep-roster-name-text">{name}</span></span>
                      <span className="prep-roster-meta">覆盖 {scene.event_ids?.length ?? 0} 个事件</span>
                    </div>
                  </div>
                )
              })}
              {!scenes.length && <p className="prep-empty-hint">未列出场景</p>}
            </div>
          </section>
        </aside>
      </div>
    </>
  )
}
