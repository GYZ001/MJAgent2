import { useState } from 'react'
import { api, EpisodeScreenplay, ScriptScene, numToCn } from '../api'
import { useEpisode, useNav } from '../App'
import { EpStamp } from './BiblePage'

function ScreenplayStamp({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    pending: ['待剧本', 'grey'],
    running: ['剧本中', 'gold'],
    ready: ['剧本成', 'green'],
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

export default function ScriptPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep, refresh } = useEpisode(episodeId!)
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState<EpisodeScreenplay | null>(null)

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

  const hasDownstream = !!ep.shots?.length || ['scripted', 'confirmed', 'generating', 'done'].includes(ep.status)

  const generate = () => {
    const isRegenerate = !!ep.screenplay
    if ((isRegenerate || hasDownstream) &&
      !window.confirm('重新生成剧本会清空本集已有分镜、关键帧、视频和成片，需要后续重新展开。确定继续？')) return
    act(() => api.post(`/episodes/${ep.id}/screenplay`, { force: isRegenerate || hasDownstream }),
      '剧本生成已开始（完成后可在本页编辑，再进入分镜）')
  }

  const saveDraft = async () => {
    if (!draft) return
    if (hasDownstream &&
      !window.confirm('保存剧本修改会清空本集已有分镜、关键帧、视频和成片，需要重新生成分镜。确定保存？')) return
    await act(() => api.put(`/episodes/${ep.id}/screenplay`, { screenplay: draft, force: hasDownstream }),
      hasDownstream ? '剧本已保存，下游分镜已清空' : '剧本已保存')
    setDraft(null)
  }

  const enterBoard = async () => {
    const needGenerate = !ep.shots?.length || ['planned', 'script_failed'].includes(ep.status)
    if (needGenerate && ep.status !== 'scripting') {
      await act(() => api.post(`/episodes/${ep.id}/storyboard`), '已进入分镜台，正在按最新剧本拆分分镜')
    }
    go('board', projectId, ep.id)
  }

  const script = draft ?? ep.screenplay ?? null
  const editing = !!draft

  const updateScript = (patch: Partial<EpisodeScreenplay>) => {
    if (!draft) return
    setDraft({ ...draft, ...patch })
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
        <div className="crumb">
          <a style={{ cursor: 'pointer' }} onClick={() => go('episodes', projectId, null)}>分集台</a> / 第{numToCn(ep.episode_no)}集
        </div>
        <h1>剧本台 <span className="sub">《{ep.title}》 · 小说先转可拍剧本，再展开分镜</span></h1>
        <hr className="rule" />
      </header>

      <section className="card">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <ScreenplayStamp status={ep.screenplay_status} />
          <EpStamp status={ep.status} />
          <button className="btn" disabled={busy || ep.screenplay_status === 'running' || ep.status === 'scripting'}
            onClick={generate}>
            {ep.screenplay ? '重新生成剧本' : '生成剧本'}
          </button>
          {ep.screenplay_status === 'running' && (
            <button className="btn ghost" disabled={busy}
              onClick={() => act(() => api.post(`/episodes/${ep.id}/screenplay/cancel`), '已取消剧本生成')}>
              取消生成
            </button>
          )}
          {ep.screenplay && !editing && (
            <button className="btn" disabled={busy || ep.screenplay_status !== 'ready'} onClick={() => setDraft(cloneScript(ep.screenplay))}>
              修改剧本
            </button>
          )}
          {editing && (
            <>
              <button className="btn primary" disabled={busy} onClick={saveDraft}>保存剧本</button>
              <button className="btn ghost" disabled={busy} onClick={() => setDraft(null)}>放弃</button>
            </>
          )}
          {ep.screenplay_status === 'ready' && !editing && (
            <>
              <button className="btn primary" disabled={busy} onClick={enterBoard}>
                进入分镜台 →
              </button>
            </>
          )}
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
            目标 {ep.target_duration_s}s · 完整剧本视图
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10, marginTop: 12 }}>
          <div className="kv"><b>当前分集</b>第{numToCn(ep.episode_no)}集</div>
          <div className="kv"><b>原文来源范围</b>{script?.source_text_range || sourceRangeText(ep.source_chapters)}</div>
          <div className="kv"><b>目标时长</b>{ep.target_duration_s}s</div>
          <div className="kv"><b>剧本状态</b>{ep.screenplay_status === 'ready' ? '已生成' : ep.screenplay_status === 'running' ? '生成中' : ep.screenplay_status === 'failed' ? '生成失败' : '待生成'}</div>
        </div>
        {ep.screenplay_status === 'running' && <div style={{ marginTop: 10 }}><span className="stamp gold">剧本中</span> <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>正在生成整集完整剧本，完成后再进入分镜拆分……</span></div>}
        {ep.screenplay_error && <div className="error-banner">剧本提示：{'\n'}{ep.screenplay_error}</div>}
        {ep.script_error && <div className="error-banner">分镜提示：{'\n'}{ep.script_error}</div>}
      </section>

      <div style={{ height: 20 }} />

      {!script
        ? <div className="empty"><div className="big">剧</div>尚无剧本<br />点击上方「生成剧本」</div>
        : (
            <>
              <section className="card">
                {!editing ? (
                  <>
                    <div className="kv full"><b>标题</b>{script.title || ep.title}</div>
                    <div className="kv full"><b>本集一句话梗概</b>{script.logline || ep.synopsis}</div>
                    {!!script.script_format_note && <div className="kv full"><b>稿件格式</b>{script.script_format_note}</div>}
                    <div className="kv full"><b>完整剧本文本</b>
                      <div className="script-manuscript">{script.full_script_text}</div>
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
