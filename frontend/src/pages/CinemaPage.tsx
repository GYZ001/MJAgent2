import { useEffect, useState } from 'react'
import { api, MixStatus, MixResult, numToCn } from '../api'
import { useEpisode, useNav } from '../App'

export default function CinemaPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep } = useEpisode(episodeId!, 5000)
  const [mix, setMix] = useState<MixStatus | null>(null)
  const [mixBusy, setMixBusy] = useState(false)

  const refreshMix = () => {
    if (!episodeId) return
    api.get(`/episodes/${episodeId}/mix-status`)
      .then((d: unknown) => setMix(d as MixStatus))
      .catch(e => toast(String(e.message || e), true))
  }

  useEffect(() => {
    refreshMix()
  }, [episodeId])

  if (!ep) return <div className="empty">展卷中……</div>

  return (
    <>
      <header className="desk-head">
        <div className="crumb">
          <a style={{ cursor: 'pointer' }} onClick={() => go('wall', projectId, episodeId)}>评审墙</a> / 第{numToCn(ep.episode_no)}集
        </div>
        <h1>成片台 <span className="sub">按镜号顺序拼接 · 预览 · 导出</span></h1>
        <hr className="rule" />
      </header>

      {mix ? (
        <>
          <section className="card">
            <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
              <span className={`stamp ${mix.ready ? 'green' : 'gold'}`}>
                {mix.ready ? '可合成' : '制作中'}
              </span>
              <span style={{ fontSize: 14, color: 'var(--ink-soft)' }}>
                {mix.shots_ready} / {mix.shots_total} 镜已有成片（{Math.floor((mix.shots_ready / (mix.shots_total || 1)) * 100)}%）
              </span>
              <span style={{ flex: 1 }} />
              <button className="btn" onClick={refreshMix}>刷新状态</button>
              <button
                className="btn primary"
                disabled={!mix.ready || mixBusy}
                onClick={async () => {
                  setMixBusy(true)
                  try {
                    const r = (await api.post(`/episodes/${ep.id}/concatenate`)) as MixResult
                    if (r.ffmpeg_missing) {
                      toast('服务端缺少 ffmpeg，已回退为首个片段的直链')
                    } else {
                      toast(`已合成 ${r.shots} 个片段，共约 ${r.total_duration_s}s`)
                    }
                    refreshMix()
                  } catch (e) {
                    toast((e as Error).message, true)
                  } finally {
                    setMixBusy(false)
                  }
                }}
              >合成成品</button>
              {mix.final_video_url && (
                <a className="btn" href={mix.final_video_url} target="_blank" rel="noreferrer" style={{ textDecoration: 'none' }}>
                  下载成品
                </a>
              )}
            </div>
          </section>
          {mix.final_video_url && (
            <section className="card">
              <h3>成品预览 <span className="hint">《{ep.title}》</span></h3>
              <video src={mix.final_video_url} controls playsInline style={{ width: '100%', maxHeight: 520, background: '#1d1a16', borderRadius: 8 }} />
            </section>
          )}
        </>
      ) : <div className="empty">加载成片台…</div>}
    </>
  )
}
