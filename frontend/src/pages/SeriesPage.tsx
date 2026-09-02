import { useEffect, useRef, useState } from 'react'
import { useNav, usePoll } from '../App'
import { api, type SeriesFilmSnapshot, type SeriesRun, type SeriesRunStatus } from '../api'
import QueryState from '../components/QueryState'
import OperationError from '../components/OperationError'
import EpisodeRangePicker, { SERIES_MAX_SPAN, validateEpisodeRange } from './series/EpisodeRangePicker'
import SeriesProgressBoard from './series/SeriesProgressBoard'
import SeriesFilmPlayer from './series/SeriesFilmPlayer'
import '../styles/SeriesPage.css'

const seriesFilmPollInterval = (snap: SeriesFilmSnapshot | null) =>
  snap?.run?.status === 'running' ? 4000 : 0

export type SeriesPrimaryActionKind = 'start' | 'pause' | 'resume'

export interface SeriesPrimaryAction {
  kind: SeriesPrimaryActionKind
  label: string
}

/** 无运行或终态里的"成功/取消" → 可以开始新一段区间；运行中 → 暂停；暂停或
 *  失败 → 继续（失败不归入"可重新开始"这一档：用户应该先看错误、按提示修完
 *  再继续原区间，而不是绕开问题另起一段）。 */
export function seriesPrimaryAction(run: SeriesRun | null): SeriesPrimaryAction {
  if (!run || run.status === 'succeeded' || run.status === 'cancelled') {
    return { kind: 'start', label: '开始制作连播成片' }
  }
  if (run.status === 'running') return { kind: 'pause', label: '暂停' }
  return { kind: 'resume', label: '继续' }
}

const STATUS_LABEL: Record<SeriesRunStatus, string> = {
  running: '连播制作中',
  paused: '已暂停',
  failed: '失败',
  succeeded: '已完成',
  cancelled: '已取消',
}

const STATUS_TONE: Record<SeriesRunStatus, 'grey' | 'gold' | 'green' | 'red'> = {
  running: 'gold',
  paused: 'grey',
  failed: 'red',
  succeeded: 'green',
  cancelled: 'grey',
}

export function seriesRunStatusLabel(status: SeriesRunStatus | null | undefined): string {
  return status ? (STATUS_LABEL[status] ?? '状态未知') : '尚未开始'
}

export function seriesRunStatusTone(
  status: SeriesRunStatus | null | undefined,
): 'grey' | 'gold' | 'green' | 'red' {
  return status ? (STATUS_TONE[status] ?? 'grey') : 'grey'
}

function newIdemKey(prefix: string): string {
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`
}

export default function SeriesPage() {
  const { projectId } = useNav()
  const { data, error, status, loading, refresh } = usePoll<SeriesFilmSnapshot>(
    () => api.getSeriesFilm(projectId!),
    seriesFilmPollInterval,
    [projectId],
  )
  const [from, setFrom] = useState<number | null>(null)
  const [to, setTo] = useState<number | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const run = data?.run ?? null
  const available = data?.episodes_available ?? []
  // 只在"看到一个新的 run_id"时才用它的区间覆盖选择框——避免同一个 run 的
  // 重复轮询（甚至窗口重新获得焦点触发的一次性追平）反复把用户刚为下一段
  // 选好的区间静默冲掉。
  const hydratedRunIdRef = useRef<string | null>(null)

  useEffect(() => {
    if (run) {
      if (hydratedRunIdRef.current === run.run_id) return
      hydratedRunIdRef.current = run.run_id
      setFrom(run.episode_from)
      setTo(run.episode_to)
      return
    }
    if (from != null || to != null || !available.length) return
    const nos = available.map(ep => ep.episode_no).sort((a, b) => a - b)
    setFrom(nos[Math.max(0, nos.length - SERIES_MAX_SPAN)])
    setTo(nos[nos.length - 1])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, available.length])

  if (!data) {
    return (
      <QueryState
        loading={loading}
        error={error}
        status={status}
        hasData={false}
        objectName="连播台数据"
        onRetry={() => void refresh({ force: true })}
      >
        {null}
      </QueryState>
    )
  }

  const film = data.film
  const validation = validateEpisodeRange(available, from, to)
  const primaryAction = seriesPrimaryAction(run)
  const rangeLocked = run?.status === 'running'

  const runAction = async (action: () => Promise<unknown>) => {
    setActionBusy(true)
    setActionError(null)
    try {
      await action()
      await refresh({ force: true })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setActionBusy(false)
    }
  }

  const onPrimary = () => {
    if (primaryAction.kind === 'start') {
      if (!validation.ok || from == null || to == null) return
      void runAction(() => api.startSeriesFilm(projectId!, {
        episode_from: from,
        episode_to: to,
        idempotency_key: newIdemKey(`series-film:${projectId}`),
      }))
      return
    }
    if (primaryAction.kind === 'pause') {
      void runAction(() => api.pauseSeriesFilm(projectId!))
      return
    }
    void runAction(() => api.resumeSeriesFilm(projectId!))
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 连播台</div>
        <h1>连播台 <span className="sub">按顺序串行制作并连成一部连播成片</span></h1>
        <hr className="rule" />
      </header>
      <p className="series-intro">
        选择连续的 1–10 集，系统会逐集串行完成映射、分镜、确认、生成、成片，最后把这些集按顺序连成一部连播成片。
        每集完整链路约 20–90 分钟，十集需要数小时；可以关掉页面，进度保存在服务端。
      </p>
      <section className="series-status-bar card">
        <span className={`stamp ${seriesRunStatusTone(run?.status)}`}>{seriesRunStatusLabel(run?.status)}</span>
        <button
          type="button"
          className="btn primary"
          disabled={actionBusy || (primaryAction.kind === 'start' && !validation.ok)}
          onClick={onPrimary}
        >
          {actionBusy ? '处理中…' : primaryAction.label}
        </button>
      </section>
      {actionError && <OperationError title="操作失败" guidance={actionError} />}
      <EpisodeRangePicker
        available={available}
        from={from}
        to={to}
        onChangeFrom={setFrom}
        onChangeTo={setTo}
        disabled={rangeLocked}
      />
      <SeriesProgressBoard run={run} projectId={projectId!} />
      {film && <SeriesFilmPlayer film={film} />}
    </>
  )
}
