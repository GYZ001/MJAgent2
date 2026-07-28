import { useEffect, useMemo, useState } from 'react'
import type { Shot } from '../api'

const ACTIVE_PIPELINE = new Set([
  'queued', 'running', 'waiting', 'waiting_provider', 'waiting_retry',
  'waiting_budget', 'paused_budget', 'blocked',
])

const PROVIDER_STAGES = new Set(['video_generating', 'video_poll'])
const POST_STAGES = new Set([
  'video_downloading', 'video_download', 'video_technical_check', 'video_qa',
])
const WAITING_STAGES = new Set([
  'waiting_continuity_anchor', 'waiting_video_slot', 'waiting_human',
  'paused_budget', 'auto_retake_queued',
])

export interface VideoActivitySummary {
  activeCount: number
  taskAcceptedCount: number
  preparingCount: number
  waitingCount: number
  submittingCount: number
  providerSubmittedCount: number
  providerCount: number
  postCount: number
  uncertainCount: number
  startedAt: number | null
  activeShots: Shot[]
}

function hasActiveVideoTask(shot: Shot) {
  if (shot.pipeline && ACTIVE_PIPELINE.has(shot.pipeline.pipeline_status)) return true
  return shot.versions.some(version => ['queued', 'running', 'waiting_provider'].includes(version.status))
}

export function summarizeVideoActivity(shots: Shot[]): VideoActivitySummary {
  const activeShots = shots.filter(hasActiveVideoTask)
  let taskAcceptedCount = 0
  let preparingCount = 0
  let waitingCount = 0
  let submittingCount = 0
  let providerSubmittedCount = 0
  let providerCount = 0
  let postCount = 0
  let uncertainCount = 0
  const starts: number[] = []

  for (const shot of activeShots) {
    const pipeline = shot.pipeline
    const stage = pipeline?.pipeline_stage || pipeline?.current_stage || ''
    if (pipeline?.task_accepted || pipeline?.task_id) taskAcceptedCount += 1
    const createdAt = pipeline?.task_created_at
      ?? shot.versions.find(version => ['queued', 'running', 'waiting_provider'].includes(version.status))?.created_at
    if (typeof createdAt === 'number' && Number.isFinite(createdAt)) starts.push(createdAt)
    if (pipeline?.provider_submitted) providerSubmittedCount += 1

    if (POST_STAGES.has(stage)) postCount += 1
    else if (pipeline?.provider_submitted || PROVIDER_STAGES.has(stage)) providerCount += 1
    else if (stage === 'video_submitting' || stage === 'video_submit') submittingCount += 1
    else if (WAITING_STAGES.has(stage) || ['waiting', 'blocked', 'paused_budget'].includes(pipeline?.pipeline_status || '')) waitingCount += 1
    else if (pipeline) preparingCount += 1
    else uncertainCount += 1
  }

  return {
    activeCount: activeShots.length,
    taskAcceptedCount,
    preparingCount,
    waitingCount,
    submittingCount,
    providerSubmittedCount,
    providerCount,
    postCount,
    uncertainCount,
    startedAt: starts.length ? Math.min(...starts) : null,
    activeShots,
  }
}

export function compactShotStage(shot: Shot): string {
  const pipeline = shot.pipeline
  const stage = pipeline?.pipeline_stage || pipeline?.current_stage || ''
  const progress = pipeline?.stage_progress
  const current = progress?.current
  const total = progress?.total
  const labels: Record<string, string> = {
    job_queued: '已入队',
    reference_prompt: '准备参考图词',
    reference_generate: current != null && total ? `候选图 ${current}/${total}` : '生成候选图',
    reference_qa: current != null && total ? `参考图质检 ${current}/${total}` : '参考图质检',
    reference_consistency: '参考图一致性',
    waiting_continuity_anchor: '等待上一镜',
    continuity_assembling: '装配连续性',
    video_ready: '视频输入就绪',
    waiting_video_slot: pipeline?.queue_position ? `等待名额（前方 ${pipeline.queue_position}）` : '等待视频名额',
    video_submitting: '提交供应商',
    video_generating: '供应商生成中',
    video_downloading: '下载视频',
    video_technical_check: '技术校验',
    video_qa: '内容质检',
    auto_retake_queued: '重新生成排队',
    paused_budget: '预算暂停',
    waiting_human: '等待人工',
  }
  return labels[stage] || pipeline?.stage_label || '状态同步中'
}

function formatDuration(seconds: number) {
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours) return `${hours}小时${String(minutes).padStart(2, '0')}分`
  if (minutes) return `${minutes}分${String(secs).padStart(2, '0')}秒`
  return `${secs}秒`
}

export default function VideoGenerationActivity({
  shots,
  requestPending = false,
  lastSyncedAt,
  syncError,
  refreshing = false,
  onRefresh,
}: {
  shots: Shot[]
  requestPending?: boolean
  lastSyncedAt: number | null
  syncError?: string | null
  refreshing?: boolean
  onRefresh: () => void
}) {
  const summary = useMemo(() => summarizeVideoActivity(shots), [shots])
  const [clock, setClock] = useState(Date.now())

  useEffect(() => {
    if (!summary.activeCount) return
    setClock(Date.now())
    const id = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [summary.activeCount, summary.startedAt])

  if (!summary.activeCount && !requestPending && !syncError) return null

  const elapsed = summary.startedAt ? formatDuration(clock / 1000 - summary.startedAt) : null
  const unconfirmed = Math.max(0, summary.activeCount - summary.taskAcceptedCount)
  const heading = syncError
    ? '生成状态同步中断'
    : requestPending && !summary.activeCount
      ? '正在提交生成请求'
      : summary.providerSubmittedCount > 0
        ? `任务已下发：${summary.providerSubmittedCount} 镜供应商已接单`
        : `任务已创建：${summary.taskAcceptedCount || summary.activeCount} 镜已进入系统队列`
  const detail = summary.providerSubmittedCount > 0
    ? `当前共 ${summary.activeCount} 镜在处理；${summary.providerCount} 镜仍在供应商生成，${summary.postCount} 镜已进入下载或质检，其余仍在准备或排队。`
    : summary.activeCount > 0
      ? '任务已写入系统，尚未得到供应商接单回执；当前正在准备参考输入或等待执行槽位。'
      : '请求正在送往服务端，尚未返回任务标识，此时不代表已下发供应商。'

  return (
    <section className={`video-activity${syncError ? ' is-stale' : ''}`} role={syncError ? 'alert' : 'status'} aria-live="polite">
      <div className="video-activity-head">
        {!syncError && <span className="video-activity-pulse" aria-hidden />}
        <div>
          <strong>{heading}</strong>
          <p>{syncError ? '下方显示上一次成功同步的数据，暂停据此判断任务结果。' : detail}</p>
          {syncError && <details><summary>查看同步错误</summary><pre>{syncError}</pre></details>}
        </div>
        <div className="video-activity-sync">
          {elapsed && !syncError && <b>已运行 {elapsed}</b>}
          <span>{lastSyncedAt ? `最后同步 ${new Date(lastSyncedAt).toLocaleTimeString()}` : '等待首次同步'}</span>
          <button type="button" className="btn ghost small" disabled={refreshing} onClick={onRefresh}>{refreshing ? '刷新中…' : '立即刷新'}</button>
        </div>
      </div>

      {summary.activeCount > 0 && (
        <>
          <div className="video-activity-stages" aria-label="当前任务阶段分布">
            <span className="accepted"><b>{summary.taskAcceptedCount}</b><small>系统已受理</small></span>
            <span><b>{summary.preparingCount}</b><small>准备输入</small></span>
            <span><b>{summary.waitingCount}</b><small>排队/等待</small></span>
            <span><b>{summary.submittingCount}</b><small>提交中</small></span>
            <span className={summary.providerCount ? 'active' : ''}><b>{summary.providerCount}</b><small>供应商生成</small></span>
            <span><b>{summary.postCount}</b><small>下载/质检</small></span>
          </div>
          {(unconfirmed > 0 || summary.uncertainCount > 0) && (
            <p className="video-activity-warning">{unconfirmed || summary.uncertainCount} 镜缺少新版任务标识，状态尚待服务端确认，请刷新后再操作。</p>
          )}
          <details className="video-activity-details">
            <summary>查看 {summary.activeCount} 个任务的当前阶段</summary>
            <div>
              {summary.activeShots.map(shot => (
                <span key={shot.id}>
                  <b>镜 {String(shot.shot_no).padStart(2, '0')}</b>
                  <em>{compactShotStage(shot)}</em>
                  <small>{shot.pipeline?.provider_submitted ? '供应商已接单' : shot.pipeline?.task_accepted ? '系统已受理' : '待确认'}</small>
                  {shot.pipeline?.task_id && <code title={shot.pipeline.task_id}>{shot.pipeline.task_id}</code>}
                </span>
              ))}
            </div>
          </details>
        </>
      )}
    </section>
  )
}
