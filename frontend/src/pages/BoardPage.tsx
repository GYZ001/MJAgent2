import { useEffect, useRef, useState } from 'react'
import { api, ApiError, Episode, Shot } from '../api'
import { useEpisode, useNav } from '../App'
import { EpStamp } from './BiblePage'
import EpisodeCrumb from '../components/EpisodeCrumb'
import AsyncButton from '../components/AsyncButton'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'
import ImpactDialog, { ImpactSummary } from '../components/harness/ImpactDialog'
import SupervisorPanel from '../components/SupervisorPanel'

const SIZES = ['远景', '全景', '中景', '近景', '特写']
const MOVES = ['固定', '推近', '拉远', '横摇', '跟随']
const TRANS = ['硬切', '叠化', '淡出淡入', '黑场', '闪黑', '闪白', '甩镜', '遮挡转场', '匹配剪辑', '声音延续+叠化', '声音先行+淡入']
const DURATIONS = [5, 6, 7, 8, 9, 10]
const CONTINUITY_MODES = ['action_continuation', 'same_scene_cut', 'reaction_cut', 'reverse_angle', 'insert_detail', 'scene_change']

const ARTIFACT_STATUS_LABEL: Record<string, string> = {
  candidate: '草稿',
  needs_revision: '待修改',
  validated: '已通过',
  approved: '已确认',
  rejected: '已拒绝',
  superseded: '已替代',
  stale: '已失效',
}

const CONTINUITY_MODE_LABEL: Record<string, string> = {
  action_continuation: '动作延续',
  same_scene_cut: '同场切换',
  reaction_cut: '反应镜头',
  reverse_angle: '反打',
  insert_detail: '细节插入',
  scene_change: '转场换景',
}

/** 分镜台只露出「需要用户处理」的状态；正常通过不堆徽章。 */
function ShotLifecycleBadges({ shot }: { shot: Shot }) {
  const ev = shot.storyboard_evidence
  const artifactStatus = ev?.status || ''
  const spoken = shot.spoken_contract_status || ''
  const problemStatuses = new Set(['needs_revision', 'rejected', 'stale', 'superseded', 'candidate'])
  const badges: Array<{ key: string; label: string; className: string }> = []

  if (spoken === 'conflict') {
    badges.push({ key: 'spoken', label: '口播冲突', className: 'shot-badge spoken-conflict' })
  }
  if (problemStatuses.has(artifactStatus)) {
    badges.push({
      key: 'status',
      label: ARTIFACT_STATUS_LABEL[artifactStatus] || artifactStatus,
      className: `shot-badge status-${artifactStatus}`,
    })
  }
  if (shot.legacy_unvalidated) {
    badges.push({ key: 'legacy', label: '待补全', className: 'shot-badge legacy' })
  }
  if (!badges.length) return null
  return (
    <span className="shot-lifecycle-badges" aria-label="镜头状态">
      {badges.map(b => (
        <span key={b.key} className={b.className}>{b.label}</span>
      ))}
    </span>
  )
}

function continuityLabel(mode?: string | null, fromPrev?: boolean | number | null): string {
  if (mode && CONTINUITY_MODE_LABEL[mode]) return CONTINUITY_MODE_LABEL[mode]
  if (mode) return mode
  return fromPrev ? '接上镜' : ''
}

function ShotMetaTokens({ values, tone }: { values?: string[]; tone: 'visible' | 'audio' | 'information' }) {
  const items = (values ?? []).filter(Boolean)
  return (
    <div className={`shot-context-tokens ${tone}`} role="list">
      {items.length
        ? items.map(item => <span key={item} role="listitem">{item}</span>)
        : <span className="empty" role="listitem">无</span>}
    </div>
  )
}

function ShotInformationTokens({ shot }: { shot: Shot }) {
  const grouped = new Map<string, string[]>()
  for (const item of shot.new_information_items ?? []) {
    const content = item.content?.trim()
    if (!content) continue
    grouped.set(content, [...(grouped.get(content) ?? []), item.info_id])
  }
  if (!grouped.size && (shot.new_information_ids?.length ?? 0) > 0) {
    const fallback = shot.purpose || shot.primary_action || shot.state_out || '本镜首次交付的剧情信息'
    grouped.set(fallback, shot.new_information_ids ?? [])
  }
  return (
    <div className="shot-context-tokens information" role="list">
      {grouped.size
        ? [...grouped].map(([content, ids]) => (
            <span key={content} role="listitem" title={ids.length ? `内部信息编号：${ids.join('、')}` : undefined}>{content}</span>
          ))
        : <span className="empty" role="listitem">无</span>}
    </div>
  )
}

function parseCommaList(value: string): string[] {
  return value.split(/[，,]/).map(x => x.trim()).filter(Boolean)
}

/** 与后端 content_char_count 同口径的前端兜底：去空白与常见标点。 */
function countSpokenContentChars(shot: Shot): number {
  const punct = /[\s\u2000-\u206F\u3000-\u303F\uFF00-\uFFEF!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~，。！？：；、…—·「」『』【】（）《》〈〉“”‘’]/g
  return (shot.dialogues ?? []).reduce((sum, d) => sum + (d.line || '').replace(punct, '').length, 0)
}

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
  const lastShot = ep.shots?.length ? ep.shots[ep.shots.length - 1] : null
  const storyboardAlreadyFinal = Boolean(lastShot?.is_final)
  // 与后端 /storyboard/resume 对齐：有已落库镜头 + 未完成提示，且最后一镜未收束，才可续跑。
  // 已 is_final 的集禁止「从下一镜继续」，避免幻觉补镜。
  const canResumeCheckpoint = Boolean(
    ep.shots?.length &&
    ep.script_error &&
    (ep.status === 'scripted' || ep.status === 'script_failed') &&
    !storyboardAlreadyFinal
  )
  const selectedShot = ep.shots?.find(shot => shot.id === selectedShotId) ?? ep.shots?.[0]
  const selectedShotIndex = Math.max(0, ep.shots?.findIndex(shot => shot.id === selectedShot?.id) ?? 0)

  const selectRelativeShot = (offset: number) => {
    if (!ep.shots?.length) return
    const nextIndex = Math.min(ep.shots.length - 1, Math.max(0, selectedShotIndex + offset))
    setSelectedShotId(ep.shots[nextIndex].id)
  }

  const confirmRegenerateStoryboard = (
    mode: 'fresh' | 'resume',
    completionMode: 'ready_for_manual_confirm' | 'auto_confirm' = 'ready_for_manual_confirm',
  ) => {
    if (mode === 'resume') {
      storyboardTimer.start()
      void act(
        () => api.post(`/episodes/${ep.id}/storyboard/resume`, { completion_mode: completionMode }),
        `已从工作 checkpoint 继续局部修复（已验证前 ${ep.shots?.length ?? 0} 镜）`,
      ).then(r => { if (r === undefined) storyboardTimer.clear() })
      return
    }
    const shotCount = ep.shots?.length ?? 0
    const hasPublished = shotCount > 0 && ['scripted', 'confirmed', 'generating', 'done'].includes(ep.status)
    const autoConfirmNote = completionMode === 'auto_confirm'
      ? `\n\n【自动确认授权】\n`
        + `· 剧集：第 ${ep.episode_no} 集《${ep.title}》\n`
        + `· 全量门禁通过后将自动确认，解锁付费视频能力\n`
        + `· 本任务不会自动提交任何付费视频生成\n`
        + `· 修复过程只做局部 Patch，不会整版重规划`
      : ''
    const ok = window.confirm(
      (hasPublished
        ? `将创建新的分镜生产修订：页面在交付前继续显示上一已发布版本；工作副本通过全部门禁后一次性切换。\n`
          + `不会提供「删除全部镜头后整版重生成」路径。确定继续？`
        : completionMode === 'auto_confirm'
          ? '将生成全部可用分镜，通过后自动确认。确定继续？'
          : '将开始生成所有可用分镜（一次大纲 + 逐镜填充 + 局部自愈）。确定继续？')
      + autoConfirmNote,
    )
    if (!ok) return
    storyboardTimer.start()
    void act(
      () => api.post(`/episodes/${ep.id}/storyboard`, { completion_mode: completionMode }),
      hasPublished
        ? (completionMode === 'auto_confirm'
          ? '已启动分镜修订（通过后自动确认）'
          : '已启动分镜修订（局部修复至可交付）')
        : (completionMode === 'auto_confirm'
          ? '分镜生成已开始（通过后将自动确认；尚未产生视频费用）'
          : '分镜生成已开始（工作镜头验证中，整集门禁通过后一次性交付）'),
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
          {ep.screenplay_status !== 'ready' && (
            <span className={`stamp ${ep.screenplay_status === 'running' ? 'gold' : ep.screenplay_status === 'failed' || ep.screenplay_status === 'warning' ? 'red' : 'grey'}`}>
              {ep.screenplay_status === 'warning' ? '剧本有阻塞' : ep.screenplay_status === 'running' ? '剧本中' : ep.screenplay_status === 'failed' ? '剧本败' : '待剧本'}
            </span>
          )}
          </div>
          <div className="board-action-group">
          {ep.status !== 'scripting' && (
            <button className="btn" disabled={busy || ep.screenplay_status !== 'ready'}
              onClick={() => confirmRegenerateStoryboard(canResumeCheckpoint ? 'resume' : 'fresh')}>
              {canResumeCheckpoint
                ? `继续修复（已验证 ${ep.shots?.length ?? 0} 镜）`
                : ep.shots?.length
                  ? '让 Agent 迭代分镜'
                  : '生成所有可用分镜'}
            </button>
          )}
          {ep.status !== 'scripting' && !canResumeCheckpoint && ep.screenplay_status === 'ready' && (
            <button className="btn ghost" disabled={busy}
              onClick={() => confirmRegenerateStoryboard('fresh', 'auto_confirm')}>
              通过后自动确认
            </button>
          )}
          {ep.status !== 'scripting' && canResumeCheckpoint && (
            <button className="btn ghost" disabled={busy}
              onClick={() => confirmRegenerateStoryboard('resume', 'auto_confirm')}>
              继续修复并自动确认
            </button>
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
          {ep.status === 'scripting' && <TaskTimer label="分镜" timer={storyboardTimer} />}
          </div>
        </div>
        <div className="board-stat-strip" aria-label="分镜统计">
          <span><b>{ep.shots?.length ?? 0}</b> 镜头</span>
          <span><b>{totalDur}s</b> 总时长</span>
          {ep.supervisor?.expected_total ? (
            <span><b>{ep.supervisor.expected_total}</b> 计划镜数</span>
          ) : null}
        </div>
        {ep.screenplay_status !== 'ready' && <div className="error-banner">本集还没有可用剧本。请先到剧本台生成/保存完整剧本，再展开分镜。</div>}
        {(ep.status === 'scripting' || ep.supervisor != null) && (
          <SupervisorPanel
            api={api}
            episodeId={ep.id}
            runId={ep.active_storyboard_run_id}
            supervisor={ep.supervisor}
            scripting={ep.status === 'scripting'}
            onChanged={() => { void refresh() }}
          />
        )}
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
        ? <div className="empty"><div className="big">镜</div>尚无分镜<br />点击上方「生成并等待确认」或「生成完成后自动确认」</div>
        : <div className="board-workspace">
            <section className="shot-navigator" aria-label="镜头轨道">
              <div className="shot-navigator-head">
              <div><b>镜头轨道</b></div>
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
  const [conflictOpen, setConflictOpen] = useState(false)
  const [conflictBusy, setConflictBusy] = useState(false)
  const s = edit ?? shot
  const contLabel = continuityLabel(s.continuity_mode, !!s.continuity_from_prev)

  async function save() {
    if (!edit) return
    try {
      const result = await api.put(`/shots/${shot.id}`, {
        duration_s: edit.duration_s, shot_size: edit.shot_size, camera_move: edit.camera_move,
        scene_setting: edit.scene_setting, characters: edit.characters, action_desc: edit.action_desc,
        first_frame_desc: edit.first_frame_desc, last_frame_desc: edit.last_frame_desc,
        source_excerpt: edit.source_excerpt,
        narration: null, dialogues: edit.dialogues, transition: edit.transition,
        continuity_from_prev: !!edit.continuity_from_prev,
        continuity_mode: edit.continuity_mode || '',
        state_in: edit.state_in || '',
        primary_action: edit.primary_action || '',
        state_out: edit.state_out || '',
        characters_visible: edit.characters_visible ?? [],
        audio_cast: edit.audio_cast ?? [],
        new_information_ids: edit.new_information_ids ?? [],
        audio_timeline: edit.audio_timeline ?? [],
        spine_beat_ids: edit.spine_beat_ids ?? [],
        key_line_ids: edit.key_line_ids ?? [],
        expected_version: shot.storyboard_artifact_id || undefined,
      })
      const impact = (result as { impact?: ImpactSummary }).impact
      toast(`镜 ${shot.shot_no} 已保存；${impact?.stale_descendant_ids?.length ?? 0} 个下游证据已标记失效`)
      setEdit(null); onChanged()
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 422) {
        const detail = e.detail as { code?: string; checkpoint_preserved?: boolean; draft_artifact_id?: string; issues?: string[] } | undefined
        if (detail?.code === 'SHOT_EDIT_VALIDATION_FAILED' || /分叉|口播/.test(e.message)) {
          toast(
            detail?.checkpoint_preserved
              ? `保存未通过业务校验（已保留草稿，未覆盖已通过版本）：${e.message}`
              : e.message,
            true,
          )
          if (shot.spoken_contract_status === 'conflict' || /分叉|冲突/.test(e.message)) {
            setConflictOpen(true)
          }
          return
        }
      }
      toast((e as Error).message, true)
    }
  }

  async function resolveConflict(choice: 'rebuild_timeline_from_dialogues' | 'rebuild_dialogues_from_timeline') {
    setConflictBusy(true)
    try {
      await api.post(`/shots/${shot.id}/resolve-spoken-conflict`, {
        choice,
        invalidate_media: true,
      })
      toast(choice === 'rebuild_timeline_from_dialogues' ? '已按台词重建时间轴' : '已按时间轴重建台词')
      setConflictOpen(false)
      setEdit(null)
      onChanged()
    } catch (e: unknown) {
      toast((e as Error).message, true)
    } finally {
      setConflictBusy(false)
    }
  }

  return (
    <div className="shot-strip">
      <div className="shot-head">
        <div className="shot-head-copy">
          <span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
          <span className="meta">{s.duration_s}s · {s.shot_size} · {s.camera_move} · {s.transition}{contLabel ? ` · ${contLabel}` : ''}</span>
          <span className="meta shot-characters">{s.characters.join(' / ') || '缺角色（需修改）'}</span>
          <ShotLifecycleBadges shot={shot} />
        </div>
        <div className="shot-head-actions">
          {shot.spoken_contract_status === 'conflict' && (
            <button className="btn small ghost" disabled={disabled || conflictBusy} onClick={() => setConflictOpen(true)}>
              解决口播冲突
            </button>
          )}
          {!edit
            ? <button className="btn small" disabled={disabled} onClick={() => setEdit(JSON.parse(JSON.stringify(shot)))}>修改</button>
            : <>
              <button className="btn small primary" onClick={() => setImpactOpen(true)}>保存</button>
              <button className="btn small ghost" onClick={() => setEdit(null)}>放弃</button>
            </>}
        </div>
      </div>
      {conflictOpen && (
        <div className="shot-conflict-panel" role="dialog" aria-label="口播冲突修复">
          <p>本镜 dialogues 与 audio_timeline 分叉。请选择以哪一侧为准；若已有视频将一并失效。</p>
          <div className="shot-conflict-actions">
            <button className="btn small primary" disabled={conflictBusy}
              onClick={() => void resolveConflict('rebuild_timeline_from_dialogues')}>
              以台词为准重建时间轴
            </button>
            <button className="btn small" disabled={conflictBusy}
              onClick={() => void resolveConflict('rebuild_dialogues_from_timeline')}>
              以时间轴为准重建台词
            </button>
            <button className="btn small ghost" disabled={conflictBusy} onClick={() => setConflictOpen(false)}>取消</button>
          </div>
        </div>
      )}
      <div className={`shot-body ${edit ? 'editing' : 'reviewing'}`}>
        {edit ? (
          <>
            <div className="shot-edit-grid full">
              <div><label className="f">时长（默认 5s；&gt;5 需 AI 审核）</label>
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
            <div className="full">
              <label className="f">连续性（进入 → 动作 → 离开）</label>
              <div className="shot-frame-pair shot-continuity-chain">
                <div className="shot-frame-card"><span className="shot-frame-label"><strong>进入状态</strong></span><textarea rows={2} value={edit.state_in ?? ''} onChange={e => setEdit({ ...edit, state_in: e.target.value })} /></div>
                <div className="shot-frame-flow" aria-hidden="true">→</div>
                <div className="shot-frame-card"><span className="shot-frame-label"><strong>镜头动作</strong></span><textarea rows={2} value={edit.primary_action ?? ''} onChange={e => setEdit({ ...edit, primary_action: e.target.value })} /></div>
                <div className="shot-frame-flow" aria-hidden="true">→</div>
                <div className="shot-frame-card"><span className="shot-frame-label"><strong>离开状态</strong></span><textarea rows={2} value={edit.state_out ?? ''} onChange={e => setEdit({ ...edit, state_out: e.target.value })} /></div>
              </div>
            </div>
            <div><label className="f">与上镜关系</label>
              <select style={{ width: '100%' }} value={edit.continuity_mode ?? ''} onChange={e => setEdit({ ...edit, continuity_mode: e.target.value })}>
                <option value="">自动/未设定</option>
                {CONTINUITY_MODES.map(x => <option key={x} value={x}>{CONTINUITY_MODE_LABEL[x] || x}</option>)}
              </select></div>
            <div><label className="f">画面可见角色（逗号分隔）</label>
              <input type="text" value={(edit.characters_visible ?? []).join(', ')} onChange={e => setEdit({ ...edit, characters_visible: parseCommaList(e.target.value) })} /></div>
            <div><label className="f">声音角色（逗号分隔）</label>
              <input type="text" value={(edit.audio_cast ?? []).join(', ')} onChange={e => setEdit({ ...edit, audio_cast: parseCommaList(e.target.value) })} /></div>
            <div><label className="f">本镜新信息编号（高级）</label>
              <input type="text" value={(edit.new_information_ids ?? []).join(', ')} onChange={e => setEdit({ ...edit, new_information_ids: parseCommaList(e.target.value) })} /></div>
            <div><label className="f">首帧画面（本镜开始的静止画面）</label>
              <textarea rows={2} value={edit.first_frame_desc ?? ''} onChange={e => setEdit({ ...edit, first_frame_desc: e.target.value })} /></div>
            <div><label className="f">尾帧画面（结束的静止画面，须与首帧明显不同）</label>
              <textarea rows={2} value={edit.last_frame_desc ?? ''} onChange={e => setEdit({ ...edit, last_frame_desc: e.target.value })} /></div>
            <div className="full"><label className="f">上游改编证据（不送 Seedance）</label>
              <textarea rows={3} value={edit.source_excerpt ?? ''} onChange={e => setEdit({ ...edit, source_excerpt: e.target.value })} /></div>
            <div className="full"><label className="f">旁白（已废弃，保存时强制清空）</label>
              <textarea rows={2} value={edit.narration ?? ''} readOnly placeholder="禁止旁白/内心OS；请改用台词或画面" />
              {(edit.narration || '').trim() ? <p className="shot-spoken-warn">检测到历史旁白，保存后将自动清空</p> : null}
            </div>
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
                <div className="shot-spec-wide"><dt>连续性</dt><dd>{contLabel || '新场景'}</dd></div>
              </dl>
            </div>

            <div className="shot-frame-pair shot-continuity-chain full" aria-label="镜头状态链">
              <div className="shot-frame-card"><span className="shot-frame-label"><strong>进入状态</strong></span><p>{s.state_in || s.first_frame_desc || '未设置'}</p></div>
              <div className="shot-frame-flow" aria-hidden="true">→</div>
              <div className="shot-frame-card"><span className="shot-frame-label"><strong>镜头动作</strong></span><p>{s.primary_action || s.action_desc || '未设置'}</p></div>
              <div className="shot-frame-flow" aria-hidden="true">→</div>
              <div className="shot-frame-card"><span className="shot-frame-label"><strong>离开状态</strong></span><p>{s.state_out || s.last_frame_desc || '未设置'}</p></div>
            </div>

            <section className="shot-spoken-panel full" aria-labelledby={`shot-spoken-${shot.id}`}>
              <header className="shot-context-head">
                <span id={`shot-spoken-${shot.id}`} className="shot-section-label">本镜台词</span>
                <span>
                  纯文字 {(s.spoken_content_chars ?? countSpokenContentChars(s))} /
                  上限 {s.spoken_limit ?? '—'}（不计标点）
                </span>
              </header>
              {(s.has_legacy_narration || !!(s.narration || '').trim()) && (
                <div className="shot-spoken-warn" role="status">旁白已废弃：请清空后保存；口播只保留真实台词</div>
              )}
              {s.dialogues.length ? (
                <div className="shot-audio-copy shot-spoken-list">
                  {s.dialogues.map((d, i) => (
                    <div key={i} className="shot-audio-line"><b>{d.speaker}<small>{d.emotion}</small></b><p>「{d.line}」</p></div>
                  ))}
                </div>
              ) : (
                <div className="shot-audio-empty">本镜无台词</div>
              )}
            </section>

            <section className="shot-context-summary full" aria-labelledby={`shot-context-${shot.id}`}>
              <div className="shot-context-panel">
                <header className="shot-context-head">
                  <span id={`shot-context-${shot.id}`} className="shot-section-label">镜头要素</span>
                  <span>参与角色与本镜信息</span>
                </header>
                <dl className="shot-context-grid">
                  <div className="shot-context-card">
                    <dt>可见角色</dt>
                    <dd><ShotMetaTokens values={s.characters_visible} tone="visible" /></dd>
                  </div>
                  <div className="shot-context-card">
                    <dt>声音角色</dt>
                    <dd><ShotMetaTokens values={s.audio_cast} tone="audio" /></dd>
                  </div>
                  <div className="shot-context-card information">
                    <dt>本镜新信息</dt>
                    <dd><ShotInformationTokens shot={s} /></dd>
                  </div>
                </dl>
              </div>
            </section>

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
                  <span className="shot-section-label">上游改编证据（不送 Seedance）</span>
                  <p>{s.source_excerpt || '暂无对应原文'}</p>
                </div>
                <div className="shot-audio-copy">
                  {(s.has_legacy_narration || !!(s.narration || '').trim()) && (
                    <div className="shot-audio-line"><b>旁白（废弃）</b><p>{s.narration}</p></div>
                  )}
                  {!!s.dialogues.length && s.dialogues.map((d, i) => (
                    <div key={i} className="shot-audio-line"><b>{d.speaker}<small>{d.emotion}</small></b><p>「{d.line}」</p></div>
                  ))}
                  {!s.dialogues.length && !(s.narration || '').trim() && <div className="shot-audio-empty">本镜无台词</div>}
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
