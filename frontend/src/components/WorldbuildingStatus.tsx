import { useState } from 'react'
import { api, Project } from '../api'
import { usePoll } from '../App'
import { ServerTaskTimer } from './TaskTimer'
import DecisionDialog from './DecisionDialog'

export type RefsProgress = Awaited<ReturnType<typeof api.refsProgress>>

export function summarizeRefsProgress(progress: RefsProgress | null): string {
  if (!progress) return ''
  const parts = [
    `定妆进度：已完成 ${progress.ready} / ${progress.total}`,
    `失败 ${progress.failed}`,
    `缺失 ${progress.missing}`,
  ]
  if (progress.deferred) parts.push(`暂缓 ${progress.deferred}`)
  if (progress.blocked) parts.push(`外观未通过 ${progress.blocked}`)
  return parts.join('，')
}

type WorldbuildingProject = Pick<Project, 'id' | 'bible_status' | 'refs_status' | 'task_timings'>

/** 人物谱/定妆照两个阶段——「出图之前」的前两段——是否仍在跑。人物谱页与
 *  场景库页在这个窗口内必须呈现同一套 UI：同一个停止按钮、同一批状态芯片、
 *  同一份已执行计时，不能因为观察者是哪个页面就显示不同结论或不同入口
 *  （2026-08-29 用户实测反馈：场景库页曾在人物谱生成中显示「未开始」+
 *  禁用按钮，而人物谱页显示正确的进行中状态）。 */
export function worldbuildingRunning(project: Pick<Project, 'bible_status' | 'refs_status'>): boolean {
  return project.bible_status === 'running' || project.refs_status === 'running'
}

export function worldbuildingStopLabel(project: Pick<Project, 'bible_status'>): string {
  return project.bible_status === 'running' ? '停止谱写' : '停止定妆'
}

export default function WorldbuildingStatus({
  project, running, busy, setBusy, toast, refresh, refreshRefsProgress: refreshRefsProgressProp,
}: {
  project: WorldbuildingProject
  /** 是否仍在人物谱/定妆照阶段。调用方自行判定并传入（人物谱页在
   *  bible_status/refs_status 之外还叠加了轮询中的 refsProgress 信号，两页
   *  不必共用同一份口径，但都必须诚实——不能因为这里再算一遍而漂移）。 */
  running: boolean
  busy: boolean
  setBusy: (value: boolean) => void
  toast: (message: string, isErr?: boolean) => void
  refresh: () => void
  /** 可选：调用方如果已经在轮询 refs/progress（人物谱页为角色级定妆清单复用
   *  同一份数据），把它的 refresh 函数传进来，这里就不再重复起一个轮询打
   *  同一个接口；不传时（场景库页）组件自己按需轮询，仅用于停止后的进度小结。 */
  refreshRefsProgress?: () => Promise<RefsProgress | null>
}) {
  const [stopConfirm, setStopConfirm] = useState(false)
  const hasExternalPoll = !!refreshRefsProgressProp
  const { refresh: ownRefreshRefsProgress } = usePoll<RefsProgress>(
    () => api.refsProgress(project.id),
    () => (running && !hasExternalPoll ? 4000 : 0),
    [project.id],
  )
  const refreshRefsProgress = refreshRefsProgressProp ?? ownRefreshRefsProgress
  if (!running) return null
  const stopLabel = worldbuildingStopLabel(project)

  const stopGeneration = async () => {
    setBusy(true)
    let stopped = ''
    try {
      if (project.bible_status === 'running') {
        await api.cancelBibleGeneration(project.id)
        stopped = '已停止谱写；已落盘资产保留'
      } else {
        await api.cancelRefsGeneration(project.id)
        stopped = '已停止定妆；已落盘资产保留'
      }
      let summary = ''
      try {
        const progress = await refreshRefsProgress()
        if (progress) summary = summarizeRefsProgress(progress)
      } catch { /* keep original stop toast */ }
      toast(summary ? `${stopped}；${summary}` : stopped)
      refresh()
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : String(e), true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button type="button" className="btn ghost danger" disabled={busy}
        aria-label={busy ? `${stopLabel}，暂不可用：正在处理上一项操作` : stopLabel}
        onClick={() => setStopConfirm(true)}>
        {stopLabel}
      </button>
      {project.bible_status === 'running' && <span className="stamp gold">谱写中</span>}
      {project.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
      <ServerTaskTimer
        startedAt={project.task_timings?.bible?.started_at}
        finishedAt={project.task_timings?.bible?.finished_at}
        running={project.bible_status === 'running'}
      />
      <ServerTaskTimer
        startedAt={project.task_timings?.refs?.started_at}
        finishedAt={project.task_timings?.refs?.finished_at}
        running={project.refs_status === 'running'}
      />
      {stopConfirm && (
        <DecisionDialog
          title={`${stopLabel}？`}
          summary={project.bible_status === 'running' ? '人物谱尚未完成' : '定妆照仍在生成'}
          message={project.bible_status === 'running'
            ? '系统会停止当前谱写任务；尚未完成的人物谱不会发布，已有原著和旧版本保持不变。'
            : '系统会停止后续定妆队列并保留已落盘素材；已提交给图片服务的当前请求可能仍会完成并产生费用。'}
          details={[
            project.bible_status === 'running' ? '稍后可重新发起人物谱生成' : '已完成定妆照不会删除，可稍后补齐缺失项',
            '停止请求不代表供应商会退回已经发生的费用',
          ]}
          confirmLabel={`确认${stopLabel}`}
          cancelLabel="继续生成"
          danger
          onClose={() => setStopConfirm(false)}
          onConfirm={() => { setStopConfirm(false); void stopGeneration() }}
        />
      )}
    </>
  )
}
