import { useState } from 'react'
import { api, EpisodeScreenplay, PlotSpine, ScriptScene, numToCn } from '../api'
import { useEpisode, useNav } from '../App'
import { EpStamp } from './BiblePage'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import EvidenceDrawer from '../components/harness/EvidenceDrawer'

function ScreenplayStamp({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    pending: ['待剧本', 'grey'],
    running: ['生成中', 'gold'],
    repairing: ['修复中', 'gold'],
    ready: ['已交付', 'green'],
    warning: ['修复中', 'gold'],
    failed: ['剧本败', 'red'],
  }
  const [label, color] = map[status] ?? [status, 'grey']
  return <span className={`stamp ${color}`}>{label}</span>
}

const cloneScript = (script: EpisodeScreenplay | null | undefined): EpisodeScreenplay | null =>
  script ? JSON.parse(JSON.stringify(script)) : null

const splitLines = (text: string) => text.split('\n').map(x => x.trim()).filter(Boolean)
const sourceRangeText = (chapters: number[]) => chapters.length <= 1 ? `第 ${chapters[0] ?? '-'} 章` : `第 ${chapters[0]}-${chapters[chapters.length - 1]} 章`
const parseSceneOutlineText = (text: string): ScriptScene[] =>
  text.split('\n')
    .map(x => x.trim())
    .filter(Boolean)
    .map((line, index) => {
      const [scene_heading = '', story_function = '', summary = '', conflict = '', turn = '', source_basis = '', characters = ''] = line.split('|').map(part => part.trim())
      return {
        scene_no: index + 1,
        scene_heading,
        story_function,
        summary,
        conflict,
        turn,
        source_basis,
        characters: characters.split(/[、,，/]/).map(x => x.trim()).filter(Boolean),
      }
    })

const sceneOutlineText = (sceneOutline: ScriptScene[] | undefined) =>
  (sceneOutline ?? [])
    .map(scene => [scene.scene_heading, scene.story_function, scene.summary, scene.conflict ?? '', scene.turn ?? '', scene.source_basis ?? '', (scene.characters ?? []).join('、')].join(' | '))
    .join('\n')

const emptySpine = (): PlotSpine => ({
  episode_premise: '',
  spine_beats: [],
  must_keep_ending: '',
  drop_list: [],
})

const restoreDropItem = (script: EpisodeScreenplay, dropText: string): EpisodeScreenplay => {
  const spine = { ...(script.plot_spine ?? emptySpine()) }
  const drops = [...(spine.drop_list ?? [])]
  const idx = drops.findIndex(d => d === dropText)
  if (idx < 0) return script
  drops.splice(idx, 1)
  spine.drop_list = drops
  const points = [...(script.key_plot_points ?? [])]
  if (!points.includes(dropText)) points.push(dropText)
  const approved = [...(script.approved_adaptations ?? [])]
  const mark = `恢复拍摄：${dropText}`
  if (!approved.includes(mark)) approved.push(mark)
  return { ...script, plot_spine: spine, key_plot_points: points, approved_adaptations: approved }
}

export default function ScriptPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep, refresh, error, loading } = useEpisode(episodeId!, 'script')
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState<EpisodeScreenplay | null>(null)
  const [manuscriptExpanded, setManuscriptExpanded] = useState(false)
  const [restoreEnabled, setRestoreEnabled] = useState(false)
  const [selectedDialogueLines, setSelectedDialogueLines] = useState<string[] | null>(null)
  const screenplayTimer = useTaskTimer(
    `episode.${episodeId}.screenplay`,
    ep?.screenplay_production?.task_active
      ?? ep?.screenplay_status === 'running',
  )
  const storyboardTimer = useTaskTimer(`episode.${episodeId}.storyboard`, ep?.status === 'scripting')

  if (error && !ep) return <div className="empty">{error}</div>
  if (loading && !ep) return <div className="empty">展卷中……</div>
  if (!ep) return <div className="empty">展卷中……</div>

  const act = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true)
    try {
      const r = await fn()
      if (doneMsg) toast(doneMsg)
      refresh()
      return r
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const hasDownstream = (ep.shot_count ?? 0) > 0 || ['scripted', 'confirmed', 'generating', 'done'].includes(ep.status)
  const productionOperation = ep.screenplay_production?.operation
    ?? (ep.screenplay_status === 'repairing' || ep.screenplay_status === 'warning' ? 'repair' : 'baseline')
  const screenplayTaskActive = ep.screenplay_production?.task_active
    ?? ep.screenplay_status === 'running'
  const canResumeRepair = ep.screenplay_production?.can_resume_repair
    ?? (ep.screenplay_status === 'repairing' || ep.screenplay_status === 'warning')
  const sourceDialogueLines = ep.source_dialogue_lines ?? []
  const requiredDialogueLines = selectedDialogueLines ?? ep.required_dialogue_lines ?? []
  const requiredDialogueSet = new Set(requiredDialogueLines)
  const allDialogueSelected = sourceDialogueLines.length > 0
    && sourceDialogueLines.every(line => requiredDialogueSet.has(line))

  const startBaseline = () => {
    screenplayTimer.start()
    void act(
      () => api.post(`/episodes/${ep.id}/screenplay`, {
        required_dialogue_lines: requiredDialogueLines,
      }),
      '首次整版 Baseline 已开始；落库后只做局部 Patch',
    ).then(r => { if (r === undefined) screenplayTimer.clear() })
  }

  const resumeRepair = () => {
    screenplayTimer.start()
    void act(
      () => api.post(`/episodes/${ep.id}/screenplay/resume`, {}),
      '已从工作副本继续局部修复（不会再次整版生成）',
    ).then(r => { if (r === undefined) screenplayTimer.clear() })
  }

  const deleteCurrentScreenplay = async () => {
    const r = await act(
      () => api.del(`/episodes/${ep.id}/screenplay`),
      '当前剧本及下游产物已删除；必保留台词选择已保留',
    )
    if (r !== undefined) {
      setDraft(null)
      setRestoreEnabled(false)
      screenplayTimer.clear()
      storyboardTimer.clear()
    }
  }

  const saveDraft = async () => {
    if (!draft) return
    if (hasDownstream &&
      !window.confirm('保存剧本修改会清空本集已有分镜、参考图、视频和成片，需要重新生成分镜。确定保存？')) return
    const r = await act(() => api.put(`/episodes/${ep.id}/screenplay`, { screenplay: draft, force: hasDownstream }),
      hasDownstream ? '剧本已保存，下游分镜已清空' : '剧本已保存')
    if (r !== undefined) {
      setDraft(null)
      setRestoreEnabled(false)
    }
  }

  const enterBoard = async () => {
    const canResumeCheckpoint = Boolean(
      (ep.shot_count ?? 0) > 0 &&
      ep.script_error &&
      (ep.status === 'scripted' || ep.status === 'script_failed') &&
      !(ep.shots?.length && ep.shots[ep.shots.length - 1]?.is_final)
    )
    const needGenerate = (ep.shot_count ?? 0) === 0 || ['planned', 'script_failed'].includes(ep.status)
    if (needGenerate && ep.status !== 'scripting') {
      storyboardTimer.start()
      const path = canResumeCheckpoint
        ? `/episodes/${ep.id}/storyboard/resume`
        : `/episodes/${ep.id}/storyboard`
      const r = await act(
        () => api.post(path),
        canResumeCheckpoint
          ? `已进入分镜台，从前 ${ep.shot_count ?? 0} 镜 checkpoint 继续生成`
          : '已进入分镜台，正在逐镜头生成，QA 通过后陆续展示',
      )
      if (r === undefined) {
        storyboardTimer.clear()
        return
      }
    }
    go('board', projectId, ep.id)
  }

  const script = draft ?? ep.screenplay ?? null
  const editing = !!draft
  const spine = script?.plot_spine

  const updateScript = (patch: Partial<EpisodeScreenplay>) => {
    if (!draft) return
    setDraft({ ...draft, ...patch })
  }

  const updateSpine = (patch: Partial<PlotSpine>) => {
    if (!draft) return
    setDraft({ ...draft, plot_spine: { ...(draft.plot_spine ?? emptySpine()), ...patch } })
  }

  const structureItems = [
    ['开端', script?.opening],
    ['发展', script?.development],
    ['冲突', script?.conflict],
    ['高潮', script?.climax],
    ['结尾钩子', script?.ending_hook],
  ].filter(([, value]) => !!(value ?? '').toString().trim())

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="剧本台" view="script" episodeNo={ep.episode_no} />
        <h1>剧本台 <span className="sub">《{ep.title}》 · 先完成可拍剧本，再进入镜头设计</span></h1>
        <hr className="rule" />
      </header>

      <section className="card script-toolbar">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <ScreenplayStamp status={ep.screenplay_status} />
          <EpStamp status={ep.status} />
          {screenplayTaskActive ? (
            <button className="btn" disabled>
              {productionOperation === 'baseline' ? '首次整版生成中…' : '局部修复中…'}
            </button>
          ) : canResumeRepair ? (
            <>
              <button className="btn" disabled={busy || ep.status === 'scripting'} onClick={resumeRepair}
                title="从已有 working Artifact 和 checkpoint 继续，只执行字段/节点级 Patch">
                继续局部修复
              </button>
              <button className="btn" disabled={busy || ep.status === 'scripting'} onClick={deleteCurrentScreenplay}
                title={ep.screenplay
                  ? '放弃当前工作副本和已交付剧本，并清空下游产物'
                  : '放弃未通过的工作副本，清除失败 checkpoint 后重新首次生成'}>
                {ep.screenplay ? '删除当前剧本' : '删除失败剧本'}
              </button>
            </>
          ) : ep.screenplay ? (
            <button className="btn" disabled={busy || ep.status === 'scripting'} onClick={deleteCurrentScreenplay}
              title="删除当前剧本；若已有分镜、媒体或成片也会一并清空">
              删除当前剧本
            </button>
          ) : (
            <button className="btn" disabled={busy || ep.status === 'scripting'} onClick={startBaseline}
              title="唯一会向模型发送完整剧本生成提示词的动作">
              首次生成整版
            </button>
          )}
          {screenplayTaskActive && (
            <button className="btn ghost" disabled={busy}
              onClick={() => act(() => api.post(`/episodes/${ep.id}/screenplay/cancel`), '已取消剧本任务')}>
              {productionOperation === 'baseline' ? '停止首次生成' : '停止局部修复'}
            </button>
          )}
          {ep.screenplay && !editing && (
            <button className="btn" disabled={busy || !['ready'].includes(ep.screenplay_status)} onClick={() => setDraft(cloneScript(ep.screenplay))}>
              手工编辑全文
            </button>
          )}
          {editing && (
            <>
              <button className="btn primary" disabled={busy} onClick={saveDraft}>保存剧本</button>
              <button className="btn ghost" disabled={busy} onClick={() => { setDraft(null); setRestoreEnabled(false) }}>放弃</button>
            </>
          )}
          {ep.screenplay_status === 'ready' && !editing && (
            <button className="btn primary" disabled={busy} onClick={enterBoard}>
              进入分镜台 →
            </button>
          )}
          <span style={{ flex: 1 }} />
          {ep.screenplay_evidence && <EvidenceDrawer evidence={ep.screenplay_evidence} label="剧本证据" />}
          <TaskTimer label="剧本" timer={screenplayTimer} />
          <TaskTimer label="分镜" timer={storyboardTimer} />
          <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
            目标 {ep.target_duration_s}s · renderability_v1
          </span>
        </div>
        {!script && !screenplayTaskActive && (
          <div className="screenplay-dialogue-picker">
            <div className="screenplay-dialogue-picker-head">
              <div>
                <b>必保留原文台词</b>
                <span>已选 {requiredDialogueLines.length} / {sourceDialogueLines.length} 条；勾选项会逐字进入剧本和后续分镜</span>
              </div>
              {sourceDialogueLines.length > 0 && (
                <button type="button" className="btn ghost" disabled={busy}
                  onClick={() => setSelectedDialogueLines(allDialogueSelected ? [] : [...sourceDialogueLines])}>
                  {allDialogueSelected ? '取消全选' : '全选'}
                </button>
              )}
            </div>
            {sourceDialogueLines.length > 0 ? (
              <div className="screenplay-dialogue-options">
                {sourceDialogueLines.map((line, index) => (
                  <label key={`${index}-${line}`} className="screenplay-dialogue-option">
                    <input type="checkbox" checked={requiredDialogueSet.has(line)}
                      onChange={event => {
                        const next = new Set(requiredDialogueLines)
                        if (event.target.checked) next.add(line)
                        else next.delete(line)
                        setSelectedDialogueLines(sourceDialogueLines.filter(item => next.has(item)))
                      }} />
                    <span><em>D{String(index + 1).padStart(3, '0')}</em>{line}</span>
                  </label>
                ))}
              </div>
            ) : (
              <div className="screenplay-dialogue-empty">本集原文未识别到显式台词，可直接首次生成整版。</div>
            )}
            {requiredDialogueLines.length > 6 && (
              <div className="screenplay-dialogue-warning">
                已选择较多台词；系统会全部保留，但剧本与后续分镜可能相应变长。
              </div>
            )}
          </div>
        )}
        <div className="script-capability-note" style={{ marginTop: 10, fontSize: 13, color: 'var(--ink-soft)' }}>
          能力边界：仅「首次生成整版」发送完整剧本提示词；「继续局部修复」恢复 Patch checkpoint；「删除当前剧本」放弃当前版本并清空下游；自由改稿请用「手工编辑全文」。
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10, marginTop: 12 }}>
          <div className="kv"><b>当前分集</b>第{numToCn(ep.episode_no)}集</div>
          <div className="kv"><b>原文来源范围</b>{script?.source_text_range || sourceRangeText(ep.source_chapters)}</div>
          <div className="kv"><b>目标时长</b>{ep.target_duration_s}s</div>
          <div className="kv"><b>剧本状态</b>{
            ep.screenplay_status === 'ready' ? '已交付（含完成凭证）'
              : screenplayTaskActive && productionOperation === 'repair' ? '局部修复中'
              : screenplayTaskActive ? '首次整版生成中'
              : canResumeRepair ? '局部修复已暂停，可继续'
              : ep.screenplay_status === 'failed' ? '生成失败'
              : '待生成'
          }</div>
        </div>
        {screenplayTaskActive && productionOperation === 'baseline' && <div style={{ marginTop: 10 }}><span className="stamp gold">首次整版 Baseline</span> <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>这是唯一一次完整剧本模型请求；落库后切换为局部 Patch。</span></div>}
        {screenplayTaskActive && productionOperation === 'repair' && (
          <div className="error-banner">
            Agent 正在按 Issue 做局部修复；未通过完成凭证前不会作为可用剧本交付，也不能进入分镜。
          </div>
        )}
        {!screenplayTaskActive && canResumeRepair && (
          <div className="error-banner">
            局部修复已暂停或等待续跑；点击「继续局部修复」会从现有工作副本恢复，不会发送完整剧本生成提示词。
          </div>
        )}
        {ep.screenplay_error && <div className="error-banner">剧本提示：{'\n'}{ep.screenplay_error}</div>}
        {ep.script_error && <div className="error-banner">分镜提示：{'\n'}{ep.script_error}</div>}
      </section>

      <div className="workspace-gap" />

      {!script
        ? <div className="empty"><div className="big">剧</div>尚无可交付剧本<br />点击上方「首次生成整版」</div>
        : (
            <>
              {(spine || editing) && (
                <section className="card spine-card">
                  <div className="shot-head" style={{ marginBottom: 10 }}>
                    <span className="sn">主线骨架</span>
                    <span className="meta">只拍这些；drop_list 默认不拍 · 合同 renderability_v1</span>
                  </div>
                  {!editing ? (
                    <div className="shot-body">
                      {!!spine?.episode_premise && (
                        <div className="kv full"><b>本集前提</b>{spine.episode_premise}</div>
                      )}
                      {!!spine?.spine_beats?.length && (
                        <div className="kv full"><b>主线节拍</b>
                          <ol className="spine-beat-list">
                            {spine.spine_beats.map((b, i) => (
                              <li key={b.beat_id || i}>
                                <code>{b.beat_id || `S${i + 1}`}</code>
                                <span>{b.who}｜{b.does}→{b.turn}</span>
                                {b.must_keep === false && <em className="spine-optional">可删过渡</em>}
                              </li>
                            ))}
                          </ol>
                        </div>
                      )}
                      {!!spine?.must_keep_ending && (
                        <div className="kv full"><b>必须收束</b>{spine.must_keep_ending}</div>
                      )}
                      {!!spine?.drop_list?.length && (
                        <div className="kv full"><b>本集不拍</b>
                          <ul className="key-list drop-list">
                            {spine.drop_list.map((d, i) => <li key={i}>{d}</li>)}
                          </ul>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="shot-body">
                      <div className="full"><label className="f">本集前提（一句话）</label>
                        <input type="text" style={{ width: '100%' }} value={draft?.plot_spine?.episode_premise ?? ''}
                          onChange={e => updateSpine({ episode_premise: e.target.value })} /></div>
                      <div className="full"><label className="f">主线节拍（每行：beat_id | who | does | turn）</label>
                        <textarea rows={6} value={(draft?.plot_spine?.spine_beats ?? []).map(b =>
                          [b.beat_id, b.who ?? '', b.does ?? '', b.turn ?? ''].join(' | ')).join('\n')}
                          onChange={e => updateSpine({
                            spine_beats: splitLines(e.target.value).map((line, i) => {
                              const [beat_id = `S${String(i + 1).padStart(2, '0')}`, who = '', does = '', turn = ''] = line.split('|').map(p => p.trim())
                              return { beat_id, who, does, turn, must_keep: true }
                            }),
                          })} /></div>
                      <div className="full"><label className="f">必须收束</label>
                        <textarea rows={2} value={draft?.plot_spine?.must_keep_ending ?? ''}
                          onChange={e => updateSpine({ must_keep_ending: e.target.value })} /></div>
                      <div className="full">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 6 }}>
                          <label className="f" style={{ margin: 0 }}>本集不拍（drop_list）</label>
                          <label style={{ fontSize: 13, color: 'var(--ink-soft)', display: 'flex', gap: 6, alignItems: 'center' }}>
                            <input type="checkbox" checked={restoreEnabled}
                              onChange={e => setRestoreEnabled(e.target.checked)} />
                            启用「恢复一条 drop」授权
                          </label>
                        </div>
                        <textarea rows={3} value={(draft?.plot_spine?.drop_list ?? []).join('\n')}
                          onChange={e => updateSpine({ drop_list: splitLines(e.target.value) })} />
                        {restoreEnabled && !!(draft?.plot_spine?.drop_list?.length) && (
                          <ul className="drop-restore-list">
                            {(draft?.plot_spine?.drop_list ?? []).map((d, i) => (
                              <li key={`${d}-${i}`}>
                                <span>{d}</span>
                                <button type="button" className="btn ghost" style={{ fontSize: 12 }}
                                  onClick={() => {
                                    if (!draft) return
                                    if (!window.confirm(`确认恢复拍摄「${d}」？将移出 drop_list 并写入关键剧情点，保存后才会进分镜。`)) return
                                    setDraft(restoreDropItem(draft, d))
                                    toast('已标记恢复；请保存剧本后再进分镜')
                                  }}>
                                  恢复拍摄
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    </div>
                  )}
                </section>
              )}

              <div style={{ height: 16 }} />

              <section className={`card script-editor${editing ? ' editing' : ''}`}>
                {!editing ? (
                  <>
                    <div className="kv full"><b>标题</b>{script.title || ep.title}</div>
                    <div className="kv full"><b>本集一句话梗概</b>{script.logline || ep.synopsis}</div>
                    {!!script.script_format_note && <div className="kv full"><b>稿件格式</b>{script.script_format_note}</div>}
                    {!!script.dramatic_question && <div className="kv full"><b>本集戏剧问题</b>{script.dramatic_question}</div>}
                    {(!!script.protagonist_goal || !!script.obstacle || !!script.stakes) && (
                      <div className="kv full"><b>目标 / 阻力 / 代价</b>
                        {[script.protagonist_goal, script.obstacle, script.stakes].filter(Boolean).join(' ｜ ')}
                      </div>
                    )}
                    {!!script.key_lines?.length && (
                      <div className="kv full"><b>主线台词</b>
                        <ul className="key-list">{script.key_lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
                      </div>
                    )}
                    {!!script.key_plot_points?.length && (
                      <div className="kv full"><b>主线剧情点</b>
                        <ul className="key-list">{script.key_plot_points.map((p, i) => <li key={i}>{p}</li>)}</ul>
                      </div>
                    )}
                    <div className={`kv full script-manuscript-section ${manuscriptExpanded ? 'expanded' : 'collapsed'}`}>
                      <div className="script-manuscript-head">
                        <div>
                          <b>完整剧本文本</b>
                          <span>{(script.full_script_text ?? '').length.toLocaleString()} 字 · {(script.full_script_text ?? '').split('\n').filter(Boolean).length} 行</span>
                        </div>
                        <button
                          type="button"
                          className="script-manuscript-toggle"
                          aria-expanded={manuscriptExpanded}
                          aria-controls="full-script-manuscript"
                          onClick={() => setManuscriptExpanded(value => !value)}
                        >
                          {manuscriptExpanded ? '收起全文 ↑' : '展开全文 ↓'}
                        </button>
                      </div>
                      {manuscriptExpanded ? (
                        <div id="full-script-manuscript" className="script-manuscript">{script.full_script_text || '暂无完整剧本文本'}</div>
                      ) : (
                        <button
                          id="full-script-manuscript"
                          type="button"
                          className="script-manuscript-collapsed"
                          onClick={() => setManuscriptExpanded(true)}
                        >
                          <span>正文已收起</span>
                          <small>点击展开并阅读完整剧本</small>
                        </button>
                      )}
                    </div>
                    <div className="kv"><b>情绪曲线说明</b>{script.emotional_curve}</div>
                    <div className="kv"><b>结尾钩子</b>{script.ending_hook}</div>
                    <div className="kv full"><b>原文依据</b>{script.source_basis}</div>
                    {!!script.character_state_changes?.length && (
                      <div className="kv full"><b>主要人物状态变化</b>{script.character_state_changes.join('；')}</div>
                    )}
                    {!!script.adaptation_direction && (
                      <div className="kv full"><b>改编方向</b>{script.adaptation_direction}</div>
                    )}
                  </>
                ) : (
                  <>
                    <div className="full"><label className="f">标题</label>
                      <input type="text" style={{ width: '100%' }} value={draft?.title ?? ''}
                        onChange={e => updateScript({ title: e.target.value })} /></div>
                    <div className="full"><label className="f">原文来源范围</label>
                      <input type="text" style={{ width: '100%' }} value={draft?.source_text_range ?? sourceRangeText(ep.source_chapters)}
                        onChange={e => updateScript({ source_text_range: e.target.value })} /></div>
                    <div className="full"><label className="f">本集一句话梗概</label>
                      <textarea rows={2} value={draft?.logline ?? ''}
                        onChange={e => updateScript({ logline: e.target.value })} /></div>
                    <div className="full"><label className="f">稿件格式说明</label>
                      <input type="text" style={{ width: '100%' }} value={draft?.script_format_note ?? ''}
                        onChange={e => updateScript({ script_format_note: e.target.value })} /></div>
                    <div className="full"><label className="f">本集戏剧问题</label>
                      <input type="text" style={{ width: '100%' }} value={draft?.dramatic_question ?? ''}
                        onChange={e => updateScript({ dramatic_question: e.target.value })} /></div>
                    <div><label className="f">主角目标</label>
                      <textarea rows={2} value={draft?.protagonist_goal ?? ''}
                        onChange={e => updateScript({ protagonist_goal: e.target.value })} /></div>
                    <div><label className="f">阻力（外部+内部）</label>
                      <textarea rows={2} value={draft?.obstacle ?? ''}
                        onChange={e => updateScript({ obstacle: e.target.value })} /></div>
                    <div className="full"><label className="f">失败代价</label>
                      <textarea rows={2} value={draft?.stakes ?? ''}
                        onChange={e => updateScript({ stakes: e.target.value })} /></div>
                    <div className="full"><label className="f">主线台词（每行一条，最多 6 条）</label>
                      <textarea rows={4} value={(draft?.key_lines ?? []).join('\n')}
                        onChange={e => updateScript({ key_lines: splitLines(e.target.value) })} /></div>
                    <div className="full"><label className="f">主线剧情点（每行一条）</label>
                      <textarea rows={4} value={(draft?.key_plot_points ?? []).join('\n')}
                        onChange={e => updateScript({ key_plot_points: splitLines(e.target.value) })} /></div>
                    <div className="full"><label className="f">完整剧本文本</label>
                      <textarea rows={18} value={draft?.full_script_text ?? ''}
                        onChange={e => updateScript({ full_script_text: e.target.value })} /></div>
                    <div className="full"><label className="f">场次结构（每行：场次标题 | 本场功能 | 本场摘要 | 冲突 | 转折 | 原文依据 | 角色）</label>
                      <textarea rows={7} value={sceneOutlineText(draft?.scene_outline)}
                        onChange={e => updateScript({ scene_outline: parseSceneOutlineText(e.target.value) })} /></div>
                    <div><label className="f">情绪曲线说明</label>
                      <textarea rows={3} value={draft?.emotional_curve ?? ''}
                        onChange={e => updateScript({ emotional_curve: e.target.value })} /></div>
                    <div><label className="f">结尾钩子</label>
                      <textarea rows={3} value={draft?.ending_hook ?? ''}
                        onChange={e => updateScript({ ending_hook: e.target.value })} /></div>
                    <div className="full"><label className="f">原文依据</label>
                      <textarea rows={4} value={draft?.source_basis ?? ''}
                        onChange={e => updateScript({ source_basis: e.target.value })} /></div>
                    <div className="full"><label className="f">主要人物状态变化（每行一条）</label>
                      <textarea rows={3} value={(draft?.character_state_changes ?? []).join('\n')}
                        onChange={e => updateScript({ character_state_changes: splitLines(e.target.value) })} /></div>
                    <div className="full"><label className="f">改编方向</label>
                      <textarea rows={3} value={draft?.adaptation_direction ?? ''}
                        onChange={e => updateScript({ adaptation_direction: e.target.value })} /></div>
                  </>
                )}
              </section>

              {!!script.scene_outline?.length && (
                <>
                  <div style={{ height: 16 }} />
                  <section className="card">
                    <div className="shot-head" style={{ marginBottom: 10 }}>
                      <span className="sn">场次结构</span>
                      <span className="meta">导演审戏与分镜拆解使用，不是拍卡</span>
                    </div>
                    <div className="scene-outline-grid">
                      {script.scene_outline.map(scene => (
                        <article key={scene.scene_no} className="scene-outline-card">
                          <div className="scene-outline-head">
                            <span className="sn">场{scene.scene_no}</span>
                            <span className="meta">{scene.scene_heading}</span>
                          </div>
                          <div className="scene-outline-body">
                            <div className="kv full"><b>本场功能</b>{scene.story_function}</div>
                            <div className="kv full"><b>本场内容</b>{scene.summary}</div>
                            {!!scene.conflict && <div className="kv"><b>冲突</b>{scene.conflict}</div>}
                            {!!scene.turn && <div className="kv"><b>转折/交接</b>{scene.turn}</div>}
                            {!!scene.source_basis && <div className="kv full"><b>原文依据</b>{scene.source_basis}</div>}
                            {!!scene.characters?.length && <div className="kv full"><b>角色</b>{scene.characters.join('、')}</div>}
                          </div>
                        </article>
                      ))}
                    </div>
                  </section>
                </>
              )}

              {(editing || structureItems.length > 0) && (
                <>
                  <div style={{ height: 16 }} />
                  <section className="card">
                    <div className="shot-head" style={{ marginBottom: 10 }}>
                      <span className="sn">辅助结构</span>
                      <span className="meta">作为拆分分镜时的辅助，不作为剧本主内容</span>
                    </div>
                    {!editing ? (
                      <div className="shot-body">
                        {structureItems.map(([label, value]) => (
                          <div key={label} className="kv full"><b>{label}</b>{value}</div>
                        ))}
                      </div>
                    ) : (
                      <div className="shot-body">
                        <div><label className="f">开端</label>
                          <textarea rows={2} value={draft?.opening ?? ''}
                            onChange={e => updateScript({ opening: e.target.value })} /></div>
                        <div><label className="f">发展</label>
                          <textarea rows={2} value={draft?.development ?? ''}
                            onChange={e => updateScript({ development: e.target.value })} /></div>
                        <div><label className="f">冲突</label>
                          <textarea rows={2} value={draft?.conflict ?? ''}
                            onChange={e => updateScript({ conflict: e.target.value })} /></div>
                        <div><label className="f">高潮</label>
                          <textarea rows={2} value={draft?.climax ?? ''}
                            onChange={e => updateScript({ climax: e.target.value })} /></div>
                      </div>
                    )}
                  </section>
                </>
              )}
            </>
          )}
    </>
  )
}
