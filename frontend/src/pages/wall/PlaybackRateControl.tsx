import { useEffect, useState } from 'react'
import { api, type ShotVersion } from '../../api'

/** 生成台调速控件（09-23 方案 P0-4）：只对「已采纳版本」显示（由父组件先判好、只在
 *  存在采纳版本时传入 version，本组件不重复 adopted_version_id 逻辑）。倍速真正生效
 *  在成片合成阶段（app/final_edit.py、app/media_exec/concat.py 读 shot_versions.
 *  playback_rate），生成台预览播放器不需要跟着变速。走既有 POST /shots/{id}/adopt：
 *  对已是采纳版本的 version_id 重新调用只改倍速是后端允许的合法用法
 *  （app/domain/video_ops/adopt.py::_assert_version_adoptable 不检查 version_id 是否
 *  已是当前采纳版本），全程不预留视频时长额度。 */

const RATE_OPTIONS = [0.5, 0.75, 1, 1.25, 1.5, 2]

function newIdemKey(prefix: string): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : Math.random().toString(16).slice(2)
  return `${prefix}:${rand}`
}

export default function PlaybackRateControl({
  shotId,
  version,
  qualificationVersion,
  onToast,
  onRefresh,
}: {
  shotId: string
  /** 传入即渲染；调用方只在这是已采纳版本时才传，传 null/undefined 表示不展示。 */
  version: Pick<ShotVersion, 'id' | 'version_no' | 'playback_rate'> | null | undefined
  qualificationVersion?: string
  onToast: (message: string, isErr?: boolean) => void
  onRefresh: () => Promise<void>
}) {
  const [rate, setRate] = useState(version?.playback_rate ?? 1)
  const [applying, setApplying] = useState(false)

  // 采纳关系变化（比如切换采纳版本）或倍速在别处被改过后，跟着同步一次初值。
  useEffect(() => {
    setRate(version?.playback_rate ?? 1)
  }, [version?.id, version?.playback_rate])

  if (!version) return null

  const apply = async () => {
    if (applying) return
    setApplying(true)
    try {
      await api.shotAdoptVersion(
        shotId, version.id, `生成台调整播放倍速为 ${rate}x`,
        qualificationVersion, newIdemKey(`wall-playback-rate:${shotId}:${version.id}`), rate,
      )
      onToast(`已应用 ${rate}x 倍速，重新合成后生效`)
      await onRefresh()
    } catch (error) {
      onToast(error instanceof Error ? error.message : String(error), true)
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="wall-playback-rate" aria-label="调速">
      <label htmlFor={`playback-rate-${shotId}`}>播放倍速</label>
      <select id={`playback-rate-${shotId}`} value={rate} disabled={applying}
        onChange={event => setRate(Number(event.target.value))}>
        {RATE_OPTIONS.map(option => <option key={option} value={option}>{option}x</option>)}
      </select>
      <button type="button" className="btn wall-playback-rate-apply" disabled={applying}
        onClick={() => apply()}>
        {applying ? '应用中…' : '应用'}
      </button>
      <small>不重新生成、不消耗视频额度，只影响成片合成时的播放速度。</small>
    </div>
  )
}
