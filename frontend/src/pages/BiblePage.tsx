import { type CSSProperties, useCallback, useEffect, useState } from 'react'
import { api, AutoStatus, Bible, BrowseResult, Character, Portrait } from '../api'
import { useNav, useProject, usePoll } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

const CHAR_PAGE_SIZE = 6  // 人物谱每页展示的角色卡数

export default function BiblePage() {
  const { projectId, toast } = useNav()
  const { data: p, refresh } = useProject(projectId!)
  const { data: auto, refresh: refreshAuto } = usePoll<AutoStatus>(
    () => api.get(`/projects/${projectId}/auto/status`), 3000, [projectId])
  const [editing, setEditing] = useState<Bible | null>(null)
  const [busy, setBusy] = useState(false)
  const [charSearch, setCharSearch] = useState('')
  const [charPage, setCharPage] = useState(0)
  const bibleTimer = useTaskTimer(`project.${projectId}.bible`, p?.bible_status === 'running')
  const refsTimer = useTaskTimer(`project.${projectId}.refs`, p?.refs_status === 'running')

  if (!p) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const bible = editing ?? p.bible
  // 角色卡分页（每页 6 张）+ 按名检索；保留原始下标 i 供编辑态写回 editing.characters[i]
  const charQuery = charSearch.trim()
  const indexedChars = (bible?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredChars = charQuery ? indexedChars.filter(({ c }) => c.name.includes(charQuery)) : indexedChars
  const charPageCount = Math.max(1, Math.ceil(filteredChars.length / CHAR_PAGE_SIZE))
  const curCharPage = Math.min(charPage, charPageCount - 1)
  const pagedChars = filteredChars.slice(curCharPage * CHAR_PAGE_SIZE, curCharPage * CHAR_PAGE_SIZE + CHAR_PAGE_SIZE)
  const generating = p.bible_status === 'running' || p.refs_status === 'running'

  const startBible = async () => {
    bibleTimer.start()
    setBusy(true)
    try {
      await api.post(`/projects/${p.id}/bible`)
      toast('人物谱与定妆照生成已开始')
      refresh()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const stopGeneration = async () => {
    setBusy(true)
    try {
      if (p.bible_status === 'running') {
        await api.post(`/projects/${p.id}/bible/cancel`)
      } else {
        await api.post(`/projects/${p.id}/refs/cancel`)
      }
      toast('已停止当前人物谱/定妆照生成')
      refresh()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

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
          {!p.bible && !generating && (
            <button className="btn primary" disabled={busy} onClick={startBible}>
              开始生成人物谱和定妆照
            </button>
          )}
          {generating && (
            <button className="btn ghost" disabled={busy} onClick={stopGeneration}>
              停止
            </button>
          )}
          {p.bible_status === 'running' && <span className="stamp gold">谱写中（约 1~3 分钟）</span>}
          {p.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
          {p.bible && <span className="stamp green">第 {`${p.bible_version ?? ''}`} 稿</span>}
          <TaskTimer label="人物谱" timer={bibleTimer} />
          <TaskTimer label="定妆照" timer={refsTimer} />
        </div>
        {!generating && p.bible && (
          <div className="hint" style={{ marginTop: 10 }}>
            已启动过后端持续生成人物谱链路；后续在分镜阶段按集自动判定角色外观是否变化、按需重绘定妆照，前端不再提供打回重生入口。
          </div>
        )}
        {p.bible_status === 'failed' && <div className="error-banner">人物谱生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.bible_error}</div>}
      </section>

      <AutoCard projectId={p.id} auto={auto} busy={busy}
        onStart={async (exportDir: string) => {
          setBusy(true)
          try { await api.post(`/projects/${p.id}/auto`, { export_dir: exportDir }); toast('已启动一键全自动成片，可离开页面，进度在此实时显示'); refreshAuto() }
          catch (e: unknown) { toast((e as Error).message, true) }
          finally { setBusy(false) }
        }}
        onCancel={async () => {
          setBusy(true)
          try { await api.post(`/projects/${p.id}/auto/cancel`); toast('已请求停止（已入队的镜头会继续跑完）'); refreshAuto() }
          catch (e: unknown) { toast((e as Error).message, true) }
          finally { setBusy(false) }
        }} />

      {bible && (
        <section className="card">
          <h3>世界观
            <span className="hint">era {bible.world.era} · genre {bible.world.genre}</span>
            {!editing
              ? <button className="btn small" style={{ marginLeft: 14 }} onClick={() => setEditing(JSON.parse(JSON.stringify(p.bible)))}>修订</button>
              : <>
                <button className="btn small primary" style={{ marginLeft: 14 }} disabled={busy}
                  onClick={() => act(async () => {
                    const r = await api.put(`/projects/${p.id}/bible`, editing) as { style_changed?: boolean; purged?: { versions: number } | null }
                    setEditing(null)
                    toast(r.style_changed
                      ? `画风已变更：旧画风定妆照与已生成视频（${r.purged?.versions ?? 0} 个版本）已全部作废，请重新生成定妆照后再生成视频`
                      : '人物谱已定稿，版本 +1')
                  })}>定稿</button>
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
            {p.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              启动后会先为全部角色生成初始定妆照；随后在分镜阶段按集判断角色外观是否相比当前定妆照大变，大变才图生图重绘并切分适用集，新登场重要人物会自动补人物卡并生成定妆照（¥0.2/张）
            </span>
          </div>
          {p.refs_status === 'failed' && <div className="error-banner">定妆照生成失败：{'\n'}{p.refs_error}</div>}
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 12px' }}>
            <input type="text" value={charSearch}
              onChange={e => { setCharSearch(e.target.value); setCharPage(0) }}
              placeholder="搜索角色名…"
              style={{ flex: '0 1 240px', fontSize: 13, padding: '6px 10px' }} />
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              共 {bible.characters.length} 个角色{charQuery ? ` · 命中 ${filteredChars.length}` : ''}
            </span>
          </div>
          <div className="figure-grid">
            {pagedChars.map(({ c, i }: { c: Character; i: number }) => {
              const fitting = p.refs_status === 'running' && (!p.refs_target || p.refs_target === c.name)
              return (
              <div key={c.name} className="figure">
                <div className="f-name">{c.name} <span className="f-role">{c.role}</span>
                  {fitting ? <span className="stamp gold">定妆中</span>
                    : c.ref_image_url ? <span className="stamp green">已定妆</span> : <span className="stamp grey">未定妆</span>}
                </div>
                {c.ref_image_url && (
                  <img src={c.ref_image_url} alt={c.name}
                    style={{ width: '100%', borderRadius: 8, border: '1px solid var(--hairline)', marginBottom: 8,
                             opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
                )}
                {(c.portraits?.length ?? 0) > 1 && <PortraitStrip portraits={c.portraits!} />}
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
            )})}
          </div>
          {!pagedChars.length && (
            <div className="empty">{charQuery ? `没有匹配「${charQuery}」的角色` : '暂无角色'}</div>
          )}
          {charPageCount > 1 && (
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'center', marginTop: 14 }}>
              <button className="btn small" disabled={curCharPage <= 0} onClick={() => setCharPage(curCharPage - 1)}>← 上一页</button>
              <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>第 {curCharPage + 1} / {charPageCount} 页</span>
              <button className="btn small" disabled={curCharPage >= charPageCount - 1} onClick={() => setCharPage(curCharPage + 1)}>下一页 →</button>
            </div>
          )}
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

function DirPicker({ initial, onPick, onClose }: {
  initial: string; onPick: (p: string) => void; onClose: () => void
}) {
  const { toast } = useNav()
  const [data, setData] = useState<BrowseResult | null>(null)
  const [cur, setCur] = useState('')
  const [newName, setNewName] = useState('')
  const [loading, setLoading] = useState(false)

  const load = useCallback((path: string) => {
    setLoading(true)
    api.get(`/system/browse?path=${encodeURIComponent(path)}`)
      .then((d: BrowseResult) => { setData(d); setCur(d.path) })
      .catch((e: Error) => toast(e.message, true))
      .finally(() => setLoading(false))
  }, [toast])

  // 从初始目录的父级开始浏览（这样能直接看到并改选同级目录）；没有则从盘符/根开始
  useEffect(() => { load(initial || '') }, [load, initial])

  const mkdir = async () => {
    const name = newName.trim()
    if (!name || !cur) return
    try {
      const r = await api.post('/system/mkdir', { path: cur, name }) as { path: string }
      setNewName(''); toast('已创建文件夹'); load(r.path)
    } catch (e: unknown) { toast((e as Error).message, true) }
  }

  const overlay: CSSProperties = {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', zIndex: 1000,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  }
  const panel: CSSProperties = {
    width: 'min(640px, 92vw)', maxHeight: '82vh', background: 'var(--paper, #faf7f0)',
    borderRadius: 10, boxShadow: '0 12px 40px rgba(0,0,0,0.3)', display: 'flex',
    flexDirection: 'column', overflow: 'hidden',
  }

  return (
    <div style={overlay} onClick={onClose}>
      <div style={panel} onClick={e => e.stopPropagation()}>
        <div style={{ padding: '14px 18px', borderBottom: '1px solid var(--hairline, #e5e0d5)' }}>
          <h3 style={{ margin: 0 }}>选择成片导出目录</h3>
          <div style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginTop: 4, fontFamily: 'ui-monospace, monospace',
                        wordBreak: 'break-all' }}>
            当前：{cur || '（盘符 / 根目录）'}
          </div>
        </div>

        <div style={{ padding: '10px 18px', display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center',
                      borderBottom: '1px solid var(--hairline, #e5e0d5)' }}>
          {data?.drives?.map(d => (
            <button key={d} className="btn small" onClick={() => load(d)}>{d}</button>
          ))}
          {data?.parent != null && <button className="btn small" onClick={() => load(data.parent!)}>⬆ 上级</button>}
          {data?.parent == null && data?.drives?.length ? <span style={{ fontSize: 12, color: 'var(--ink-faint)' }}>（已在盘符列表）</span> : null}
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '6px 0', minHeight: 180 }}>
          {loading && <div style={{ padding: '12px 18px', color: 'var(--ink-faint)' }}>读取中……</div>}
          {!loading && data?.dirs?.length === 0 && (
            <div style={{ padding: '12px 18px', color: 'var(--ink-faint)' }}>此目录下没有子文件夹（可直接「选定此目录」或新建）</div>
          )}
          {!loading && data?.dirs?.map(d => (
            <button key={d.path} onClick={() => load(d.path)}
              style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px 18px', border: 'none',
                       background: 'transparent', cursor: 'pointer', fontSize: 13.5, color: 'var(--ink, #333)' }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(181,68,52,0.07)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
              📁 {d.name}
            </button>
          ))}
        </div>

        <div style={{ padding: '10px 18px', borderTop: '1px solid var(--hairline, #e5e0d5)', display: 'flex', gap: 8 }}>
          <input type="text" value={newName} onChange={e => setNewName(e.target.value)}
            placeholder="新建文件夹名" disabled={!cur}
            onKeyDown={e => { if (e.key === 'Enter') mkdir() }}
            style={{ flex: 1, fontSize: 13 }} />
          <button className="btn small" disabled={!cur || !newName.trim()} onClick={mkdir}>新建</button>
        </div>

        <div style={{ padding: '12px 18px', borderTop: '1px solid var(--hairline, #e5e0d5)', display: 'flex',
                      gap: 10, justifyContent: 'flex-end' }}>
          <button className="btn ghost" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={!cur} onClick={() => onPick(cur)}>选定此目录</button>
        </div>
      </div>
    </div>
  )
}

function AutoCard({ projectId, auto, busy, onStart, onCancel }: {
  projectId: string; auto: AutoStatus | null; busy: boolean
  onStart: (exportDir: string) => void; onCancel: () => void
}) {
  void projectId
  const running = !!auto?.running
  const pr = auto?.progress
  const autoTimer = useTaskTimer(`project.${projectId}.auto`, running)
  const stat = (s?: string) => (s === 'ready' ? '✓' : s === 'running' ? '…' : s === 'failed' ? '✗' : '—')
  // 导出目录：用户未编辑时显示服务端记忆值；一旦选择/输入则以其为准（避免每 3 秒轮询把它冲掉）
  const [dir, setDir] = useState<string | null>(null)
  const [picking, setPicking] = useState(false)
  const dirValue = dir ?? auto?.export_dir ?? ''
  return (
    <section className="card" style={{ borderLeft: '3px solid var(--cinnabar)' }}>
      <h3>一键全自动成片
        <span className="hint">人物谱 → 定妆照+分集 → 每集（剧本→分镜→自动确认→参考图视频）→ 合成导出 · 自动跳过已完成步骤</span>
      </h3>

      <label className="f">成片导出目录</label>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 4 }}>
        <button className="btn" disabled={running} onClick={() => setPicking(true)}>
          {dirValue ? '更换目录' : '选择目录'}
        </button>
        {dirValue ? (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '5px 10px',
                         background: 'rgba(91,114,83,0.08)', borderRadius: 6, fontSize: 12.5,
                         fontFamily: 'ui-monospace, monospace', color: 'var(--ink-soft)',
                         maxWidth: '100%', overflow: 'hidden' }}>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>📁 {dirValue}</span>
            {!running && (
              <button onClick={() => setDir('')} title="清除"
                style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--ink-faint)',
                         fontSize: 14, lineHeight: 1, padding: 0 }}>✕</button>
            )}
          </span>
        ) : (
          <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>未选择 · 仅在成片台生成，不另存到外部文件夹</span>
        )}
      </div>
      <p style={{ fontSize: 12, color: 'var(--ink-faint)', margin: '0 0 14px' }}>
        每集合成后另存为「书名第N集.mp4」，同名已存在则跳过。
      </p>
      {picking && (
        <DirPicker initial={dirValue}
          onClose={() => setPicking(false)}
          onPick={(p) => { setDir(p); setPicking(false) }} />
      )}

      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn primary" disabled={busy || running} onClick={() => {
          autoTimer.start()
          onStart(dirValue.trim())
        }}>
          {running ? '自动成片进行中…' : '一键全自动成片'}
        </button>
        {running && <button className="btn ghost" disabled={busy} onClick={onCancel}>停止</button>}
        {auto?.phase && <span className={`stamp ${running ? 'gold' : auto.error ? 'red' : 'green'}`}>{auto.phase}</span>}
        <TaskTimer label="自动成片" timer={autoTimer} />
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--ink-faint)', marginTop: 8 }}>
        视频是花钱环节（¥0.8/秒），自动化会跳过人工确认直接出片；每集设有成本上限，触顶则该集暂停并在日志报红，其余集继续。
      </p>

      {auto?.error && <div className="error-banner">自动成片中断（原始错误，不做静默兜底）：{'\n'}{auto.error}</div>}

      {pr && (pr.episodes_total || pr.shots_total) ? (
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', margin: '12px 0', fontSize: 13 }}>
          <span>人物谱 {stat(pr.bible)}</span>
          <span>定妆照 {stat(pr.refs)}</span>
          <span>分集 {stat(pr.plan)}</span>
          <span>剧集 {pr.episodes_done ?? 0}/{pr.episodes_total ?? 0} 成片</span>
          <span>剧本 {pr.screenplays_ready ?? 0}/{pr.episodes_total ?? 0} 集</span>
          <span>视频 {pr.shots_video ?? 0}/{pr.shots_total ?? 0} 镜</span>
        </div>
      ) : null}

      {auto?.log?.length ? (
        <div style={{ maxHeight: 220, overflowY: 'auto', background: 'rgba(0,0,0,0.03)', borderRadius: 6,
                      padding: '8px 12px', fontSize: 12, lineHeight: 1.8, fontFamily: 'ui-monospace, monospace' }}>
          {auto.log.slice().reverse().map((l, i) => (
            <div key={i} style={{ color: /失败|中断|跳过|暂停|报红|无法/.test(l.msg) ? 'var(--cinnabar)' : 'var(--ink-soft)' }}>
              {new Date(l.t * 1000).toLocaleTimeString()} · {l.msg}
            </div>
          ))}
        </div>
      ) : null}
    </section>
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

function portraitRangeLabel(start: number, end: number | null): string {
  if (end == null) return `第${start}集起`
  return start === end ? `第${start}集` : `第${start}~${end}集`
}

function PortraitStrip({ portraits }: { portraits: Portrait[] }) {
  const sorted = [...portraits].sort((a, b) => a.ep_start - b.ep_start)
  return (
    <div style={{ margin: '2px 0 8px' }}>
      <label className="f">定妆照分段（按适用集横向预览）</label>
      <div style={{ display: 'flex', gap: 8, overflowX: 'auto', paddingBottom: 4 }}>
        {sorted.map(pt => (
          <div key={pt.id} style={{ flex: '0 0 auto', width: 92, textAlign: 'center' }}>
            {pt.image_url
              ? <img src={pt.image_url} alt={portraitRangeLabel(pt.ep_start, pt.ep_end)}
                  style={{ width: 92, height: 164, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hairline)' }} />
              : <div style={{ width: 92, height: 164, borderRadius: 6, border: '1px dashed var(--hairline)',
                              display: 'flex', alignItems: 'center', justifyContent: 'center',
                              fontSize: 11, color: 'var(--ink-faint)' }}>无图</div>}
            <div style={{ fontSize: 11, color: 'var(--ink-soft)', marginTop: 3 }}>{portraitRangeLabel(pt.ep_start, pt.ep_end)}</div>
          </div>
        ))}
      </div>
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
