import { useRef, useState } from 'react'
import { api, type ShotVersion } from '../../api'

/**
 * 生成台「全部尝试」列表：点选哪个版本预览，哪个就成为采纳版本（2026-09-15 用户拍板：
 * 「我最后选择预览哪个就把那个作为采纳，合成的时候用采纳的片段」）。
 *
 * 采纳走既有的 POST /shots/{id}/adopt：只有 succeeded 且过技术门禁的版本能采纳；其它版本
 * 照常可以预览，但不改采纳关系，并把原因告诉用户。采纳成功后成片会被标为过期，需重新合成。
 */

export type AttemptAdoptability = { adoptable: boolean; reason: string }

/** status='stale' 时没有专属文案（如台词修订保留）就用这句兜底——不能不提示，
 *  否则用户看不出这一版为什么点不动。 */
const STALE_FALLBACK_REASON = '该版本依据的分镜内容已被修订，仅供对照，不可采纳'

function newIdemKey(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Math.random().toString(16).slice(2)
  return `${prefix}:${rand}`
}

/** 人工采纳是最高优先级（2026-09-15 用户拍板）：有可播放视频就能采纳，字幕闸门这类质量判定
 *  只作提示不拦人；没有视频文件的（失败/生成中）才不能采纳。stale（台词修订/身份复核保留的
 *  历史版本，2026-09-23 用户拍板）永远不可采纳——它依据的分镜内容已经作废。 */
export function attemptAdoptability(
  version: Pick<ShotVersion, 'id' | 'status' | 'video_url' | 'error'>,
  adoptedId: string | null | undefined,
): AttemptAdoptability {
  if (version.id === adoptedId) return { adoptable: false, reason: '已是采纳版本' }
  if (version.status === 'stale') return { adoptable: false, reason: version.error || STALE_FALLBACK_REASON }
  if (!version.video_url) return { adoptable: false, reason: '该版本没有可播放的视频，只能查看记录，不能采纳' }
  return { adoptable: true, reason: '' }
}

export default function AttemptList({
  shotId,
  versions,
  previewId,
  adoptedId,
  qualificationVersion,
  statusLabel,
  stampClass,
  onPreview,
  onToast,
  onRefresh,
  projectAspectRatio,
}: {
  shotId: string
  versions: ShotVersion[]
  previewId: string | null
  adoptedId: string | null | undefined
  qualificationVersion?: string
  statusLabel: (status: string) => string
  stampClass: (status: string) => string
  onPreview: (versionId: string) => void
  onToast: (message: string, isErr?: boolean) => void
  onRefresh: () => Promise<void>
  /** 项目当前画幅；拿不到就不显示「旧画幅」徽标（宁可不提示，也不编造一个画幅）。 */
  projectAspectRatio?: string
}) {
  const [adopting, setAdopting] = useState<string | null>(null)
  const adoptingRef = useRef(false)

  const select = async (version: ShotVersion) => {
    onPreview(version.id)
    const verdict = attemptAdoptability(version, adoptedId)
    if (!verdict.adoptable) {
      if (version.id !== adoptedId) onToast(`预览 v${version.version_no}：${verdict.reason}`)
      return
    }
    if (adoptingRef.current) return
    adoptingRef.current = true
    setAdopting(version.id)
    try {
      await api.shotAdoptVersion(
        shotId, version.id, `生成台预览时人工选定 v${version.version_no} 为采纳版本`,
        qualificationVersion, newIdemKey(`wall-adopt:${shotId}:${version.id}`),
      )
      onToast(`已采纳 v${version.version_no}，重新合成后生效`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    } finally {
      adoptingRef.current = false
      setAdopting(null)
    }
  }

  // 正常只有 1 个版本时不值得展示选择列表；但那 1 个如果是 stale（台词修订保留的
  // 历史版本），必须照样露出来——否则用户在生成台完全看不到它，也不知道为什么
  // 不能采纳（2026-09-23 用户拍板：生成台候选列表要能看到保留版本）。
  if (versions.length === 0) return null
  if (versions.length === 1 && versions[0].status !== 'stale') return null
  return (
    <div className="wall-attempt-list" aria-label="全部尝试">
      <b>全部尝试 · {versions.length}</b>
      <small>点选哪个版本预览，成片就采纳哪个</small>
      {versions.map(version => {
        const isAdopted = version.id === adoptedId
        const isStale = version.status === 'stale'
        // 「旧画幅」只提示不拦采纳：拿不到项目当前画幅（未接入/未加载）就不显示，
        // 宁可不提示也不编造一个画幅（同 CLAUDE.md「不得兜底填充」）。
        const versionAspectRatio = version.aspect_ratio || '9:16'
        const isOutdatedAspect = Boolean(projectAspectRatio) && versionAspectRatio !== projectAspectRatio
        return (
          <button type="button" key={version.id}
            className={`wall-attempt-card${version.id === previewId ? ' selected' : ''}${isAdopted ? ' adopted' : ''}`}
            aria-pressed={version.id === previewId}
            aria-label={`v${version.version_no}，${isStale ? '已过期，仅供对照，不可采纳' : statusLabel(version.status)}${isAdopted ? '，已采纳' : ''}${isOutdatedAspect ? `，旧画幅 ${versionAspectRatio}` : ''}`}
            disabled={adopting != null}
            onClick={() => void select(version)}>
            <span className="wall-attempt-card-top">
              <b>v{version.version_no}</b>
              <span className={stampClass(version.status)}>{isStale ? '已过期' : statusLabel(version.status)}</span>
              {isAdopted && <span className="stamp ok">已采纳</span>}
              {adopting === version.id && <span className="stamp">采纳中…</span>}
              {isOutdatedAspect && <span className="stamp grey" title="该版本生成时的画幅与项目当前画幅不同，仅提示，不影响采纳">旧画幅 {versionAspectRatio}</span>}
            </span>
            {isStale && <small>{version.error || STALE_FALLBACK_REASON}</small>}
          </button>
        )
      })}
    </div>
  )
}
