import { useState } from 'react'
import { api, Bible, Character, numToCn } from '../api'
import { useNav, useProject } from '../App'

export default function BiblePage() {
  const { projectId, go, toast } = useNav()
  const { data: p, refresh } = useProject(projectId!)
  const [editing, setEditing] = useState<Bible | null>(null)
  const [busy, setBusy] = useState(false)

  if (!p) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const bible = editing ?? p.bible

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
        <h1>人物谱 <span className="sub">角色锚点一旦定稿，所有镜头逐字复用，不可漂移</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>原著 <span className="hint">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapters?.length} 章</span></h3>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <button className="btn primary" disabled={busy || p.bible_status === 'running'}
            onClick={() => act(() => api.post(`/projects/${p.id}/bible`))}>
            {p.bible ? '重新谱写人物谱' : '谱写人物谱'}
          </button>
          {p.bible_status === 'running' && <span className="stamp gold">谱写中（约 1~3 分钟）</span>}
          {p.bible && <span className="stamp green">第 {`${p.bible_version ?? ''}`} 稿</span>}
        </div>
        {p.bible_status === 'failed' && <div className="error-banner">人物谱生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.bible_error}</div>}
      </section>

      {bible && (
        <section className="card">
          <h3>世界观
            <span className="hint">era {bible.world.era} · genre {bible.world.genre}</span>
            {!editing
              ? <button className="btn small" style={{ marginLeft: 14 }} onClick={() => setEditing(JSON.parse(JSON.stringify(p.bible)))}>修订</button>
              : <>
                <button className="btn small primary" style={{ marginLeft: 14 }} disabled={busy}
                  onClick={() => act(async () => { await api.put(`/projects/${p.id}/bible`, editing); setEditing(null) }, '人物谱已定稿，版本 +1')}>定稿</button>
                <button className="btn small ghost" style={{ marginLeft: 8 }} onClick={() => setEditing(null)}>放弃</button>
              </>}
          </h3>
          <label className="f">全局画风锚点串（逐字注入每个镜头 prompt）</label>
          {editing
            ? <textarea rows={2} value={editing.world.visual_style_canonical}
                onChange={e => setEditing({ ...editing, world: { ...editing.world, visual_style_canonical: e.target.value } })} />
            : <div style={{ fontSize: 14, background: 'rgba(181,68,52,0.05)', borderLeft: '3px solid var(--cinnabar)', padding: '8px 12px', borderRadius: '0 6px 6px 0', lineHeight: 1.9 }}>{bible.world.visual_style_canonical}</div>}

          <div style={{ height: 16 }} />
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap' }}>
            <button className="btn" disabled={busy || p.refs_status === 'running'}
              onClick={() => act(() => api.post(`/projects/${p.id}/refs`), '定妆照生成已开始（每角色约 20 秒）')}>
              生成全部定妆照
            </button>
            {p.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              定妆照随镜头注入 Seedance 参考图，是人物跨集一致性的视觉锚点（¥0.2/张）
            </span>
          </div>
          {p.refs_status === 'failed' && <div className="error-banner">定妆照生成失败：{'\n'}{p.refs_error}</div>}
          <div className="figure-grid">
            {bible.characters.map((c: Character, i: number) => (
              <div key={c.name} className="figure">
                <div className="f-name">{c.name} <span className="f-role">{c.role}</span>
                  {c.ref_image_url ? <span className="stamp green">已定妆</span> : <span className="stamp grey">未定妆</span>}
                </div>
                {c.ref_image_url && (
                  <img src={c.ref_image_url + `?v=${p.bible_version}`} alt={c.name}
                    style={{ width: '100%', borderRadius: 8, border: '1px solid var(--hairline)', marginBottom: 8 }} />
                )}
                <label className="f">外观锚点串（40~60 字，定稿后锁定）</label>
                {editing
                  ? <textarea rows={3} value={editing.characters[i].appearance_canonical}
                      onChange={e => {
                        const next = { ...editing, characters: [...editing.characters] }
                        next.characters[i] = { ...next.characters[i], appearance_canonical: e.target.value }
                        setEditing(next)
                      }} />
                  : <div className="f-anchor">{c.appearance_canonical}</div>}
                <div className="f-misc">
                  性格：{c.personality}<br />
                  语风：{c.speech_style}<br />
                  {c.relationships.map(r => `${r.relation}→${r.to}`).join('；')}
                </div>
                <PortraitBlock projectId={p.id} character={c} disabled={busy || p.refs_status === 'running'}
                  onChanged={refresh} regenerate={() =>
                    act(() => api.post(`/projects/${p.id}/refs`, { character: c.name }), `正在为「${c.name}」重新定妆`)} />
              </div>
            ))}
          </div>
        </section>
      )}

      {p.bible && (
        <section className="card">
          <h3>分集 <span className="hint">覆盖全书全部 {p.chapters?.length} 章，分批续写，每集 60~90 秒</span></h3>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 }}>
            <button className="btn primary" disabled={busy || p.plan_status === 'running'}
              onClick={() => act(() => api.post(`/projects/${p.id}/plan`))}>
              {p.episodes?.length ? '重新分集' : '开始分集'}
            </button>
            {p.plan_status === 'running' && <span className="stamp gold">分集中（含全书章节摘要，篇幅长时需数分钟）</span>}
          </div>
          {p.plan_status === 'failed' && <div className="error-banner">分集失败：{'\n'}{p.plan_error}</div>}

          {p.episodes?.map(ep => (
            <div key={ep.id} className="episode-row">
              <div className="ep-no">第{numToCn(ep.episode_no)}集</div>
              <div className="ep-body">
                <div className="ep-title">{ep.title}</div>
                <div className="ep-hook">钩子：{ep.hook}</div>
                <div className="ep-syn">{ep.synopsis}</div>
                <div className="ep-hook">尾钩：{ep.cliffhanger}</div>
              </div>
              <div className="ep-side">
                源章 {ep.source_chapters[0]}–{ep.source_chapters[ep.source_chapters.length - 1]}<br />
                目标 {ep.target_duration_s}s · 已耗 ¥{ep.cost_cny.toFixed(1)}<br />
                <EpStamp status={ep.status} /><br />
                <button className="btn small" style={{ marginTop: 4 }} onClick={() => go('board', p.id, ep.id)}>入分镜台 →</button>
              </div>
            </div>
          ))}
        </section>
      )}

      {!!p.key_timeline?.length && (
        <section className="card">
          <h3>全书关键事件线 <span className="hint">防长篇伏笔丢失</span></h3>
          <ol style={{ paddingLeft: 22, fontSize: 13.5, color: 'var(--ink-soft)' }}>
            {p.key_timeline.map((k, i) => <li key={i}>{k}</li>)}
          </ol>
        </section>
      )}
    </>
  )
}

function PortraitBlock({ projectId, character: c, disabled, onChanged, regenerate }: {
  projectId: string; character: Character; disabled: boolean
  onChanged: () => void; regenerate: () => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)  // null=非编辑态
  const [saving, setSaving] = useState(false)
  const isOverridden = !!(c.portrait_prompt_override || '').trim()

  async function save(thenRegen: boolean) {
    setSaving(true)
    try {
      const r = await api.put(`/projects/${projectId}/characters/${encodeURIComponent(c.name)}/portrait`,
        { portrait_prompt: draft ?? '' })
      toast(r.reset_to_default ? `「${c.name}」画像描述已恢复默认` : `「${c.name}」画像描述已保存`)
      setDraft(null); onChanged()
      if (thenRegen) regenerate()
    } catch (e: unknown) { toast((e as Error).message, true) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">画像描述（定妆照生成词）{isOverridden ? ' · 已自定义' : ' · 默认（由画风+锚点串合成）'}</label>
      {draft === null ? (
        <>
          <div className="f-misc" style={{ background: 'rgba(91,114,83,0.06)', borderLeft: '3px solid var(--moss)', padding: '6px 10px', borderRadius: '0 6px 6px 0', fontSize: 12.5 }}>
            {c.portrait_prompt_effective}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={disabled || saving}
              onClick={() => setDraft(c.portrait_prompt_override || c.portrait_prompt_effective || '')}>改画像描述</button>
            <button className="btn small" disabled={disabled || saving} onClick={regenerate}>
              {c.ref_image_url ? '重新定妆' : '单独定妆'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="描述定妆照画面：画风、人物外观、姿态、背景……（10~400 字）" />
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={saving || disabled} onClick={() => save(true)}>保存并重新定妆</button>
            <button className="btn small" disabled={saving} onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving}
              onClick={() => { setDraft(''); }} title="清空后保存即恢复默认">清空</button>}
            <button className="btn small ghost" disabled={saving} onClick={() => setDraft(null)}>放弃</button>
          </div>
        </>
      )}
    </div>
  )
}

export function EpStamp({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    planned: ['待分镜', 'grey'], scripting: ['分镜中', 'gold'], scripted: ['待确认', 'blue'],
    script_failed: ['分镜失败', 'red'], confirmed: ['已确认', 'green'],
    generating: ['生成中', 'gold'], done: ['成片', 'green'],
  }
  const [label, color] = map[status] ?? [status, 'grey']
  return <span className={`stamp ${color}`}>{label}</span>
}
