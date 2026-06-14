import { useRef, useState } from 'react'
import { api, Shot, SceneCandidate, numToCn } from '../api'
import { useEpisode, useNav } from '../App'

export default function WallPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep, refresh } = useEpisode(episodeId!, 5000)
  const [busy, setBusy] = useState(false)
  const [active, setActive] = useState(0)
  const carRef = useRef<HTMLDivElement>(null)

  if (!ep) return <div className="empty">展卷中……</div>

  const overLimit = ep.cost_limit_cny !== undefined && ep.cost_cny >= ep.cost_limit_cny
  const shots = ep.shots ?? []
  const sceneApproved = shots.filter(s => s.scene_status === 'approved').length
  const videoReady = shots.filter(s => s.versions.some(v => v.status === 'succeeded')).length

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    setBusy(true)
    try { await fn(); if (msg) toast(msg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const goTo = (i: number) => {
    const idx = Math.max(0, Math.min(shots.length - 1, i))
    setActive(idx)
    const el = carRef.current
    if (el) el.scrollTo({ left: idx * el.clientWidth, behavior: 'smooth' })
  }
  const onScroll = () => {
    const el = carRef.current
    if (el) setActive(Math.round(el.scrollLeft / el.clientWidth))
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">
          <a style={{ cursor: 'pointer' }} onClick={() => go('board', projectId, episodeId)}>分镜台</a> / 第{numToCn(ep.episode_no)}集
        </div>
        <h1>评审墙 <span className="sub">一屏一镜 · 左右滚动切换 · 先审首尾关键帧 → 通过后审视频</span></h1>
        <hr className="rule" />
      </header>

      <section className="card" style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
        <div className="cost-ink">¥{ep.cost_cny.toFixed(1)} <small>/ 上限 ¥{ep.cost_limit_cny}</small></div>
        {overLimit && (
          <>
            <span className="stamp red">预算熔断</span>
            <button className="btn small" disabled={busy} onClick={() => act(async () => {
              const r = await api.post(`/episodes/${ep.id}/resume`); toast(`已恢复 ${r.resumed_jobs} 个任务（请先在监制房调高上限）`)
            })}>调高上限后恢复队列</button>
          </>
        )}
        <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
          关键帧已过审 {sceneApproved}/{shots.length} · 已有成片 {videoReady}/{shots.length}
        </span>
        <span style={{ flex: 1 }} />
        <button className="btn small" disabled={busy} onClick={() => act(async () => {
          const r = await api.post(`/episodes/${ep.id}/scenes-all`) as { started: number }
          toast(`已为 ${r.started} 个镜头生成首/尾关键帧（自动评审，约数分钟）`)
        })}>生成全部关键帧</button>
        <button className="btn small primary" disabled={busy || sceneApproved === 0} onClick={() => act(async () => {
          const r = await api.post(`/episodes/${ep.id}/generate`) as { enqueued: { error?: string }[] }
          const ok = r.enqueued.filter(x => !x.error).length
          toast(`已入队 ${ok} 镜：连续镜首帧接上一镜尾图，全部可按并发生成`)
        })}>生成全部视频</button>
        <button className="btn small" onClick={() => go('cinema', projectId, episodeId)}>入成片台 →</button>
      </section>

      {/* 镜头分页导航 */}
      <div className="shot-pager">
        <button className="btn small" disabled={active <= 0} onClick={() => goTo(active - 1)}>← 上一镜</button>
        <span className="pg-no">镜 {String(shots[active]?.shot_no ?? active + 1).padStart(2, '0')} / {shots.length}</span>
        <button className="btn small" disabled={active >= shots.length - 1} onClick={() => goTo(active + 1)}>下一镜 →</button>
        <div className="shot-chips">
          {shots.map((s, i) => {
            const hasVideo = s.versions.some(v => v.status === 'succeeded')
            const cls = ['shot-chip', i === active ? 'active' : '', hasVideo ? 'has-video' : s.scene_status === 'approved' ? 'scene-ok' : ''].join(' ')
            return <div key={s.id} className={cls} title={`镜${s.shot_no}`} onClick={() => goTo(i)}>{String(s.shot_no).padStart(2, '0')}</div>
          })}
        </div>
      </div>

      <div className="shot-carousel" ref={carRef} onScroll={onScroll}>
        {shots.map(s => (
          <div className="shot-slide" key={s.id}>
            <ShotSlide shot={s} onChanged={refresh} />
          </div>
        ))}
      </div>
    </>
  )
}

function ShotSlide({ shot, onChanged }: { shot: Shot; onChanged: () => void }) {
  const { toast } = useNav()
  const [busy, setBusy] = useState(false)
  const sceneApproved = shot.scene_status === 'approved'

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    setBusy(true)
    try { await fn(); if (msg) toast(msg); onChanged() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const sceneStamp = sceneApproved ? ['关键帧已过审', 'green']
    : shot.scene_status === 'generating' ? ['关键帧生成中', 'gold']
      : shot.scene_status === 'review' ? ['关键帧待选', 'red'] : ['未生成关键帧', 'grey']
  const videoDone = shot.versions.some(v => v.status === 'succeeded')

  return (
    <div className="slide-card">
      <div className="slide-head">
        <span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
        <span className="meta">{shot.duration_s}s · {shot.shot_size} · {shot.camera_move} · {shot.transition} · {shot.characters.join(' / ') || '缺角色'}</span>
        <span className={`stamp ${sceneStamp[1]}`}>{sceneStamp[0]}</span>
        {videoDone && <span className="stamp green">已有成片</span>}
      </div>

      <div className="slide-top">
        <div className="slide-left">
          <div className="kv"><b>场景</b>{shot.scene_setting}</div>
          <div className="kv"><b>画面</b>{shot.action_desc}</div>
          {shot.first_frame_desc && <div className="kv"><b>首帧</b>{shot.first_frame_desc}</div>}
          {shot.last_frame_desc && <div className="kv"><b>尾帧</b>{shot.last_frame_desc}</div>}
          {shot.narration && <div className="kv"><b>旁白</b>{shot.narration}</div>}
          {!!shot.dialogues.length && (
            <div className="kv"><b>台词</b>
              {shot.dialogues.map((d, i) => (
                <div key={i} className="dlg-line"><span className="dlg-speaker">{d.speaker}</span>「{d.line}」</div>
              ))}
            </div>
          )}
        </div>
        <div className="slide-right">
          <VideoPhase shot={shot} busy={busy} act={act} sceneApproved={sceneApproved} />
        </div>
      </div>

      <div className="slide-keyframes">
        <KeyframePhase shot={shot} busy={busy} act={act} />
      </div>
    </div>
  )
}

function KeyframePhase({ shot, busy, act }: {
  shot: Shot; busy: boolean; act: (fn: () => Promise<unknown>, msg?: string) => Promise<void>
}) {
  const done = (shot.scenes ?? []).filter(s => s.status === 'succeeded')
  const requiredKinds: ('head' | 'tail')[] = shot.required_keyframes?.length
    ? shot.required_keyframes
    : (shot.continuity_from_prev ? ['tail'] : ['head', 'tail'])
  const needsHead = requiredKinds.includes('head')
  const missingKinds = requiredKinds.filter(kind =>
    kind === 'head' ? !shot.approved_head_scene_id : !shot.approved_tail_scene_id
  )
  const targetKinds = missingKinds.length ? missingKinds : requiredKinds
  const targetLabel = targetKinds.map(kind => kind === 'head' ? '首图' : '尾图').join(' / ')
  const headDone = done.filter(s => s.kind === 'head')
  const tailDone = done.filter(s => s.kind === 'tail')
  const thumb = (sc: SceneCandidate, approvedId: string | null | undefined, what: string) => (
    <SceneThumb key={sc.id} scene={sc} approved={sc.id === approvedId} busy={busy}
      onApprove={() => act(() => api.post(`/shots/${shot.id}/scene/approve`, { scene_id: sc.id }), `已采用该${what}`)}
      onDelete={() => act(() => api.del(`/scenes/${sc.id}`), '已删除该关键帧（关键帧删空时会一并删除本镜旧成片）')} />
  )

  return (
    <>
      <div className="kf-head">
        <b style={{ fontSize: 13 }}>关键帧</b>
        <button className="btn small primary" disabled={busy || shot.scene_status === 'generating'}
          onClick={() => act(
            () => api.post(`/shots/${shot.id}/scene`, { kinds: targetKinds }),
            `已发起${targetLabel}生成（自动评审）`
          )}>
          {done.length ? `重新生成${targetLabel}` : `生成${targetLabel}`}
        </button>
        {shot.scene_status === 'generating' && <span className="stamp gold">生成中…</span>}
        {shot.scene_status === 'review' && <span style={{ fontSize: 12, color: 'var(--cinnabar-deep)' }}>评审未自动通过，请分别确认所需首图/尾图或重生/删除</span>}
        {shot.scene_status === 'approved' && <span style={{ fontSize: 12, color: 'var(--moss)' }}>已通过，右上方即可生成视频</span>}
        {!needsHead && <span style={{ fontSize: 12, color: 'var(--ink-faint)' }}>本镜首帧沿用上一镜尾图，只需审核尾图</span>}
      </div>
      {!done.length && shot.scene_status !== 'generating' && (
        <div style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>尚无关键帧，点击「生成关键帧」开始。</div>
      )}
      {needsHead && !!headDone.length && (
        <>
          <div className="kf-group-label">首图候选（{headDone.length}）</div>
          <div className="kf-scroll">{headDone.map(sc => thumb(sc, shot.approved_head_scene_id, '首图'))}</div>
        </>
      )}
      {!!tailDone.length && (
        <>
          <div className="kf-group-label">尾图候选（{tailDone.length}）</div>
          <div className="kf-scroll">{tailDone.map(sc => thumb(sc, shot.approved_tail_scene_id, '尾图'))}</div>
        </>
      )}
    </>
  )
}

function SceneThumb({ scene, approved, busy, onApprove, onDelete }: {
  scene: SceneCandidate; approved: boolean; busy: boolean; onApprove: () => void; onDelete: () => void
}) {
  const overall = scene.qa?.overall
  const label = scene.kind === 'head' ? '首图' : '尾图'
  return (
    <div className={`scene-thumb ${approved ? 'approved' : ''}`}>
      <img src={scene.image_url} alt="" />
      <div className="st-body">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
          <span>{label} · 第{scene.version_no}版{overall !== undefined && overall >= 0 ? ` · ${overall.toFixed(2)}` : ''}</span>
          <span style={{ display: 'flex', gap: 4 }}>
            {approved ? <span className="stamp green" style={{ fontSize: 11 }}>采用</span>
              : <button className="btn small" disabled={busy} onClick={onApprove}>采用</button>}
            <button className="btn small ghost" disabled={busy} onClick={onDelete} title="删除这张关键帧">删</button>
          </span>
        </div>
        {scene.qa?.issues?.length ? <div style={{ color: 'var(--ink-faint)', marginTop: 3 }}>{scene.qa.issues[0]}</div> : null}
      </div>
    </div>
  )
}

function VideoPhase({ shot, busy, act, sceneApproved }: {
  shot: Shot; busy: boolean; sceneApproved: boolean
  act: (fn: () => Promise<unknown>, msg?: string) => Promise<void>
}) {
  const [showVer, setShowVer] = useState<string | null>(null)
  const [editPrompt, setEditPrompt] = useState<string | null>(null)

  const adopted = shot.versions.find(v => v.id === shot.adopted_version_id)
  const current = showVer ? shot.versions.find(v => v.id === showVer)! : (adopted ?? shot.versions[0])

  return (
    <div className="video-panel">
      <div className="vp-media">
        {current?.video_url
          ? <video className="rev-video" src={current.video_url} controls loop muted playsInline />
          : <div className="vp-empty">
              {current?.status === 'running' || current?.status === 'queued'
                ? <><span className="stamp gold">生成中</span><span style={{ fontSize: 12 }}>{shot.duration_s}s · 约需数分钟</span></>
                : current?.status === 'failed' ? <span className="stamp red">失败</span>
                : current?.status === 'paused_budget' ? <span className="stamp red">预算暂停</span>
                : sceneApproved ? <span>等待生成视频<br /><span style={{ fontSize: 11 }}>用上方「生成全部视频」入队</span></span>
                : <span>关键帧通过后<br />可生成视频</span>}
            </div>}
      </div>
      <div className="vp-controls">
        {current?.qa && current.qa.overall >= 0 && (
          <div className="fc-qa" style={{ margin: 0 }}>质检 {current.qa.overall.toFixed(2)}{current.qa.issues?.length ? ` · ${current.qa.issues[0]}` : ''}</div>
        )}
        {current?.status === 'failed' && current.error && (
          <div className="error-banner" style={{ margin: 0, fontSize: 12 }}>{current.error}</div>
        )}
        {shot.versions.length > 1 && (
          <select style={{ width: '100%' }} value={current?.id ?? ''} onChange={e => setShowVer(e.target.value)}>
            {shot.versions.map(v => (
              <option key={v.id} value={v.id}>
                第{v.version_no}版 · {v.status}{v.id === shot.adopted_version_id ? ' · 已采用' : ''}{v.qa && v.qa.overall >= 0 ? ` · QA ${v.qa.overall.toFixed(2)}` : ''}
              </option>
            ))}
          </select>
        )}

        {editPrompt !== null ? (
          <>
            <div className="hint" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
              改词建议：保留「以给定首尾帧」「单一连贯动作」「画面稳定/不跳切」等约束，只调整动作细节。
            </div>
            <textarea rows={6} style={{ fontSize: 12.5 }} value={editPrompt} onChange={e => setEditPrompt(e.target.value)} />
            <div className="fc-actions">
              <button className="btn small primary" disabled={busy}
                onClick={() => act(async () => { await api.post(`/shots/${shot.id}/generate`, { prompt_override: editPrompt }); setEditPrompt(null) }, '已按修改后的 prompt 入队')}>提交生成</button>
              <button className="btn small ghost" onClick={() => setEditPrompt(null)}>放弃</button>
            </div>
          </>
        ) : (
          <div className="fc-actions">
            {shot.video_stale && (
              <button className="btn small primary" disabled={busy}
                onClick={() => act(() => api.post(`/episodes/${shot.episode_id}/generate`, { from_shot_no: shot.shot_no }), '已从本镜起、沿连续段往后重生')}>从此镜往后重生</button>
            )}
            {current && current.status === 'succeeded' && current.id !== shot.adopted_version_id && (
              <button className="btn small primary" disabled={busy}
                onClick={() => act(() => api.post(`/shots/${shot.id}/adopt`, { version_id: current.id }), '已采用该版本')}>采用此版</button>
            )}
            {current?.video_url && (
              <a className="btn small" href={current.video_url}
                download={`镜${String(shot.shot_no).padStart(2, '0')}_第${current.version_no}版.mp4`}
                style={{ textDecoration: 'none' }}>导出</a>
            )}
            {shot.versions.length > 0 && (
              <>
                <button className="btn small primary" disabled={busy}
                  onClick={() => act(() => api.post(`/shots/${shot.id}/generate`, { with_critique: true }), '已带 AI 评语重生（针对上一版问题改正）')}>带评语重生</button>
                <button className="btn small" disabled={busy} onClick={() => setEditPrompt(current?.prompt_text ?? '')}>改词重生</button>
                <button className="btn small" disabled={busy}
                  onClick={() => act(() => api.post(`/shots/${shot.id}/generate`, { reroll: true }), '已原词重抽（新任务入队）')}>原词重抽</button>
                {current && (
                  <button className="btn small ghost" disabled={busy}
                    onClick={() => { if (window.confirm(`删除镜${shot.shot_no} 第${current.version_no}版视频？`)) act(() => api.del(`/versions/${current.id}`), '已删除该视频版本') }}>删除此版</button>
                )}
              </>
            )}
          </div>
        )}
        {current?.image_inputs?.first_frame_used && (
          <div style={{ fontSize: 11, color: 'var(--ink-faint)' }}>
            首帧：{current.image_inputs.first_frame_src === 'prev_tail_keyframe' ? '上一镜尾图' : '本镜首图'}
            {current.image_inputs.last_frame_used ? ' · 尾帧：本镜尾图' : ''}
          </div>
        )}
        {shot.video_stale && <div style={{ fontSize: 11.5, color: 'var(--cinnabar-deep)' }}>关键帧已变更，本镜视频链已过期，建议「从此镜往后重生」</div>}
      </div>
    </div>
  )
}
