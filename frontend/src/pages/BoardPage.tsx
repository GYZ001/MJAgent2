import { useState } from 'react'
import { api, Episode, Shot, numToCn } from '../api'
import { useEpisode, useNav } from '../App'
import { EpStamp } from './BiblePage'

const SIZES = ['远景', '全景', '中景', '近景', '特写']
const MOVES = ['固定', '推近', '拉远', '横摇', '跟随']
const TRANS = ['硬切', '叠化', '黑场']

export default function BoardPage() {
  const { episodeId, go, projectId, toast } = useNav()
  const { data: ep, refresh } = useEpisode(episodeId!)
  const [busy, setBusy] = useState(false)

  if (!ep) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { const r = await fn(); if (doneMsg) toast(doneMsg); refresh(); return r }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const totalDur = ep.shots?.reduce((s, x) => s + x.duration_s, 0) ?? 0
  const estCost = ep.shots?.reduce((s, x) => s + x.est_cost_cny, 0) ?? 0

  return (
    <>
      <header className="desk-head">
        <div className="crumb">
          <a style={{ cursor: 'pointer' }} onClick={() => go('bible', projectId, null)}>人物谱</a> / 第{numToCn(ep.episode_no)}集
        </div>
        <h1>分镜台 <span className="sub">《{ep.title}》 · 脚本免费可改，确认后才花钱</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <EpStamp status={ep.status} />
          <button className="btn" disabled={busy || ep.status === 'scripting'}
            onClick={() => act(() => api.post(`/episodes/${ep.id}/storyboard`), '分镜生成已开始（约 2~5 分钟，含校验修复回路）')}>
            {ep.shots?.length ? '重新生成分镜' : '生成分镜脚本'}
          </button>
          {ep.status === 'scripted' && (
            <button className="btn primary" disabled={busy}
              onClick={async () => {
                const r = await act(() => api.post(`/episodes/${ep.id}/confirm`)) as { estimated_cost_cny: number } | undefined
                if (r) toast(`分镜已确认。预估生成成本 ¥${r.estimated_cost_cny}，可入评审墙开始生成`)
              }}>确认分镜（解锁生成）</button>
          )}
          {(ep.status === 'confirmed' || ep.status === 'generating') && (
            <button className="btn primary" disabled={busy}
              onClick={async () => {
                if (!window.confirm(`将为 ${ep.shots?.length} 个镜头创建生成任务，预估 ¥${estCost.toFixed(1)}（上限 ¥${ep.cost_limit_cny}）。继续？`)) return
                await act(() => api.post(`/episodes/${ep.id}/generate`), '已入队，转到评审墙查看进度')
                go('wall', projectId, ep.id)
              }}>整集生成 ¥{estCost.toFixed(0)}</button>
          )}
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
            共 {ep.shots?.length ?? 0} 镜 · 总时长 {totalDur}s / 目标 {ep.target_duration_s}s · 已耗 ¥{ep.cost_cny.toFixed(1)}
          </span>
        </div>
        {ep.status === 'scripting' && <div style={{ marginTop: 10 }}><span className="stamp gold">分镜中</span> <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>正在写作并通过 V1~V7 校验器，失败会自动带错误修复重试……</span></div>}
        {ep.script_error && <div className="error-banner">分镜失败（错误已列明，修改源头或重试）：{'\n'}{ep.script_error}</div>}
      </section>

      <div style={{ height: 20 }} />

      {!ep.shots?.length
        ? <div className="empty"><div className="big">镜</div>尚无分镜<br />点击上方「生成分镜脚本」</div>
        : ep.shots.map(shot => <ShotStrip key={shot.id} shot={shot} episode={ep} onChanged={refresh} disabled={busy} />)}
    </>
  )
}

function ShotStrip({ shot, episode, onChanged, disabled }: {
  shot: Shot; episode: Episode; onChanged: () => void; disabled: boolean
}) {
  const { toast } = useNav()
  const [edit, setEdit] = useState<Shot | null>(null)
  const s = edit ?? shot

  async function save() {
    if (!edit) return
    try {
      await api.put(`/shots/${shot.id}`, {
        duration_s: Number(edit.duration_s), shot_size: edit.shot_size, camera_move: edit.camera_move,
        scene_setting: edit.scene_setting, characters: edit.characters, action_desc: edit.action_desc,
        narration: edit.narration || null, dialogues: edit.dialogues, transition: edit.transition,
        continuity_from_prev: !!edit.continuity_from_prev,
      })
      toast(`镜 ${shot.shot_no} 已保存（剧集回到待确认状态）`)
      setEdit(null); onChanged()
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  return (
    <div className="shot-strip">
      <div className="shot-head">
        <span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
        <span className="meta">{s.duration_s}s · {s.shot_size} · {s.camera_move} · {s.transition}{s.continuity_from_prev ? ' · 接上镜' : ''}</span>
        <span className="meta" style={{ color: 'var(--indigo)' }}>{s.characters.join(' / ') || '无角色'}</span>
        <span style={{ flex: 1 }} />
        <span className="meta">¥{shot.est_cost_cny.toFixed(1)}</span>
        {!edit
          ? <button className="btn small" disabled={disabled} onClick={() => setEdit(JSON.parse(JSON.stringify(shot)))}>修改</button>
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
                <input type="number" min={4} max={12} style={{ width: '100%' }} value={edit.duration_s}
                  onChange={e => setEdit({ ...edit, duration_s: Number(e.target.value) })} /></div>
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
            <div className="full"><label className="f">场景（同场景须逐字一致："时间，地点，氛围"）</label>
              <textarea rows={1} value={edit.scene_setting} onChange={e => setEdit({ ...edit, scene_setting: e.target.value })} /></div>
            <div className="full"><label className="f">画面描述（单一动作，15~50 字）</label>
              <textarea rows={2} value={edit.action_desc} onChange={e => setEdit({ ...edit, action_desc: e.target.value })} /></div>
            <div className="full"><label className="f">旁白（可空）</label>
              <textarea rows={2} value={edit.narration ?? ''} onChange={e => setEdit({ ...edit, narration: e.target.value })} /></div>
            <div className="full">
              <label className="f">台词</label>
              {edit.dialogues.map((d, i) => (
                <div key={i} className="dlg-line">
                  <input type="text" style={{ width: 110 }} value={d.speaker} placeholder="角色/旁白"
                    onChange={e => { const next = [...edit.dialogues]; next[i] = { ...d, speaker: e.target.value }; setEdit({ ...edit, dialogues: next }) }} />
                  <input type="text" style={{ flex: 1 }} value={d.line} placeholder="台词（≤20字）"
                    onChange={e => { const next = [...edit.dialogues]; next[i] = { ...d, line: e.target.value }; setEdit({ ...edit, dialogues: next }) }} />
                  <input type="text" style={{ width: 70 }} value={d.emotion}
                    onChange={e => { const next = [...edit.dialogues]; next[i] = { ...d, emotion: e.target.value }; setEdit({ ...edit, dialogues: next }) }} />
                  <button className="btn small ghost" onClick={() => setEdit({ ...edit, dialogues: edit.dialogues.filter((_, j) => j !== i) })}>删</button>
                </div>
              ))}
              <button className="btn small" style={{ marginTop: 6 }}
                onClick={() => setEdit({ ...edit, dialogues: [...edit.dialogues, { speaker: episode.shots?.find(x => x.id === shot.id)?.characters[0] ?? '旁白', line: '', emotion: '平静' }] })}>+ 加一句</button>
            </div>
          </>
        ) : (
          <>
            <div className="kv full"><b>场景</b>{s.scene_setting}</div>
            <div className="kv full"><b>画面</b>{s.action_desc}</div>
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
