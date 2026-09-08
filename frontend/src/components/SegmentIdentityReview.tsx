import { useState } from 'react'
import { api, type StoryboardPackSegment } from '../api'

type Review = { baseline: string; segment: StoryboardPackSegment; issues: string[]; versions: { id: string; version_no: number; status: string; prompt_text: string; video_url?: string; observations?: { notes: string }[]; reference_images?: { label: string; url?: string }[] }[] }
type Props = { shotId: string; onSaved: () => void; notify: (message: string, error?: boolean) => void }
const speechKinds = { spoken_dialogue: '画内对白', offscreen_dialogue: '人物画外对白', inner_monologue: '内心独白', narration: '旁白' }
const versionStatuses: Record<string, string> = { succeeded: '已生成', stale: '历史版本', queued: '排队中', running: '生成中', failed: '生成失败' }

export default function SegmentIdentityReview({ shotId, onSaved, notify }: Props) {
  const [review, setReview] = useState<Review | null>(null)
  const [candidate, setCandidate] = useState<StoryboardPackSegment | null>(null)
  const [busy, setBusy] = useState(false)
  const [previewed, setPreviewed] = useState(false)
  const [notes, setNotes] = useState('')
  const [versionId, setVersionId] = useState('')
  const [extraLabel, setExtraLabel] = useState('')
  const base = `/shots/${shotId}/identity-review`

  async function perform(action: () => Promise<void>) {
    setBusy(true)
    try { await action() } catch (error) { notify(error instanceof Error ? error.message : '操作失败，请重试', true) }
    finally { setBusy(false) }
  }
  function update(next: StoryboardPackSegment) { setCandidate(next); setPreviewed(false) }
  async function open() {
    const result = await api.get(base) as Review
    setReview(result); setCandidate(result.segment); setPreviewed(false)
    setVersionId(result.versions[0]?.id || '')
  }
  async function generate() {
    if (!review) return
    const result = await api.post(`${base}/regenerate`, { baseline: review.baseline }) as { candidate: StoryboardPackSegment }
    update(result.candidate)
    notify('本段候选已生成，请核对说话人与画面人物，再预览保存')
  }
  async function preview() {
    if (!review || !candidate) return
    const result = await api.post(`${base}/preview`, { baseline: review.baseline, candidate }) as { candidate: StoryboardPackSegment }
    setCandidate(result.candidate); setPreviewed(true)
  }
  async function save() {
    if (!review || !candidate || !previewed) return
    await api.post(`${base}/apply`, { baseline: review.baseline, candidate })
    notify('本段修订已保存，旧视频保留为历史版本；可前往生成台生成本段视频')
    setReview(null); setCandidate(null); onSaved()
  }
  const editable = Boolean(candidate?.identity_contract_version)
  return <section className="segment-identity-review" aria-label="发声与群演复核">
    <button type="button" disabled={busy} onClick={() => void perform(open)}>复核说话人和群演</button>
    {review && candidate && <div>
      <p>核对每句由谁发声、哪些人物实际出镜。保存只更新本段，旧视频保留供对比。</p>
      {review.issues.length > 0 && <ul>{review.issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>}
      {!editable && <p>这是旧版片段。先生成本段修订候选，补齐说话人与出镜人物关系。</p>}
      <button type="button" disabled={busy} onClick={() => void perform(generate)}>{busy ? '处理中…' : '仅重新编写本段（调用文本模型）'}</button>
      {editable && <>
        <fieldset disabled={busy}><legend>每句由谁发声</legend>
          {candidate.dialogue.map((line, i) => <div key={line.utterance_id || i}>
            <p>{line.line}</p>
            <small>原文第 {line.source_segment_index} 段 · {line.attribution_evidence || '请结合原文核对归属'}</small>
            <label>说话人 <select value={line.speaker_identity_id} onChange={event => {
              const id = event.target.value
              const kind = id === '旁白' ? 'narration' : line.delivery_kind === 'narration' ? 'offscreen_dialogue' : line.delivery_kind
              update({ ...candidate, dialogue: candidate.dialogue.map((d, j) => j === i ? { ...d, speaker_identity_id: id, delivery_kind: kind, delivery: kind === 'spoken_dialogue' ? 'spoken_dialogue' : 'offscreen_voice' } : d) })
            }}>
              <option value="旁白">旁白</option>
              {candidate.resources.characters.map(c => <option key={c.identity_id} value={c.identity_id}>{c.display_name || c.identity_id}</option>)}
            </select></label>
            <label>发声方式 <select value={line.delivery_kind || ''} onChange={event => {
              const kind = event.target.value as keyof typeof speechKinds
              update({ ...candidate, dialogue: candidate.dialogue.map((d, j) => j === i ? { ...d, delivery_kind: kind, delivery: kind === 'spoken_dialogue' ? 'spoken_dialogue' : 'offscreen_voice' } : d) })
            }}><option value="" disabled>请选择</option>{Object.entries(speechKinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          </div>)}
        </fieldset>
        <fieldset disabled={busy}><legend>画面里有哪些人</legend>
          {candidate.resources.characters.map((character, i) => <label key={character.identity_id} style={{ display: 'block' }}>
            {character.display_name || character.identity_id} · {character.subject_kind === 'character' ? '已确认角色' : '独立群演'}
            <select value={character.visibility || 'unknown'} onChange={event => {
              const visibility = event.target.value as 'visible' | 'voice_only'
              update({ ...candidate, resources: { ...candidate.resources, characters: candidate.resources.characters.map((c, j) => j === i ? { ...c, visibility } : c) } })
            }}><option value="unknown" disabled>请选择</option><option value="visible">实际出镜</option><option value="voice_only">仅有声音</option></select>
          </label>)}
          <label>补充独立群演称谓 <input value={extraLabel} onChange={e => setExtraLabel(e.target.value)} placeholder="使用原文里的称谓" /></label>
          <button type="button" disabled={!extraLabel.trim() || candidate.resources.characters.some(c => c.identity_id === extraLabel.trim())} onClick={() => {
            const label = extraLabel.trim()
            update({ ...candidate, resources: { ...candidate.resources, characters: [...candidate.resources.characters, { identity_id: label, display_name: label, visibility: 'visible', subject_kind: 'extra', description: label }] } }); setExtraLabel('')
          }}>添加独立群演</button>
        </fieldset>
        <label>片段镜头稿（保留每句台词的发声位置标记）<textarea rows={8} value={candidate.speech_template || candidate.prompt_text} onChange={event => update({ ...candidate, speech_template: event.target.value })} /></label>
        <button type="button" disabled={busy} onClick={() => void perform(preview)}>校验并预览修订</button>
        {previewed && <div><pre style={{ whiteSpace: 'pre-wrap' }}>{candidate.prompt_text}</pre><button type="button" disabled={busy} onClick={() => void perform(save)}>保存本段修订</button></div>}
      </>}
      {!!review.versions.length && <details><summary>成片与实际提交记录</summary>
        <select aria-label="视频版本" value={versionId} onChange={event => setVersionId(event.target.value)}>{review.versions.map(v => <option key={v.id} value={v.id}>版本 {v.version_no} · {versionStatuses[v.status] || '处理中'}</option>)}</select>
        {review.versions.filter(v => v.id === versionId).map(v => <div key={v.id}>{v.video_url && <video controls preload="none" src={v.video_url} style={{ maxWidth: '100%', maxHeight: 360 }} />}<pre style={{ whiteSpace: 'pre-wrap' }}>{v.prompt_text}</pre>
          {v.reference_images?.map((ref, i) => <figure key={i}>{ref.url && <img src={ref.url} alt={ref.label} loading="lazy" />}<figcaption>{ref.label}</figcaption></figure>)}
          {v.observations?.map((observation, i) => <p key={i}>复核记录：{observation.notes}</p>)}
        </div>)}
        <label>视听复核记录<textarea value={notes} onChange={e => setNotes(e.target.value)} placeholder="记录实际听到的说话人、看到的人物，以及无法判断的部分" /></label>
        <button type="button" disabled={busy || !notes.trim() || !versionId} onClick={() => void perform(async () => {
          await api.post(`${base}/observation`, { version_id: versionId, notes }); notify('复核记录已保存，不会自动重抽')
          setReview({ ...review, versions: review.versions.map(v => v.id === versionId ? { ...v, observations: [...(v.observations || []), { notes }] } : v) }); setNotes('')
        })}>保存复核记录</button>
      </details>}
      <button type="button" disabled={busy} onClick={() => { setReview(null); setCandidate(null) }}>关闭复核</button>
    </div>}
  </section>
}
