export type ProductionStatusTone = 'grey' | 'gold' | 'green' | 'red' | 'blue'

export interface ProductionStatusMeta {
  label: string
  tone: ProductionStatusTone
  known: boolean
}

const SCREENPLAY_STATUS: Record<string, Omit<ProductionStatusMeta, 'known'>> = {
  pending: { label: '剧本待生成', tone: 'grey' },
  running: { label: '剧本生成中', tone: 'gold' },
  repairing: { label: '剧本修复中', tone: 'gold' },
  ready: { label: '剧本已就绪', tone: 'green' },
  warning: { label: '剧本待修复', tone: 'red' },
  failed: { label: '剧本生成失败', tone: 'red' },
}

const EPISODE_STATUS: Record<string, Omit<ProductionStatusMeta, 'known'>> = {
  planned: { label: '分镜待生成', tone: 'grey' },
  drafting: { label: '剧本准备中', tone: 'gold' },
  scripting: { label: '分镜生成中', tone: 'gold' },
  storyboarding: { label: '分镜生成中', tone: 'gold' },
  scripted: { label: '分镜待确认', tone: 'blue' },
  script_failed: { label: '分镜生成失败', tone: 'red' },
  confirmed: { label: '分镜已确认', tone: 'green' },
  generating: { label: '视频生成中', tone: 'gold' },
  paused_budget: { label: '视频因预算暂停', tone: 'red' },
  mixed: { label: '视频处理中', tone: 'gold' },
  done: { label: '本集已成片', tone: 'green' },
}

function productionStatusMeta(
  status: string | null | undefined,
  map: Record<string, Omit<ProductionStatusMeta, 'known'>>,
  fallback: string,
): ProductionStatusMeta {
  const match = status ? map[status] : undefined
  return match
    ? { ...match, known: true }
    : { label: fallback, tone: 'grey', known: false }
}

export function screenplayStatusMeta(status: string | null | undefined): ProductionStatusMeta {
  return productionStatusMeta(status, SCREENPLAY_STATUS, '剧本状态待确认')
}

export function episodeStatusMeta(status: string | null | undefined): ProductionStatusMeta {
  return productionStatusMeta(status, EPISODE_STATUS, '制作状态待确认')
}

function StatusStamp({
  status,
  meta,
}: {
  status: string | null | undefined
  meta: ProductionStatusMeta
}) {
  return (
    <span
      className={`stamp ${meta.tone}`}
      title={meta.known ? undefined : `系统返回了未识别状态：${status || '空值'}，请刷新后再试`}
    >
      {meta.label}
    </span>
  )
}

export function ScreenplayStatusStamp({ status }: { status: string | null | undefined }) {
  return <StatusStamp status={status} meta={screenplayStatusMeta(status)} />
}

export function EpisodeStatusStamp({ status }: { status: string | null | undefined }) {
  return <StatusStamp status={status} meta={episodeStatusMeta(status)} />
}
