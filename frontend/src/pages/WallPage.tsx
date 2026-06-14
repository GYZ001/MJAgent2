import { useRef, useState } from 'react'
import { api, Shot, ShotVersion, SceneCandidate } from '../api'
import { useEpisode, useNav } from '../App'
import EpisodeCrumb from '../components/EpisodeCrumb'
import { TaskTimer, useTaskTimer } from '../components/TaskTimer'

type VideoModeValue = 'REFERENCE_IMAGE_MODE' | 'FIRST_LAST_FRAME_MODE'
type ModeChoice = 'AUTO' | VideoModeValue

type PreviewModeDecision = {
  mode: VideoModeValue
  reason: string
  confidence: number
  source: 'manual' | 'frontend_preview'
  evidence: string[]
  scores: {
    reference: number
    firstLast: number
  }
  needReusePreviousScene: boolean
  needGenerateNewReferences: boolean
  referenceImagePlan: {
    totalCount: number
    reusePreviousSceneCount: number
    generateNewCount: number
    types: string[]
  }
  forced?: boolean
}

const STRONG_WORDS = [
  'fight', 'battle', 'explode', 'explosion', 'transform', 'spell', 'magic', 'blast',
  '打斗', '战斗', '搏斗', '爆气', '爆炸', '法术', '施法', '变身', '快速转身', '转场',
  '过渡到', '落点', '结尾画面', '尾帧', '强控制', '冲刺', '闪现',
]

const LIGHT_WORDS = [
  'dialogue', 'talk', 'walk', 'stand', 'sit', 'look back', 'scene continues',
  '对话', '说', '交谈', '站', '坐', '走', '回头', '场景延续', '连续出场',
  '情绪', '环境', '展示', '看向', '轻声',
]

function modeLabel(mode?: string | null) {
  return mode === 'REFERENCE_IMAGE_MODE' ? '参考图模式' : '首尾帧模式'
}

function modeSummary(mode: VideoModeValue) {
  return mode === 'REFERENCE_IMAGE_MODE'
    ? '靠参考图维持人物、场景与气质一致，更适合剧情、对白和轻动作镜头。'
    : '用首图 + 尾图把动作起点和落点钉住，更适合强动作、转场和结果明确的镜头。'
}

function containsAny(text: string, words: string[]) {
  return words.some(word => text.includes(word.toLowerCase()))
}

function matchedWords(text: string, words: string[]) {
  return words.filter(word => text.includes(word.toLowerCase())).slice(0, 4)
}

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value))
}

function roundedScore(value: number) {
  return Number(clamp01(value).toFixed(2))
}

function textForRules(shot: Shot) {
  return [
    shot.scene_setting,
    shot.action_desc,
    shot.first_frame_desc,
    shot.last_frame_desc,
    shot.narration ?? '',
    shot.transition,
    ...shot.dialogues.map(d => `${d.speaker} ${d.line} ${d.emotion}`),
  ].join(' ').toLowerCase()
}

function sceneContinues(prevShot: Shot | undefined, shot: Shot) {
  if (!prevShot) return Boolean(shot.continuity_from_prev)
  return Boolean(shot.continuity_from_prev) || prevShot.scene_setting.trim() === shot.scene_setting.trim()
}

// 上一镜实际可复用的首尾帧素材数：只有上一镜生成并通过评审的关键帧才可复用。
// 普通剧情镜（参考图模式）不生成关键帧，因此这里通常为 0——避免预估“复用 N 张”却无可复用之物。
function reusablePrevSceneCount(prevShot: Shot | undefined) {
  if (!prevShot) return 0
  return prevShot.scenes.filter(s => s.status === 'succeeded' && (s.qa?.overall ?? 0) >= 0.75).length
}

function referencePlanPreview(shot: Shot, prevShot: Shot | undefined) {
  const maxRefs = 8
  const isFirst = shot.shot_no <= 1
  const sameScene = sceneContinues(prevShot, shot)
  const complexShot = shot.characters.length >= 2 || ((shot.scene_setting || '') + (shot.action_desc || '')).length > 90
  if (isFirst) {
    const total = Math.min(Math.max(complexShot ? 8 : 4, 4), maxRefs)
    return { totalCount: total, reusePreviousSceneCount: 0, generateNewCount: total, types: ['character', 'scene', 'prop', 'style', 'plot_key_frame'] }
  }
  if (sameScene) {
    const reuse = Math.min(4, maxRefs, reusablePrevSceneCount(prevShot))
    const generated = complexShot ? 2 : 1
    const total = Math.min(Math.max(4, reuse + generated), maxRefs)
    return {
      totalCount: total,
      reusePreviousSceneCount: reuse,
      generateNewCount: Math.max(0, total - reuse),
      types: ['scene', 'previous_shot_frame', 'character', 'plot_key_frame'],
    }
  }
  const total = Math.min(complexShot ? 8 : 4, maxRefs)
  return { totalCount: total, reusePreviousSceneCount: 0, generateNewCount: total, types: ['character', 'scene', 'plot_key_frame'] }
}

function buildPreviewDecision(shot: Shot, prevShot: Shot | undefined, choice: ModeChoice): PreviewModeDecision {
  const autoPlan = referencePlanPreview(shot, prevShot)
  if (choice === 'REFERENCE_IMAGE_MODE' || choice === 'FIRST_LAST_FRAME_MODE') {
    const isReference = choice === 'REFERENCE_IMAGE_MODE'
    return {
      mode: choice,
      reason: isReference
        ? '已手动指定为参考图模式，本次优先用参考图维持人物与场景一致性。'
        : '已手动指定为首尾帧模式，本次将用首图和尾图强控动作起止状态。',
      confidence: 1,
      source: 'manual',
      evidence: isReference
        ? ['手动选择参考图模式', autoPlan.generateNewCount > 0 ? `预计新生成参考 ${autoPlan.generateNewCount} 张` : '优先复用已有参考']
        : ['手动选择首尾帧模式', '需要先通过首图/尾图评审'],
      scores: { reference: isReference ? 1 : 0, firstLast: isReference ? 0 : 1 },
      needReusePreviousScene: isReference && autoPlan.reusePreviousSceneCount > 0,
      needGenerateNewReferences: isReference && autoPlan.generateNewCount > 0,
      referenceImagePlan: isReference
        ? autoPlan
        : { totalCount: 0, reusePreviousSceneCount: 0, generateNewCount: 0, types: [] },
      forced: true,
    }
  }

  const text = textForRules(shot)
  const strongMatches = matchedWords(text, STRONG_WORDS)
  const lightMatches = matchedWords(text, LIGHT_WORDS)
  const sameScene = sceneContinues(prevShot, shot)
  const hasDialogue = shot.dialogues.length > 0
  const hasContinuity = Boolean(shot.continuity_from_prev)
  const transitionNeedsLanding = ['甩镜', '遮挡转场', '匹配剪辑', '闪黑', '闪白', '黑场', '变身'].some(word => shot.transition.includes(word))
  const hasFrameDescriptions = Boolean(shot.first_frame_desc?.trim() && shot.last_frame_desc?.trim())

  let firstLastScore = 0.34
  let referenceScore = 0.38
  const firstLastEvidence: string[] = []
  const referenceEvidence: string[] = []

  if (strongMatches.length) {
    firstLastScore += Math.min(0.3, strongMatches.length * 0.12)
    firstLastEvidence.push(`强动作/落点词：${strongMatches.join('、')}`)
  }
  if (hasContinuity) {
    firstLastScore += 0.26
    firstLastEvidence.push('接上镜：需要精确首帧衔接')
  }
  if (transitionNeedsLanding) {
    firstLastScore += 0.16
    firstLastEvidence.push(`转场需要卡落点：${shot.transition}`)
  }
  if (hasFrameDescriptions) {
    firstLastScore += 0.08
    firstLastEvidence.push('首帧/尾帧描述完整')
  }
  if (hasDialogue) {
    referenceScore += 0.16
    referenceEvidence.push(`对白 ${shot.dialogues.length} 句`)
  }
  if (lightMatches.length) {
    referenceScore += Math.min(0.2, lightMatches.length * 0.08)
    referenceEvidence.push(`轻动作/情绪词：${lightMatches.join('、')}`)
  }
  if (sameScene && !hasContinuity) {
    referenceScore += 0.14
    referenceEvidence.push('同场景续镜：优先保持人物和场景稳定')
  }
  if (shot.characters.length >= 2) {
    referenceScore += 0.08
    referenceEvidence.push(`多角色同场：${shot.characters.slice(0, 3).join('、')}`)
  }

  firstLastScore = roundedScore(firstLastScore)
  referenceScore = roundedScore(referenceScore)
  const mode: VideoModeValue = firstLastScore > referenceScore ? 'FIRST_LAST_FRAME_MODE' : 'REFERENCE_IMAGE_MODE'
  const gap = Math.abs(firstLastScore - referenceScore)
  const confidence = roundedScore(0.58 + gap * 0.45)

  if (mode === 'FIRST_LAST_FRAME_MODE') {
    const evidence = firstLastEvidence.length ? firstLastEvidence : ['画面起止状态比参考一致性更关键']
    return {
      mode,
      reason: `前端预估偏向首尾帧：${evidence[0]}。生成入队后会记录后台选择器的真实判定。`,
      confidence,
      source: 'frontend_preview',
      evidence,
      scores: { reference: referenceScore, firstLast: firstLastScore },
      needReusePreviousScene: false,
      needGenerateNewReferences: false,
      referenceImagePlan: { totalCount: 0, reusePreviousSceneCount: 0, generateNewCount: 0, types: [] },
    }
  }

  if (referenceEvidence.length) {
    return {
      mode,
      reason: `前端预估偏向参考图：${referenceEvidence[0]}。生成入队后会记录后台选择器的真实判定。`,
      confidence,
      source: 'frontend_preview',
      evidence: referenceEvidence,
      scores: { reference: referenceScore, firstLast: firstLastScore },
      needReusePreviousScene: autoPlan.reusePreviousSceneCount > 0,
      needGenerateNewReferences: autoPlan.generateNewCount > 0,
      referenceImagePlan: autoPlan,
    }
  }

  return {
    mode: 'REFERENCE_IMAGE_MODE',
    reason: '前端预估信息不足，默认偏向参考图以保持人物与画面风格稳定；生成入队后会记录后台选择器的真实判定。',
    confidence,
    source: 'frontend_preview',
    evidence: ['未命中强动作/强转场词', '默认保角色与场景一致'],
    scores: { reference: referenceScore, firstLast: firstLastScore },
    needReusePreviousScene: autoPlan.reusePreviousSceneCount > 0,
    needGenerateNewReferences: autoPlan.generateNewCount > 0,
    referenceImagePlan: autoPlan,
  }
}

function planSummary(plan?: { totalCount?: number; reusePreviousSceneCount?: number; generateNewCount?: number }) {
  if (!plan?.totalCount) return '不需要参考图'
  const reuse = plan.reusePreviousSceneCount ?? 0
  const generated = plan.generateNewCount ?? 0
  const parts = [`新生 ${generated} 张`]
  if (reuse > 0) parts.unshift(`复用 ${reuse} 张`)
  return `参考图 ${plan.totalCount} 张：${parts.join(' / ')}`
}

type UsedRef = NonNullable<NonNullable<ShotVersion['image_inputs']>['reference_images']>[number]

function refSourceLabel(source: string) {
  return source === 'previous_shot' ? '复用上镜'
    : source === 'seedream_generated' ? '新生成'
    : '定妆资产'
}

// 已生成版本：按真正发给视频模型的参考图，统计实际构成（定妆资产 / 复用上镜 / 新生成）。
function actualRefSummary(refs: UsedRef[]) {
  const reuse = refs.filter(r => r.source === 'previous_shot').length
  const fresh = refs.filter(r => r.source === 'seedream_generated').length
  const asset = refs.length - reuse - fresh
  const parts: string[] = []
  if (asset) parts.push(`定妆资产 ${asset}`)
  if (reuse) parts.push(`复用上镜 ${reuse}`)
  if (fresh) parts.push(`新生成 ${fresh}`)
  return `实际参考图 ${refs.length} 张${parts.length ? `：${parts.join(' · ')}` : ''}`
}

export default function WallPage() {
  const { episodeId, projectId, go, toast } = useNav()
  const { data: ep, refresh } = useEpisode(episodeId!, 5000)
  const [busy, setBusy] = useState(false)
  const [active, setActive] = useState(0)
  const carRef = useRef<HTMLDivElement>(null)
  const shots = ep?.shots ?? []
  const previewDecisions = shots.map((shot, idx) => buildPreviewDecision(shot, idx > 0 ? shots[idx - 1] : undefined, 'AUTO'))
  const firstLastShotIds = new Set(previewDecisions
    .map((decision, idx) => decision.mode === 'FIRST_LAST_FRAME_MODE' ? shots[idx]?.id : null)
    .filter(Boolean))
  const firstLastNeeded = firstLastShotIds.size
  const sceneApproved = shots.filter(s => firstLastShotIds.has(s.id) && s.scene_status === 'approved').length
  const videoReady = shots.filter(s => s.versions.some(v => v.status === 'succeeded')).length
  const keyframeTimer = useTaskTimer(`episode.${episodeId}.keyframes`, shots.some(s => s.scene_status === 'generating'))
  const videoTimer = useTaskTimer(`episode.${episodeId}.videos`, shots.some(s => s.versions.some(v => v.status === 'queued' || v.status === 'running')))

  if (!ep) return <div className="empty">展卷中……</div>

  const overLimit = ep.cost_limit_cny !== undefined && ep.cost_cny >= ep.cost_limit_cny

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    setBusy(true)
    try { await fn(); if (msg) toast(msg); refresh() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const goTo = (i: number) => {
    const idx = Math.max(0, Math.min(shots.length - 1, i))
    setActive(idx)
    const el = carRef.current
    if (el) el.scrollTo({ left: idx * el.clientWidth, behavior: 'smooth' })
  }
  const onScroll = () => {
    const el = carRef.current
    if (el) setActive(Math.round(el.scrollLeft / el.clientWidth))
  }

  return (
    <>
      <header className="desk-head">
        <EpisodeCrumb label="评审墙" view="wall" episodeNo={ep.episode_no} />
        <h1>评审墙 <span className="sub">一屏一镜 · 自动选择剧情参考或首尾帧控制 · 生成后在此复核视频与素材</span></h1>
        <hr className="rule" />
      </header>

      <section className="card" style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
        <div className="cost-ink">¥{ep.cost_cny.toFixed(1)} <small>/ 上限 ¥{ep.cost_limit_cny}</small></div>
        {overLimit && (
          <>
            <span className="stamp red">预算熔断</span>
            <button className="btn small" disabled={busy} onClick={() => act(async () => {
              const r = await api.post(`/episodes/${ep.id}/resume`); toast(`已恢复 ${r.resumed_jobs} 个任务（请先在监制房调高上限）`)
            })}>调高上限后恢复队列</button>
          </>
        )}
        <span style={{ fontSize: 13, color: 'var(--ink-soft)' }}>
          {firstLastNeeded
            ? <>首尾帧素材已备 {sceneApproved}/{firstLastNeeded} · 已有成片 {videoReady}/{shots.length}</>
            : <>本集自动预估以参考图模式为主 · 已有成片 {videoReady}/{shots.length}</>}
        </span>
        <span style={{ flex: 1 }} />
        {firstLastNeeded > 0 && (
          <button className="btn small" disabled={busy} onClick={() => act(async () => {
            keyframeTimer.start()
            const r = await api.post(`/episodes/${ep.id}/scenes-all`) as { started: number }
            toast(`已为 ${r.started} 个镜头生成首/尾帧候选（供首尾帧模式使用）`)
          })}>准备首尾帧素材</button>
        )}
        <button className="btn small primary" disabled={busy} onClick={() => act(async () => {
          videoTimer.start()
          const r = await api.post(`/episodes/${ep.id}/generate`) as { enqueued: { error?: string }[] }
          const ok = r.enqueued.filter(x => !x.error).length
          toast(`已入队 ${ok} 镜：系统会逐镜自动选择参考图模式或首尾帧模式`)
        })}>全片自动生成</button>
        <button className="btn small" onClick={() => go('cinema', projectId, episodeId)}>入成片台 →</button>
        <TaskTimer label="首尾帧" timer={keyframeTimer} />
        <TaskTimer label="视频生成" timer={videoTimer} />
      </section>

      <section className="card wall-mode-guide">
        <div className="wall-mode-head">
          <div>
            <b>视频生成有两种模式</b>
            <div className="hint" style={{ marginTop: 4 }}>
              全片批量生成时会逐镜自动判定；单镜右侧现在也可以直接看到推荐模式，并手动切换后单独生成。
            </div>
          </div>
          <span className="stamp grey">自动判定前置可见</span>
        </div>
        <div className="wall-mode-grid">
          <ModeGuideCard
            title="参考图模式"
            desc="更适合对白、剧情推进、场景延续和轻动作。"
            points={[
              '优先保持人物脸、服装、场景和气质稳定',
              '同场景续镜会优先复用上一镜素材',
              '必要时自动补生成新的参考图',
            ]}
          />
          <ModeGuideCard
            title="首尾帧模式"
            desc="更适合打斗、转场、变身、爆发和必须卡落点的镜头。"
            points={[
              '首图负责起势，尾图负责落点',
              '动作路径更受控，但需要先确认关键帧',
              '适合对起止状态要求很强的镜头',
            ]}
          />
        </div>
      </section>

      {/* 镜头分页导航 */}
      <div className="shot-pager">
        <button className="btn small" disabled={active <= 0} onClick={() => goTo(active - 1)}>← 上一镜</button>
        <span className="pg-no">镜 {String(shots[active]?.shot_no ?? active + 1).padStart(2, '0')} / {shots.length}</span>
        <button className="btn small" disabled={active >= shots.length - 1} onClick={() => goTo(active + 1)}>下一镜 →</button>
        <div className="shot-chips">
          {shots.map((s, i) => {
            const hasVideo = s.versions.some(v => v.status === 'succeeded')
            const cls = ['shot-chip', i === active ? 'active' : '', hasVideo ? 'has-video' : s.scene_status === 'approved' ? 'scene-ok' : ''].join(' ')
            return <div key={s.id} className={cls} title={`镜${s.shot_no}`} onClick={() => goTo(i)}>{String(s.shot_no).padStart(2, '0')}</div>
          })}
        </div>
      </div>

      <div className="shot-carousel" ref={carRef} onScroll={onScroll}>
        {shots.map((s, idx) => (
          <div className="shot-slide" key={s.id}>
            <ShotSlide shot={s} prevShot={idx > 0 ? shots[idx - 1] : undefined} onChanged={refresh} />
          </div>
        ))}
      </div>
    </>
  )
}

function ModeGuideCard({ title, desc, points }: { title: string; desc: string; points: string[] }) {
  return (
    <div className="mode-guide-card">
      <div className="mode-guide-title">{title}</div>
      <div className="mode-guide-desc">{desc}</div>
      <div className="mode-guide-points">
        {points.map(point => <span key={point} className="stamp grey">{point}</span>)}
      </div>
    </div>
  )
}

function ShotSlide({ shot, prevShot, onChanged }: { shot: Shot; prevShot?: Shot; onChanged: () => void }) {
  const { toast } = useNav()
  const [busy, setBusy] = useState(false)
  const [modeChoice, setModeChoice] = useState<ModeChoice>('AUTO')
  const sceneApproved = shot.scene_status === 'approved'
  const previewDecision = buildPreviewDecision(shot, prevShot, modeChoice)

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    setBusy(true)
    try { await fn(); if (msg) toast(msg); onChanged() }
    catch (e: unknown) { toast((e as Error).message, true) }
    finally { setBusy(false) }
  }

  const sceneStamp = sceneApproved ? ['首尾帧素材已备', 'green']
    : shot.scene_status === 'generating' ? ['首尾帧素材生成中', 'gold']
      : shot.scene_status === 'review' ? ['首尾帧素材待选', 'red'] : ['未生成首尾帧素材', 'grey']
  const videoDone = shot.versions.some(v => v.status === 'succeeded')

  return (
    <div className="slide-card">
      <div className="slide-head">
        <span className="sn">镜{String(shot.shot_no).padStart(2, '0')}</span>
        <span className="meta">{shot.duration_s}s · {shot.shot_size} · {shot.camera_move} · {shot.transition} · {shot.characters.join(' / ') || '缺角色'}</span>
        {previewDecision.mode === 'FIRST_LAST_FRAME_MODE' && <span className={`stamp ${sceneStamp[1]}`}>{sceneStamp[0]}</span>}
        {videoDone && <span className="stamp green">已有成片</span>}
      </div>

      <div className="slide-top">
        <div className="slide-left">
          <div className="kv"><b>场景</b>{shot.scene_setting}</div>
          <div className="kv"><b>画面</b>{shot.action_desc}</div>
          {shot.first_frame_desc && <div className="kv"><b>首帧</b>{shot.first_frame_desc}</div>}
          {shot.last_frame_desc && <div className="kv"><b>尾帧</b>{shot.last_frame_desc}</div>}
          {shot.narration && <div className="kv"><b>旁白</b>{shot.narration}</div>}
          {!!shot.dialogues.length && (
            <div className="kv"><b>台词</b>
              {shot.dialogues.map((d, i) => (
                <div key={i} className="dlg-line"><span className="dlg-speaker">{d.speaker}</span>「{d.line}」</div>
              ))}
            </div>
          )}
        </div>
        <div className="slide-right">
          <VideoPhase
            shot={shot}
            prevShot={prevShot}
            busy={busy}
            modeChoice={modeChoice}
            setModeChoice={setModeChoice}
            previewDecision={previewDecision}
            act={act}
          />
        </div>
      </div>

      {previewDecision.mode === 'FIRST_LAST_FRAME_MODE' && (
        <div className="slide-keyframes">
          <KeyframePhase shot={shot} busy={busy} act={act} />
        </div>
      )}
    </div>
  )
}

function KeyframePhase({ shot, busy, act }: {
  shot: Shot; busy: boolean; act: (fn: () => Promise<unknown>, msg?: string) => Promise<void>
}) {
  const done = (shot.scenes ?? []).filter(s => s.status === 'succeeded')
  const requiredKinds: ('head' | 'tail')[] = shot.required_keyframes?.length
    ? shot.required_keyframes
    : (shot.continuity_from_prev ? ['tail'] : ['head', 'tail'])
  const needsHead = requiredKinds.includes('head')
  const missingKinds = requiredKinds.filter(kind =>
    kind === 'head' ? !shot.approved_head_scene_id : !shot.approved_tail_scene_id
  )
  const targetKinds = missingKinds.length ? missingKinds : requiredKinds
  const targetLabel = targetKinds.map(kind => kind === 'head' ? '首图' : '尾图').join(' / ')
  const headDone = done.filter(s => s.kind === 'head')
  const tailDone = done.filter(s => s.kind === 'tail')
  const thumb = (sc: SceneCandidate, approvedId: string | null | undefined, what: string) => (
    <SceneThumb key={sc.id} scene={sc} approved={sc.id === approvedId} busy={busy}
      onApprove={() => act(() => api.post(`/shots/${shot.id}/scene/approve`, { scene_id: sc.id }), `已采用该${what}`)}
      onDelete={() => act(() => api.del(`/scenes/${sc.id}`), '已删除该首尾帧素材（素材删空时会一并删除本镜旧成片）')} />
  )

  return (
    <>
      <div className="kf-head">
        <b style={{ fontSize: 13 }}>首尾帧素材</b>
        <button className="btn small primary" disabled={busy || shot.scene_status === 'generating'}
          onClick={() => act(
            () => api.post(`/shots/${shot.id}/scene`, { kinds: targetKinds }),
            `已发起${targetLabel}生成（自动评审）`
          )}>
          {done.length ? `重新生成${targetLabel}` : `生成${targetLabel}`}
        </button>
        {shot.scene_status === 'generating' && <span className="stamp gold">生成中…</span>}
        {shot.scene_status === 'review' && <span style={{ fontSize: 12, color: 'var(--cinnabar-deep)' }}>评审未自动通过，请分别确认所需首图/尾图或重生/删除</span>}
        {shot.scene_status === 'approved' && <span style={{ fontSize: 12, color: 'var(--moss)' }}>已可用于首尾帧控制；普通剧情镜也可走参考图模式</span>}
        {!needsHead && <span style={{ fontSize: 12, color: 'var(--ink-faint)' }}>本镜首帧沿用上一镜尾图，只需审核尾图</span>}
      </div>
      {!done.length && shot.scene_status !== 'generating' && (
        <div style={{ fontSize: 12.5, color: 'var(--ink-faint)' }}>尚无首尾帧素材；普通剧情镜可直接走参考图模式生成视频。</div>
      )}
      {needsHead && !!headDone.length && (
        <>
          <div className="kf-group-label">首图候选（{headDone.length}）</div>
          <div className="kf-scroll">{headDone.map(sc => thumb(sc, shot.approved_head_scene_id, '首图'))}</div>
        </>
      )}
      {!!tailDone.length && (
        <>
          <div className="kf-group-label">尾图候选（{tailDone.length}）</div>
          <div className="kf-scroll">{tailDone.map(sc => thumb(sc, shot.approved_tail_scene_id, '尾图'))}</div>
        </>
      )}
    </>
  )
}

function SceneThumb({ scene, approved, busy, onApprove, onDelete }: {
  scene: SceneCandidate; approved: boolean; busy: boolean; onApprove: () => void; onDelete: () => void
}) {
  const overall = scene.qa?.overall
  const label = scene.kind === 'head' ? '首图' : '尾图'
  return (
    <div className={`scene-thumb ${approved ? 'approved' : ''}`}>
      <img src={scene.image_url} alt="" />
      <div className="st-body">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
          <span>{label} · 第{scene.version_no}版{overall !== undefined && overall >= 0 ? ` · ${overall.toFixed(2)}` : ''}</span>
          <span style={{ display: 'flex', gap: 4 }}>
            {approved ? <span className="stamp green" style={{ fontSize: 11 }}>采用</span>
              : <button className="btn small" disabled={busy} onClick={onApprove}>采用</button>}
            <button className="btn small ghost" disabled={busy} onClick={onDelete} title="删除这张首尾帧素材">删</button>
          </span>
        </div>
        {scene.qa?.issues?.length ? <div style={{ color: 'var(--ink-faint)', marginTop: 3 }}>{scene.qa.issues[0]}</div> : null}
      </div>
    </div>
  )
}

function VideoModeDetails({ imageInputs }: { imageInputs: NonNullable<ShotVersion['image_inputs']> }) {
  const mode = modeLabel(imageInputs.mode)
  const decision = imageInputs.mode_decision
  const refs = imageInputs.reference_images ?? []
  const sourceLabel = decision?.llmUsed
    ? '模型判定'
    : decision?.defaulted
      ? '规则兜底'
      : '未调用模型'
  const scoreLabel = decision?.llmUsed ? '模型置信' : '规则分'
  const reasonText = explainDecisionReason(decision?.reason, Boolean(decision?.llmUsed))
  return (
    <div className="mode-detail-card">
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="stamp grey">当前版本</span>
        <b style={{ color: 'var(--ink)' }}>{mode}</b>
        <span className="decision-pill">{sourceLabel}</span>
        {decision?.confidence !== undefined && <span className="decision-pill">{scoreLabel} {decision.confidence.toFixed(2)}</span>}
      </div>
      {reasonText && <div style={{ marginTop: 5 }}>{reasonText}</div>}
      {imageInputs.mode === 'REFERENCE_IMAGE_MODE' && (
        refs.length
          ? <div style={{ marginTop: 6, color: 'var(--ink)' }}>{actualRefSummary(refs)}</div>
          : decision?.referenceImagePlan
            ? <div style={{ marginTop: 6, color: 'var(--ink-faint)' }}>{planSummary(decision.referenceImagePlan)}</div>
            : null
      )}
      {!!refs.length && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(78px, 1fr))', gap: 8, marginTop: 8 }}>
          {refs.map(ref => (
            <div key={ref.id} title={`${refSourceLabel(ref.source)} · ${ref.type}${ref.qualityScore != null ? ` · QA ${ref.qualityScore.toFixed(2)}` : ''}`} style={{ minWidth: 0 }}>
              <div style={{ position: 'relative' }}>
                {ref.image_url
                  ? <img src={ref.image_url} alt="" style={{ width: '100%', aspectRatio: '3 / 4', objectFit: 'cover', borderRadius: 6, border: '1px solid var(--hairline)' }} />
                  : <div style={{ width: '100%', aspectRatio: '3 / 4', borderRadius: 6, border: '1px dashed var(--hairline)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--ink-faint)', fontSize: 11 }}>无图</div>}
                <span style={{ position: 'absolute', top: 4, left: 4, fontSize: 10, lineHeight: 1.4, padding: '1px 5px', borderRadius: 999, background: 'rgba(250,248,243,0.92)', border: '1px solid var(--hairline)', color: ref.source === 'previous_shot' ? 'var(--cinnabar-deep)' : 'var(--ink)' }}>{refSourceLabel(ref.source)}</span>
              </div>
              <div style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: 3 }}>{ref.type}</div>
              {ref.qualityScore != null && <div style={{ color: 'var(--ink-faint)' }}>QA {ref.qualityScore.toFixed(2)}</div>}
            </div>
          ))}
        </div>
      )}
      {imageInputs.fallback_reason && <div style={{ marginTop: 6, color: 'var(--cinnabar-deep)' }}>回退：{imageInputs.fallback_reason}</div>}
      {imageInputs.retry_reason && <div style={{ marginTop: 6, color: 'var(--cinnabar-deep)' }}>重试：{imageInputs.retry_reason}</div>}
      {!!imageInputs.reference_failure_logs?.length && (
        <div style={{ marginTop: 6, color: 'var(--cinnabar-deep)' }}>
          badcase：{imageInputs.reference_failure_logs[imageInputs.reference_failure_logs.length - 1]?.reason}
        </div>
      )}
    </div>
  )
}

function explainDecisionReason(reason?: string, llmUsed?: boolean) {
  if (!reason) return ''
  if (llmUsed) return reason
  if (reason.includes('dialogue') || reason.includes('light action')) {
    return '命中对白/轻动作规则，本版未调用后台模型改判。'
  }
  if (reason.includes('strong action') || reason.includes('transition') || reason.includes('end-state')) {
    return '命中强动作/转场/落点规则，本版未调用后台模型改判。'
  }
  if (reason.includes('exact first-frame handoff')) {
    return '命中接上镜规则，需要用上一镜尾图做精确首帧衔接。'
  }
  if (reason.includes('Default ordinary story shot')) {
    return '普通剧情镜规则兜底，优先保持人物与场景一致。'
  }
  if (reason.includes('Forced by video_generation_default_mode')) {
    return '由监制房默认视频模式强制指定。'
  }
  if (reason.includes('Reference image mode is disabled')) {
    return '参考图模式已在配置中关闭，自动改走首尾帧模式。'
  }
  return reason
}

function VideoPhase({ shot, prevShot, busy, modeChoice, setModeChoice, previewDecision, act }: {
  shot: Shot; prevShot?: Shot; busy: boolean
  modeChoice: ModeChoice
  setModeChoice: (mode: ModeChoice) => void
  previewDecision: PreviewModeDecision
  act: (fn: () => Promise<unknown>, msg?: string) => Promise<void>
}) {
  const [showVer, setShowVer] = useState<string | null>(null)
  const [editPrompt, setEditPrompt] = useState<string | null>(null)

  const adopted = shot.versions.find(v => v.id === shot.adopted_version_id)
  const current = showVer ? shot.versions.find(v => v.id === showVer)! : (adopted ?? shot.versions[0])
  const autoDecision = buildPreviewDecision(shot, prevShot, 'AUTO')
  const nextDecision = previewDecision
  const nextMode = nextDecision.mode
  const editingReferenceMode = nextMode === 'REFERENCE_IMAGE_MODE'
  const keyframeBlocked = nextMode === 'FIRST_LAST_FRAME_MODE' && shot.scene_status !== 'approved'
  const makeGenerateBody = (extra?: Record<string, unknown>) =>
    modeChoice === 'AUTO' ? (extra ?? {}) : { ...(extra ?? {}), mode_override: modeChoice }
  const generateLabel = current ? '再生成一版' : '生成本镜'
  const generateToast = modeChoice === 'AUTO'
    ? `已按自动判定入队（当前建议：${modeLabel(autoDecision.mode)}）`
    : `已按${modeLabel(modeChoice)}入队`

  return (
    <div className="video-panel">
      <div className="vp-media">
        {current?.video_url
          ? <video className="rev-video" src={current.video_url} controls loop muted playsInline />
          : <div className="vp-empty">
              {current?.status === 'running' || current?.status === 'queued'
                ? <><span className="stamp gold">生成中</span><span style={{ fontSize: 12 }}>{shot.duration_s}s · 约需数分钟</span></>
                : current?.status === 'failed' ? <span className="stamp red">失败</span>
                : current?.status === 'paused_budget' ? <span className="stamp red">预算暂停</span>
                : <span>等待生成视频<br /><span style={{ fontSize: 11 }}>可全片自动生成，也可在右侧先选模式后单独生成</span></span>}
            </div>}
      </div>
      <div className="vp-controls">
        {current?.qa && current.qa.overall >= 0 && (
          <div className="fc-qa" style={{ margin: 0 }}>质检 {current.qa.overall.toFixed(2)}{current.qa.issues?.length ? ` · ${current.qa.issues[0]}` : ''}</div>
        )}
        {current?.status === 'failed' && current.error && (
          <div className="error-banner" style={{ margin: 0, fontSize: 12 }}>{current.error}</div>
        )}
        {current?.image_inputs?.mode && (
          <VideoModeDetails imageInputs={current.image_inputs} />
        )}
        <div className="mode-detail-card">
          <div className="mode-preview-head">
            <b>下次生成</b>
            <span>{modeChoice === 'AUTO' ? '自动判定' : `手动指定：${modeLabel(modeChoice)}`}</span>
          </div>
          <div className="mode-choice-row">
            {([
              ['AUTO', '自动判定'],
              ['REFERENCE_IMAGE_MODE', '参考图'],
              ['FIRST_LAST_FRAME_MODE', '首尾帧'],
            ] as [ModeChoice, string][]).map(([value, label]) => (
              <button
                key={value}
                className={`mode-choice ${modeChoice === value ? 'active' : ''}`}
                onClick={() => setModeChoice(value)}
                type="button"
              >
                {label}
              </button>
            ))}
          </div>
          <div style={{ marginTop: 8, color: 'var(--ink)' }}>
            <b>{modeLabel(nextMode)}</b> · {modeSummary(nextMode)}
          </div>
          <div style={{ marginTop: 5 }}>{nextDecision.reason}</div>
          <div className="decision-score-row">
            {modeChoice === 'AUTO' && (
              <>
                <span className="decision-pill">前端预估 {nextDecision.confidence.toFixed(2)}</span>
                <span className="decision-pill">参考 {nextDecision.scores.reference.toFixed(2)}</span>
                <span className="decision-pill">首尾帧 {nextDecision.scores.firstLast.toFixed(2)}</span>
              </>
            )}
            {nextMode === 'REFERENCE_IMAGE_MODE' && <span className="decision-pill">{planSummary(nextDecision.referenceImagePlan)}</span>}
            {nextMode === 'FIRST_LAST_FRAME_MODE' && <span className="decision-pill">依赖已过审首图 / 尾图</span>}
          </div>
          {!!nextDecision.evidence.length && (
            <div className="decision-evidence">
              {nextDecision.evidence.map(item => <span key={item}>{item}</span>)}
            </div>
          )}
          {modeChoice === 'AUTO' && (
            <div style={{ marginTop: 6, color: 'var(--ink-faint)' }}>
              这里是页面预估；真正入队时后台选择器会再次判定，并在生成版本里显示“模型判定”或“规则兜底”。
            </div>
          )}
          {nextMode === 'REFERENCE_IMAGE_MODE' && (
            <div style={{ marginTop: 6, color: 'var(--ink-faint)' }}>
              参考图模式不需要先准备首尾帧素材；下方素材区已收起。
            </div>
          )}
          {modeChoice !== 'AUTO' && (
            <div style={{ marginTop: 6, color: 'var(--ink-faint)' }}>
              自动判定原本建议：{modeLabel(autoDecision.mode)}。{autoDecision.reason}
            </div>
          )}
          {nextMode === 'FIRST_LAST_FRAME_MODE' && shot.scene_status !== 'approved' && (
            <div style={{ marginTop: 6, color: 'var(--cinnabar-deep)' }}>
              当前还没有备齐可用首尾帧素材，先在下方完成首图 / 尾图评审，再生成本镜视频。
            </div>
          )}
        </div>
        {shot.versions.length > 1 && (
          <select style={{ width: '100%' }} value={current?.id ?? ''} onChange={e => setShowVer(e.target.value)}>
            {shot.versions.map(v => (
              <option key={v.id} value={v.id}>
                第{v.version_no}版 · {v.status}{v.id === shot.adopted_version_id ? ' · 已采用' : ''}{v.qa && v.qa.overall >= 0 ? ` · QA ${v.qa.overall.toFixed(2)}` : ''}
              </option>
            ))}
          </select>
        )}

        {editPrompt !== null ? (
          <>
            <div className="hint" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
              {editingReferenceMode
                ? '改词建议：保留参考图用途说明、角色/场景一致性和单一连贯动作约束，只调整动作细节。'
                : '改词建议：保留首尾帧素材约束、单一连贯动作和画面稳定/不跳切等约束，只调整动作细节。'}
            </div>
            <textarea rows={6} style={{ fontSize: 12.5 }} value={editPrompt} onChange={e => setEditPrompt(e.target.value)} />
            <div className="fc-actions">
              <button className="btn small primary" disabled={busy || keyframeBlocked}
                onClick={() => act(async () => {
                  await api.post(`/shots/${shot.id}/generate`, makeGenerateBody({ prompt_override: editPrompt }))
                  setEditPrompt(null)
                }, `已按修改后的 prompt 入队（${modeLabel(nextMode)}）`)}>提交生成</button>
              <button className="btn small ghost" onClick={() => setEditPrompt(null)}>放弃</button>
            </div>
          </>
        ) : (
          <div className="fc-actions">
            <button className="btn small primary" disabled={busy || keyframeBlocked}
              onClick={() => act(() => api.post(`/shots/${shot.id}/generate`, makeGenerateBody()), generateToast)}>{generateLabel}</button>
            {shot.video_stale && (
              <button className="btn small primary" disabled={busy}
                onClick={() => act(() => api.post(`/episodes/${shot.episode_id}/generate`, { from_shot_no: shot.shot_no }), '已从本镜起、沿连续段往后重生')}>从此镜往后重生</button>
            )}
            {current && current.status === 'succeeded' && current.id !== shot.adopted_version_id && (
              <button className="btn small primary" disabled={busy}
                onClick={() => act(() => api.post(`/shots/${shot.id}/adopt`, { version_id: current.id }), '已采用该版本')}>采用此版</button>
            )}
            {current?.video_url && (
              <a className="btn small" href={current.video_url}
                download={`镜${String(shot.shot_no).padStart(2, '0')}_第${current.version_no}版.mp4`}
                style={{ textDecoration: 'none' }}>导出</a>
            )}
            {shot.versions.length > 0 && (
              <>
                <button className="btn small primary" disabled={busy || keyframeBlocked}
                  onClick={() => act(() => api.post(`/shots/${shot.id}/generate`, makeGenerateBody({ with_critique: true })), `已按${modeLabel(nextMode)}带评语重生`)}>带评语重生</button>
                <button className="btn small" disabled={busy} onClick={() => setEditPrompt(current?.prompt_text ?? '')}>改词重生</button>
                <button className="btn small" disabled={busy || keyframeBlocked}
                  onClick={() => act(() => api.post(`/shots/${shot.id}/generate`, makeGenerateBody({ reroll: true })), `已按${modeLabel(nextMode)}原词重抽`)}>原词重抽</button>
                {current && (
                  <button className="btn small ghost" disabled={busy}
                    onClick={() => { if (window.confirm(`删除镜${shot.shot_no} 第${current.version_no}版视频？`)) act(() => api.del(`/versions/${current.id}`), '已删除该视频版本') }}>删除此版</button>
                )}
              </>
            )}
          </div>
        )}
        {current?.image_inputs?.first_frame_used && (
          <div style={{ fontSize: 11, color: 'var(--ink-faint)' }}>
            首帧：{current.image_inputs.first_frame_src === 'prev_tail_keyframe' ? '上一镜尾图' : '本镜首图'}
            {current.image_inputs.last_frame_used ? ' · 尾帧：本镜尾图' : ''}
          </div>
        )}
        {shot.video_stale && <div style={{ fontSize: 11.5, color: 'var(--cinnabar-deep)' }}>首尾帧素材已变更，本镜视频链已过期，建议「从此镜往后重生」</div>}
      </div>
    </div>
  )
}
