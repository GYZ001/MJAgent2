import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { episodeBusy, useEpisode, useNav, useProject } from '../App'
import {
  api,
  ApiError,
  type Bible,
  type EpisodeGenerateResult,
  type ReferenceImage,
  type ReusedReason,
  type ReviewWallContext,
  type Shot,
  type ShotVersion,
  type StoryboardPackResources,
} from '../api'
import { ItemTaskTimer } from '../components/TaskTimer'
import QueryState from '../components/QueryState'
import { compactShotStage } from '../shotStatus'
import { characterPortraitDisplay, findSceneReferenceImage, portraitPlaceholderText, refsBusyPollInterval, resolvePortraitPlaceholderKind, type ImageGenTaskLike } from '../lib/bibleAssets'
import { compressSegmentIndexes } from '../lib/segmentIndexes'
import PortraitPlaceholder from '../components/PortraitPlaceholder'; import SceneReferencePlaceholder from '../components/SceneReferencePlaceholder'
import '../styles/WallPage.css'

// 生成台 2.0（storyboard_pack/2.0.1，见 app/production/storyboard_pack.py）：一个
// 15 秒段 = shots 表一行，storyboard_pack_segment 非 null 是这一行有内容可展示的
// 唯一标记——与分镜台 BoardPage.tsx 同一口径（isStoryboardPackSegmentShot）。老前端
// 的逐镜 shot、首尾帧链、prompt 编译预览已整块拆除：只保留参考图模式一种输入，
// 只保留一个生成入口（模型选择在分镜台，此处只读展示 target_model）。
export function isSegmentShot(shot: Shot): boolean {
  return shot.storyboard_pack_segment != null
}

/** shot_versions.status 的活动态（app/media_pipeline/stages.py 权威枚举的子集）。 */
const ACTIVE_VERSION_STATUSES = ['queued', 'running', 'waiting_provider', 'waiting_retry', 'waiting_budget']

const VERSION_STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  waiting_provider: '等待生成服务',
  running: '生成中',
  succeeded: '已完成',
  failed: '失败',
  waiting_human: '等待人工处理',
  quarantined: '已隔离（不可用）',
  waiting_retry: '等待重试',
  waiting_budget: '预算等待',
  paused_budget: '预算暂停',
  cancelled: '已取消',
}

export function versionStatusLabel(status: string): string {
  return VERSION_STATUS_LABEL[status] || status
}

export type VersionStatusTone = 'grey' | 'gold' | 'green' | 'red'

export function versionStatusTone(status: string): VersionStatusTone {
  if (status === 'succeeded') return 'green'
  if (status === 'failed' || status === 'quarantined') return 'red'
  if (ACTIVE_VERSION_STATUSES.includes(status)) return 'gold'
  return 'grey'
}

export function stampClassForStatus(status: string): string {
  return `stamp ${versionStatusTone(status)}`
}

/** 优先展示已采纳版本，否则回退到版本号最大的最新一次尝试。 */
export function resolveCurrentVersion(shot: Pick<Shot, 'versions' | 'adopted_version_id'>): ShotVersion | null {
  const versions = shot.versions ?? []
  if (!versions.length) return null
  const adopted = shot.adopted_version_id
    ? versions.find(version => version.id === shot.adopted_version_id)
    : undefined
  if (adopted) return adopted
  return [...versions].sort((a, b) => b.version_no - a.version_no)[0]
}

export type SegmentPhase = 'pending' | 'generating' | 'succeeded' | 'attention'

export function segmentPhase(shot: Shot): SegmentPhase {
  const current = resolveCurrentVersion(shot)
  if (!current) return 'pending'
  if (ACTIVE_VERSION_STATUSES.includes(current.status)) return 'generating'
  if (current.status === 'succeeded') return 'succeeded'
  return 'attention'
}

export function segmentPhaseCounts(shots: Shot[]): Record<SegmentPhase, number> {
  const counts: Record<SegmentPhase, number> = { pending: 0, generating: 0, succeeded: 0, attention: 0 }
  for (const shot of shots) counts[segmentPhase(shot)] += 1
  return counts
}

/** 实际已发生费用：累加全部尝试（含失败/隔离候选），不是只算已采纳版本。 */
export function episodeSpentCny(shots: Shot[]): number {
  return shots.reduce(
    (sum, shot) => sum + (shot.versions ?? []).reduce((inner, version) => inner + (version.cost_cny || 0), 0),
    0,
  )
}

export function resolveSelectedShotId(shots: Array<Pick<Shot, 'id'>>, currentId: string | null): string | null {
  if (currentId && shots.some(shot => shot.id === currentId)) return currentId
  return shots[0]?.id ?? null
}

export interface ParsedTechnicalValidation {
  passed: boolean
  issues: string[]
  durationS: number | null
  sizeBytes: number | null
}

/** technical_validation_json 是原样 JSON 字符串（app/domain/storyboard_ops.py 不解析它），
 *  分辨率后端不落盘，只能靠 <video> 元数据在浏览器里读；这里只解出时长/体积/校验结论。 */
export function parseTechnicalValidation(raw?: string | null): ParsedTechnicalValidation | null {
  if (!raw) return null
  try {
    const data = JSON.parse(raw) as {
      passed?: boolean
      issues?: string[]
      evidence?: { duration_s?: number; size_bytes?: number }
    }
    return {
      passed: Boolean(data.passed),
      issues: Array.isArray(data.issues) ? data.issues : [],
      durationS: typeof data.evidence?.duration_s === 'number' ? data.evidence.duration_s : null,
      sizeBytes: typeof data.evidence?.size_bytes === 'number' ? data.evidence.size_bytes : null,
    }
  } catch {
    return null
  }
}

export function formatResolution(width: number, height: number): string {
  return width > 0 && height > 0 ? `${width}×${height}` : '解析中…'
}

/** GET /shots/{id}/review 才带 image_inputs.reference_images（列表接口为控制体积不带）；
 *  按版本 id 摊平成 map，供生成面板只读展示"挂的是哪张参考图"。 */
export function extractReferenceImagesByVersion(shot: Shot): Record<string, ReferenceImage[]> {
  const map: Record<string, ReferenceImage[]> = {}
  for (const version of shot.versions ?? []) {
    const refs = version.image_inputs?.reference_images
    if (refs?.length) map[version.id] = refs
  }
  return map
}

export function referenceImageLabel(ref: ReferenceImage): string {
  if (ref.type === 'character' || ref.entity_type === 'character') return `人物 · ${ref.entity_name || '未命名'}`
  if (ref.type === 'scene' || ref.entity_type === 'scene') return `场景 · ${ref.entity_name || '未命名'}`
  return ref.entity_name || ref.source || '参考图'
}

/** 触发生成按钮的可用性判据；eligible=null 表示生成资格尚未加载完成。 */
export function segmentGenerateDisabledReason(params: {
  submitting: boolean
  currentStatus: string | null
  eligible: boolean | null
  blockers: string[]
}): string {
  if (params.submitting) return '正在提交生成请求'
  if (params.currentStatus && ACTIVE_VERSION_STATUSES.includes(params.currentStatus)) {
    return '当前已有任务在处理中'
  }
  if (params.eligible === null) return '正在核对生成资格'
  if (!params.eligible) return params.blockers.join('；') || '当前生成资格未通过'
  return ''
}

/** 「生成所有视频」一次提交前的口径预估。与后端 `only_incomplete` 同一套判据：
 *  只有已采纳或已有成功候选的段才算「已完成」而被跳过（app/domain/video_ops.py
 *  `_generate_episode_core` 里 `completed_ids` 的查询条件），生成中/需处理的段仍会
 *  被送进这次请求——生成中的段会在 enqueue_shot 里被判定为「已有活动任务」而复用、
 *  不产生新费用，需处理（失败/隔离）的段会被真正重新尝试、产生新费用。 */
export interface BulkGenerateEstimate {
  totalCount: number
  succeededCount: number
  generatingCount: number
  attentionCount: number
  pendingCount: number
  /** 会被送进这次 /generate 请求的段数：total - succeeded。 */
  submitCount: number
  /** 会实际产生新费用的段（待生成 + 需处理；生成中的段走幂等复用，不重复计费）。 */
  newCostShotIds: string[]
  estimatedNewCostCny: number
}

export function bulkGenerateEstimate(shots: Shot[]): BulkGenerateEstimate {
  let succeededCount = 0
  let generatingCount = 0
  let attentionCount = 0
  let pendingCount = 0
  let estimatedNewCostCny = 0
  const newCostShotIds: string[] = []
  for (const shot of shots) {
    const phase = segmentPhase(shot)
    if (phase === 'succeeded') { succeededCount += 1; continue }
    if (phase === 'generating') { generatingCount += 1; continue }
    if (phase === 'attention') attentionCount += 1
    else pendingCount += 1
    estimatedNewCostCny += shot.est_cost_cny ?? 0
    newCostShotIds.push(shot.id)
  }
  return {
    totalCount: shots.length,
    succeededCount,
    generatingCount,
    attentionCount,
    pendingCount,
    submitCount: shots.length - succeededCount,
    newCostShotIds,
    estimatedNewCostCny: Math.round(estimatedNewCostCny * 100) / 100,
  }
}

/** 「生成所有视频」按钮的可用性判据：没有待生成/需处理的段时禁用并说明原因，
 *  不允许假装可点。资格未通过时把 blockers 原文透出，同一份判据同一份措辞，
 *  不在按钮和上方封锁横幅之间各写一套。 */
export function bulkGenerateDisabledReason(params: {
  submitting: boolean
  eligible: boolean | null
  blockers: string[]
  submitCount: number
}): string {
  if (params.submitting) return '正在提交批量生成请求'
  if (params.eligible === null) return '正在核对生成资格'
  if (!params.eligible) return params.blockers.join('；') || '当前生成资格未通过'
  if (params.submitCount === 0) return '全部片段已完成，无需再次生成'
  return ''
}

/** 确认弹窗文案——集中在一处生成，避免「弹窗上写一套、点击后端实际做另一套」：
 *  summary 是这次会花的钱，details 逐条交代已完成/生成中/需处理三种段各自的命运。 */
export function bulkGenerateDialogCopy(estimate: BulkGenerateEstimate): { summary: string; details: string[] } {
  const summary = estimate.submitCount === 0
    ? '本次没有可提交的片段'
    : `本次将提交 ${estimate.submitCount} 段，预计新增费用 ￥${estimate.estimatedNewCostCny.toFixed(2)}`
  const details: string[] = []
  if (estimate.pendingCount || estimate.attentionCount) {
    details.push(
      `待生成 ${estimate.pendingCount} 段` +
      (estimate.attentionCount ? ` + 需处理 ${estimate.attentionCount} 段将重新尝试生成` : '会提交生成') +
      '，实际费用以供应商返回为准',
    )
  }
  if (estimate.generatingCount) {
    details.push(`生成中的 ${estimate.generatingCount} 段已有任务在处理，会被去重复用，不会重复扣费`)
  }
  if (estimate.succeededCount) {
    details.push(`已完成的 ${estimate.succeededCount} 段不会重新生成`)
  }
  return { summary, details }
}

/** 409 · REVIEW_QUALIFICATION_CHANGED（CON-409）：整集范围的 qualification_version
 *  会因为兄弟片段生成、素材清单增长等正常操作漂移。后端把刷新后的快照直接带在这个
 *  错误响应里（见 app/domain/review_wall.py::_review_assert_positive_action），不用
 *  再发一次 GET 去拿新版本号——命中就返回新版本号，调用方只重试这一次，不是别的
 *  409（如资格未通过/资产未就绪/规划已失效）也一起吞。 */
export function qualificationChangedRetryVersion(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const detail = error.detail as
    | { code?: string; qualification?: { qualification_version?: string } }
    | undefined
  if (detail?.code !== 'REVIEW_QUALIFICATION_CHANGED') return null
  return detail.qualification?.qualification_version ?? null
}

const TARGET_MODEL_LABEL: Record<string, string> = { seedance_2: 'Seedance 2.0', minimax_h3: 'MiniMax H3' }
function targetModelLabel(model: string): string {
  return TARGET_MODEL_LABEL[model] || model
}

function newIdemKey(prefix: string): string {
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`
}

/** reused=true 时的诚实文案——不再说"输入未变化"，那句话从未真正比较过输入。
 *  按服务端回传的 reused_reason 如实转述命中记录的真实状态。 */
function reusedReasonLabel(reason?: ReusedReason): string {
  switch (reason) {
    case 'succeeded':
      return '已有交付版本，未重新生成'
    case 'stuck_needs_human':
      return '现有任务卡在需要人工处理，未提交新任务；请核对供应商任务状态'
    case 'in_flight':
    default:
      return '已有任务在处理中，未重复提交'
  }
}

type DetailState =
  | { status: 'idle' }
  | { status: 'loading'; shotId: string }
  | { status: 'ready'; shotId: string; referenceImages: Record<string, ReferenceImage[]> }
  | { status: 'error'; shotId: string; message: string }

export default function WallPage() {
  const { projectId, episodeId, go, toast } = useNav()
  const { data: ep, refresh, error, status, loading } = useEpisode(
    episodeId || '', 'wall', current => episodeBusy(current) ? 4000 : 0,
  )
  const { data: project } = useProject(projectId!, refsBusyPollInterval, 'bible')
  const bible = project?.bible ?? null

  const shots = useMemo(() => (ep?.shots ?? []).filter(isSegmentShot), [ep?.shots])

  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const [detail, setDetail] = useState<DetailState>({ status: 'idle' })
  const [context, setContext] = useState<ReviewWallContext | null>(null)
  const [contextError, setContextError] = useState<string | null>(null)
  const [bulkPreparing, setBulkPreparing] = useState(false)
  const [bulkSubmitting, setBulkSubmitting] = useState(false)
  const detailRequest = useRef(0)
  // 生成前的费用确认弹窗已删除（2026-08-29 用户拍板：模型与视频生成走公司自
  // 有服务，不计费）。bulkLockRef 是同步锁：refreshAll() 与 runBulkGenerate()
  // 之间有一段 await 间隙，bulkPreparing/bulkSubmitting 两个 state 各自的
  // disabled 判据在这段间隙里都还没生效，连点两次可能在这个窗口里都通过——
  // ref 在同一个事件循环内立即生效，堵住这个窗口，保证点两次只跑一遍。
  const bulkLockRef = useRef(false)

  useEffect(() => {
    setSelectedShotId(current => resolveSelectedShotId(shots, current))
  }, [shots])

  const loadContext = useCallback(async () => {
    if (!episodeId) return
    try {
      const next = await api.getReviewContext(episodeId)
      setContext(next)
      setContextError(null)
    } catch (reason) {
      setContextError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [episodeId])

  useEffect(() => { void loadContext() }, [loadContext, ep?.status, ep?.storyboard_artifact_id])

  const loadDetail = useCallback(async (shotId: string) => {
    const request = ++detailRequest.current
    setDetail(current => (
      (current.status === 'ready' || current.status === 'loading') && current.shotId === shotId
        ? current
        : { status: 'loading', shotId }
    ))
    try {
      const loaded = await api.getShotReview(shotId)
      if (request !== detailRequest.current) return
      setDetail({ status: 'ready', shotId, referenceImages: extractReferenceImagesByVersion(loaded) })
    } catch (reason) {
      if (request !== detailRequest.current) return
      const value = reason as Error
      setDetail({ status: 'error', shotId, message: value.message || String(reason) })
    }
  }, [])

  useEffect(() => {
    if (!selectedShotId) { setDetail({ status: 'idle' }); return }
    void loadDetail(selectedShotId)
  }, [selectedShotId, loadDetail])

  const refreshAll = useCallback(async () => {
    await refresh({ force: true })
    await loadContext()
    if (selectedShotId) await loadDetail(selectedShotId)
  }, [loadContext, loadDetail, refresh, selectedShotId])

  const runBulkGenerate = useCallback(async () => {
    if (!episodeId || !context) return
    setBulkSubmitting(true)
    const idempotencyKey = newIdemKey(`wall-generate-all:${episodeId}`)
    try {
      let result: EpisodeGenerateResult
      try {
        result = await api.episodeGenerate(episodeId, idempotencyKey, {
          onlyIncomplete: true,
          qualificationVersion: context.upstream.qualification_version,
        })
      } catch (err) {
        // 只吞 CON-409（资格版本漂移）这一种、只重试这一次；其余 409（资格未通过/
        // 资产未就绪/计划已失效等）原样抛出去，不能在这里悄悄咽掉。
        const retryVersion = qualificationChangedRetryVersion(err)
        if (retryVersion === null) throw err
        result = await api.episodeGenerate(episodeId, idempotencyKey, {
          onlyIncomplete: true,
          qualificationVersion: retryVersion,
        })
      }
      const failed = result.enqueued.filter(item => item.error)
      // reused 不等于"已提交"：命中记录卡在需要人工处理时，这里其实什么
      // 新任务都没有提交，不能并进"已提交"的计数里（否则按钮会一直谎报
      // 「已提交 N 段」，而里面有的镜头其实永远没被处理）。
      const stuck = result.enqueued.filter(
        item => !item.error && item.reused && item.reused_reason === 'stuck_needs_human',
      )
      const okCount = result.enqueued.length - failed.length - stuck.length
      if (failed.length || stuck.length) {
        const segments = [`已提交 ${okCount} 段生成请求`]
        if (stuck.length) {
          segments.push(`${stuck.length} 段卡在需要人工处理，未提交（请在对应镜头核对供应商任务状态）`)
        }
        if (failed.length) {
          segments.push(
            `${failed.length} 段提交失败：${failed[0].error}` +
            (failed.length > 1 ? `（另有 ${failed.length - 1} 段失败，详见各段状态）` : ''),
          )
        }
        toast(segments.join('；'), true)
      } else {
        toast(
          `已提交 ${result.selected_shots} 段生成请求` +
          (result.skipped_completed ? `，已跳过完成 ${result.skipped_completed} 段` : ''),
        )
      }
      await refreshAll()
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), true)
    } finally {
      setBulkSubmitting(false)
    }
  }, [context, episodeId, refreshAll, toast])

  // 点「生成所有视频」直接发起，不再弹窗等用户二次确认（2026-08-29 用户拍板：
  // 删除生成前的费用确认弹窗）。刷新一遍片段状态与资格快照的动作保留——
  // 整集批量发起最容易在「页面停留了一阵子、期间兄弟片段生成或素材清单
  // 增长」之后撞上 CON-409（详见 qualificationChangedRetryVersion 的注释），
  // 这一步降低撞上的概率，命中时下面 runBulkGenerate 里的自动重试兜底，
  // 不代表可以省掉其中一个。runBulkGenerate 自带的「已提交 N 段生成请求」
  // toast 就是发起后的明确反馈，不需要另外再报一遍。
  const generateAll = useCallback(async () => {
    if (bulkLockRef.current) return
    bulkLockRef.current = true
    try {
      setBulkPreparing(true)
      try {
        await refreshAll()
      } finally {
        setBulkPreparing(false)
      }
      await runBulkGenerate()
    } finally {
      bulkLockRef.current = false
    }
  }, [refreshAll, runBulkGenerate])

  const bulkEstimate = useMemo(() => bulkGenerateEstimate(shots), [shots])

  if (error && !ep) {
    return <QueryState loading={false} error={error} status={status} hasData={false} objectName="生成台">{null}</QueryState>
  }
  if (!ep) {
    return <QueryState loading={loading !== false} error={null} hasData={false} objectName="生成台">{null}</QueryState>
  }

  const selectedSummary = shots.find(shot => shot.id === selectedShotId) ?? null
  const counts = segmentPhaseCounts(shots)
  const spent = episodeSpentCny(shots)
  const goProcess = () => go(
    ep.status === 'scripting' || ep.status === 'planned' ? 'script' : 'board', projectId, ep.id,
  )
  const eligible = context ? context.upstream.eligible_for_production : null
  const blockers = context?.upstream.blockers ?? []
  const bulkDisabledReason = bulkGenerateDisabledReason({
    submitting: bulkSubmitting,
    eligible,
    blockers,
    submitCount: bulkEstimate.submitCount,
  })

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="生成台" view="wall" episodeNo={ep.episode_no} />
        <h1>生成台 <span className="sub">《{ep.title}》 · 按 15 秒片段生成参考图视频</span></h1>
        <hr className="rule" />
      </header>

      <section className="card wall-summary" aria-label="本集生成概览">
        <div className="wall-summary-row">
          <span><b>{shots.length}</b> 个片段</span>
          <span>待生成 <b>{counts.pending}</b></span>
          <span>生成中 <b>{counts.generating}</b></span>
          <span>已完成 <b>{counts.succeeded}</b></span>
          <span className={counts.attention ? 'wall-summary-count attention' : 'wall-summary-count'}>需处理 <b>{counts.attention}</b></span>
          <span>已产生费用 <b>￥{spent.toFixed(2)}</b></span>
        </div>
        <div className="wall-summary-actions">
          <button type="button" className="btn primary small wall-summary-generate-all"
            disabled={Boolean(bulkDisabledReason) || bulkPreparing} title={bulkDisabledReason || undefined}
            aria-label={bulkDisabledReason ? `生成所有视频，暂不可用：${bulkDisabledReason}` : '生成所有视频；点击后立即提交'}
            onClick={() => { void generateAll() }}
          >
            {bulkPreparing ? '核对生成资格…' : '生成所有视频'}
          </button>
          {!bulkPreparing && bulkDisabledReason && (
            <span className="wall-summary-actions-hint">{bulkDisabledReason}</span>
          )}
        </div>
        {contextError && (
          <div className="wall-context-error" role="alert">
            <span>生成资格加载失败：{contextError}</span>
            <button type="button" className="btn small" onClick={() => { void loadContext() }}>重试</button>
          </div>
        )}
        {context && !context.upstream.eligible_for_production && (
          <div className="wall-blocked-banner" role="status">
            <b>当前不能生成</b><span>{context.upstream.blockers.join('；') || '生成资格未通过'}</span>
            <button type="button" className="btn small" onClick={goProcess}>
              去{ep.status === 'scripting' || ep.status === 'planned' ? '映射台' : '分镜台'}处理
            </button>
          </div>
        )}
      </section>

      <div className="workspace-gap" />

      {!shots.length ? (
        <div className="empty">
          <div className="big">段</div>
          本集尚无可生成的片段，请先在分镜台生成视频提示词。
          <div><button type="button" className="btn primary" onClick={() => go('board', projectId, ep.id)}>去分镜台</button></div>
        </div>
      ) : (
        <div className="wall-workspace">
          <section className="shot-navigator" aria-label="片段轨道">
            <div className="shot-navigator-head">
              <div><b>片段轨道</b><span>{shots.length} 段</span></div>
            </div>
            <div className="shot-navigator-list" role="listbox" aria-label="片段列表">
              {shots.map(shot => <SegmentNavItem
                key={shot.id}
                shot={shot}
                selected={shot.id === selectedShotId}
                onSelect={() => setSelectedShotId(shot.id)}
              />)}
            </div>
          </section>

          <section className="shot-editor-pane">
            {selectedSummary && (
              <SegmentWorkbench
                key={selectedSummary.id}
                shot={selectedSummary}
                bible={bible} project={project}
                context={context}
                detail={detail}
                onRefresh={refreshAll}
                onToast={toast}
              />
            )}
          </section>
        </div>
      )}
    </>
  )
}

function SegmentNavItem({ shot, selected, onSelect }: { shot: Shot; selected: boolean; onSelect: () => void }) {
  const segment = shot.storyboard_pack_segment
  if (!segment) return null
  const current = resolveCurrentVersion(shot)
  const phase = segmentPhase(shot)
  const runningSince = current?.running_since ?? null
  return (
    <button type="button" role="option" aria-selected={selected} className={selected ? 'active' : ''} onClick={onSelect}>
      <span className="shot-nav-top">
        <span className="shot-nav-no">段 {String(segment.segment_no).padStart(2, '0')}</span>
        <span>{segment.duration_s}s</span>
      </span>
      <span className="shot-nav-main">
        <b>{current ? versionStatusLabel(current.status) : '待生成'}</b>
        <small title={segment.synopsis || undefined}>{segment.synopsis || '（无梗概）'}</small>
      </span>
      <span className="shot-nav-badges">
        {phase === 'generating' && runningSince != null && <ItemTaskTimer elapsedMs={0} runningSince={runningSince} compact />}
        {phase === 'attention' && <i className="problem">需处理</i>}
        {!!segment.degraded_capabilities.length && <i className="problem" title="本段存在能力降级项，详见段落详情">能力降级</i>}
      </span>
    </button>
  )
}

function SegmentWorkbench({ shot, bible, context, detail, onRefresh, onToast, project }: {
  shot: Shot
  bible: Bible | null; project: ImageGenTaskLike | null
  context: ReviewWallContext | null
  detail: DetailState
  onRefresh: () => Promise<void>
  onToast: (message: string, isErr?: boolean) => void
}) {
  const segment = shot.storyboard_pack_segment
  if (!segment) {
    return <article className="card wall-segment"><p className="wall-empty-hint">本段暂无数据</p></article>
  }
  const rangeText = compressSegmentIndexes(segment.source_segment_indexes ?? [])
  const referenceImages = detail.status === 'ready' && detail.shotId === shot.id ? detail.referenceImages : {}
  const detailLoading = detail.status === 'loading' && detail.shotId === shot.id
  const detailError = detail.status === 'error' && detail.shotId === shot.id ? detail.message : null

  const copyPromptText = async () => {
    if (!navigator.clipboard) { onToast('当前浏览器无法访问剪贴板，请检查浏览器权限后重试', true); return }
    try {
      await navigator.clipboard.writeText(segment.prompt_text)
      onToast('提示词已整块复制')
    } catch {
      onToast('复制失败，请允许浏览器访问剪贴板后重试', true)
    }
  }

  return (
    <article className="card wall-segment">
      <header className="wall-segment-head">
        <div className="wall-segment-head-copy">
          <div className="wall-segment-head-top">
            <b>第 {segment.segment_no} 段</b>
            <span>{segment.duration_s}s</span>
          </div>
          <p className="wall-segment-synopsis">{segment.synopsis || '（本段无梗概）'}</p>
        </div>
        <SegmentResourceStrip resources={segment.resources} bible={bible} project={project} />
      </header>

      <section className="wall-prompt-block">
        <div className="wall-prompt-head">
          <b>视频生成提示词</b>
          <button type="button" className="text-action" onClick={() => { void copyPromptText() }}>复制整段提示词</button>
        </div>
        {segment.prompt_text
          ? <pre className="wall-prompt-text">{segment.prompt_text}</pre>
          : <p className="wall-empty-hint">暂无数据</p>}
      </section>

      <section className="wall-meta-strip" aria-label="次要信息">
        <span className="wall-meta-chip">{segment.shot_count} 镜切换</span>
        <span className="wall-meta-chip">{targetModelLabel(segment.target_model)}</span>
        <span className="wall-meta-chip">对应原文{rangeText ? ` 第 ${rangeText} 段` : '暂无数据'}</span>
        {segment.dialogue.length ? (
          <details className="wall-meta-details">
            <summary className="wall-meta-chip">台词 {segment.dialogue.length} 条</summary>
            <ul className="wall-dialogue-list">
              {segment.dialogue.map((line, index) => (
                <li key={index}>
                  <span className="wall-dialogue-speaker">{line.speaker_identity_id || '未知说话人'}</span>
                  <span className="wall-dialogue-line">{line.line}</span>
                  <span className="wall-dialogue-source">原文第 {line.source_segment_index} 段</span>
                </li>
              ))}
            </ul>
          </details>
        ) : <span className="wall-meta-chip wall-chip-muted">无台词</span>}
        <details className="wall-meta-details">
          <summary className="wall-meta-chip">素材详情</summary>
          <SegmentResourceRoster resources={segment.resources} bible={bible} project={project} />
        </details>
        {segment.degraded_capabilities.map((item, index) => (
          <span key={index} className="wall-meta-chip wall-chip-degraded">{item}</span>
        ))}
      </section>

      <GenerationPanel
        shot={shot}
        context={context}
        referenceImages={referenceImages}
        detailLoading={detailLoading}
        detailError={detailError}
        onRefresh={onRefresh}
        onToast={onToast}
      />
    </article>
  )
}

function SegmentResourceStrip({ resources, bible, project }: { resources: StoryboardPackResources; bible: Bible | null; project: ImageGenTaskLike | null }) {
  const characters = resources.characters ?? []
  const scenes = resources.scenes ?? []
  const props = resources.props ?? []
  if (!characters.length && !scenes.length && !props.length) {
    return <div className="wall-empty-hint">暂无数据</div>
  }
  return (
    <div className="wall-resource-strip" aria-label="本段素材">
      {characters.map((character, index) => {
        const { imageUrl, updated } = characterPortraitDisplay(character)
        const label = character.identity_id || '未命名角色'
        const tipBase = character.description ? `${label} · ${character.description}` : label
        const placeholderText = portraitPlaceholderText(resolvePortraitPlaceholderKind(character.identity_id, project))
        const tip = imageUrl
          ? (updated ? `${tipBase}（定妆照已更新）` : tipBase)
          : `${label} · ${placeholderText}`
        return imageUrl
          ? (
            <img
              key={`c-${index}`}
              className={`wall-resource-chip-thumb${updated ? ' wall-resource-chip-thumb-updated' : ''}`}
              src={imageUrl} alt={label} title={tip} loading="lazy" decoding="async"
            />
          )
          : <div key={`c-${index}`} className="wall-resource-chip-empty" title={tip} aria-label={tip}>{label.slice(0, 1) || '无'}</div>
      })}
      {scenes.map((scene, index) => {
        const imageUrl = findSceneReferenceImage(bible, scene.scene_reference_id)
        const label = scene.scene_id || '未命名场景'
        const tip = scene.description ? `${label} · ${scene.description}` : label
        return imageUrl
          ? <img key={`s-${index}`} className="wall-resource-chip-thumb wall-resource-chip-scene" src={imageUrl} alt={label} title={tip} loading="lazy" decoding="async" />
          : <div key={`s-${index}`} className="wall-resource-chip-empty wall-resource-chip-scene" title={tip} aria-label={tip}>{label.slice(0, 1) || '无'}</div>
      })}
      {props.map((prop, index) => (
        <span key={`p-${index}`} className="wall-resource-chip-text" title={prop.description || prop.label}>{prop.label || '未命名道具'}</span>
      ))}
    </div>
  )
}

export function SegmentResourceRoster({ resources, bible, project }: { resources: StoryboardPackResources; bible: Bible | null; project: ImageGenTaskLike | null }) {
  const characters = resources.characters ?? []
  const scenes = resources.scenes ?? []
  const props = resources.props ?? []
  return (
    <section className="wall-resource-roster">
      <div className="wall-resource-group">
        <b>人物 · {characters.length}</b>
        <div className="wall-resource-list">
          {characters.map((character, index) => {
            const { imageUrl, updated } = characterPortraitDisplay(character)
            return (
              <div className="wall-resource-item" key={`${character.identity_id || 'character'}-${index}`}>
                {imageUrl
                  ? (
                    <span className="wall-resource-thumb-wrap">
                      <img className="wall-resource-thumb" src={imageUrl} alt={character.identity_id} loading="lazy" decoding="async" />
                      {updated && (
                        <span className="wall-resource-thumb-updated" title="定妆照已更新，与本段素材记录当时依据的那张不同">已更新</span>
                      )}
                    </span>
                  )
                  : <PortraitPlaceholder identityId={character.identity_id} project={project} className="wall-resource-thumb-empty" />}
                <div className="wall-resource-body">
                  <span className="wall-resource-name">{character.identity_id || '未命名角色'}</span>
                  <span className="wall-resource-desc">{character.description || (imageUrl ? '' : '暂无文字描述')}</span>
                </div>
              </div>
            )
          })}
          {!characters.length && <p className="wall-empty-hint">本段无人物资源</p>}
        </div>
      </div>
      <div className="wall-resource-group">
        <b>场景 · {scenes.length}</b>
        <div className="wall-resource-list">
          {scenes.map((scene, index) => {
            const imageUrl = findSceneReferenceImage(bible, scene.scene_reference_id)
            return (
              <div className="wall-resource-item" key={`${scene.scene_id || 'scene'}-${index}`}>
                {imageUrl
                  ? <img className="wall-resource-thumb" src={imageUrl} alt={scene.scene_id} loading="lazy" decoding="async" />
                  : <SceneReferencePlaceholder sceneId={scene.scene_id} label={scene.scene_id || '未命名场景'} project={project} className="wall-resource-thumb-empty" />}
                <div className="wall-resource-body">
                  <span className="wall-resource-name">{scene.scene_id || '未命名场景'}</span>
                  <span className="wall-resource-desc">{scene.description || (imageUrl ? '' : '暂无文字描述')}</span>
                </div>
              </div>
            )
          })}
          {!scenes.length && <p className="wall-empty-hint">本段无场景资源</p>}
        </div>
      </div>
      <div className="wall-resource-group">
        <b>道具 · {props.length}</b>
        <div className="wall-resource-list">
          {props.map((prop, index) => (
            <div className="wall-resource-item" key={`${prop.label || 'prop'}-${index}`}>
              <div className="wall-resource-icon" aria-hidden="true">物</div>
              <div className="wall-resource-body">
                <span className="wall-resource-name">{prop.label || '未命名道具'}</span>
                <span className="wall-resource-desc">{prop.description || '暂无文字描述'}</span>
              </div>
            </div>
          ))}
          {!props.length && <p className="wall-empty-hint">本段无道具</p>}
        </div>
      </div>
    </section>
  )
}

function GenerationPanel({ shot, context, referenceImages, detailLoading, detailError, onRefresh, onToast }: {
  shot: Shot
  context: ReviewWallContext | null
  referenceImages: Record<string, ReferenceImage[]>
  detailLoading: boolean
  detailError: string | null
  onRefresh: () => Promise<void>
  onToast: (message: string, isErr?: boolean) => void
}) {
  const segment = shot.storyboard_pack_segment
  const versions = useMemo(
    () => [...(shot.versions ?? [])].sort((a, b) => b.version_no - a.version_no),
    [shot.versions],
  )
  const current = resolveCurrentVersion(shot)
  const [previewId, setPreviewId] = useState<string | null>(current?.id ?? versions[0]?.id ?? null)
  useEffect(() => {
    setPreviewId(prev => (prev && versions.some(version => version.id === prev)) ? prev : (current?.id ?? versions[0]?.id ?? null))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shot.id, current?.id, versions.length])
  const selected = versions.find(version => version.id === previewId) ?? current ?? null

  const [submitting, setSubmitting] = useState(false)
  const [resolutions, setResolutions] = useState<Record<string, { width: number; height: number }>>({})
  // 生成前的费用确认弹窗已删除（2026-08-29 用户拍板）。submittingRef 是同步锁：
  // submitting state 的更新在下一次渲染才会让按钮 disabled 生效，连点两次
  // 可能在这个窗口里都通过；ref 在同一个事件循环内立即生效，堵住这个窗口。
  const submittingRef = useRef(false)

  const eligible = context ? context.upstream.eligible_for_production : null
  const blockers = context?.upstream.blockers ?? []
  const disabledReason = segmentGenerateDisabledReason({
    submitting,
    currentStatus: current?.status ?? null,
    eligible,
    blockers,
  })
  const hasAttempt = versions.length > 0
  const runningSince = current?.running_since ?? null

  const runGenerate = async () => {
    if (!context || submittingRef.current) return
    submittingRef.current = true
    setSubmitting(true)
    try {
      // 按本镜作用域的资格版本提交：兄弟镜新增素材会改变整集范围的
      // qualification_version，但不改变本镜自己这一份，避免"点段1生成 ->
      // 段2立刻被拒"的自我作废（CON-409）。取不到本镜版本时（旧后端兼容）
      // 回退整集版本。
      const shotQualificationVersion =
        context.upstream.shot_qualification_versions?.[shot.id]
        ?? context.upstream.qualification_version
      const result = await api.shotGenerate(
        shot.id, undefined, true, false,
        shotQualificationVersion,
        newIdemKey(`wall-generate:${shot.id}`),
      ) as { reused?: boolean; reused_reason?: ReusedReason; job_id?: string }
      onToast(result.reused
        ? reusedReasonLabel(result.reused_reason)
        : `已提交生成请求${result.job_id ? `，任务 ${result.job_id}` : ''}`,
        result.reused_reason === 'stuck_needs_human')
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  const technical = parseTechnicalValidation(selected?.technical_validation_json)
  const refs = selected ? referenceImages[selected.id] ?? [] : []
  const resolution = selected ? resolutions[selected.id] : undefined
  const targetDurationS = segment?.duration_s ?? shot.duration_s

  return (
    <section className="wall-generation" aria-label="生成与预览">
      <div className="wall-generation-toolbar">
        <div>
          <b>{hasAttempt ? `已尝试 ${versions.length} 次` : '尚未生成'}</b>
          <span>预计单次 ￥{(shot.est_cost_cny ?? 0).toFixed(2)}</span>
        </div>
        <button type="button" className="btn primary small"
          disabled={Boolean(disabledReason)}
          aria-label={disabledReason
            ? `${hasAttempt ? '重新生成' : '生成'}，暂不可用：${disabledReason}`
            : `${hasAttempt ? '重新生成本段视频' : '生成本段视频'}；点击后立即提交`}
          title={disabledReason || undefined}
          onClick={() => void runGenerate()}>
          {submitting ? '提交中…' : hasAttempt ? '重新生成' : '生成'}
        </button>
      </div>

      {current && ACTIVE_VERSION_STATUSES.includes(current.status) && (
        <div className="wall-attempt-issue" role="status">
          <b>{versionStatusLabel(current.status)}</b>
          <span>{compactShotStage(shot)}</span>
          {runningSince != null && <ItemTaskTimer elapsedMs={0} runningSince={runningSince} />}
        </div>
      )}
      {current && ['failed', 'waiting_human', 'quarantined'].includes(current.status) && (
        <div className="wall-attempt-issue" role="alert">
          <b>{versionStatusLabel(current.status)}</b>
          <span>{shot.pipeline?.reason_text || shot.pipeline?.blocked_reason || '请查看下方错误详情。'}</span>
          {current.error && <code>{current.error}</code>}
        </div>
      )}
      {detailError && (
        <div className="wall-attempt-issue" role="alert"><b>参考图详情加载失败</b><span>{detailError}</span></div>
      )}

      <div className="wall-player-layout">
        <div className="wall-player">
          {selected?.video_url ? (
            <video
              key={selected.id}
              src={selected.video_url}
              controls
              preload="metadata"
              onLoadedMetadata={event => {
                const video = event.currentTarget
                const versionId = selected.id
                setResolutions(currentResolutions => ({
                  ...currentResolutions,
                  [versionId]: { width: video.videoWidth, height: video.videoHeight },
                }))
              }}
            />
          ) : (
            <div className="wall-player-empty">
              {selected ? `${versionStatusLabel(selected.status)}，暂无可播放视频` : '尚无生成记录，点击「生成」开始'}
            </div>
          )}
          {selected && (
            <dl className="wall-player-facts">
              <div><dt>时长</dt><dd>{technical?.durationS != null ? `${technical.durationS.toFixed(1)}s` : `${targetDurationS}s（目标）`}</dd></div>
              <div><dt>分辨率</dt><dd>{resolution ? formatResolution(resolution.width, resolution.height) : '未播放'}</dd></div>
              <div><dt>成本</dt><dd>￥{selected.cost_cny.toFixed(2)}</dd></div>
            </dl>
          )}
          {detailLoading && <p className="wall-empty-hint">正在加载参考图…</p>}
          {!!refs.length && (
            <div className="wall-refs" aria-label="参考图">
              {refs.map(ref => (
                <figure className="wall-ref-card" key={ref.id}>
                  {ref.image_url
                    ? <img src={ref.image_url} alt={referenceImageLabel(ref)} loading="lazy" />
                    : <div className="wall-resource-thumb-empty">无图</div>}
                  <span title={referenceImageLabel(ref)}>{referenceImageLabel(ref)}</span>
                </figure>
              ))}
            </div>
          )}
          {selected?.provider_task_id && <p className="wall-empty-hint">供应商任务：{selected.provider_task_id}</p>}
        </div>
        {versions.length > 1 && (
          <div className="wall-attempt-list" aria-label="全部尝试">
            <b>全部尝试 · {versions.length}</b>
            {versions.map(version => (
              <button type="button" key={version.id}
                className={`wall-attempt-card${version.id === previewId ? ' selected' : ''}`}
                onClick={() => setPreviewId(version.id)}>
                <span className="wall-attempt-card-top">
                  <b>v{version.version_no}</b>
                  <span className={stampClassForStatus(version.status)}>{versionStatusLabel(version.status)}</span>
                </span>
                <small>￥{version.cost_cny.toFixed(2)}</small>
              </button>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}
