import { useEffect, useRef, useState } from 'react'
import { api, Episode, Shot } from '../api'
import { useEpisode, useNav } from '../App'
import { EpStamp } from './BiblePage'
import EpisodeCrumb from '../components/EpisodeCrumb'
import AsyncButton from '../components/AsyncButton'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'

const SIZES = ['远景', '全景', '中景', '近景', '特写']
const MOVES = ['固定', '推近', '拉远', '横摇', '跟随']
const TRANS = ['硬切', '叠化', '淡出淡入', '黑场', '闪黑', '闪白', '甩镜', '遮挡转场', '匹配剪辑', '声音延续+叠化', '声音先行+淡入']
const DURATIONS = [5, 6, 7, 8, 9, 10]
export default function BoardPage() {
  const { episodeId, go, projectId, toast } = useNav()
  const { data: ep, refresh, error, loading } = useEpisode(episodeId!, 'board')
  const [busy, setBusy] = useState(false)
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  const timelineRef = useRef<HTMLDivElement>(null)
  const storyboardTimer = useTaskTimer(`episode.${episodeId}.storyboard`, ep?.status === 'scripting')

  useEffect(() => {
    timelineRef.current
      ?.querySelector<HTMLElement>('[aria-current="true"]')
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
  }, [selectedShotId, ep?.shots?.[0]?.id])

  if (error && !ep) return <div className="empty">{error}</div>
  if (loading && !ep) return <div className="empty">展卷中……</div>
  if (!ep) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { const r = await fn(); if (doneMsg) toast(doneMsg); refresh(); return r }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const totalDur = ep.shots?.reduce((s, x) => s + x.duration_s, 0) ?? 0
  // 与后端 /storyboard/resume 对齐：有已落库镜头 + 未完成提示，即视为可续跑 checkpoint。
  // 不依赖具体失败文案（「需修改镜头」「追加镜生成失败」「逐镜 checkpoint」等都会变）。
  const canResumeCheckpoint = Boolean(
    ep.shots?.length &&
    ep.script_error &&
    (ep.status === 'scripted' || ep.status === 'script_failed')
  )
  const selectedShot = ep.shots?.find(shot => shot.id === selectedShotId) ?? ep.shots?.[0]
  const selectedShotIndex = Math.max(0, ep.shots?.findIndex(shot => shot.id === selectedShot?.id) ?? 0)

  const selectRelativeShot = (offset: number) => {
    if (!ep.shots?.length) return
    const nextIndex = Math.min(ep.shots.length - 1, Math.max(0, selectedShotIndex + offset))
    setSelectedShotId(ep.shots[nextIndex].id)
  }

  const confirmRegenerateStoryboard = (mode: 'fresh' | 'resume') => {
    if (mode === 'resume') {
      storyboardTimer.start()
      void act(
        () => api.post(`/episodes/${ep.id}/storyboard/resume`),
        `已从前 ${ep.shots?.length ?? 0} 镜 checkpoint 继续生成`,
      ).then(r => { if (r === undefined) storyboardTimer.clear() })
      return
    }
    const shotCount = ep.shots?.length ?? 0
    const hasDownstream = shotCount > 0
    const ok = window.confirm(
      hasDownstream
        ? `重新生成分镜将删除本集全部 ${shotCount} 个镜头，以及其参考图、视频版本与成片依赖。\n`
          + `费用：会重新消耗文本模型额度；已产生的视频费用不会退回。\n`
          + `此操作不可恢复。确定继续？`
        : '将开始生成分镜脚本（先规划大纲，再逐镜填充）。确定继续？',
    )
    if (!ok) return
    storyboardTimer.start()
    void act(
      () => api.post(`/episodes/${ep.id}/storyboard`),
      hasDownstream
        ? '已删除旧分镜，开始重新生成整版分镜'
        : '分镜生成已开始（先规划大纲，再逐镜填充，QA 通过后陆续展示）',
    ).then(r => { if (r === undefined) storyboardTimer.clear() })
  }

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="分镜台" view="board" episodeNo={ep.episode_no} />
        <h1>分镜台 <span className="sub">《{ep.title}》 · 总览镜头节奏，聚焦编辑当前一镜</span></h1>
        <hr className="rule" />
      </header>

      <section className="card board-toolbar">
        <div className="board-toolbar-row">
          <div className="board-status-group">
          <EpStamp status={ep.status} />
          <span className={`stamp ${ep.screenplay_status === 'ready' ? 'green' : ep.screenplay_status === 'running' ? 'gold' : ep.screenplay_status === 'failed' || ep.screenplay_status === 'warning' ? 'red' : 'grey'}`}>
            {ep.screenplay_status === 'ready' ? '剧本成' : ep.screenplay_status === 'warning' ? '剧本有阻塞' : ep.screenplay_status === 'running' ? '剧本中' : ep.screenplay_status === 'failed' ? '剧本败' : '待剧本'}
          </span>
          {ep.screenplay_mode === 'full_script' && <span className="stamp grey">完整剧本</span>}
          </div>
          <div className="board-action-group">
          <button className="btn" disabled={busy || ep.status === 'scripting' || ep.screenplay_status !== 'ready'}
            onClick={() => confirmRegenerateStoryboard(canResumeCheckpoint ? 'resume' : 'fresh')}>
            {canResumeCheckpoint ? `从镜${String((ep.shots?.length ?? 0) + 1).padStart(2, '0')}继续` : ep.shots?.length ? '重新生成分镜' : '生成分镜脚本'}
          </button>
          {canResumeCheckpoint && (
            <AsyncButton className="btn ghost" disabled={busy || ep.status === 'scripting'}
              busyLabel="提交中…"
              onAction={async () => { confirmRegenerateStoryboard('fresh') }}>
              重新生成整版
            </AsyncButton>
          )}
          {ep.screenplay_status !== 'ready' && (
            <button className="btn primary" disabled={busy} onClick={() => go('script', projectId, ep.id)}>
              先去剧本台
            </button>
          )}
          {ep.status === 'scripting' && (
            <button className="btn ghost" disabled={busy}
              onClick={() => act(() => api.post(`/episodes/${ep.id}/storyboard/cancel`), '已取消分镜生成请求，可重新发起')}>
              取消生成
            </button>
          )}
          {ep.status === 'scripted' && (
            <button className="btn primary" disabled={busy}
              onClick={async () => {
                const r = await act(() => api.post(`/episodes/${ep.id}/confirm`)) as { estimated_cost_cny: number; total_duration_s?: number } | undefined
                if (r) {
                  toast(`分镜已确认。实际总时长 ${r.total_duration_s ?? totalDur}s，预估生成成本 ¥${r.estimated_cost_cny}，可入评审墙开始生成`)
                }
              }}>确认分镜（解锁生成）</button>
          )}
          {(ep.status === 'confirmed' || ep.status === 'generating') && (
            <button className="btn primary" disabled={busy}
              onClick={() => go('wall', projectId, ep.id)}>
              入评审墙生成视频 →
            </button>
          )}
          </div>
          <div className="board-toolbar-meta">
          <TaskTimer label="分镜" timer={storyboardTimer} />
          {ep.storyboard_evidence && <EvidenceDrawer evidence={ep.storyboard_evidence} label="分镜证据" />}
          </div>
        </div>
        <div className="board-stat-strip" aria-label="分镜统计">
          <span><b>{ep.shots?.length ?? 0}</b> 镜头</span>
          <span><b>{totalDur}s</b> 总时长</span>
          <span><b>5–10s</b> 单镜时长</span>
          <span><b>¥{ep.cost_cny.toFixed(1)}</b> 已耗</span>
        </div>
        {ep.screenplay_status !== 'ready' && <div className="error-banner">本集还没有可用剧本。请先到剧本台生成/保存完整剧本，再展开分镜。</div>}
        {ep.status === 'scripting' && <div style={{ marginTop: 10 }}><span className="stamp gold">分镜中</span> <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>{ep.storyboard_planned_shots ? `已规划约 ${ep.storyboard_planned_shots} 镜（会随逐镜细化增减），已通过 ${ep.shots?.length ?? 0} 镜，通过后会继续下一镜……` : `正在逐镜头生成并 QA；已通过 ${ep.shots?.length ?? 0} 镜，通过后会继续下一镜……`}</span></div>}
        {ep.storyboard_warning && (
          <div className="error-banner" style={{ borderColor: 'var(--gold, #b08d57)', background: 'rgba(176,141,87,0.08)' }}>
            {ep.storyboard_warning}
          </div>
        )}
        {ep.script_error && (
          <div className="error-banner">
            {ep.status === 'script_failed' ? `分镜失败（错误已列明，修改源头或重试）：\n${ep.script_error}` : `分镜提示：\n${ep.script_error}`}
          </div>
        )}
      </section>

      <div className="workspace-gap" />

      {!ep.shots?.length
        ? <div className="empty"><div className="big">镜</div>尚无分镜<br />点击上方「生成分镜脚本」</div>
        : <div className="board-workspace">
            <section className="shot-navigator" aria-label="镜头轨道">
              <div className="shot-navigator-head">
                <div><b>镜头轨道</b><span>点击镜头聚焦编辑</span></div>
                <div className="shot-navigator-actions">
                  <span>{selectedShotIndex + 1} / {ep.shots.length}</span>
                  <button type="button" aria-label="上一镜" disabled={selectedShotIndex === 0} onClick={() => selectRelativeShot(-1)}>←</button>
                  <button type="button" aria-label="下一镜" disabled={selectedShotIndex === ep.shots.length - 1} onClick={() => selectRelativeShot(1)}>→</button>
                </div>
              </div>
              <div
                ref={timelineRef}
                className="shot-navigator-list"
                tabIndex={0}
                onKeyDown={event => {
                  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
                  event.preventDefault()
                  selectRelativeShot(event.key === 'ArrowLeft' ? -1 : 1)
                }}
              >
                {ep.shots.map(shot => (
                  <button
                    key={shot.id}
                    type="button"
                    aria-current={shot.id === selectedShot?.id}
                    className={shot.id === selectedShot?.id ? 'active' : ''}
                    onClick={() => setSelectedShotId(shot.id)}
                  >
                    <span className="shot-nav-top"><span className="shot-nav-no">镜 {String(shot.shot_no).padStart(2, '0')}</span><span className="shot-nav-meta">{shot.duration_s}s</span></span>
                    <span className="shot-nav-main"><b>{shot.shot_size} · {shot.camera_move}</b><small>{shot.scene_setting}</small></span>
                  </button>
                ))}
              </div>
            </section>
            <section className="shot-editor-pane">
              {selectedShot && <ShotStrip key={selectedShot.id} shot={selectedShot} episode={ep} onChanged={refresh} disabled={busy} />}
            </section>
          </div>}
    </>
  )
}

function ShotStrip({ shot, episode, onChanged, disabled }: {
  shot: Shot; episode: Episode
  onChanged: () => void; disabled: boolean
}) {
  const { toast } = useNav()
  const [edit, setEdit] = useState<Shot | null>(null)
  const [impactOpen, setImpactOpen] = useState(false)
  const [detailTab, setDetailTab] = useState<'frames' | 'script'>('frames')
  const s = edit ?? shot

  async function save() {
    if (!edit) return
    try {
      const result = await api.put(`/shots/${shot.id}`, {
        duration_s: edit.duration_s, shot_size: edit.shot_size, camera_move: edit.camera_move,
        scene_setting: edit.scene_setting, characters: edit.characters, action_desc: edit.action_desc,
        first_frame_desc: edit.first_frame_desc, last_frame_desc: edit.last_frame_desc,
        source_excerpt: edit.source_excerpt,
        narration: edit.narration || null, dialogues: edit.dialogues, transition: edit.transition,
        continuity_from_prev: !!edit.continuity_from_prev,
      })
      const impact = (result as { impact?: ImpactSummary }).impact
      toast(`镜 ${shot.shot_no} 已保存；${impact?.stale_descendant_ids?.length ?? 0} 个下游证据已标记失效`)
      setEdit(null); onChanged()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  return (
    <div className="shot-strip">
      <div className="shot-head">
        <div className="shot-head-copy">
          <span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
          <span className="meta">{s.duration_s}s · {s.shot_size} · {s.camera_move} · {s.transition}{s.continuity_from_prev ? ' · 接上镜' : ''}</span>
          <span className="meta shot-characters">{s.characters.join(' / ') || '缺角色（需修改）'}</span>
        </div>
        <div className="shot-head-actions">
          <span className="meta">¥{shot.est_cost_cny.toFixed(1)}</span>
          {shot.storyboard_evidence && <EvidenceDrawer evidence={shot.storyboard_evidence} label="本镜证据" />}
          {!edit
            ? <button className="btn small" disabled={disabled} onClick={() => setEdit(JSON.parse(JSON.stringify(shot)))}>修改</button>
            : <>
              <button className="btn small primary" onClick={() => setImpactOpen(true)}>保存</button>
              <button className="btn small ghost" onClick={() => setEdit(null)}>放弃</button>
            </>}
        </div>
      </div>
      <div className={`shot-body ${edit ? 'editing' : 'reviewing'}`}>
        {edit ? (
          <>
            <div className="shot-edit-grid full">
              <div><label className="f">时长（5~10s）</label>
                <select style={{ width: '100%' }} value={edit.duration_s} onChange={e => setEdit({ ...edit, duration_s: Number(e.target.value) })}>
                  {DURATIONS.map(x => <option key={x} value={x}>{x}s</option>)}</select></div>
              <div><label className="f">景别</label>
                <select style={{ width: '100%' }} value={edit.shot_size} onChange={e => setEdit({ ...edit, shot_size: e.target.value })}>
                  {SIZES.map(x => <option key={x}>{x}</option>)}</select></div>
              <div><label className="f">运镜</label>
                <select style={{ width: '100%' }} value={edit.camera_move} onChange={e => setEdit({ ...edit, camera_move: e.target.value })}>
                  {MOVES.map(x => <option key={x}>{x}</option>)}</select></div>
              <div><label className="f">转场</label>
                <select style={{ width: '100%' }} value={edit.transition} onChange={e => setEdit({ ...edit, transition: e.target.value })}>
                  {TRANS.map(x => <option key={x}>{x}</option>)}</select></div>
            </div>
            <div className="full"><label className="f">场景标签（只写时间+地点，越短越好）</label>
              <textarea rows={1} value={edit.scene_setting} onChange={e => setEdit({ ...edit, scene_setting: e.target.value })} /></div>
            <div className="full"><label className="f">画面描述（一个连贯动作，人物和剧情优先）</label>
              <textarea rows={3} value={edit.action_desc} onChange={e => setEdit({ ...edit, action_desc: e.target.value })} /></div>
            <div><label className="f">首帧画面（本镜开始的静止画面）</label>
              <textarea rows={2} value={edit.first_frame_desc ?? ''} onChange={e => setEdit({ ...edit, first_frame_desc: e.target.value })} /></div>
            <div><label className="f">尾帧画面（结束的静止画面，须与首帧明显不同）</label>
              <textarea rows={2} value={edit.last_frame_desc ?? ''} onChange={e => setEdit({ ...edit, last_frame_desc: e.target.value })} /></div>
            <div className="full"><label className="f">对应小说原文（逐字摘录，给 Seedance 兜底参考）</label>
              <textarea rows={3} value={edit.source_excerpt ?? ''} onChange={e => setEdit({ ...edit, source_excerpt: e.target.value })} /></div>
            <div className="full"><label className="f">旁白（可空）</label>
              <textarea rows={2} value={edit.narration ?? ''} onChange={e => setEdit({ ...edit, narration: e.target.value })} /></div>
            <div className="full">
              <label className="f">台词</label>
              {edit.dialogues.map((d, i) => (
                <div key={i} className="dlg-line">
                  <input type="text" style={{ width: 110 }} value={d.speaker} placeholder="角色名"
                    onChange={e => { const next = [...edit.dialogues]; next[i] = { ...d, speaker: e.target.value }; setEdit({ ...edit, dialogues: next }) }} />
                  <input type="text" style={{ flex: 1 }} value={d.line} placeholder="台词（不设字数上限）"
                    onChange={e => { const next = [...edit.dialogues]; next[i] = { ...d, line: e.target.value }; setEdit({ ...edit, dialogues: next }) }} />
                  <input type="text" style={{ width: 70 }} value={d.emotion}
                    onChange={e => { const next = [...edit.dialogues]; next[i] = { ...d, emotion: e.target.value }; setEdit({ ...edit, dialogues: next }) }} />
                  <button className="btn small ghost" onClick={() => setEdit({ ...edit, dialogues: edit.dialogues.filter((_, j) => j !== i) })}>删</button>
                </div>
              ))}
              <button className="btn small" style={{ marginTop: 6 }}
                onClick={() => setEdit({ ...edit, dialogues: [...edit.dialogues, { speaker: episode.shots?.find(x => x.id === shot.id)?.characters[0] ?? '', line: '', emotion: '平静' }] })}>+ 加一句</button>
            </div>
          </>
        ) : (
          <>
            <div className="shot-overview full">
              <div className="shot-visual-brief">
                <span className="shot-section-label">画面设计</span>
                <h2>{s.scene_setting}</h2>
                <p>{s.action_desc}</p>
              </div>
              <dl className="shot-specs" aria-label="镜头参数">
                <div><dt>时长</dt><dd>{s.duration_s}s</dd></div>
                <div><dt>景别</dt><dd>{s.shot_size}</dd></div>
                <div><dt>运镜</dt><dd>{s.camera_move}</dd></div>
                <div><dt>转场</dt><dd>{s.transition}</dd></div>
              </dl>
            </div>

            <div className="shot-detail-tabs full" role="tablist" aria-label="镜头详情">
              <button type="button" role="tab" aria-selected={detailTab === 'frames'} className={detailTab === 'frames' ? 'active' : ''} onClick={() => setDetailTab('frames')}>起止画面</button>
              <button type="button" role="tab" aria-selected={detailTab === 'script'} className={detailTab === 'script' ? 'active' : ''} onClick={() => setDetailTab('script')}>声音与原文{s.dialogues.length ? <i>{s.dialogues.length}</i> : null}</button>
            </div>

            {detailTab === 'frames' ? (
              <div className="shot-frame-pair full" role="tabpanel">
                <div className="shot-frame-card"><span>01 · 首帧</span><p>{s.first_frame_desc || '暂未描述首帧画面'}</p></div>
                <div className="shot-frame-flow" aria-hidden="true">→</div>
                <div className="shot-frame-card"><span>02 · 尾帧</span><p>{s.last_frame_desc || '暂未描述尾帧画面'}</p></div>
              </div>
            ) : (
              <div className="shot-script-grid full" role="tabpanel">
                <div className="shot-script-copy">
                  <span className="shot-section-label">对应原文</span>
                  <p>{s.source_excerpt || '暂无对应原文'}</p>
                </div>
                <div className="shot-audio-copy">
                  {s.narration && <div className="shot-audio-line"><b>旁白</b><p>{s.narration}</p></div>}
                  {!!s.dialogues.length && s.dialogues.map((d, i) => (
                    <div key={i} className="shot-audio-line"><b>{d.speaker}<small>{d.emotion}</small></b><p>「{d.line}」</p></div>
                  ))}
                  {!s.narration && !s.dialogues.length && <div className="shot-audio-empty">本镜无旁白或台词</div>}
                </div>
              </div>
            )}
          </>
        )}
      </div>
      <ImpactDialog
        open={impactOpen}
        title={`保存镜 ${shot.shot_no} 并传播影响`}
        impact={{
          requires_reconfirm: true,
          paid_media_invalidated: (shot.version_count ?? shot.versions?.length ?? 0) > 0,
        }}
        knownEffects={[
          (shot.version_count ?? shot.versions?.length ?? 0) > 0
            ? `本镜 ${shot.version_count ?? shot.versions.length} 个视频版本将被清空`
            : '本镜暂无视频版本',
          '精确失效 Artifact 数量将在保存后由服务端计算并回传',
        ]}
        onClose={() => setImpactOpen(false)}
        onConfirm={() => { setImpactOpen(false); void save() }}
      />
    </div>
  )
}
