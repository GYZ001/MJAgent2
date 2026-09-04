import { useNav, usePoll } from '../../App'
import { api } from '../../api'
import type { SeriesTaskDetail as SeriesTaskDetailType } from '../../api'
import QueryState from '../../components/QueryState'
import OperationError from '../../components/OperationError'
import SeriesProgressBoard from './SeriesProgressBoard'
import SeriesFilmPlayer from './SeriesFilmPlayer'
import { seriesTaskProgressLabel, seriesTaskStatusLabel, seriesTaskStatusTone, seriesTaskTitle } from './seriesTaskText'

const detailPollInterval = (detail: SeriesTaskDetailType | null) =>
  detail && (detail.status === 'running' || detail.status === 'queued') ? 4000 : 0

/** 任务详情页：路由 /projects/{pid}/series/{taskId}，taskId 是 Nav 自己的字段
 *  （见 App.tsx 的 Nav.taskId，不借用 episodeId）。刷新页面要停在详情页，所以
 *  这里必须真的走 URL 路由，不能只靠父组件内部 state 记住"当前在看哪个任务"。 */
export default function SeriesTaskDetail({ projectId, taskId }: { projectId: string; taskId: string }) {
  const { go } = useNav()
  const { data, error, status, loading, refresh } = usePoll<SeriesTaskDetailType>(
    () => api.getSeriesTaskDetail(projectId, taskId),
    detailPollInterval,
    [projectId, taskId],
  )

  if (!data) {
    return (
      <QueryState
        loading={loading}
        error={error}
        status={status}
        hasData={false}
        objectName="连播任务详情"
        onRetry={() => void refresh({ force: true })}
      >
        {null}
      </QueryState>
    )
  }

  const title = seriesTaskTitle(data)

  return (
    <>
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 连播台 / {title}</div>
        <h1>
          {title}
          {data.title && <span className="sub">第 {data.episode_from}-{data.episode_to} 集</span>}
        </h1>
        <hr className="rule" />
      </header>
      <button type="button" className="btn" onClick={() => go('series', projectId)}>← 返回任务列表</button>
      <section className="series-task-detail-meta card">
        <span className={`stamp ${seriesTaskStatusTone(data.status)}`}>{seriesTaskStatusLabel(data.status)}</span>
        <span>{seriesTaskProgressLabel(data)}</span>
        <span>{data.steps_done}/{data.steps_total} 步</span>
        {data.note && <span className="hint" role="status">{data.note}</span>}
      </section>
      {data.missing_episode_nos.length > 0 && (
        <OperationError
          title="区间内缺集，无法入队"
          guidance={`缺第 ${data.missing_episode_nos.join('、')} 集，请先到分集规划补齐这些集，再回到任务列表重新执行。`}
        />
      )}
      {data.film_stale && (
        <OperationError
          variant="warning"
          title="成片已过期"
          guidance="区间内有集重新生成过，当前成片的输入指纹已不是最新，重新执行任务后会替换。"
        />
      )}
      <SeriesProgressBoard
        episodes={data.episodes}
        currentEpisodeNo={data.current_episode_no}
        currentStage={data.current_stage}
        error={data.error}
        projectId={projectId}
      />
      {data.film && <SeriesFilmPlayer film={data.film} />}
    </>
  )
}
