import { useNav } from '../../App'
import OperationError from '../../components/OperationError'
import type { EpisodeStage, SeriesRun, Stage, StageState } from '../../api'

type SeriesStageTone = 'grey' | 'gold' | 'green' | 'red'

const STAGE_STATE_META: Record<StageState, { label: string; tone: SeriesStageTone }> = {
  pending: { label: '待办', tone: 'grey' },
  running: { label: '进行中', tone: 'gold' },
  done: { label: '完成', tone: 'green' },
  skipped: { label: '✓ 已有产物', tone: 'grey' },
  failed: { label: '失败', tone: 'red' },
}

export function seriesStageMeta(state: StageState | undefined) {
  return STAGE_STATE_META[state ?? 'pending'] ?? STAGE_STATE_META.pending
}

export function seriesStageStampClass(state: StageState | undefined): string {
  return `stamp ${seriesStageMeta(state).tone}`
}

export const SERIES_STAGE_COLUMNS: { key: EpisodeStage; label: string }[] = [
  { key: 'screenplay', label: '映射台' },
  { key: 'storyboard', label: '分镜台' },
  { key: 'confirm', label: '确认分镜' },
  { key: 'video', label: '生成台' },
  { key: 'final', label: '成片台' },
]

/** run.error 停在哪一步——翻译成该去哪个工作台修；merge（合成连播成片）不属于
 *  任何单集工作台，返回 null（错误就地展示，不给跳转链接）。 */
export function seriesRepairView(
  stage: Stage | null | undefined,
): 'script' | 'board' | 'wall' | 'cinema' | null {
  switch (stage) {
    case 'screenplay': return 'script'
    case 'storyboard': return 'board'
    case 'confirm': return 'board'
    case 'video': return 'wall'
    case 'final': return 'cinema'
    default: return null
  }
}

export default function SeriesProgressBoard({
  run,
  projectId,
}: {
  run: SeriesRun | null
  projectId: string
}) {
  const { go } = useNav()
  if (!run) return null

  const errorEpisode = run.episodes.find(ep => ep.episode_no === run.current_episode_no)
  const repairView = seriesRepairView(run.current_stage)

  return (
    <section className="series-board card">
      <table className="series-board-table">
        <thead>
          <tr>
            <th>集</th>
            {SERIES_STAGE_COLUMNS.map(col => <th key={col.key}>{col.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {run.episodes.map(ep => {
            const isCurrentEpisode = ep.episode_no === run.current_episode_no
            return (
              <tr key={ep.episode_id} className={isCurrentEpisode ? 'series-board-row-current' : ''}>
                <td>第 {ep.episode_no} 集</td>
                {SERIES_STAGE_COLUMNS.map(col => {
                  const state = ep.stages[col.key]
                  const isCurrentCell = isCurrentEpisode && run.current_stage === col.key
                  return (
                    <td key={col.key} className={isCurrentCell ? 'series-board-cell-current' : ''}>
                      <span className={seriesStageStampClass(state)}>{seriesStageMeta(state).label}</span>
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
      {run.error && (
        <OperationError title="连播制作遇到问题" guidance={run.error}>
          {repairView && errorEpisode && (
            <button
              type="button"
              className="btn"
              onClick={() => go(repairView, projectId, errorEpisode.episode_id)}
            >
              去对应工作台修复
            </button>
          )}
        </OperationError>
      )}
    </section>
  )
}
