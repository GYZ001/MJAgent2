import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { api, DeliveryPackageRecord, DeliveryReadiness, MixStatus, MixResult } from '../api'
import { useEpisode, useNav, usePoll } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import QueryState from '../components/QueryState'
import DecisionDialog from '../components/DecisionDialog'
import OperationError from '../components/OperationError'
import "../styles/CinemaPage.css";

const DELIVERY_STATUS_LABELS: Record<string, string> = {
  waiting_human: '待人工复验',
  approved: '已批准',
  approved_with_risk: '带风险批准',
  rejected: '已拒绝',
}

export type CinemaTab = 'preview' | 'readiness' | 'records'
const CINEMA_TABS: CinemaTab[] = ['preview', 'readiness', 'records']
const deliveryOperationStorageKey = (episodeId: string) =>
  `manju:delivery-package-operation:${episodeId}`

export function persistentDeliveryOperationKey(episodeId: string): string {
  const storageKey = deliveryOperationStorageKey(episodeId)
  const existing = localStorage.getItem(storageKey)
  if (existing) return existing
  const created = `delivery-package:${episodeId}:${Date.now()}:${Math.random().toString(36).slice(2)}`
  localStorage.setItem(storageKey, created)
  return created
}

export const deliveryStatusLabel = (status: string) => DELIVERY_STATUS_LABELS[status] || '处理中'

export function nextCinemaTab(current: CinemaTab, key: string): CinemaTab | null {
  const index = CINEMA_TABS.indexOf(current)
  if (key === 'Home') return CINEMA_TABS[0]
  if (key === 'End') return CINEMA_TABS[CINEMA_TABS.length - 1]
  if (key === 'ArrowRight') return CINEMA_TABS[(index + 1) % CINEMA_TABS.length]
  if (key === 'ArrowLeft') return CINEMA_TABS[(index - 1 + CINEMA_TABS.length) % CINEMA_TABS.length]
  return null
}

export function canConcatenateMix(mix: Pick<MixStatus, 'ready' | 'shots_ready'> | null): boolean {
  return Boolean(mix && mix.ready && mix.shots_ready > 0)
}

export function finalEditStatusLabel(report: Record<string, unknown>): string {
  if (report.ok === true) return '当前成片已执行确定性文字、镜间转场与音轨衔接'
  if (report.mode === 'draft_concat' && report.skipped_final_edit === true) {
    if (report.decision_reason === 'partial_timeline_fast_preview') {
      return '当前成片使用快速阶段拼接；缺镜补齐后再执行终剪增强'
    }
    if (report.decision_reason === 'simple_timeline_fast_concat') {
      return '当前成片使用快速无重编码拼接；未检测到必须终剪的文字或转场'
    }
    if (report.decision_reason === 'disabled_by_env') {
      return '当前成片使用快速基础拼接；终剪增强已关闭'
    }
    return '当前成片使用快速基础拼接'
  }
  return '当前成片已使用基础合成降级，但时间线仍完整交付'
}

/**
 * 部分合成是主流程：任意一镜没有可用的已采纳视频（从没生成、生成中、生成
 * 失败、或采纳指向已失效/未过技术校验的版本）都会被透明跳过，不拖垮整份
 * 成片。跳过不能只在一次性 toast 里一闪而过——用户随时刷新页面回来查看时，
 * 仍要能看到"本次成片跳过了第几镜、为什么"，不能让人误以为拿到的是完整
 * 成片。返回 null 表示没有镜头被跳过（即完整成片，无需展示）。
 */
export function finalSkipSummary(report: Record<string, unknown> | null | undefined): string | null {
  const timeline = report && typeof report === 'object' ? (report as Record<string, unknown>).timeline : null
  if (!timeline || typeof timeline !== 'object') return null
  const skipped = (timeline as Record<string, unknown>).skipped_shot_nos
  if (!Array.isArray(skipped) || skipped.length === 0) return null
  const reasonsRaw = (timeline as Record<string, unknown>).skip_reasons
  const reasons = reasonsRaw && typeof reasonsRaw === 'object' ? (reasonsRaw as Record<string, unknown>) : {}
  const detail = skipped
    .map(no => {
      const reason = reasons[String(no)]
      return typeof reason === 'string' && reason ? `第 ${no} 镜（${reason}）` : `第 ${no} 镜`
    })
    .join('、')
  return `本次成片跳过了${skipped.length}个镜头，其余镜头正常合成：${detail}。补齐后重新合成即可自动补全。`
}

/**
 * 状态轮询只更新真正变化的字段，并保护已经展示的整集成品。
 *
 * 合成请求完成与较早发出的状态请求可能交错返回；较早响应中的空 URL 不应把
 * 刚得到或正在播放的成品从页面移除。后端会继续保留旧文件，这里也为旧服务端
 * 和瞬时响应提供一层展示保护。
 */
export function reconcileMixStatus(previous: MixStatus | null, incoming: MixStatus): MixStatus {
  if (!previous || previous.episode_id !== incoming.episode_id) return incoming
  const next = previous.final_video_url && !incoming.final_video_url
    ? {
        ...incoming,
        final_video_url: previous.final_video_url,
        final_video_stale: true,
        final_edit_report: incoming.final_edit_report ?? previous.final_edit_report,
      }
    : incoming
  return JSON.stringify(previous) === JSON.stringify(next) ? previous : next
}

export function deliveryWarningLabel(value: string): string {
  const translated = value
    .replace(/Duplicate frames(?:\s*\(frame \d+ and frame \d+\))?/gi, '存在重复画面帧')
    .replace(/Missing start state of /gi, '未呈现预期起始状态：')
    .replace(/End state mismatch:\s*/gi, '结束状态不符合预期：')
    .replace(/Mismatched starting state/gi, '起始状态不符合预期')
    .replace(/Start state partially mismatched:\s*/gi, '起始状态部分不符合预期：')
    .replace(/Character outfit does not match the expected design(?:\s*\([^)]*\))?/gi, '角色服装与预期设计不一致')
    .replace(/The expected core action is not fully completed/gi, '预期核心动作未完整完成')
    .replace(/Some character faces do not match the provided character anchors/gi, '部分角色面部与人物设定不一致')
    .replace(/target character/gi, '目标角色')
    .replace(/is not present in the scene/gi, '未出现在画面中')
    .replace(/the girl is bowing instead of standing calmly as expected/gi, '角色正在鞠躬，而预期为平静站立')
    .replace(/'s outfit has incorrect accessory\s*/gi, '的服装配饰与人物设定不一致：')
    .replace(/'s outfit does not match the character anchor/gi, '的服装与人物设定不一致')
    .replace(/'s outfit does not match the expected light green top and tight pants, instead wearing a purple dress/gi, '的服装不符合预期：应为淡绿色上衣搭配紧腿长裤，实际为紫色连衣裙')
    .replace(/has raised his head instead of not responding yet/gi, '已抬头回应，而预期仍未作出反应')
    .replace(/preparing to approach/gi, '准备走向')
    .replaceAll('角色锚点', '人物设定')
    .replaceAll('锚点', '设定参考')
    .replaceAll('AI生成', '生成工具')
    .replace(/(\d+)s\b/gi, '$1 秒')
  return /[A-Za-z]{3}/.test(translated)
    ? '画面状态或人物一致性与预期不符，请结合对应镜头人工复验'
    : translated
}

export function deliveryCheckLabel(value: string): string {
  return value
    // 时长区间由后端 config.VIDEO_DURATION_MIN_S~MAX_S 决定；这里用正则而不是
    // 写死 "5~10"，避免后端上限一变前端文案就悄悄对不上（历史教训见 A1 时长上限改造）。
    .replace(/每镜时长为模型选择的 (\d+)~(\d+) 秒整数/, '每镜时长为 $1 到 $2 秒整数')
    .replace('每镜都有已采用且通过技术校验的视频', '每镜都有已采用且可正常播放的视频')
}

export function formatDeliveryTime(value: number): string {
  const timestamp = value < 1_000_000_000_000 ? value * 1000 : value
  return new Date(timestamp).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export type DeliveryDecision = 'approve' | 'approve_with_risk' | 'reject'

export function deliveryReviewDisabledReason(
  decision: DeliveryDecision,
  busy: boolean,
  packageStatus: string | null,
  reviewer: string,
  reason: string,
  acceptedRisk: string,
): string {
  if (busy) return '正在处理上一项交付操作'
  if (!packageStatus) return '请先生成并选择一个待复验交付候选'
  if (packageStatus !== 'waiting_human') {
    return `当前候选${deliveryStatusLabel(packageStatus)}，不能重复审核`
  }
  if (!reviewer.trim()) return '请先填写复验人'
  if (!reason.trim()) return '请先填写审核意见'
  if (decision === 'approve_with_risk' && !acceptedRisk.trim()) return '请先填写接受风险说明'
  return ''
}

export default function CinemaPage() {
  const { episodeId, toast } = useNav()
  const { data: ep, error, status, loading, refresh: refreshEpisode } = useEpisode(episodeId!, 'cinema')
  const [mix, setMix] = useState<MixStatus | null>(null)
  const [mixBusy, setMixBusy] = useState(false)
  const [mixConfirmOpen, setMixConfirmOpen] = useState(false)
  const [packageConfirmOpen, setPackageConfirmOpen] = useState(false)
  const [deliveryBusy, setDeliveryBusy] = useState(false)
  const [readiness, setReadiness] = useState<DeliveryReadiness | null>(null)
  const [packages, setPackages] = useState<DeliveryPackageRecord[]>([])
  const [selectedPackageId, setSelectedPackageId] = useState<string | null>(null)
  const [reviewer, setReviewer] = useState('')
  const [reason, setReason] = useState('')
  const [acceptedRisk, setAcceptedRisk] = useState('')
  const [feedback, setFeedback] = useState('')
  const [feedbackBusy, setFeedbackBusy] = useState(false)
  const [feedbackConfirmOpen, setFeedbackConfirmOpen] = useState(false)
  const [downloadBusy, setDownloadBusy] = useState<string | null>(null)
  const [reviewDecision, setReviewDecision] = useState<DeliveryDecision | null>(null)
  const [activeTab, setActiveTab] = useState<CinemaTab>('preview')
  const tabGroupId = useId()
  const tabRefs = useRef<Record<CinemaTab, HTMLButtonElement | null>>({
    preview: null,
    readiness: null,
    records: null,
  })
  const dialogTriggerRef = useRef<HTMLElement | null>(null)
  const mixTimer = useTaskTimer(`episode.${episodeId}.mix`, mixBusy)
  const cinemaPollInterval = mixBusy || ep?.status === 'generating' ? 4000 : 0
  const {
    data: polledMix,
    error: mixError,
    status: mixErrorStatus,
    refresh: refreshMix,
  } = usePoll<MixStatus>(
    () => api.get(`/episodes/${episodeId}/mix-status`),
    cinemaPollInterval,
    [episodeId, cinemaPollInterval],
  )
  const {
    data: polledDelivery,
    error: deliveryError,
    refresh: refreshDelivery,
  } = usePoll<{ readiness: DeliveryReadiness; packages: DeliveryPackageRecord[] }>(
    async () => {
      const [nextReadiness, nextPackages] = await Promise.all([
        api.get(`/episodes/${episodeId}/delivery/readiness`),
        api.get(`/episodes/${episodeId}/delivery/packages`),
      ])
      return { readiness: nextReadiness, packages: nextPackages }
    },
    cinemaPollInterval,
    [episodeId, cinemaPollInterval],
  )

  useEffect(() => {
    if (polledMix) setMix(previous => reconcileMixStatus(previous, polledMix))
  }, [polledMix])

  useEffect(() => {
    if (!polledDelivery) return
    setReadiness(polledDelivery.readiness)
    setPackages(polledDelivery.packages)
    setSelectedPackageId(current => {
      if (current && polledDelivery.packages.some(item => item.id === current)) return current
      return polledDelivery.packages.find(item => item.status === 'waiting_human')?.id
        ?? polledDelivery.packages[0]?.id
        ?? null
    })
  }, [polledDelivery])

  if (!ep) {
    return (
      <QueryState
        loading={loading}
        error={error}
        status={status}
        hasData={false}
        objectName="成片台"
        loadingText="正在加载成片、交付检查与交付记录…"
        emptyText="未找到可展示的成片数据，请刷新后重试。"
        onRetry={() => void refreshEpisode()}
      >
        {null}
      </QueryState>
    )
  }

  const selectedPackage = packages.find(item => item.id === selectedPackageId) ?? null
  const spedShots = mix?.shots.filter(shot => shot.has_adopted && Math.abs((shot.playback_rate ?? 1) - 1) > 0.0001) ?? []
  const canReview = selectedPackage?.status === 'waiting_human'
  const selectedPackageIndex = selectedPackage
    ? packages.findIndex(item => item.id === selectedPackage.id)
    : -1
  const selectedPackageLabel = selectedPackageIndex >= 0
    ? `第 ${selectedPackageIndex + 1} 个交付候选`
    : '当前交付候选'
  const createPackageDisabledReason = deliveryBusy
    ? '正在处理上一项交付操作'
    : readiness == null
      ? '正在检查交付条件'
      : !readiness.ready
        ? '需先通过全部交付检查'
        : ''
  const approveDisabledReason = deliveryReviewDisabledReason(
    'approve',
    deliveryBusy,
    selectedPackage?.status ?? null,
    reviewer,
    reason,
    acceptedRisk,
  )
  const riskApprovalDisabledReason = deliveryReviewDisabledReason(
    'approve_with_risk',
    deliveryBusy,
    selectedPackage?.status ?? null,
    reviewer,
    reason,
    acceptedRisk,
  )
  const rejectDisabledReason = deliveryReviewDisabledReason(
    'reject',
    deliveryBusy,
    selectedPackage?.status ?? null,
    reviewer,
    reason,
    acceptedRisk,
  )

  const activateTab = (tab: CinemaTab, moveFocus = false) => {
    setActiveTab(tab)
    if (moveFocus) {
      window.requestAnimationFrame(() => tabRefs.current[tab]?.focus())
    }
  }

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, current: CinemaTab) => {
    const next = nextCinemaTab(current, event.key)
    if (!next) return
    event.preventDefault()
    activateTab(next, true)
  }

  const decide = async (decision: DeliveryDecision) => {
    if (!selectedPackage) {
      toast('请先选择一个交付候选', true)
      return
    }
    if (!reviewer.trim()) {
      toast('请填写真实审核人', true)
      return
    }
    if (!reason.trim()) {
      toast('请填写审核意见', true)
      return
    }
    if (decision === 'approve_with_risk' && !acceptedRisk.trim()) {
      toast('带风险批准必须填写接受风险说明', true)
      return
    }
    setDeliveryBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/delivery/approve`, {
        package_id: selectedPackage.id,
        decided_by: reviewer.trim(),
        decision,
        reason: reason.trim(),
        accepted_risk: decision === 'approve_with_risk' ? acceptedRisk.trim() : undefined,
        idempotency_key: `delivery-review:${ep.id}:${selectedPackage.id}:${decision}`,
      })
      toast(decision === 'reject'
        ? '交付候选已拒绝'
        : decision === 'approve_with_risk'
          ? '风险已记录，交付候选已标记为交付已验证'
          : '交付候选已标记为交付已验证')
      setReason('')
      setAcceptedRisk('')
      refreshDelivery()
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDeliveryBusy(false)
    }
  }

  const concatenate = async () => {
    const currentMix = mix
    if (!currentMix || !canConcatenateMix(currentMix)) return
    mixTimer.start()
    setMixBusy(true)
    try {
      const concatKey = persistentDeliveryOperationKey(`concat:${ep.id}`)
      const result = (await api.post(`/episodes/${ep.id}/concatenate`, {
        idempotency_key: concatKey,
      })) as MixResult
      localStorage.removeItem(deliveryOperationStorageKey(`concat:${ep.id}`))
      if (result.ffmpeg_missing) {
        mixTimer.clear()
        toast(result.note || '服务端缺少视频合成组件，当前仅返回首个片段，不能视为最终成片', true)
      } else {
        if (result.video_url) {
          setMix(previous => previous ? {
            ...previous,
            final_video_url: result.video_url,
            final_video_stale: false,
            final_is_partial: Boolean(result.partial),
            final_edit_report: result.final_edit ?? previous.final_edit_report,
          } : previous)
        }
        const skipped = result.shots_skipped ?? Math.max((result.shots_total ?? currentMix.shots_total) - result.shots, 0)
        const skippedList = result.skipped_shot_nos?.length ? `（第 ${result.skipped_shot_nos.join('、')} 镜）` : ''
        toast(`已按镜号合成 ${result.shots} 个真实视频片段，共约 ${result.total_duration_s} 秒${skipped ? `；跳过 ${skipped} 个尚未完成或未通过技术校验的镜头${skippedList}` : ''}`)
      }
      refreshMix()
      refreshDelivery()
    } catch (e) {
      toast((e as Error).message, true)
      mixTimer.clear()
    } finally {
      setMixBusy(false)
    }
  }

  const createDeliveryPackage = async () => {
    setDeliveryBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/delivery/package`, {
        idempotency_key: persistentDeliveryOperationKey(ep.id),
      })
      localStorage.removeItem(deliveryOperationStorageKey(ep.id))
      toast('交付候选已生成，等待人工复验')
      await refreshDelivery()
      setActiveTab('records')
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDeliveryBusy(false)
    }
  }

  const submitCustomerFeedback = async () => {
    const message = feedback.trim()
    if (!message) return
    setFeedbackBusy(true)
    try {
      await api.post(`/episodes/${ep.id}/customer-feedback`, {
        message,
        created_by: reviewer.trim() || 'customer',
        request_revision: true,
      })
      setFeedback('')
      toast('反馈已回流，并创建修订任务')
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setFeedbackBusy(false)
    }
  }

  const downloadDeliveryFile = async (
    item: DeliveryPackageRecord,
    index: number,
    kind: 'report' | 'archive',
  ) => {
    const busyKey = `${item.id}:${kind}`
    setDownloadBusy(busyKey)
    try {
      const blob = await api.download(`/delivery/packages/${item.id}/${kind}`)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = kind === 'report'
        ? `episode-${ep.episode_no}-delivery-${index + 1}-quality-report.html`
        : `episode-${ep.episode_no}-delivery-${index + 1}.zip`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      toast(kind === 'report' ? '质量报告已开始下载' : '交付归档已开始下载')
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setDownloadBusy(null)
    }
  }

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="成片台" view="cinema" episodeNo={ep.episode_no} />
        <h1>成片台 <span className="sub">预览成片、完成交付检查并沉淀可追溯记录</span></h1>
        <hr className="rule" />
      </header>

      {mix ? (
        <>
          {mixError && (
            <OperationError
              title="成片状态刷新失败"
              message={mixError}
              guidance="当前仍展示上次成功结果，不会将旧状态误报为最新状态。"
              variant="warning"
            >
              <button type="button" className="btn small" onClick={() => void refreshMix()}>
                重试刷新
              </button>
            </OperationError>
          )}
          <section className="card cinema-status">
            <div className="cinema-status-copy">
              <span className={`stamp ${canConcatenateMix(mix) ? 'green' : 'gold'}`}>
                {canConcatenateMix(mix) ? '可合成当前片段' : mix.generation_active ? '模型生成中' : '暂无真实视频'}
              </span>
              <div>
                <b>{mix.shots_ready} 个真实视频片段可合成</b>
                <span>将按分镜号顺序合成当前已有的真实视频；缺失或生成中的镜头直接跳过，不使用静态图片、轻运动卡或静音片段代替{spedShots.length ? `；${spedShots.length} 镜使用变速定稿` : ''}</span>
                {mix.final_edit_report && (
                  <span>
                    {finalEditStatusLabel(mix.final_edit_report)}
                  </span>
                )}
              </div>
            </div>
            <div className="cinema-progress" aria-label="成片准备进度">
              <i style={{ width: `${Math.floor((mix.shots_ready / (mix.shots_total || 1)) * 100)}%` }} />
            </div>
            <div className="cinema-status-actions">
              <button className="btn" onClick={() => { refreshMix(); refreshDelivery() }}>刷新状态</button>
              <button
                className="btn primary"
                disabled={!canConcatenateMix(mix) || mixBusy}
                aria-label={
                  mixBusy
                    ? '合成成品，正在处理中'
                    : !canConcatenateMix(mix)
                      ? '合成成品，暂不可用：当前还没有任何真实模型视频'
                      : '合成成品'
                }
                title={
                  mixBusy
                    ? '成品正在合成，请稍候'
                    : !canConcatenateMix(mix)
                      ? '请等待至少一个真实模型视频落盘'
                      : '按镜号合成当前已有真实视频；未完成镜头会跳过，未采纳真实候选会先自动择优'
                }
                onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setMixConfirmOpen(true)
                }}
              >合成成品</button>
              {mix.final_video_url && (
                <a className="btn" href={mix.final_video_url} download={`episode-${ep.episode_no}-final.mp4`} style={{ textDecoration: 'none' }}>
                  {mix.final_video_stale ? '下载现有成品' : '下载成品'}
                </a>
              )}
              <TaskTimer label="合成" timer={mixTimer} />
            </div>
          </section>

          <div className="cinema-tabs" role="tablist" aria-label="成片台视图">
            <button
              ref={node => { tabRefs.current.preview = node }}
              id={`${tabGroupId}-tab-preview`}
              type="button"
              role="tab"
              aria-selected={activeTab === 'preview'}
              aria-controls={`${tabGroupId}-panel-preview`}
              tabIndex={activeTab === 'preview' ? 0 : -1}
              className={activeTab === 'preview' ? 'active' : ''}
              onClick={() => activateTab('preview')}
              onKeyDown={event => handleTabKeyDown(event, 'preview')}
            >
              <span>01</span>成片预览
            </button>
            <button
              ref={node => { tabRefs.current.readiness = node }}
              id={`${tabGroupId}-tab-readiness`}
              type="button"
              role="tab"
              aria-selected={activeTab === 'readiness'}
              aria-controls={`${tabGroupId}-panel-readiness`}
              tabIndex={activeTab === 'readiness' ? 0 : -1}
              className={activeTab === 'readiness' ? 'active' : ''}
              onClick={() => activateTab('readiness')}
              onKeyDown={event => handleTabKeyDown(event, 'readiness')}
            >
              <span>02</span>交付检查
              <i className={readiness?.ready ? 'ok' : 'warn'}>
                {readiness == null ? '检查中' : readiness.ready ? '通过' : '待处理'}
              </i>
            </button>
            <button
              ref={node => { tabRefs.current.records = node }}
              id={`${tabGroupId}-tab-records`}
              type="button"
              role="tab"
              aria-selected={activeTab === 'records'}
              aria-controls={`${tabGroupId}-panel-records`}
              tabIndex={activeTab === 'records' ? 0 : -1}
              className={activeTab === 'records' ? 'active' : ''}
              onClick={() => activateTab('records')}
              onKeyDown={event => handleTabKeyDown(event, 'records')}
            >
              <span>03</span>交付记录
              {!!packages.length && <i>{packages.length}</i>}
            </button>
          </div>

          {activeTab === 'preview' && (
            <section
              id={`${tabGroupId}-panel-preview`}
              className="card cinema-preview"
              role="tabpanel"
              aria-labelledby={`${tabGroupId}-tab-preview`}
              tabIndex={0}
            >
              <div className="section-heading">
                <div><span className="eyebrow">最终成片</span><h3>《{ep.title}》</h3></div>
                {mix.final_video_url && (
                  <span className={`stamp ${mix.final_video_stale ? 'gold' : 'green'}`}>
                    {mix.final_video_stale ? '现有成品 · 待更新' : mix.final_is_partial ? '最新阶段成片' : '最新完整合成版'}
                  </span>
                )}
              </div>
              {mix.final_video_url ? (
                <>
                  <video src={mix.final_video_url} controls playsInline preload="metadata" />
                  {mix.final_video_stale && (
                    <p className="hint" role="status">
                      新的分镜成品已就绪；当前合成成品继续保留并可正常播放，重新合成后会更新为最新版本。
                    </p>
                  )}
                  {finalSkipSummary(mix.final_edit_report) && (
                    <p className="hint" role="status">
                      {finalSkipSummary(mix.final_edit_report)}
                    </p>
                  )}
                </>
              ) : (
                <div className="cinema-preview-empty">
                  <span>▶</span>
                  <b>{canConcatenateMix(mix) ? `当前可合成 ${mix.shots_ready} 个真实视频片段` : mix.generation_active ? '等待第一个真实视频落盘' : '当前还没有真实模型视频'}</b>
                  <p>{canConcatenateMix(mix) ? '点击上方“合成成品”；未完成镜头会跳过，以后可随时重新合成更新。' : '系统不会使用静态图片、轻运动卡或静音片段冒充视频。'}</p>
                </div>
              )}
            </section>
          )}

          {activeTab === 'readiness' && (
            <section
              id={`${tabGroupId}-panel-readiness`}
              className="card delivery-panel"
              role="tabpanel"
              aria-labelledby={`${tabGroupId}-tab-readiness`}
              tabIndex={0}
            >
              <div className="delivery-head">
                <div><span className="eyebrow">交付检查</span><h3>交付就绪度</h3></div>
                <span className={`stamp ${readiness == null ? 'gold' : readiness.ready ? 'green' : 'red'}`}>
                  {readiness == null ? '检查中' : readiness.ready ? '必检项全部通过' : '尚不可交付'}
                </span>
                <span className="delivery-coverage">
                  {readiness == null
                    ? '正在计算证据覆盖率…'
                    : `证据覆盖率 ${(readiness.evidence_coverage * 100).toFixed(0)}%`}
                </span>
                <button className="btn small" disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '重新检查，暂不可用：正在处理交付操作' : '重新检查交付条件'}
                  onClick={() => void refreshDelivery()}>重新检查</button>
              </div>
              {deliveryError && (
                <OperationError
                  title="交付检查刷新失败"
                  message={deliveryError}
                  guidance="当前检查结果可能不是最新状态；在重新加载成功前不会自动提交交付。"
                >
                  <button type="button" className="btn small" onClick={() => void refreshDelivery()}>重试加载</button>
                </OperationError>
              )}
              <div className="delivery-checks">
                {readiness?.checks.map(item => (
                  <div key={item.key} className={item.passed ? 'passed' : 'failed'}>
                    <span>{item.passed ? '✓' : '!'}</span><b>{deliveryCheckLabel(item.message)}</b>
                    <details className="delivery-check-tech"><summary aria-label={`查看“${deliveryCheckLabel(item.message)}”的检查标识`}>检查标识</summary><code>{item.key}</code></details>
                  </div>
                ))}
              </div>
              {!!readiness?.warnings.length && (
                <details className="delivery-warnings"><summary>查看已知风险（{readiness.warnings.length}）</summary>
                  {readiness.warnings.map((item, index) => <p key={index}>镜 {item.shot_no ?? '—'} · {deliveryWarningLabel(item.message || item.code || '未提供风险说明')}</p>)}
                </details>
              )}
              {!!packages.length && (
                <div className="delivery-package-picker">
                  <label>审核目标交付候选
                    <select
                      value={selectedPackageId ?? ''}
                      disabled={deliveryBusy}
                      aria-label={deliveryBusy
                        ? '审核目标交付候选，暂不可用：正在处理交付操作'
                        : '审核目标交付候选'}
                      onChange={event => setSelectedPackageId(event.target.value || null)}
                    >
                      {packages.map((item, index) => (
                        <option key={item.id} value={item.id}>
                          第 {index + 1} 个交付候选 · {deliveryStatusLabel(item.status)}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              )}
              <div className="delivery-review-form">
                <label>复验人（必填）<input disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '复验人，暂不可用：正在处理交付操作' : '复验人（必填）'}
                  value={reviewer} onChange={event => setReviewer(event.target.value)} placeholder="填写真实审核人姓名" /></label>
                <label>审核意见（必填）<input disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '审核意见，暂不可用：正在处理交付操作' : '审核意见（必填）'}
                  value={reason} onChange={event => setReason(event.target.value)} placeholder="说明通过或拒绝的依据" /></label>
                <label className="full">接受风险（仅带风险批准时必填）<textarea rows={2} disabled={deliveryBusy}
                  aria-label={deliveryBusy ? '接受风险，暂不可用：正在处理交付操作' : '接受风险（仅带风险批准时必填）'}
                  value={acceptedRisk} onChange={event => setAcceptedRisk(event.target.value)} /></label>
              </div>
              <div className="dialog-actions">
                <button
                  className="btn primary"
                  disabled={Boolean(createPackageDisabledReason)}
                  aria-label={createPackageDisabledReason ? `生成交付候选，暂不可用：${createPackageDisabledReason}` : '生成交付候选'}
                  title={createPackageDisabledReason || '基于当前成片与检查结果创建待人工复验的交付候选'}
                  onClick={event => {
                    dialogTriggerRef.current = event.currentTarget
                    setPackageConfirmOpen(true)
                  }}>{deliveryBusy ? '处理中…' : '生成交付候选'}</button>
                <button className="btn" disabled={Boolean(approveDisabledReason)} aria-label={approveDisabledReason ? `批准交付，暂不可用：${approveDisabledReason}` : '批准交付'} title={approveDisabledReason || '确认后将写入交付审计记录'} onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setReviewDecision('approve')
                }}>批准交付</button>
                <button className="btn" disabled={Boolean(riskApprovalDisabledReason)} aria-label={riskApprovalDisabledReason ? `带风险批准，暂不可用：${riskApprovalDisabledReason}` : '带风险批准'} title={riskApprovalDisabledReason || '确认后将记录接受风险并写入交付审计'} onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setReviewDecision('approve_with_risk')
                }}>带风险批准</button>
                <button className="btn ghost danger" disabled={Boolean(rejectDisabledReason)} aria-label={rejectDisabledReason ? `拒绝交付，暂不可用：${rejectDisabledReason}` : '拒绝交付'} title={rejectDisabledReason || '确认后该候选将不能用于交付'} onClick={event => {
                  dialogTriggerRef.current = event.currentTarget
                  setReviewDecision('reject')
                }}>拒绝</button>
              </div>
              <p className="delivery-action-hint" role="status">
                {readiness == null
                  ? '正在加载交付检查，完成前不会开放提交。'
                  : !readiness.ready
                    ? '需先通过全部交付检查，才能生成交付候选。'
                  : !packages.length
                    ? '先生成交付候选，再填写复验人和审核意见。'
                    : !canReview
                      ? `当前选择的交付候选为“${deliveryStatusLabel(selectedPackage?.status || '')}”；请选择“待人工复验”的候选。`
                      : approveDisabledReason
                        ? `${approveDisabledReason}。`
                        : riskApprovalDisabledReason
                          ? '当前已可批准或拒绝；如需带风险批准，请填写接受风险说明。'
                          : '当前可批准、带风险批准或拒绝；提交前会再次确认范围和后果。'}
              </p>
            </section>
          )}

          {activeTab === 'records' && (
            <section
              id={`${tabGroupId}-panel-records`}
              className="card delivery-records"
              role="tabpanel"
              aria-labelledby={`${tabGroupId}-tab-records`}
              tabIndex={0}
            >
              <div className="section-heading">
                <div><span className="eyebrow">审计记录</span><h3>交付候选与反馈记录</h3></div>
                <span className="hint">已交付快照不会被后续反馈覆盖</span>
              </div>
              {packages.length ? (
                <div className="delivery-packages">
                  {packages.map((item, index) => <div key={item.id}>
                    <button
                      type="button"
                      className={`btn small${item.id === selectedPackageId ? ' primary' : ''}`}
                      onClick={() => { setSelectedPackageId(item.id); activateTab('readiness') }}
                    >查看或审核</button>
                    <div className="delivery-package-copy">
                      <b>交付候选 {index + 1}</b>
                      <time dateTime={new Date(item.created_at < 1_000_000_000_000 ? item.created_at * 1000 : item.created_at).toISOString()}>
                        创建于 {formatDeliveryTime(item.created_at)}
                      </time>
                    </div>
                    <span className={`stamp ${['approved', 'approved_with_risk'].includes(item.status) ? 'green' : item.status === 'rejected' ? 'red' : 'gold'}`}>{deliveryStatusLabel(item.status)}</span>
                    <details><summary>技术标识</summary><code>{item.id}</code></details>
                    <button
                      type="button"
                      className="btn small"
                      disabled={downloadBusy !== null}
                      aria-label={downloadBusy
                        ? `下载交付候选 ${index + 1} 的质量报告，暂不可用：正在下载其他交付文件`
                        : `下载交付候选 ${index + 1} 的质量报告`}
                      onClick={() => { void downloadDeliveryFile(item, index, 'report') }}
                    >{downloadBusy === `${item.id}:report` ? '报告下载中…' : '下载质量报告'}</button>
                    <button
                      type="button"
                      className="btn small"
                      disabled={downloadBusy !== null}
                      aria-label={downloadBusy
                        ? `下载交付候选 ${index + 1} 的交付归档，暂不可用：正在下载其他交付文件`
                        : `下载交付候选 ${index + 1} 的交付归档`}
                      onClick={() => { void downloadDeliveryFile(item, index, 'archive') }}
                    >{downloadBusy === `${item.id}:archive` ? '归档下载中…' : '下载交付归档'}</button>
                  </div>)}
                </div>
              ) : (
                <div className="delivery-records-empty">暂无交付记录，请先通过交付检查并生成交付候选。</div>
              )}
              <div className="customer-feedback">
                <input disabled={feedbackBusy}
                  aria-label={feedbackBusy ? '客户反馈，暂不可用：正在提交并创建修订任务' : '客户反馈'}
                  value={feedback} onChange={event => setFeedback(event.target.value)}
                  placeholder="输入客户反馈；确认后创建新的修订任务" />
                <button className="btn primary small" disabled={feedbackBusy || !feedback.trim()}
                  aria-label={feedbackBusy
                    ? '提交客户反馈，暂不可用：正在创建修订任务'
                    : !feedback.trim()
                      ? '提交客户反馈，暂不可用：请先填写反馈内容'
                      : '预览客户反馈影响并发起修订'}
                  onClick={event => {
                    dialogTriggerRef.current = event.currentTarget
                    setFeedbackConfirmOpen(true)
                  }}>{feedbackBusy ? '提交中…' : '提交并发起修订'}</button>
              </div>
            </section>
          )}

          {reviewDecision && selectedPackage && (
            <DecisionDialog
              title={reviewDecision === 'approve'
                ? '批准当前交付候选？'
                : reviewDecision === 'approve_with_risk'
                  ? '带风险批准当前交付候选？'
                  : '拒绝当前交付候选？'}
              summary={`${selectedPackageLabel} · ${deliveryStatusLabel(selectedPackage.status)}`}
              message={reviewDecision === 'approve'
                ? '确认后将标记为交付已验证，并写入不可覆盖的交付审计记录。'
                : reviewDecision === 'approve_with_risk'
                  ? '确认后将连同接受风险说明一起标记为交付已验证，并写入交付审计记录。'
                  : '确认后该候选将被标记为已拒绝，不能再作为交付结果。'}
              details={[
                `复验人：${reviewer.trim()}`,
                `审核意见：${reason.trim()}`,
                ...(reviewDecision === 'approve_with_risk' ? [`接受风险：${acceptedRisk.trim()}`] : []),
                '本次审核不会生成新媒体或产生模型费用；既有成片和其他候选保留',
              ]}
              confirmLabel={reviewDecision === 'approve'
                ? '确认批准交付'
                : reviewDecision === 'approve_with_risk'
                  ? '确认带风险批准'
                  : '确认拒绝交付'}
              cancelLabel="返回检查"
              danger={reviewDecision === 'reject'}
              returnFocus={dialogTriggerRef.current}
              onClose={() => setReviewDecision(null)}
              onConfirm={() => {
                const decision = reviewDecision
                setReviewDecision(null)
                void decide(decision)
              }}
            />
          )}

          {packageConfirmOpen && (
            <DecisionDialog
              title="生成当前交付候选？"
              summary={`第 ${ep.episode_no} 集 · 《${ep.title}》`}
              message="系统会把当前成片、交付检查和质量依据固化为一个新的待人工复验候选。"
              details={[
                '不会自动批准或对外交付，生成后仍需填写复验人和审核意见',
                '不会重新生成图片或视频，也不产生模型生成费用',
                '现有成片、既有交付候选和审核记录都会保留',
              ]}
              confirmLabel="确认生成交付候选"
              cancelLabel="取消（不生成）"
              returnFocus={dialogTriggerRef.current}
              onClose={() => setPackageConfirmOpen(false)}
              onConfirm={() => {
                setPackageConfirmOpen(false)
                void createDeliveryPackage()
              }}
            />
          )}

          {feedbackConfirmOpen && (
            <DecisionDialog
              title="提交客户反馈并发起修订？"
              summary={`第 ${ep.episode_no} 集 · ${feedback.trim()}`}
              message="确认后会保存这条反馈并创建新的修订任务，供制作人员后续处理。"
              details={[
                '不会立即重新生成图片、视频或产生模型费用',
                '当前成片、已交付快照和既有审核记录不会被覆盖',
                '反馈提交成功后会清空本次输入',
              ]}
              confirmLabel="确认提交并创建修订任务"
              cancelLabel="返回修改反馈"
              returnFocus={dialogTriggerRef.current}
              onClose={() => setFeedbackConfirmOpen(false)}
              onConfirm={() => {
                setFeedbackConfirmOpen(false)
                void submitCustomerFeedback()
              }}
            />
          )}

          {mixConfirmOpen && (
            <DecisionDialog
              title={mix.final_video_stale ? '更新合成本集成品？' : mix.final_video_url ? '重新合成本集成品？' : '合成本集成品？'}
              summary={`将按镜号合成当前 ${mix.shots_ready} 个真实视频片段；其余 ${Math.max(mix.shots_total - mix.shots_ready, 0)} 镜直接跳过`}
              message={mix.final_video_url
                ? '现有最新成片入口会更新为本次结果；镜头候选、采用关系和既有交付记录不会随之删除。'
                : '将使用当前已有的真实采用版；未采纳但已有可播放模型候选的分镜会先自动择优，其余镜头跳过。'}
              details={[
                '合成过程不产生模型生成费用',
                spedShots.length
                  ? `倍速定稿：${spedShots.map(shot => `镜 ${shot.shot_no} ${shot.playback_rate}×`).join('、')}`
                  : '所有采纳片段均按 1× 合成',
                mix.skipped_shot_nos?.length
                  ? `本次直接跳过尚无真实视频的镜号：${mix.skipped_shot_nos.join('、')}`
                  : '所有分镜均已有真实视频',
                '若视频合成组件不可用，系统会明确提示，首个片段不会冒充最终成片',
              ]}
              confirmLabel={mix.final_video_url ? '确认重新合成' : '确认合成成品'}
              cancelLabel="暂不合成"
              returnFocus={dialogTriggerRef.current}
              onClose={() => setMixConfirmOpen(false)}
              onConfirm={() => {
                setMixConfirmOpen(false)
                void concatenate()
              }}
            />
          )}
        </>
      ) : (
        <QueryState
          loading={!mixError}
          error={mixError}
          status={mixErrorStatus}
          hasData={false}
          objectName="成片状态"
          loadingText="正在检查镜头就绪度与成片状态…"
          emptyText="尚未取得成片状态，请刷新后重试。"
          onRetry={() => void refreshMix()}
        >
          {null}
        </QueryState>
      )}
    </>
  )
}
