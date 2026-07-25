import { useEffect, useRef, useState } from 'react'
import { api, Bible, Character, Portrait } from '../api'
import { useNav, useProject } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import QueryState from '../components/QueryState'
import PrepSubnav from '../components/PrepSubnav'
import { useFillPageSize } from '../hooks/useFillPageSize'

export default function BiblePage() {
  const { projectId, toast } = useNav()
  const { data: p, refresh, error, loading } = useProject(projectId!, undefined, 'bible')
  const [editing, setEditing] = useState<Bible | null>(null)
  const [busy, setBusy] = useState(false)
  const [charSearch, setCharSearch] = useState('')
  const [charPage, setCharPage] = useState(0)
  const [paramsCharacterName, setParamsCharacterName] = useState<string | null>(null)
  const [impactOpen, setImpactOpen] = useState(false)
  const pageSize = useFillPageSize({ minCardWidth: 270, rows: 3, floor: 8, ceiling: 24 })
  const bibleTimer = useTaskTimer(`project.${projectId}.bible`, p?.bible_status === 'running')
  const refsTimer = useTaskTimer(`project.${projectId}.refs`, p?.refs_status === 'running')

  const biblePreview = editing ?? p?.bible
  const charQuery = charSearch.trim()
  const indexedCharsPreview = (biblePreview?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredCharsPreview = charQuery
    ? indexedCharsPreview.filter(({ c }) => c.name.includes(charQuery))
    : indexedCharsPreview
  const charPageCount = Math.max(1, Math.ceil(filteredCharsPreview.length / pageSize))

  useEffect(() => {
    if (charPage > charPageCount - 1) setCharPage(Math.max(0, charPageCount - 1))
  }, [charPage, charPageCount])

  if (error && !p) return <QueryState loading={false} error={error} hasData={false}>{null}</QueryState>
  if (!p) return <QueryState loading={loading !== false} error={null} hasData={false}>{null}</QueryState>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const bible = editing ?? p.bible
  // 角色卡分页：按视口列数×行数铺满再分页；保留原始下标 i 供编辑态写回
  const indexedChars = (bible?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredChars = charQuery ? indexedChars.filter(({ c }) => c.name.includes(charQuery)) : indexedChars
  const curCharPage = Math.min(charPage, charPageCount - 1)
  const pagedChars = filteredChars.slice(curCharPage * pageSize, curCharPage * pageSize + pageSize)
  const generating = p.bible_status === 'running' || p.refs_status === 'running'
  const paramsCharacter = paramsCharacterName
    ? bible?.characters.find(character => character.name === paramsCharacterName) ?? null
    : null

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

  const saveBible = async () => {
    if (!editing) return
    await act(async () => {
      const r = await api.put(`/projects/${p.id}/bible`, editing) as {
        style_changed?: boolean; purged?: { versions: number } | null; impact?: ImpactSummary
      }
      setEditing(null)
      toast(r.style_changed
        ? `画风已变更：旧画风定妆照与已生成视频（${r.purged?.versions ?? 0} 个版本）已全部作废，请重新生成定妆照后再生成视频`
        : `人物谱已定稿；${r.impact?.stale_descendant_ids?.length ?? 0} 个下游证据已标记失效`)
    })
  }

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
        <PrepSubnav current="bible" />
        <h1>人物谱 <span className="sub">角色资产与定妆版本中心 · 保持跨镜头、跨分集一致</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>原著 <span className="hint">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count ?? p.chapters?.length ?? 0} 章</span></h3>
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
          {p.bible_evidence && <EvidenceDrawer evidence={p.bible_evidence} label="人物谱证据" />}
          <TaskTimer label="人物谱" timer={bibleTimer} />
          <TaskTimer label="定妆照" timer={refsTimer} />
        </div>
        {!generating && p.bible && (
          <div className="hint" style={{ marginTop: 10 }}>
            已启动过后端持续生成人物谱链路；后续在分镜阶段按集自动判定角色外观是否变化、按需重绘定妆照，前端不再提供打回重生入口。
          </div>
        )}
        {p.bible_status === 'failed' && <div className="error-banner">人物谱生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.bible_error}</div>}
        {p.bible_status === 'warning' && <div className="error-banner">人物谱存在未解决的门禁问题，下游已暂停：{'\n'}{p.bible_error}</div>}
      </section>

      {bible && (
        <section className="card bible-library">
          <h3>世界观
            <span className="hint">era {bible.world.era} · genre {bible.world.genre}</span>
            {!editing
              ? <button className="btn small" style={{ marginLeft: 14 }} onClick={() => setEditing(JSON.parse(JSON.stringify(p.bible)))}>修订</button>
              : <>
                <button className="btn small primary" style={{ marginLeft: 14 }} disabled={busy}
                  onClick={() => setImpactOpen(true)}>定稿</button>
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
            <SearchField value={charSearch} onChange={value => { setCharSearch(value); setCharPage(0) }}
              placeholder="搜索角色名…" ariaLabel="搜索角色" className="library-search" />
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              共 {bible.characters.length} 个角色{charQuery ? ` · 命中 ${filteredChars.length}` : ''}
            </span>
          </div>
          <div className="figure-grid">
            {pagedChars.map(({ c, i }: { c: Character; i: number }) => {
              const fitting = p.refs_status === 'running' && (!p.refs_target || p.refs_target === c.name)
              return (
              <article key={c.name} className="figure character-card">
                <div className="f-name">{c.name} <span className="f-role">{c.role}</span>
                  {fitting ? <span className="stamp gold">定妆中</span>
                    : c.ref_image_url ? <span className="stamp green">已定妆</span> : <span className="stamp grey">未定妆</span>}
                </div>
                {c.ref_image_url && <CharacterPortraitGallery character={c} fitting={fitting} />}
                <label className="f">外观锚点串（40~60 字，定稿后锁定）</label>
                {editing
                  ? <textarea rows={3} value={editing.characters[i].appearance_canonical}
                      onChange={e => {
                        const next = { ...editing, characters: [...editing.characters] }
                        next.characters[i] = { ...next.characters[i], appearance_canonical: e.target.value }
                        setEditing(next)
                      }} />
                  : <div className="f-anchor">{c.appearance_canonical}</div>}
                <div className="asset-params-action">
                  <button className="asset-params-trigger" type="button" onClick={() => setParamsCharacterName(c.name)}>
                    角色设定与生成参数 <span aria-hidden="true">→</span>
                  </button>
                </div>
              </article>
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
        <section className="card world-card">
          <h3>全书关键事件线 <span className="hint">防长篇伏笔丢失</span></h3>
          <ol style={{ paddingLeft: 22, fontSize: 13.5, color: 'var(--ink-soft)' }}>
            {p.key_timeline.map((k, i) => <li key={i}>{k}</li>)}
          </ol>
        </section>
      )}
      <ImpactDialog
        open={impactOpen}
        title="定稿人物谱并传播影响"
        impact={{ requires_reconfirm: true, paid_media_invalidated: true }}
        knownEffects={[
          '下游分集剧本、分镜、参考图与视频可能需要重新生成',
          '精确失效 Artifact 数量将在保存后由服务端计算并回传',
        ]}
        onClose={() => setImpactOpen(false)}
        onConfirm={() => { setImpactOpen(false); void saveBible() }}
      />
      {paramsCharacter && (
        <GenerationParamsDialog
          title={`${paramsCharacter.name} · 角色设定与生成参数`}
          subtitle="查看角色设定、调整定妆照生成词，或重新生成当前角色定妆照。"
          onClose={() => setParamsCharacterName(null)}
        >
          <div className="character-param-summary">
            <div><b>性格</b><p>{paramsCharacter.personality || '未设置'}</p></div>
            <div><b>语风</b><p>{paramsCharacter.speech_style || '未设置'}</p></div>
            <div className="wide"><b>关系</b><p>{paramsCharacter.relationships.length
              ? paramsCharacter.relationships.map(r => `${r.relation}→${r.to}`).join('；')
              : '未设置'}</p></div>
          </div>
          <PortraitBlock projectId={p.id} character={paramsCharacter}
            disabled={busy || p.refs_status === 'running'} onChanged={refresh}
            regenerate={() => act(
              () => api.post(`/projects/${p.id}/refs`, { character: paramsCharacter.name }),
              `正在为「${paramsCharacter.name}」重新定妆`,
            )} />
        </GenerationParamsDialog>
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

function portraitVersionLabel(portrait: Portrait): string {
  if (!portrait.base_portrait_id) {
    return portrait.ep_start > 1 ? `第${portrait.ep_start}集首次定妆` : '初始定妆'
  }
  return `第${portrait.ep_start}集更新`
}

function CharacterPortraitGallery({ character, fitting }: { character: Character; fitting: boolean }) {
  const trackRef = useRef<HTMLDivElement>(null)
  // 当前版本排在第一张；旧图只是定妆版本历史，不代表对应集数已经投入生产。
  const portraits = [...(character.portraits ?? [])]
    .filter(portrait => !!portrait.image_url)
    .sort((a, b) => b.ep_start - a.ep_start)
  const hasVersions = portraits.length > 0
  const count = hasVersions ? portraits.length : 1

  const scroll = (direction: -1 | 1) => {
    const track = trackRef.current
    if (!track) return
    track.scrollBy({ left: direction * track.clientWidth, behavior: 'smooth' })
  }

  return (
    <div className="character-portrait">
      <div ref={trackRef} className="character-portrait-track" aria-label={`${character.name}定妆照版本`}>
        {hasVersions ? portraits.map((portrait, index) => (
          <figure key={portrait.id} className="character-portrait-slide">
            <img src={portrait.image_url!} alt={`${character.name} · ${portraitVersionLabel(portrait)}`}
              style={{ opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
            <figcaption className="portrait-version-label">
              {portraitVersionLabel(portrait)}{index === 0 && portrait.ep_end == null ? <em>当前</em> : null}
            </figcaption>
          </figure>
        )) : (
          <figure className="character-portrait-slide">
            <img src={character.ref_image_url!} alt={character.name}
              style={{ opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
          </figure>
        )}
      </div>
      {count > 1 && (
        <div className="portrait-scroll-controls" aria-label="切换定妆照">
          <span>{count} 张 · 横滑</span>
          <button type="button" onClick={() => scroll(-1)} aria-label="上一张定妆照">‹</button>
          <button type="button" onClick={() => scroll(1)} aria-label="下一张定妆照">›</button>
        </div>
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
