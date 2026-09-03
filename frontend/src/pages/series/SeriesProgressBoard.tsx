import { useNav } from '../../App'
import OperationError from '../../components/OperationError'
import type { EpisodeEntry, EpisodeStage, Stage, StageState } from '../../api'

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

/** Stage 全量（含 merge）到中文名——任务列表的「当前在哪一步」与本文件的表头
 *  共用同一份映射，只是表头去掉 merge（合成不挂在某一集上）。 */
export const SERIES_STAGE_LABEL: Record<Stage, string> = {
  screenplay: '映射台',
  storyboard: '分镜台',
  confirm: '确认分镜',
  video: '生成台',
  final: '成片台',
  merge: '合成连播成片',
}

export const SERIES_STAGE_COLUMNS: { key: EpisodeStage; label: string }[] = (
  ['screenplay', 'storyboard', 'confirm', 'video', 'final'] as EpisodeStage[]
).map(key => ({ key, label: SERIES_STAGE_LABEL[key] }))

/** 停在哪一步——翻译成该去哪个工作台修；merge（合成连播成片）不属于任何单集
 *  工作台，返回 null（错误就地展示，不给跳转链接）。 */
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

/** 修复入口指向哪一集、哪个工作台：优先第一个带「失败」格子的集（单集失败会被跳过、
 *  任务继续跑后面的集，所以「当前集」不再等于「出错的集」）；没有失败格子时才退回
 *  当前集/当前步（merge 或运行中报错的场景）。 */
export function seriesRepairTarget(
  episodes: EpisodeEntry[],
  currentEpisodeNo: number | null,
  currentStage: Stage | null,
): { episode: EpisodeEntry; stage: EpisodeStage; view: NonNullable<ReturnType<typeof seriesRepairView>> } | null {
  for (const ep of episodes) {
    const failedStage = SERIES_STAGE_COLUMNS.find(col => ep.stages[col.key] === 'failed')?.key
    const view = seriesRepairView(failedStage)
    if (failedStage && view) return { episode: ep, stage: failedStage, view }
  }
  const current = episodes.find(ep => ep.episode_no === currentEpisodeNo)
  const view = seriesRepairView(currentStage)
  if (!current || !view || !currentStage || currentStage === 'merge') return null
  return { episode: current, stage: currentStage, view }
}

/** 任务详情页的进度树：接收扁平字段（episodes/currentEpisodeNo/currentStage/
 *  error），不再接旧契约的 SeriesRun 整体对象——任务级进度现在挂在
 *  series_tasks.progress_json 上，不再有独立的 run 实体。 */
export default function SeriesProgressBoard({
  episodes,
  currentEpisodeNo,
  currentStage,
  error,
  projectId,
}: {
  episodes: EpisodeEntry[]
  currentEpisodeNo: number | null
  currentStage: Stage | null
  error: string | null
  projectId: string
}) {
  const { go } = useNav()
  if (!episodes.length) return null

  const repair = seriesRepairTarget(episodes, currentEpisodeNo, currentStage)

  return (
    <section className="series-board card">
      <div className="series-board-scroll">
        <table className="series-board-table">
          <thead>
            <tr>
              <th>集</th>
              {SERIES_STAGE_COLUMNS.map(col => <th key={col.key}>{col.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {episodes.map(ep => {
              const isCurrentEpisode = ep.episode_no === currentEpisodeNo
              return (
                <tr key={ep.episode_id} className={isCurrentEpisode ? 'series-board-row-current' : ''}>
                  <td>第 {ep.episode_no} 集{ep.title ? ` · ${ep.title}` : ''}</td>
                  {SERIES_STAGE_COLUMNS.map(col => {
                    const state = ep.stages[col.key]
                    const isCurrentCell = isCurrentEpisode && currentStage === col.key
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
      </div>
      {error && (
        <OperationError title="连播任务遇到问题" guidance={error}>
          {repair && (
            <button
              type="button"
              className="btn"
              onClick={() => go(repair.view, projectId, repair.episode.episode_id)}
            >
              去第 {repair.episode.episode_no} 集的{SERIES_STAGE_LABEL[repair.stage]}修复
            </button>
          )}
        </OperationError>
      )}
    </section>
  )
}
