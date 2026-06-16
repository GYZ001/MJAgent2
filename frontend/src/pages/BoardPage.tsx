import { useState } from 'react'
import { api, Episode, Shot } from '../api'
import { useEpisode, useNav } from '../App'
import { EpStamp } from './BiblePage'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

const SIZES = ['远景', '全景', '中景', '近景', '特写']
const MOVES = ['固定', '推近', '拉远', '横摇', '跟随']
const TRANS = ['硬切', '叠化', '淡出淡入', '黑场', '闪黑', '闪白', '甩镜', '遮挡转场', '匹配剪辑', '声音延续+叠化', '声音先行+淡入']
const MIN_SHOT_DURATION_S = 5
const MAX_SHOT_DURATION_S = 15

function clampShotDuration(value: number | string) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 10
  return Math.max(MIN_SHOT_DURATION_S, Math.min(MAX_SHOT_DURATION_S, Math.round(n)))
}

export default function BoardPage() {
  const { episodeId, go, projectId, toast } = useNav()
  const { data: ep, refresh } = useEpisode(episodeId!)
  const [busy, setBusy] = useState(false)
  const [durationOverrides, setDurationOverrides] = useState<Record<string, number>>({})
  const storyboardTimer = useTaskTimer(`episode.${episodeId}.storyboard`, ep?.status === 'scripting')

  if (!ep) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { const r = await fn(); if (doneMsg) toast(doneMsg); refresh(); return r }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const totalDur = ep.shots?.reduce((s, x) => s + (durationOverrides[x.id] ?? x.duration_s), 0) ?? 0

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="分镜台" view="board" episodeNo={ep.episode_no} />
        <h1>分镜台 <span className="sub">《{ep.title}》 · 脚本免费可改，确认后才花钱</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <EpStamp status={ep.status} />
          <span className={`stamp ${ep.screenplay_status === 'ready' ? 'green' : ep.screenplay_status === 'running' ? 'gold' : ep.screenplay_status === 'failed' ? 'red' : 'grey'}`}>
            {ep.screenplay_status === 'ready' ? '剧本成' : ep.screenplay_status === 'running' ? '剧本中' : ep.screenplay_status === 'failed' ? '剧本败' : '待剧本'}
          </span>
          {ep.screenplay_mode === 'full_script' && <span className="stamp grey">完整剧本</span>}
          <button className="btn" disabled={busy || ep.status === 'scripting' || ep.screenplay_status !== 'ready'}
            onClick={() => {
              storyboardTimer.start()
              act(() => api.post(`/episodes/${ep.id}/storyboard`), '分镜生成已开始（先规划大纲，再逐镜填充，QA 通过后陆续展示）')
            }}>
            {ep.shots?.length ? '重新生成分镜' : '生成分镜脚本'}
          </button>
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
          {!!ep.shots?.length && ep.status !== 'scripting' && (
            <button className="btn" disabled={busy}
              onClick={async () => {
                const r = await act(() => api.post(`/episodes/${ep.id}/rebalance-durations`)) as { total_before: number; total_after: number; target_total: number } | undefined
                if (r) {
                  setDurationOverrides({})
                  toast(`已自动压缩时长：${r.total_before}s → ${r.total_after}s（目标 ${r.target_total}s）`)
                }
              }}>
              自动压缩时长
            </button>
          )}
          {ep.status === 'scripted' && (
            <button className="btn primary" disabled={busy}
              onClick={async () => {
                const r = await act(() => api.post(`/episodes/${ep.id}/confirm`)) as { estimated_cost_cny: number; total_duration_s?: number } | undefined
                if (r) toast(`分镜已确认。实际总时长 ${r.total_duration_s ?? totalDur}s，预估生成成本 ¥${r.estimated_cost_cny}，可入评审墙开始生成`)
              }}>确认分镜（解锁生成）</button>
          )}
          {(ep.status === 'confirmed' || ep.status === 'generating') && (
            <button className="btn primary" disabled={busy}
              onClick={() => go('wall', projectId, ep.id)}>
              入评审墙生成视频 →
            </button>
          )}
          <span style={{ flex: 1 }} />
          <TaskTimer label="分镜" timer={storyboardTimer} />
          <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
            共 {ep.shots?.length ?? 0} 镜 · 实际 {totalDur}s / 目标 {ep.target_duration_s}s / 上限 {ep.storyboard_duration_limit_s ?? 90}s · 已耗 ¥{ep.cost_cny.toFixed(1)}
          </span>
        </div>
        {ep.screenplay_status !== 'ready' && <div className="error-banner">本集还没有可用剧本。请先到剧本台生成/保存完整剧本，再展开分镜。</div>}
        {ep.status === 'scripting' && <div style={{ marginTop: 10 }}><span className="stamp gold">分镜中</span> <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>{ep.storyboard_planned_shots ? `已按大纲规划 ${ep.storyboard_planned_shots} 镜，正在逐镜填充并 QA：已通过 ${ep.shots?.length ?? 0}/${ep.storyboard_planned_shots} 镜，通过后会继续下一镜……` : `正在逐镜头生成并 QA；已通过 ${ep.shots?.length ?? 0} 镜，通过后会继续下一镜……`}</span></div>}
        {ep.script_error && (
          <div className="error-banner">
            {ep.status === 'script_failed' ? `分镜失败（错误已列明，修改源头或重试）：\n${ep.script_error}` : `分镜提示：\n${ep.script_error}`}
          </div>
        )}
      </section>

      <div style={{ height: 20 }} />

      {!ep.shots?.length
        ? <div className="empty"><div className="big">镜</div>尚无分镜<br />点击上方「生成分镜脚本」</div>
        : ep.shots.map(shot => (
          <ShotStrip
            key={shot.id}
            shot={shot}
            durationOverride={durationOverrides[shot.id]}
            episode={ep}
            onDurationSaved={(shotId, duration) => setDurationOverrides(prev => ({ ...prev, [shotId]: duration }))}
            onChanged={refresh}
            disabled={busy}
          />
        ))}
    </>
  )
}

function ShotStrip({ shot, durationOverride, episode, onDurationSaved, onChanged, disabled }: {
  shot: Shot; durationOverride?: number; episode: Episode
  onDurationSaved: (shotId: string, duration: number) => void
  onChanged: () => void; disabled: boolean
}) {
  const { toast } = useNav()
  const [edit, setEdit] = useState<Shot | null>(null)
  const currentShot = durationOverride == null ? shot : { ...shot, duration_s: durationOverride }
  const s = edit ?? currentShot

  async function save() {
    if (!edit) return
    const duration_s = clampShotDuration(edit.duration_s)
    try {
      await api.put(`/shots/${shot.id}`, {
        duration_s, shot_size: edit.shot_size, camera_move: edit.camera_move,
        scene_setting: edit.scene_setting, characters: edit.characters, action_desc: edit.action_desc,
        first_frame_desc: edit.first_frame_desc, last_frame_desc: edit.last_frame_desc,
        source_excerpt: edit.source_excerpt,
        narration: edit.narration || null, dialogues: edit.dialogues, transition: edit.transition,
        continuity_from_prev: !!edit.continuity_from_prev,
      })
      onDurationSaved(shot.id, duration_s)
      toast(`镜 ${shot.shot_no} 已保存（剧集回到待确认状态）`)
      setEdit(null); onChanged()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  return (
    <div className="shot-strip">
      <div className="shot-head">
        <span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
        <span className="meta">{s.duration_s}s · {s.shot_size} · {s.camera_move} · {s.transition}{s.continuity_from_prev ? ' · 接上镜' : ''}</span>
        <span className="meta" style={{ color: 'var(--indigo)' }}>{s.characters.join(' / ') || '缺角色（需修改）'}</span>
        <span style={{ flex: 1 }} />
        <span className="meta">¥{shot.est_cost_cny.toFixed(1)}</span>
        {!edit
          ? <button className="btn small" disabled={disabled} onClick={() => setEdit(JSON.parse(JSON.stringify(currentShot)))}>修改</button>
          : <>
            <button className="btn small primary" onClick={save}>保存</button>
            <button className="btn small ghost" onClick={() => setEdit(null)}>放弃</button>
          </>}
      </div>
      <div className="shot-body">
        {edit ? (
          <>
            <div className="shot-edit-grid full">
              <div><label className="f">时长(s)</label>
                <input type="number" min={MIN_SHOT_DURATION_S} max={MAX_SHOT_DURATION_S} step={1} style={{ width: '100%' }} value={edit.duration_s}
                  onChange={e => setEdit({ ...edit, duration_s: clampShotDuration(e.target.value) })} /></div>
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
            <div className="kv full"><b>场景</b>{s.scene_setting}</div>
            <div className="kv full"><b>画面</b>{s.action_desc}</div>
            {s.first_frame_desc && <div className="kv"><b>首帧</b>{s.first_frame_desc}</div>}
            {s.last_frame_desc && <div className="kv"><b>尾帧</b>{s.last_frame_desc}</div>}
            {s.source_excerpt && <div className="kv full"><b>原文</b>{s.source_excerpt}</div>}
            {s.narration && <div className="kv full"><b>旁白</b>{s.narration}</div>}
            {!!s.dialogues.length && (
              <div className="kv full"><b>台词</b>
                {s.dialogues.map((d, i) => (
                  <div key={i} className="dlg-line"><span className="dlg-speaker">{d.speaker}</span>「{d.line}」<span style={{ color: 'var(--ink-faint)', fontSize: 12 }}>{d.emotion}</span></div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
