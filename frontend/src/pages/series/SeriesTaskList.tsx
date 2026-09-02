import { useState } from 'react'
import DecisionDialog from '../../components/DecisionDialog'
import type { SeriesTaskSummary } from '../../api'
import { formatFilmDuration } from './SeriesFilmPlayer'
import {
  formatFilmSize,
  seriesTaskProgressLabel,
  seriesTaskProgressPercent,
  seriesTaskStatusLabel,
  seriesTaskStatusTone,
  seriesTaskTitle,
} from './seriesTaskText'

const DELETABLE_STATUSES = new Set(['idle', 'failed', 'cancelled'])

function SeriesTaskRow({
  task,
  checked,
  onToggle,
  onStart,
  starting,
  onView,
  onRequestDelete,
}: {
  task: SeriesTaskSummary
  checked: boolean
  onToggle: () => void
  onStart: () => void
  starting: boolean
  onView: () => void
  onRequestDelete: () => void
}) {
  const hasGap = task.missing_episode_nos.length > 0
  const canStart = task.status !== 'running' && task.status !== 'queued' && !hasGap
  const canDelete = DELETABLE_STATUSES.has(task.status)
  const percent = seriesTaskProgressPercent(task.steps_done, task.steps_total)
  const title = seriesTaskTitle(task)

  return (
    <tr>
      <td>
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={`勾选 ${title}`} />
      </td>
      <td>{task.index}</td>
      <td>
        {title}
        {hasGap && (
          <p className="series-task-missing" role="alert">
            缺第 {task.missing_episode_nos.join('、')} 集，去分集规划补齐后才能入队
          </p>
        )}
      </td>
      <td>{task.episode_count} 集</td>
      <td><span className={`stamp ${seriesTaskStatusTone(task.status)}`}>{seriesTaskStatusLabel(task.status)}</span></td>
      <td>
        <div className="series-task-progress">
          <span>{seriesTaskProgressLabel(task)}</span>
          <span className="series-task-progress-steps">{task.steps_done}/{task.steps_total} 步 · {percent}%</span>
        </div>
      </td>
      <td>
        {task.film
          ? `${formatFilmDuration(task.film.duration_s)} · ${formatFilmSize(task.film.size_bytes)}`
            + (task.film_stale ? ' · 成片已过期，可重新执行' : '')
          : '尚无成片'}
      </td>
      <td className="series-task-actions">
        <button type="button" className="btn small primary" disabled={!canStart || starting} onClick={onStart}>
          {starting ? '启动中…' : '开始'}
        </button>
        <button type="button" className="btn small" onClick={onView}>查看</button>
        {canDelete && <button type="button" className="btn small danger" onClick={onRequestDelete}>删除</button>}
      </td>
    </tr>
  )
}

export default function SeriesTaskList({
  tasks,
  selected,
  onToggle,
  onToggleAllOnPage,
  allOnPageSelected,
  onStart,
  startBusyTaskId,
  onView,
  onDelete,
}: {
  tasks: SeriesTaskSummary[]
  selected: Set<string>
  onToggle: (taskId: string) => void
  onToggleAllOnPage: () => void
  allOnPageSelected: boolean
  onStart: (taskId: string) => void
  startBusyTaskId: string | null
  onView: (taskId: string) => void
  onDelete: (taskId: string) => void
}) {
  const [deleteTarget, setDeleteTarget] = useState<SeriesTaskSummary | null>(null)

  if (!tasks.length) {
    return <p className="series-empty">当前分页没有连播任务，先在上方按每 N 集切分生成任务清单。</p>
  }

  return (
    <div className="series-table-scroll">
      <table className="series-task-table">
        <thead>
          <tr>
            <th><input type="checkbox" checked={allOnPageSelected} onChange={onToggleAllOnPage} aria-label="全选本页" /></th>
            <th>序号</th>
            <th>标题</th>
            <th>集数</th>
            <th>状态</th>
            <th>进度</th>
            <th>成片</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map(task => (
            <SeriesTaskRow
              key={task.task_id}
              task={task}
              checked={selected.has(task.task_id)}
              onToggle={() => onToggle(task.task_id)}
              onStart={() => onStart(task.task_id)}
              starting={startBusyTaskId === task.task_id}
              onView={() => onView(task.task_id)}
              onRequestDelete={() => setDeleteTarget(task)}
            />
          ))}
        </tbody>
      </table>
      {deleteTarget && (
        <DecisionDialog
          title="删除连播任务"
          summary={`删除「${seriesTaskTitle(deleteTarget)}」`}
          message="只删任务记录，磁盘上已生成的成片保留；如需彻底清理请另行到文件系统删除。"
          confirmLabel="确认删除"
          cancelLabel="取消"
          danger
          onConfirm={() => { onDelete(deleteTarget.task_id); setDeleteTarget(null) }}
          onClose={() => setDeleteTarget(null)}
        />
      )}
    </div>
  )
}
