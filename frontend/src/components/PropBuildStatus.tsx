import { useState } from 'react'
import { retryPropBuild, type PropBuildRecord } from '../api/bible/props'

export default function PropBuildStatus({ projectId, builds, onRefresh, toast }: {
  projectId: string
  builds: PropBuildRecord[]
  onRefresh: () => Promise<void>
  toast: (message: string) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  if (!builds.length) return null
  const retry = async (build: PropBuildRecord) => {
    if (busy || !build.episodes.length) return
    setBusy(build.label)
    try {
      const result = await retryPropBuild(projectId, build.episodes[0].id, build.label)
      toast(result.accepted ? `已受理「${build.label}」补建，完成后可重新生成视频` : '该道具已有补建任务，请刷新查看')
      await onRefresh()
    } catch (error) {
      toast(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(null)
    }
  }
  return (
    <section className="card" aria-label="道具补建状态">
      <h3>待完成的道具补建（{builds.length}）</h3>
      <p className="hint">以下道具尚未完成建卡或出图。补建失败时，请先修复原因，再点击「重试补建」。</p>
      <button type="button" className="btn small" onClick={() => void onRefresh()}>刷新补建状态</button>
      {builds.map(build => (
        <article key={build.label} className="scene-card-summary">
          <b>{build.label}</b>
          <span className={`stamp ${build.status === 'failed' ? 'red' : 'gold'}`}>
            {build.status === 'failed' ? '补建失败' : '补建中'}
          </span>
          <p className="hint">涉及第 {build.episodes.map(episode => episode.episode_no).join('、')} 集</p>
          {build.error && <p role="alert">{build.error}</p>}
          {build.status === 'failed' && (
            <button type="button" className="btn small" disabled={busy !== null} onClick={() => void retry(build)}>
              {busy === build.label ? '受理中…' : '重试补建'}
            </button>
          )}
        </article>
      ))}
    </section>
  )
}
