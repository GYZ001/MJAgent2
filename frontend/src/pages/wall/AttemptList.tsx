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

function newIdemKey(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Math.random().toString(16).slice(2)
  return `${prefix}:${rand}`
}

/** 人工采纳是最高优先级（2026-09-15 用户拍板）：有可播放视频就能采纳，字幕闸门这类质量判定
 *  只作提示不拦人；没有视频文件的（失败/生成中）才不能采纳。 */
export function attemptAdoptability(
  version: Pick<ShotVersion, 'id' | 'status' | 'video_url'>,
  adoptedId: string | null | undefined,
): AttemptAdoptability {
  if (version.id === adoptedId) return { adoptable: false, reason: '已是采纳版本' }
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

  if (versions.length <= 1) return null
  return (
    <div className="wall-attempt-list" aria-label="全部尝试">
      <b>全部尝试 · {versions.length}</b>
      <small>点选哪个版本预览，成片就采纳哪个</small>
      {versions.map(version => {
        const isAdopted = version.id === adoptedId
        return (
          <button type="button" key={version.id}
            className={`wall-attempt-card${version.id === previewId ? ' selected' : ''}${isAdopted ? ' adopted' : ''}`}
            aria-pressed={version.id === previewId}
            aria-label={`v${version.version_no}，${statusLabel(version.status)}${isAdopted ? '，已采纳' : ''}`}
            disabled={adopting != null}
            onClick={() => void select(version)}>
            <span className="wall-attempt-card-top">
              <b>v{version.version_no}</b>
              <span className={stampClass(version.status)}>{statusLabel(version.status)}</span>
              {isAdopted && <span className="stamp ok">已采纳</span>}
              {adopting === version.id && <span className="stamp">采纳中…</span>}
            </span>
          </button>
        )
      })}
    </div>
  )
}
