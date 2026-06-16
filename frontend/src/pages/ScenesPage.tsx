import { useState } from 'react'
import { api, Scene, SceneRefSegment } from '../api'
import { useNav, useProject } from '../App'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

const SCENE_PAGE_SIZE = 6

export default function ScenesPage() {
  const { projectId, toast } = useNav()
  const { data: p, refresh } = useProject(projectId!)
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const sceneTimer = useTaskTimer(`project.${projectId}.scene_refs`, p?.scene_refs_status === 'running')

  if (!p) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try { await fn(); if (doneMsg) toast(doneMsg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const scenes = p.bible?.scenes ?? []
  const query = search.trim()
  const filtered = query ? scenes.filter(s => s.name.includes(query)) : scenes
  const pageCount = Math.max(1, Math.ceil(filtered.length / SCENE_PAGE_SIZE))
  const curPage = Math.min(page, pageCount - 1)
  const paged = filtered.slice(curPage * SCENE_PAGE_SIZE, curPage * SCENE_PAGE_SIZE + SCENE_PAGE_SIZE)
  const generating = p.scene_refs_status === 'running'
  const hasBible = !!p.bible

  return (
    <>
      <header className="desk-head">
        <div className="crumb">书房 / 《{p.name}》</div>
        <h1>场景图 <span className="sub">场景锚点一旦定稿，同场景所有镜头、跨集逐字复用同一张场景图，保持场景一致</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <h3>场景图素材库
          <span className="hint">从原文提取的规范场景 · 分镜的场景必须落在库内 · ¥0.2/张</span>
        </h3>
        {!hasBible && (
          <div className="hint">请先到「人物谱」生成角色圣经；场景圣经会在人物谱定稿后自动生成。</div>
        )}
        {hasBible && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            {!scenes.length && !generating && (
              <button className="btn primary" disabled={busy}
                onClick={() => act(() => api.genSceneBible(p.id), '已开始生成场景圣经与场景图')}>
                生成场景圣经与场景图
              </button>
            )}
            {scenes.length > 0 && !generating && (
              <button className="btn" disabled={busy}
                onClick={() => act(() => api.genSceneRefs(p.id), '已开始重新生成全部场景图')}>
                重新生成全部场景图
              </button>
            )}
            {scenes.length > 0 && !generating && (
              <button className="btn ghost" disabled={busy}
                onClick={() => act(() => api.genSceneBible(p.id), '已重新提取场景清单并出图')}>
                重新提取场景清单
              </button>
            )}
            {generating && (
              <button className="btn ghost" disabled={busy}
                onClick={() => act(() => api.cancelSceneRefs(p.id), '已停止场景图生成')}>
                停止
              </button>
            )}
            {generating && <span className="stamp gold">生成中</span>}
            {scenes.length > 0 && <span className="stamp green">{scenes.length} 个场景</span>}
            <TaskTimer label="场景图" timer={sceneTimer} />
          </div>
        )}
        {p.scene_refs_status === 'failed' && (
          <div className="error-banner">场景图生成失败（原始错误如下，不做静默兜底）：{'\n'}{p.scene_refs_error}</div>
        )}
        {scenes.length > 0 && (
          <div className="hint" style={{ marginTop: 10 }}>
            分镜阶段会自动把每个镜头的场景收敛到这些规范场景之一；剧本里出现、库里没有且戏份足够的新场景会在分镜前自动补入库并出图。
          </div>
        )}
      </section>

      {scenes.length > 0 && (
        <section className="card">
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 12px' }}>
            <input type="text" value={search}
              onChange={e => { setSearch(e.target.value); setPage(0) }}
              placeholder="搜索场景名…"
              style={{ flex: '0 1 240px', fontSize: 13, padding: '6px 10px' }} />
            <span style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>
              共 {scenes.length} 个场景{query ? ` · 命中 ${filtered.length}` : ''}
            </span>
          </div>
          <div className="figure-grid">
            {paged.map(s => {
              const fitting = generating && (!p.scene_refs_target || p.scene_refs_target === s.name)
              const qaOverall = s.scene_refs?.[0]?.qa_overall
              return (
                <div key={s.name} className="figure">
                  <div className="f-name">{s.name}
                    {s.location_kind ? <span className="f-role">{s.location_kind}</span> : null}
                    {fitting ? <span className="stamp gold">生成中</span>
                      : s.ref_image_url ? <span className="stamp green">已出图</span> : <span className="stamp grey">未出图</span>}
                  </div>
                  {s.ref_image_url && (
                    <img src={s.ref_image_url} alt={s.name}
                      style={{ width: '100%', borderRadius: 8, border: '1px solid var(--hairline)', marginBottom: 8,
                               opacity: fitting ? 0.45 : 1, transition: 'opacity 0.3s' }} />
                  )}
                  {typeof qaOverall === 'number' && (
                    <QaLine overall={qaOverall} issues={s.scene_refs?.[0]?.qa?.issues} />
                  )}
                  {(s.scene_refs?.length ?? 0) > 1 && <SceneRefStrip segments={s.scene_refs!} />}
                  <label className="f">场景锚点串（30~60 字，定稿后锁定）</label>
                  <div className="f-anchor">{s.scene_canonical}</div>
                  <ScenePromptBlock projectId={p.id} scene={s} disabled={busy || generating}
                    onChanged={refresh}
                    regenerate={() => act(() => api.genSceneRefs(p.id, s.name), `正在为「${s.name}」重新出图`)} />
                </div>
              )
            })}
          </div>
          {!paged.length && (
            <div className="empty">{query ? `没有匹配「${query}」的场景` : '暂无场景'}</div>
          )}
          {pageCount > 1 && (
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', justifyContent: 'center', marginTop: 14 }}>
              <button className="btn small" disabled={curPage <= 0} onClick={() => setPage(curPage - 1)}>← 上一页</button>
              <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>第 {curPage + 1} / {pageCount} 页</span>
              <button className="btn small" disabled={curPage >= pageCount - 1} onClick={() => setPage(curPage + 1)}>下一页 →</button>
            </div>
          )}
        </section>
      )}
    </>
  )
}

function QaLine({ overall, issues }: { overall: number; issues?: string[] }) {
  const color = overall >= 0.75 ? 'var(--moss)' : overall >= 0.6 ? 'var(--gold, #b8860b)' : 'var(--cinnabar)'
  return (
    <div style={{ fontSize: 12, color: 'var(--ink-soft)', margin: '2px 0 8px' }}>
      <span>QA：<b style={{ color }}>{overall.toFixed(2)}</b></span>
      {issues?.length ? <span style={{ color: 'var(--ink-faint)' }}>　{issues.slice(0, 2).join('；')}</span> : null}
    </div>
  )
}

function sceneRangeLabel(start: number, end: number | null): string {
  if (end == null) return `第${start}集起`
  return start === end ? `第${start}集` : `第${start}~${end}集`
}

function SceneRefStrip({ segments }: { segments: SceneRefSegment[] }) {
  const sorted = [...segments].sort((a, b) => a.ep_start - b.ep_start)
  return (
    <div style={{ margin: '2px 0 8px' }}>
      <label className="f">场景图分段（按适用集横向预览）</label>
      <div style={{ display: 'flex', gap: 8, overflowX: 'auto', paddingBottom: 4 }}>
        {sorted.map((seg, i) => (
          <div key={i} style={{ flex: '0 0 auto', width: 104, textAlign: 'center' }}>
            {seg.image_url
              ? <img src={seg.image_url} alt={sceneRangeLabel(seg.ep_start, seg.ep_end)}
                  style={{ width: 104, height: 184, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hairline)' }} />
              : <div style={{ width: 104, height: 184, borderRadius: 6, border: '1px dashed var(--hairline)',
                              display: 'flex', alignItems: 'center', justifyContent: 'center',
                              fontSize: 11, color: 'var(--ink-faint)' }}>无图</div>}
            <div style={{ fontSize: 11, color: 'var(--ink-soft)', marginTop: 3 }}>{sceneRangeLabel(seg.ep_start, seg.ep_end)}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ScenePromptBlock({ projectId, scene: s, disabled, onChanged, regenerate }: {
  projectId: string; scene: Scene; disabled: boolean
  onChanged: () => void; regenerate: () => void
}) {
  const { toast } = useNav()
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const isOverridden = !!(s.scene_prompt_override || '').trim()

  async function save(thenRegen: boolean) {
    setSaving(true)
    try {
      const r = await api.editScenePrompt(projectId, s.name, draft ?? '')
      toast(r.reset_to_default ? `「${s.name}」场景图描述已恢复默认` : `「${s.name}」场景图描述已保存`)
      setDraft(null); onChanged()
      if (thenRegen) regenerate()
    } catch (e: unknown) { toast((e as Error).message, true) }
    finally { setSaving(false) }
  }

  return (
    <div style={{ marginTop: 10 }}>
      <label className="f">场景图描述（生成词）{isOverridden ? ' · 已自定义' : ' · 默认（由画风+锚点串合成）'}</label>
      {draft === null ? (
        <>
          <div className="f-misc" style={{ background: 'rgba(91,114,83,0.06)', borderLeft: '3px solid var(--moss)', padding: '6px 10px', borderRadius: '0 6px 6px 0', fontSize: 12.5 }}>
            {s.scene_prompt_effective}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button className="btn small" disabled={disabled || saving}
              onClick={() => setDraft(s.scene_prompt_override || s.scene_prompt_effective || '')}>改场景描述</button>
            <button className="btn small" disabled={disabled || saving} onClick={regenerate}>
              {s.ref_image_url ? '重新出图' : '单独出图'}
            </button>
          </div>
        </>
      ) : (
        <>
          <textarea rows={4} style={{ fontSize: 12.5 }} value={draft} onChange={e => setDraft(e.target.value)}
            placeholder="描述场景定场图：画风、地点、光线时段、陈设、氛围……（10~400 字，不要出现人物）" />
          <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <button className="btn small primary" disabled={saving || disabled} onClick={() => save(true)}>保存并重新出图</button>
            <button className="btn small" disabled={saving} onClick={() => save(false)}>仅保存</button>
            {isOverridden && <button className="btn small" disabled={saving} onClick={() => { setDraft('') }} title="清空后保存即恢复默认">清空</button>}
            <button className="btn small ghost" disabled={saving} onClick={() => setDraft(null)}>放弃</button>
          </div>
        </>
      )}
    </div>
  )
}
