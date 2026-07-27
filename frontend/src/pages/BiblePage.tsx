import { useEffect, useRef, useState } from 'react'
import {
  api, ApiError, Bible, BibleImpactPreview, Character, Portrait, PortraitView, RefsCostPrecheck,
} from '../api'
import { useNav, useProject } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import SearchField from '../components/SearchField'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'
import PaymentConfirmDialog from '../components/PaymentConfirmDialog'
import GenerationParamsDialog from '../components/GenerationParamsDialog'
import QueryState from '../components/QueryState'
import PrepSubnav from '../components/PrepSubnav'
import { useFillPageSize } from '../hooks/useFillPageSize'

const CHAR_QA_PASS = 0.6

function formatBookTitle(name: string): string {
  const trimmed = (name || '').trim()
  if (!trimmed) return '未命名'
  if (trimmed.startsWith('《') && trimmed.endsWith('》')) return trimmed
  return `《${trimmed}》`
}

function currentPortrait(character: Character): Portrait | null {
  const portraits = [...(character.portraits ?? [])]
    .filter(portrait => !!portrait.image_url || (portrait.views ?? []).some(view => !!view.image_url))
    .sort((a, b) => b.ep_start - a.ep_start)
  return portraits.find(p => p.ep_end == null) || portraits[0] || null
}

type PortraitAvailability =
  | 'generating'
  | 'passed'
  | 'warning'
  | 'failed'
  | 'unverified'
  | 'missing'

function portraitAvailability(character: Character, fitting: boolean): PortraitAvailability {
  if (fitting) return 'generating'
  const portrait = currentPortrait(character)
  if (!portrait || (!portrait.image_url && !(portrait.views ?? []).some(v => v.image_url))) {
    return character.ref_image_url ? 'unverified' : 'missing'
  }
  const status = portrait.pack_status
  if (status === 'generating' || status === 'qa_pending') return 'generating'
  if (status === 'failed') return 'failed'
  const qa = portrait.group_qa
  const hard = (qa?.hard_failures ?? []).length > 0 || qa?.status === 'failed'
  if (hard) return 'failed'
  if (status === 'ready' || !status) {
    if (typeof qa?.overall === 'number') {
      if (qa.overall >= CHAR_QA_PASS) {
        return (qa.issues ?? []).length ? 'warning' : 'passed'
      }
      return 'failed'
    }
    return status === 'ready' ? 'unverified' : 'unverified'
  }
  return 'unverified'
}

function availabilityStamp(state: PortraitAvailability): { label: string; color: string } {
  switch (state) {
    case 'generating': return { label: '生成/验证中', color: 'gold' }
    case 'passed': return { label: '已采用且通过', color: 'green' }
    case 'warning': return { label: '已采用但有警告', color: 'gold' }
    case 'failed': return { label: '硬失败/不可用', color: 'red' }
    case 'missing': return { label: '未出图', color: 'grey' }
    default: return { label: '待复核/未验证', color: 'grey' }
  }
}

export default function BiblePage() {
  const { projectId, toast, go } = useNav()
  const { data: p, refresh, error, loading } = useProject(projectId!, undefined, 'bible')
  const [editing, setEditing] = useState<Bible | null>(null)
  const [busy, setBusy] = useState(false)
  const [charSearch, setCharSearch] = useState('')
  const [charPage, setCharPage] = useState(0)
  const [paramsCharacterName, setParamsCharacterName] = useState<string | null>(null)
  const [impactOpen, setImpactOpen] = useState(false)
  const [impactLoading, setImpactLoading] = useState(false)
  const [impactError, setImpactError] = useState<string | null>(null)
  const [impactPreview, setImpactPreview] = useState<BibleImpactPreview | null>(null)
  const [conflict, setConflict] = useState<{
    message: string
    current_version?: number
    character_names?: string[]
  } | null>(null)
  const [payOpen, setPayOpen] = useState(false)
  const [payTitle, setPayTitle] = useState('')
  const [payLoading, setPayLoading] = useState(false)
  const [payError, setPayError] = useState<string | null>(null)
  const [payPrecheck, setPayPrecheck] = useState<RefsCostPrecheck | null>(null)
  const payActionRef = useRef<null | (() => Promise<void>)>(null)
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

  useEffect(() => {
    if (!editing) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [editing])

  if (error && !p) return <QueryState loading={false} error={error} hasData={false}>{null}</QueryState>
  if (!p) return <QueryState loading={loading !== false} error={null} hasData={false}>{null}</QueryState>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const bible = editing ?? p.bible
  const indexedChars = (bible?.characters ?? []).map((c, i) => ({ c, i }))
  const filteredChars = charQuery ? indexedChars.filter(({ c }) => c.name.includes(charQuery)) : indexedChars
  const curCharPage = Math.min(charPage, charPageCount - 1)
  const pagedChars = filteredChars.slice(curCharPage * pageSize, curCharPage * pageSize + pageSize)
  const generating = p.bible_status === 'running' || p.refs_status === 'running'
  const paramsCharacter = paramsCharacterName
    ? bible?.characters.find(character => character.name === paramsCharacterName) ?? null
    : null
  const dirty = !!editing

  const openPayment = async (
    title: string,
    precheckBody: { character?: string; resume?: boolean; view_role?: string },
    action: (quote: RefsCostPrecheck) => Promise<void>,
  ) => {
    setPayTitle(title)
    setPayOpen(true)
    setPayLoading(true)
    setPayError(null)
    setPayPrecheck(null)
    try {
      const quote = await api.refsPrecheck(p.id, precheckBody)
      setPayPrecheck(quote)
      payActionRef.current = async () => {
        await action(quote)
      }
    } catch (e: unknown) {
      setPayError((e as Error).message)
      payActionRef.current = null
    } finally {
      setPayLoading(false)
    }
  }

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
        toast('已停止谱写；已落盘资产保留')
      } else {
        await api.post(`/projects/${p.id}/refs/cancel`)
        toast('已停止定妆；已落盘资产保留')
      }
      refresh()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const retryRefs = async () => {
    await openPayment(
      '补齐缺失的定妆照',
      { resume: true },
      async (quote) => {
        refsTimer.start()
        await api.post(`/projects/${p.id}/refs`, {
          resume: true, confirm: true, quote_id: quote.quote_id,
        })
        toast('已开始补齐缺失的定妆照，已有成品会保留')
        refresh()
      },
    )
  }

  const openImpactPreview = async () => {
    if (!editing) return
    setImpactOpen(true)
    setImpactLoading(true)
    setImpactError(null)
    setImpactPreview(null)
    try {
      const preview = await api.bibleImpactPreview(p.id, {
        bible: editing,
        expected_version: p.bible_version ?? 0,
      })
      setImpactPreview(preview)
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        const detail = e.detail as { code?: string; message?: string; character_names?: string[]; current_version?: number } | undefined
        if (detail?.code === 'BIBLE_VERSION_CONFLICT') {
          setImpactOpen(false)
          setConflict({
            message: detail.message || e.message,
            current_version: detail.current_version,
            character_names: detail.character_names,
          })
          return
        }
      }
      setImpactError((e as Error).message)
    } finally {
      setImpactLoading(false)
    }
  }

  const saveBible = async () => {
    if (!editing || !impactPreview) return
    setBusy(true)
    try {
      const r = await api.put(`/projects/${p.id}/bible`, {
        bible: editing,
        expected_version: p.bible_version ?? 0,
        confirm: true,
        impact_preview_fingerprint: impactPreview.fingerprint,
      }) as {
        style_changed?: boolean
        purged?: { versions: number } | null
        impact?: ImpactSummary
      }
      setEditing(null)
      setImpactOpen(false)
      setImpactPreview(null)
      toast(r.style_changed
        ? `画风已变更：旧画风定妆照与已生成视频（${r.purged?.versions ?? 0} 个版本）已全部作废，请重新生成定妆照后再生成视频`
        : `人物谱已定稿；${r.impact?.stale_descendant_ids?.length ?? 0} 个下游证据已标记失效`)
      refresh()
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 409) {
        const detail = e.detail as {
          code?: string; message?: string; character_names?: string[]; current_version?: number; preview?: BibleImpactPreview
        } | undefined
        if (detail?.code === 'BIBLE_VERSION_CONFLICT') {
          setImpactOpen(false)
          setConflict({
            message: detail.message || e.message,
            current_version: detail.current_version,
            character_names: detail.character_names,
          })
          return
        }
        if (detail?.code === 'IMPACT_PREVIEW_STALE' && detail.preview) {
          setImpactPreview(detail.preview)
          setImpactError('影响预检已过期，已刷新最新结果，请再次确认')
          return
        }
      }
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const abandonEditing = () => {
    if (!dirty) {
      setEditing(null)
      return
    }
    if (window.confirm('有未保存的人物谱修订，确定放弃吗？')) {
      setEditing(null)
    }
  }

  const stopLabel = p.bible_status === 'running' ? '停止谱写' : '停止定妆'

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / {formatBookTitle(p.name)}</div>
        <PrepSubnav current="bible" />
        <h1>人物谱 <span className="sub">角色资产与定妆版本中心 · 保持跨镜头、跨分集一致</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>原著 <span className="hint">{(p.novel_chars / 10000).toFixed(1)} 万字 · {p.chapter_count ?? p.chapters?.length ?? 0} 章</span></h3>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          {!p.bible && !generating && (
            <button className="btn primary" disabled={busy} onClick={startBible}>
              {p.bible_status === 'failed' ? '重新生成人物谱和定妆照' : '开始生成人物谱和定妆照'}
            </button>
          )}
          {p.bible && p.refs_status === 'failed' && !generating && (
            <>
              <button className="btn primary" disabled={busy} onClick={() => void retryRefs()}>
                补齐缺失的定妆照
              </button>
              <button className="btn ghost" disabled={busy} onClick={() => go('episodes', p.id)}>
                暂时跳过，继续分集
              </button>
            </>
          )}
          {generating && (
            <button className="btn ghost" disabled={busy} onClick={stopGeneration}>
              {stopLabel}
            </button>
          )}
          {p.bible_status === 'running' && <span className="stamp gold">谱写中（约 1~3 分钟）</span>}
          {p.refs_status === 'running' && <span className="stamp gold">定妆中</span>}
          {p.bible && <span className="stamp green">第 {`${p.bible_version ?? ''}`} 稿</span>}
          {dirty && <span className="stamp gold">未保存修订</span>}
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
                  onClick={() => void openImpactPreview()}>定稿</button>
                <button className="btn small ghost" style={{ marginLeft: 8 }} onClick={abandonEditing}>放弃</button>
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
              启动后会先为全部角色生成初始定妆照；随后在分镜阶段按集判断角色外观是否相比当前定妆照大变，大变才图生图重绘并切分适用集，新登场重要人物会自动补人物卡并生成定妆照
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
              const portraits = c.portraits ?? []
              const hasPortraitImage = portraits.some(portrait =>
                (!!portrait.image_url || (portrait.views ?? []).some(view => !!view.image_url)),
              )
              const fitting = p.refs_status === 'running' && (
                p.refs_target === c.name
                || (!p.refs_target && portraits.some(portrait => portrait.pack_status === 'generating'))
              )
              const availability = portraitAvailability(c, fitting)
              const stamp = availabilityStamp(availability)
              const active = currentPortrait(c)
              return (
              <article key={c.name} className="figure character-card">
                <div className="f-name">{c.name} <span className="f-role">{c.role}</span>
                  <span className={`stamp ${stamp.color}`}>{stamp.label}</span>
                </div>
                {(c.ref_image_url || hasPortraitImage) && (
                  <CharacterPortraitGallery
                    projectId={p.id}
                    character={c}
                    fitting={fitting}
                    disabled={busy || generating}
                    onChanged={refresh}
                    onPayRequest={openPayment}
                  />
                )}
                {active?.group_qa && (
                  <CharacterQaLine qa={active.group_qa} availability={availability} />
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
        impact={impactPreview}
        loading={impactLoading}
        error={impactError}
        onClose={() => { setImpactOpen(false); setImpactError(null) }}
        onConfirm={() => { void saveBible() }}
      />
      <PaymentConfirmDialog
        open={payOpen}
        title={payTitle}
        precheck={payPrecheck}
        loading={payLoading}
        error={payError}
        onClose={() => { setPayOpen(false); payActionRef.current = null }}
        onConfirm={() => {
          const run = payActionRef.current
          setPayOpen(false)
          if (!run) return
          void act(run)
        }}
      />
      {conflict && (
        <div className="evidence-backdrop" role="presentation">
          <section className="impact-dialog" role="dialog" aria-modal="true" aria-label="版本冲突">
            <h3>人物谱版本冲突</h3>
            <p>{conflict.message}</p>
            {typeof conflict.current_version === 'number' && (
              <p>服务端当前版本：第 {conflict.current_version} 稿</p>
            )}
            {!!conflict.character_names?.length && (
              <p>服务端角色：{conflict.character_names.join('、')}</p>
            )}
            <p>请刷新后重新修订；禁止用旧页面静默覆盖。</p>
            <div className="dialog-actions">
              <button type="button" className="btn" onClick={() => setConflict(null)}>留下继续查看</button>
              <button type="button" className="btn primary" onClick={() => {
                setConflict(null)
                setEditing(null)
                refresh()
              }}>刷新并放弃本地修改</button>
            </div>
          </section>
        </div>
      )}
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
            regenerate={() => openPayment(
              `重新生成「${paramsCharacter.name}」造型包`,
              { character: paramsCharacter.name },
              async (quote) => {
                await api.post(`/projects/${p.id}/refs`, {
                  character: paramsCharacter.name,
                  confirm: true,
                  quote_id: quote.quote_id,
                })
                toast(`正在为「${paramsCharacter.name}」重新定妆`)
                refresh()
              },
            )} />
        </GenerationParamsDialog>
      )}
    </>
  )
}

function CharacterQaLine({
  qa, availability,
}: {
  qa: NonNullable<Portrait['group_qa']>
  availability: PortraitAvailability
}) {
  const overall = qa.overall
  const issues = [...(qa.hard_failures ?? []), ...(qa.issues ?? [])]
  const color = availability === 'passed' ? 'var(--moss)'
    : availability === 'warning' ? 'var(--gold, #b8860b)'
      : availability === 'failed' ? 'var(--cinnabar)' : 'var(--ink-faint)'
  return (
    <div className="scene-qa-line" style={{ marginBottom: 8 }}>
      <span>整包 QA：{typeof overall === 'number'
        ? <b style={{ color }}>{overall.toFixed(2)}</b>
        : <b style={{ color }}>未验证</b>}
      </span>
      {issues.length ? <span style={{ color: 'var(--ink-faint)' }}>　{issues.slice(0, 2).join('；')}</span> : null}
    </div>
  )
}

function PortraitBlock({ projectId, character: c, disabled, onChanged, regenerate }: {
  projectId: string; character: Character; disabled: boolean
  onChanged: () => void; regenerate: () => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const isOverridden = !!(c.portrait_prompt_override || '').trim()
  const draftLen = (draft ?? '').trim().length
  const draftValid = draft !== null && (draftLen === 0 || (draftLen >= 10 && draftLen <= 400))

  async function save(thenRegen: boolean, value?: string) {
    setSaving(true)
    try {
      const text = value ?? draft ?? ''
      const r = await api.put(`/projects/${projectId}/characters/${encodeURIComponent(c.name)}/portrait`,
        { portrait_prompt: text })
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
              {c.ref_image_url ? '重新生成当前造型包' : '单独生成造型包'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="描述定妆照画面：画风、人物外观、姿态、背景……（10~400 字）" />
          <div style={{ fontSize: 12, color: draftValid ? 'var(--ink-faint)' : 'var(--cinnabar)', marginTop: 4 }}>
            {draftLen === 0 ? '留空并保存将恢复默认' : `${draftLen} / 10~400 字`}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={saving || disabled || !draftValid || draftLen === 0}
              onClick={() => save(true)}>保存并重新定妆</button>
            <button className="btn small" disabled={saving || !draftValid} onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving}
              onClick={() => {
                if (!window.confirm('确认恢复默认画像描述？不会触发生成或扣费。')) return
                void save(false, '')
              }}>恢复默认</button>}
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

const VIEW_ROLE_LABELS: Record<string, string> = {
  front_full: '正面全身',
  three_quarter: '3/4 面',
  profile: '侧面',
  back_full: '背面全身',
  face_closeup: '面部特写',
}

type PortraitSlide = {
  key: string
  imageUrl: string
  portrait: Portrait | null
  versionIndex: number
  view: PortraitView | null
}

function CharacterPortraitGallery({ projectId, character, fitting, disabled, onChanged, onPayRequest }: {
  projectId: string
  character: Character
  fitting: boolean
  disabled?: boolean
  onChanged?: () => void
  onPayRequest: (
    title: string,
    precheckBody: { character?: string; resume?: boolean; view_role?: string },
    action: (quote: RefsCostPrecheck) => Promise<void>,
  ) => Promise<void>
}) {
  const { toast } = useNav()
  const trackRef = useRef<HTMLDivElement>(null)
  const [redoing, setRedoing] = useState<string | null>(null)
  const portraits = [...(character.portraits ?? [])]
    .filter(portrait =>
      (!!portrait.image_url || (portrait.views ?? []).some(v => v.image_url)),
    )
    .sort((a, b) => b.ep_start - a.ep_start)
  const slides: PortraitSlide[] = []
  portraits.forEach((portrait, versionIndex) => {
    const views = (portrait.views ?? []).filter(view => !!view.image_url)
    if (views.length) {
      slides.push(...views.map(view => ({
        key: `${portrait.id}:${view.id}`,
        imageUrl: view.image_url!,
        portrait,
        versionIndex,
        view,
      })))
      return
    }
    if (portrait.image_url) {
      slides.push({
        key: portrait.id || `${portrait.ep_start}:${portrait.image_url}`,
        imageUrl: portrait.image_url,
        portrait,
        versionIndex,
        view: null,
      })
    }
  })
  if (!slides.length && character.ref_image_url) {
    slides.push({
      key: `${character.name}:legacy`,
      imageUrl: character.ref_image_url,
      portrait: null,
      versionIndex: 0,
      view: null,
    })
  }
  const count = slides.length

  const scroll = (direction: -1 | 1) => {
    const track = trackRef.current
    if (!track) return
    track.scrollBy({ left: direction * track.clientWidth, behavior: 'smooth' })
  }

  const redoView = async (portraitId: string, viewRole: string) => {
    const label = VIEW_ROLE_LABELS[viewRole] || viewRole
    await onPayRequest(
      `重做「${character.name}」的${label}视角`,
      { character: character.name, view_role: viewRole },
      async (quote) => {
        setRedoing(`${portraitId}:${viewRole}`)
        try {
          const result = await api.regenerateCharacterView(
            projectId, character.name, portraitId, viewRole,
            { confirm: true, quote_id: quote.quote_id },
          ) as { status?: string; run_id?: string; message?: string }
          toast(result?.status === 'accepted'
            ? `${label}视角重做已受理，可刷新查看进度`
            : `${label}视角已重做`)
          onChanged?.()
        } finally {
          setRedoing(null)
        }
      },
    )
  }

  return (
    <div className="character-portrait">
      <div ref={trackRef} className="character-portrait-track" aria-label={`${character.name}定妆照版本`}>
        {slides.map(({ key, imageUrl, portrait, versionIndex, view }, index) => (
          <figure key={key} className="character-portrait-slide">
            <img src={imageUrl}
              alt={`${character.name} · ${portrait ? portraitVersionLabel(portrait) : '定妆照'} · ${VIEW_ROLE_LABELS[view?.view_role || ''] || view?.view_role || '正面'}`}
              style={{ opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
            {portrait && <figcaption className="portrait-version-label">
              <span>
                {index + 1}/{count} · {portraitVersionLabel(portrait)} · {VIEW_ROLE_LABELS[view?.view_role || ''] || view?.view_role || '正面'}
                {portrait.ep_end != null
                  ? ` · 第${portrait.ep_start}-${portrait.ep_end}集`
                  : ` · 第${portrait.ep_start}集起`}
              </span>
              {versionIndex === 0 && portrait.ep_end == null ? <em>当前</em> : null}
            </figcaption>}
            {portrait && versionIndex === 0 && portrait.ep_end == null && view?.view_role && (
              <button
                type="button"
                className="portrait-view-redo"
                disabled={disabled || !!redoing}
                onClick={() => void redoView(portrait.id!, view.view_role!)}
              >
                {redoing === `${portrait.id}:${view.view_role}` ? '受理中…' : '重做当前视角'}
              </button>
            )}
            {portrait?.change?.reason && (
              <div className="f-misc" style={{ fontSize: 11, padding: '4px 6px' }}>
                变化：{(portrait.change.change_dimensions || []).join('/') || '外观'} · {portrait.change.reason}
              </div>
            )}
          </figure>
        ))}
      </div>
      {count > 1 && (
        <div className="portrait-scroll-controls" aria-label="切换定妆照">
          <span>{count} 张视角图 · 横滑</span>
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
