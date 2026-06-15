import { useCallback, useEffect, useRef, useState } from 'react'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { useEpisode, useNav } from '../App'
import { api, type Shot, type ShotVersion, type ReferenceImage } from '../api'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

/* ─── 常量 ─── */
/* ─── Lightbox 图片预览 ─── */
function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className="lightbox-overlay" onClick={onClose}>
      <div className="lightbox-content" onClick={e => e.stopPropagation()}>
        <button className="lightbox-close" onClick={onClose}>✕</button>
        <img src={src} alt={alt} />
        {alt && <div className="lightbox-caption">{alt}</div>}
      </div>
    </div>
  )
}

/* ─── 取本镜当前版本（采用版优先）的参考图集合 ─── */
function currentVersionRefs(shot: Shot): { versionId: string; refs: ReferenceImage[] } | null {
  const v = shot.versions.find(x => x.id === shot.adopted_version_id) || shot.versions[0]
  if (!v) return null
  return { versionId: v.id, refs: v.image_inputs?.reference_images ?? [] }
}

function refSourceLabel(source?: string): string {
  return ({
    seedream_generated: '生成参考图', asset_library: '角色定妆照',
    previous_shot: '上镜衔接帧', previous_shot_frame: '上镜衔接帧',
  } as Record<string, string>)[source ?? ''] ?? (source || '参考图')
}

function rejectReasonLabel(reason?: string | null): string {
  if (!reason) return '质检未通过'
  return ({
    quality_below_threshold: '质检分低于阈值',
    missing_quality_score: '缺少质检分',
    quality_issue_blocks_reuse: '质检问题不可复用',
  } as Record<string, string>)[reason] ?? reason
}

function refScore(r: ReferenceImage): number | null {
  const s = r.qualityScore ?? r.qa?.overall
  return typeof s === 'number' ? s : null
}

/* ─── 单张参考图卡片：图 + QA 打分 + 来源 + 操作 ─── */
function RefCard({ r, onOpen, onAction, actionLabel, discarded }: {
  r: ReferenceImage; onOpen: (src: string, label?: string) => void
  onAction?: () => void; actionLabel: string; discarded?: boolean
}) {
  const score = refScore(r)
  const src = r.image_url || undefined
  const label = refSourceLabel(r.source)
  const issue = (r.qa?.issues ?? []).filter(Boolean)[0]
  return (
    <figure className={`material-card${discarded ? ' material-card-discarded' : ''}`} title={label}>
      <div className="mc-thumb" onClick={() => src && onOpen(src, label)}>
        {src ? <img src={src} alt={label} loading="lazy" /> : <div className="mc-noimg">无图</div>}
        {score != null && (
          <span className={`mc-qa-badge${score < 0.75 ? ' bad' : ''}`}>QA {score.toFixed(2)}</span>
        )}
      </div>
      <figcaption>
        <span className="mc-label">{label}</span>
        {discarded
          ? <span className="mc-reject">{rejectReasonLabel(r.rejectReason)}</span>
          : (r.rejectReason
            ? <span className="mc-reject warn">兜底·{rejectReasonLabel(r.rejectReason)}</span>
            : issue ? <span className="mc-reject warn">{issue}</span> : null)}
        {onAction && (
          <button className={`mc-action ${discarded ? 'restore' : 'discard'}`} onClick={onAction}>
            {actionLabel}
          </button>
        )}
      </figcaption>
    </figure>
  )
}

/* ─── 单镜素材画廊：使用中（可废弃） + 废弃照片画廊（可恢复） ─── */
function ShotMaterialGallery({ shot, onOpen, onRefresh, onToast }: {
  shot: Shot; onOpen: (src: string, label?: string) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const data = currentVersionRefs(shot)
  const refs = data?.refs ?? []
  const versionId = data?.versionId
  const used = refs.filter(r => r.selectedForSeedance && !r.deleted)
  const discarded = refs.filter(r => !(r.selectedForSeedance && !r.deleted))

  const act = (fn: () => Promise<unknown>) => async () => {
    try { await fn(); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }

  return (
    <>
      {used.length ? (
        <div className="material-strip">
          {used.map(r => (
            <RefCard key={r.id} r={r} onOpen={onOpen} actionLabel="废弃"
              onAction={versionId ? act(() => api.discardReferenceImage(versionId, r.id)) : undefined} />
          ))}
        </div>
      ) : (
        <div className="material-strip-empty" aria-label="本镜暂无参考图">
          <span className="material-empty-frame" />
          <span className="material-empty-frame" />
          <span className="material-empty-frame" />
        </div>
      )}

      {discarded.length > 0 && (
        <div className="discard-gallery">
          <div className="discard-gallery-head">
            废弃照片画廊 · {discarded.length} 张
            <span className="discard-gallery-hint">质检未通过 / 已废弃，不会喂给视频模型</span>
          </div>
          <div className="material-strip">
            {discarded.map(r => (
              <RefCard key={r.id} r={r} onOpen={onOpen} discarded actionLabel="恢复使用"
                onAction={versionId ? act(() => api.restoreReferenceImage(versionId, r.id)) : undefined} />
            ))}
          </div>
        </div>
      )}
    </>
  )
}

/* ═══════════════════════════════════════════════════════════════
   WallPage
   ═══════════════════════════════════════════════════════════════ */
export default function WallPage() {
  const { episodeId } = useNav()
  const { data: ep, refresh, error } = useEpisode(episodeId || '', 5000)
  const shots = ep?.shots ?? []
  const [idx, setIdx] = useState(0)
  const [toast, setToast] = useState<string | null>(null)
  const [genMask, setGenMask] = useState<Set<string>>(new Set())
  const [lightbox, setLightbox] = useState<{ src: string; label?: string } | null>(null)
  const [showGallery, setShowGallery] = useState(true)
  const [clearMenuOpen, setClearMenuOpen] = useState(false)
  const carouselRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (shots.length && idx >= shots.length) setIdx(shots.length - 1)
  }, [shots.length, idx])

  useEffect(() => {
    if (!carouselRef.current || !shots.length) return
    const el = carouselRef.current.children[idx] as HTMLElement | undefined
    el?.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' })
  }, [idx, shots.length])

  const videoActive = shots.some(s => s.versions.some(v => v.status === 'queued' || v.status === 'running'))
  const videoTimer = useTaskTimer(`episode.${episodeId}.videos`, videoActive)

  const openLightbox = useCallback((src: string, label?: string) => {
    setLightbox({ src, label })
  }, [])

  if (!ep) return <div className="empty">展卷中……</div>
  if (error) return <div className="empty">{error}</div>

  const shot = shots[idx]
  const videoReady = shots.filter(s => s.versions.some(v => v.status === 'succeeded')).length

  const t = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); setToast(`${msg} 成功`); refresh() }
    catch (e: unknown) { setToast(e instanceof Error ? e.message : String(e)) }
    setTimeout(() => setToast(null), 3200)
  }

  const canGenerate = ep.status === 'confirmed' || ep.status === 'generating' || ep.status === 'done'

  const doGenerateEpisode = async () => {
    if (!canGenerate) { setToast('请先在分镜台确认本集分镜'); return }
    if (!confirm(`即将生成全片 ${shots.length} 个镜头的视频，是否继续？`)) return
    videoTimer.start()
    await t(() => api.episodeGenerate(ep.id), '全片生成已启动')
  }

  const doClearEpisode = async () => {
    setClearMenuOpen(false)
    if (!confirm(`确认清空第 ${ep.episode_no} 集全部 ${shots.length} 镜的参考图、关键帧、视频与模型分析结果？\n（操作不可恢复）`)) return
    await t(() => api.clearEpisodeArtifacts(ep.id), '本集已清空')
  }

  const doClearShot = async () => {
    setClearMenuOpen(false)
    if (!shot) return
    if (!confirm(`确认清空第 ${shot.shot_no} 镜的参考图、关键帧、视频与模型分析结果？\n（操作不可恢复）`)) return
    await t(() => api.clearShotArtifacts(shot.id), `镜 ${shot.shot_no} 已清空`)
  }

  const doGenerateVideo = async (
    shotId: string,
    opts?: { promptOverride?: string; reroll?: boolean; withCritique?: boolean; actionLabel?: string },
  ) => {
    setGenMask(m => new Set(m).add(shotId)); videoTimer.start()
    try {
      const actionLabel = opts?.actionLabel || '视频生成'
      setToast(`${actionLabel}已提交，正在处理…`)
      const r = await api.shotGenerate(shotId, opts?.promptOverride, opts?.reroll, opts?.withCritique) as { reused?: boolean }
      setToast(r.reused
        ? `${actionLabel}未新建任务：当前内容未变化，已复用已有版本；如需强制重出请点「原词重抽」`
        : `${actionLabel}已开始，正在生成新版本`)
      refresh()
    } catch (e: unknown) { setToast(e instanceof Error ? e.message : String(e)) }
    finally { setGenMask(m => { const n = new Set(m); n.delete(shotId); return n }) }
  }

  return (
    <main className="desk wall-page">
      {/* ── 顶栏 ── */}
      <div className="wall-topbar">
        <div className="wall-topbar-left">
          <EpisodeCrumb label="评审墙" view="wall" episodeNo={ep.episode_no} />
          <span className="wall-stats">
            {shots.length} 镜 · 视频 {videoReady}/{shots.length}
          </span>
        </div>
        <div className="wall-topbar-right">
          <TaskTimer label="视频" timer={videoTimer} />
          <span className={`stamp ${ep.status === 'confirmed' || ep.status === 'done' ? 'green' : ep.status === 'generating' ? 'gold' : 'grey'}`}>
            {{ confirmed: '已确认', generating: '生成中', done: '已完成', paused_budget: '预算暂停' }[ep.status] ?? ep.status}
          </span>
          {canGenerate && (
            <button className="btn primary small" onClick={doGenerateEpisode}>一键生成所有视频</button>
          )}
          <div className="clear-menu-wrap">
            <button className="btn ghost small danger" onClick={() => setClearMenuOpen(o => !o)}>清空 ▾</button>
            {clearMenuOpen && (
              <>
                <div className="clear-menu-backdrop" onClick={() => setClearMenuOpen(false)} />
                <div className="clear-menu">
          <div className="clear-menu-hint">清空参考图 / 视频 / 模型分析</div>
                  <button className="clear-menu-item" onClick={doClearShot} disabled={!shot}>
                    清空本镜{shot ? `（镜 ${shot.shot_no}）` : ''}
                  </button>
                  <button className="clear-menu-item" onClick={doClearEpisode}>
                    清空本集（{shots.length} 镜）
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* ── 轮播主体 ── */}
      <div className="shot-carousel" ref={carouselRef}>
        {shot && (
          <div className="shot-slide" key={shot.id}>
            <ShotSlide
              shot={shot}
              episodeStatus={ep.status}
              generating={genMask.has(shot.id) || shot.versions.some(v => v.status === 'queued' || v.status === 'running')}
              onGenVideo={(opts) => doGenerateVideo(shot.id, opts)}
              onRefresh={refresh}
              onToast={setToast}
            />
          </div>
        )}
      </div>

      {/* ── 镜头分页 ── */}
      {shots.length > 0 && (
        <div className="shot-pager">
          <button className="btn ghost small" disabled={idx === 0} onClick={() => setIdx(i => i - 1)}>← 上一镜</button>
          <span className="pg-no">第 {idx + 1} / {shots.length} 镜</span>
          <button className="btn ghost small" disabled={idx >= shots.length - 1} onClick={() => setIdx(i => i + 1)}>下一镜 →</button>
        </div>
      )}

      {/* ── 当前镜素材画廊（仅展示本镜，关键帧管理 + 横向滑动） ── */}
      {shot && (() => {
        return (
          <div className="wall-global-gallery">
            <button className="gallery-toggle" onClick={() => setShowGallery(g => !g)}>
              {showGallery ? '▾' : '▸'} 镜 {shot.shot_no} · 素材画廊
            </button>
            {showGallery && (
              <ShotMaterialGallery shot={shot} onOpen={openLightbox} onRefresh={refresh} onToast={setToast} />
            )}
          </div>
        )
      })()}

      {toast && <div className="toast">{toast}</div>}
      {lightbox && <Lightbox src={lightbox.src} alt={lightbox.label || ''} onClose={() => setLightbox(null)} />}
    </main>
  )
}

/* ═══════════════════════════════════════════════════════════════
   ShotSlide — 单镜卡片
   ═══════════════════════════════════════════════════════════════ */
function ShotSlide({ shot, episodeStatus, generating,
  onGenVideo, onRefresh, onToast }: {
  shot: Shot; episodeStatus: string; generating: boolean
  onGenVideo: (opts?: { promptOverride?: string; reroll?: boolean; withCritique?: boolean; actionLabel?: string }) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const adopted = shot.versions.find(v => v.id === shot.adopted_version_id)
  const latest = shot.versions[0]
  const current = adopted || latest
  const hasAnyVersion = shot.versions.length > 0

  return (
    <div className="slide-card">
      {/* 头部 */}
      <div className="slide-head">
        <span className="sn">镜 {shot.shot_no}</span>
        <span className="meta">{shot.shot_size} · {shot.camera_move} · {shot.duration_s}s · {shot.transition}</span>
        <span className="meta">{shot.scene_setting}</span>
        {shot.continuity_from_prev ? <span className="stamp blue">接上镜</span> : <span className="stamp grey">新场景</span>}
        {adopted ? <span className="stamp green">已采用</span> : hasAnyVersion && <span className="stamp grey">待采用</span>}
        {shot.video_stale && <span className="stamp red">视频需重生</span>}
      </div>

      <div className="slide-body">
        {/* 左栏：剧本/台词/参考图 + 操作按钮 */}
        <div className="slide-left">
          <InfoSection shot={shot} />
          <VideoControls
            shot={shot} episodeStatus={episodeStatus} current={current}
            generating={generating}
            onGenVideo={onGenVideo}
            onRefresh={onRefresh} onToast={onToast}
          />
        </div>

        {/* 右栏：仅视频播放 */}
        <div className="slide-right">
          <VideoPlayer current={current} />
        </div>
      </div>
    </div>
  )
}

/* ─── 分镜信息区块 ─── */
function ScriptMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="script-meta-item">
      <span className="script-meta-label">{label}</span>
      <span className="script-meta-value">{value}</span>
    </div>
  )
}

function InfoSection({ shot }: { shot: Shot }) {
  const dialogueText = shot.dialogues
    .map(d => `${d.speaker}：${d.line}${d.emotion && d.emotion !== '平静' ? `（${d.emotion}）` : ''}`)
    .join('\n')

  return (
    <div className="info-section">
      {!!shot.source_excerpt && (
        <section className="script-card">
          <div className="script-card-head">原文摘录</div>
          <div className="script-source">{shot.source_excerpt}</div>
        </section>
      )}

      <section className="script-card">
        <div className="script-card-head">镜头信息</div>
        <div className="script-meta-grid">
          <ScriptMeta label="场景" value={shot.scene_setting} />
          <ScriptMeta label="角色" value={shot.characters.join('、') || '无'} />
          <ScriptMeta label="时长" value={`${shot.duration_s}s`} />
          <ScriptMeta label="镜头" value={`${shot.shot_size} / ${shot.camera_move}`} />
          <ScriptMeta label="转场" value={shot.transition} />
          <ScriptMeta label="衔接" value={shot.continuity_from_prev ? '接上镜' : '新场景'} />
        </div>
      </section>

      <section className="script-card">
        <div className="script-card-head">镜头脚本</div>
        <div className="script-block">
          <div className="script-paragraph">
            <span className="script-label">画面</span>
            <p>{shot.action_desc}</p>
          </div>
          {!!shot.narration && (
            <div className="script-paragraph">
              <span className="script-label">旁白</span>
              <p>{shot.narration}</p>
            </div>
          )}
          {!!shot.dialogues.length && (
            <div className="script-paragraph">
              <span className="script-label">台词</span>
              <pre className="script-dialogues">{dialogueText}</pre>
            </div>
          )}
        </div>
      </section>
      {shot.dialogues.length > 0 && (
        <section className="script-card">
          <div className="script-card-head">台词明细</div>
          <div className="dlg-list">
            {shot.dialogues.map((d, i) => (
              <div key={i} className="dlg-line">
                <span className="dlg-speaker">{d.speaker}</span>
                <span className="dlg-text">{d.line}</span>
                {d.emotion && d.emotion !== '平静' && <span className="dlg-emotion">{d.emotion}</span>}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   VideoPlayer — 视频播放（右栏，仅播放器）
   ═══════════════════════════════════════════════════════════════ */
function VideoPlayer({ current }: { current?: ShotVersion }) {
  return (
    <div className="video-player-area">
      {current?.video_url ? (
        <video src={current.video_url} controls className="rev-video" />
      ) : (
        <div className="vp-empty">
          <span>{current?.status === 'queued' || current?.status === 'running' ? '⏳ 生成中…' : '暂无视频'}</span>
          {current?.error && <span className="err-text">{current.error}</span>}
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   VideoControls — 操作按钮 + 版本历史（左栏）
   ═══════════════════════════════════════════════════════════════ */
function VideoControls({ shot, episodeStatus, current, generating,
  onGenVideo, onRefresh, onToast }: {
  shot: Shot; episodeStatus: string; current?: ShotVersion
  generating: boolean
  onGenVideo: (opts?: { promptOverride?: string; reroll?: boolean; withCritique?: boolean; actionLabel?: string }) => void
  onRefresh: () => void; onToast: (m: string) => void
}) {
  const hasAdopted = !!shot.adopted_version_id
  const disabled = generating

  const doAdopt = async (vid: string) => {
    try { await api.adoptVersion(shot.id, vid); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doDelete = async (vid: string) => {
    if (!confirm('删除此版本？')) return
    try { await api.deleteVersion(vid); onRefresh() }
    catch (e: unknown) { onToast(e instanceof Error ? e.message : String(e)) }
  }
  const doWithCritique = () => onGenVideo({ withCritique: true, actionLabel: '带评语重生' })
  const doRewrite = () => {
    const initial = (current?.prompt_text || '').trim()
    const next = window.prompt('请输入新的生成词。留空则取消。', initial)
    if (next == null) return
    const promptOverride = next.trim()
    if (!promptOverride) {
      onToast('已取消改词重生')
      return
    }
    if (promptOverride === initial) {
      onToast('生成词未修改；如需强制重出，请点「原词重抽」')
      return
    }
    onGenVideo({ promptOverride, actionLabel: '改词重生' })
  }

  return (
    <div className="video-section">
      {/* 控制面板 */}
      <div className="video-controls">
        {/* 操作按钮 */}
        <div className="action-row">
          {(episodeStatus === 'confirmed' || episodeStatus === 'generating' || episodeStatus === 'done') && (
            <button className="btn primary small" disabled={disabled}
              onClick={() => onGenVideo({ actionLabel: hasAdopted ? '重生成视频' : '生成本镜视频' })}>
              {generating ? '生成中…' : hasAdopted ? '重生成视频' : '生成本镜视频'}
            </button>
          )}
          {current?.video_url && !hasAdopted && (
            <button className="btn small" disabled={disabled} onClick={() => doAdopt(current.id)}>采用此版</button>
          )}
          {current?.video_url && (
            <a className={`btn small ghost${disabled ? ' is-disabled' : ''}`} aria-disabled={disabled}
              onClick={e => { if (disabled) e.preventDefault() }}
              href={current.video_url} download={`${shot.shot_no}.mp4`}>导出</a>
          )}
          {hasAdopted && <button className="btn small" disabled={disabled} onClick={doRewrite}>改词重生</button>}
          {hasAdopted && current?.qa && <button className="btn small" disabled={disabled} onClick={doWithCritique}>带评语重生</button>}
          {shot.versions.length > 1 && (
            <button className="btn small ghost" disabled={disabled}
              onClick={() => onGenVideo({ reroll: true, actionLabel: '原词重抽' })}>原词重抽</button>
          )}
          {current && !hasAdopted && (
            <button className="btn small ghost" disabled={disabled} onClick={() => doDelete(current.id)}>删除此版</button>
          )}
        </div>
      </div>
    </div>
  )
}
