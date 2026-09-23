import { useId, useState } from 'react'
import { ApiError, type Dialogue, type StoryboardPackSegment } from '../api'
import {
  previewShotEditImpact, startShotEditSession, updateShot,
  type ShotEditImpactChanged, type ShotEditSessionStart,
} from '../api/storyboard/shotEditSession'
import { useFocusTrap } from '../hooks/useFocusTrap'

/**
 * 台词人工修订入口（分镜台唯一消费方，见 StoryboardPackSegmentView.tsx「台词 N 条」
 * 展开区）：视频供应商因台词原句触发 InputTextSensitiveContentDetected 时，画面
 * 描述改了也没用，这是唯一能改措辞的界面入口。三步握手照抄
 * app/storyboard_workspace.py 的契约：edit-session 起草租约、impact-preview 校验
 * 并冻结这次要提交的 patch、PUT 提交时 patch 必须与预览时的 changes 逐字相同，
 * 否则后端 409（见 edit_shot.py）。这里用 previewedPatch 存住预览通过的那份
 * patch 对象本身、保存时原样重放，不重新从 lines 计算，从结构上保证「逐字相同」。
 */
const BLOCKED_REASON = '本段没有台词模板（旧产物），不支持台词修订，请重新生成本段分镜'

interface DialoguePatchLine { speaker: string; line: string; emotion: string; delivery: string }

function fallbackSpeakerName(identityId: string): string {
  const idx = identityId.indexOf(':')
  return idx >= 0 ? identityId.slice(idx + 1) : identityId
}

/** 与 app/production/storyboard_speech_render.py::speaker_names 逐条对齐：未登记
 *  在 resources.characters 里的 identity 原样返回，不做任何裁剪。 */
function speakerDisplayName(segment: StoryboardPackSegment, identityId: string): string {
  if (identityId === '旁白') return '旁白'
  const character = segment.resources.characters.find(c => c.identity_id === identityId)
  if (!character) return identityId
  return character.display_name || fallbackSpeakerName(identityId)
}

/** emotion/delivery 一律原样回传 shots.dialogues 里已存的值：PUT 会整列替换
 *  dialogues，凭空填一个「平静」就是把别处写进去的真实值悄悄抹平（CLAUDE.md
 *  「不得兜底填充」）。长度对不上（旧产物）时才退回段落合同里的 delivery。 */
function buildDialoguePatch(
  segment: StoryboardPackSegment, lines: string[], stored: Dialogue[],
): DialoguePatchLine[] {
  const aligned = stored.length === segment.dialogue.length ? stored : null
  return segment.dialogue.map((line, index) => ({
    speaker: speakerDisplayName(segment, line.speaker_identity_id),
    line: lines[index],
    emotion: aligned?.[index]?.emotion ?? '平静',
    delivery: aligned?.[index]?.delivery || line.delivery || 'spoken_dialogue',
  }))
}

type Props = {
  shotId: string
  segment: StoryboardPackSegment
  /** shots.dialogues 当前落库值：修订只改 line，其余字段原样回传。 */
  shotDialogues: Dialogue[]
  expectedVersion?: string | null
  notify: (message: string, error?: boolean) => void
  onSaved: () => void
}

export default function SegmentDialogueRevision({
  shotId, segment, shotDialogues, expectedVersion, notify, onSaved,
}: Props) {
  const [openState, setOpenState] = useState(false)
  const [session, setSession] = useState<ShotEditSessionStart | null>(null)
  const [lines, setLines] = useState<string[]>(() => segment.dialogue.map(d => d.line))
  const [reason, setReason] = useState('')
  const [previewResult, setPreviewResult] = useState<ShotEditImpactChanged | null>(null)
  const [previewedPatch, setPreviewedPatch] = useState<DialoguePatchLine[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const titleId = useId()

  function closeDialog() {
    setOpenState(false)
    setSession(null)
  }
  const trapRef = useFocusTrap(openState, closeDialog)

  function invalidatePreview() {
    setPreviewResult(null)
    setPreviewedPatch(null)
  }
  function updateLine(index: number, value: string) {
    setLines(prev => prev.map((line, i) => (i === index ? value : line)))
    invalidatePreview()
  }
  function captureError(err: unknown) {
    setError(err instanceof Error ? err.message : String(err))
    setErrorCode(err instanceof ApiError ? err.code ?? null : null)
  }

  /** 起草/重新换取编辑租约：只负责拿 session，绝不清空已输入的台词与修订原因。
   *  租约过期或撞上新基线（app/storyboard_workspace.py::require_edit_session 的
   *  两种 409）之后，靠这个函数原地重试——草稿必须原样保留，不能逼用户重新走
   *  openDialog() 从 segment 原文重置。 */
  async function acquireSession() {
    invalidatePreview()
    setBusy(true)
    setError(null)
    setErrorCode(null)
    try {
      setSession(await startShotEditSession(shotId))
    } catch (err) {
      captureError(err)
    } finally {
      setBusy(false)
    }
  }

  async function openDialog() {
    setOpenState(true)
    setLines(segment.dialogue.map(d => d.line))
    setReason('')
    await acquireSession()
  }

  async function runPreview() {
    if (!session) return
    setBusy(true)
    setError(null)
    setErrorCode(null)
    try {
      const patch = buildDialoguePatch(segment, lines, shotDialogues)
      const result = await previewShotEditImpact(shotId, {
        edit_session_token: session.edit_session_token,
        changes: { dialogues: patch },
      })
      if (result.unchanged) {
        setError('内容未变化，无需保存')
        invalidatePreview()
      } else {
        setPreviewResult(result)
        setPreviewedPatch(patch)
      }
    } catch (err) {
      captureError(err)
      invalidatePreview()
    } finally {
      setBusy(false)
    }
  }

  async function runSave() {
    if (!session || !previewResult || !previewedPatch || !reason.trim()) return
    setBusy(true)
    setError(null)
    setErrorCode(null)
    try {
      const body: Record<string, unknown> = {
        dialogues: previewedPatch,
        edit_session_token: session.edit_session_token,
        preview_token: previewResult.preview_token,
        baseline_content_hash: session.baseline_content_hash,
        revision_reason: reason.trim(),
      }
      if (expectedVersion) body.expected_version = expectedVersion
      await updateShot(shotId, body as Parameters<typeof updateShot>[1])
      notify('本段台词已修订，原句已留档；可回生成台重新生成本段视频')
      onSaved()
      closeDialog()
    } catch (err) {
      captureError(err)
    } finally {
      setBusy(false)
    }
  }

  const blocked = !segment.speech_template
  const dirtyFlags = lines.map((line, i) => line !== segment.dialogue[i]?.line)
  const dirtyCount = dirtyFlags.filter(Boolean).length
  const canPreview = !busy && !!session && dirtyCount > 0 && !!reason.trim()
  const canSave = !busy && !!previewResult && !!reason.trim()

  return (
    <div className="dialogue-revision-entry">
      <button type="button" className="text-action" disabled={blocked} onClick={() => void openDialog()}>
        修订台词
      </button>
      {blocked && <p className="dialogue-revision-blocked-hint">{BLOCKED_REASON}</p>}
      {openState && (
        <div
          className="evidence-backdrop"
          role="presentation"
          onMouseDown={event => { if (event.currentTarget === event.target) closeDialog() }}
        >
          <section
            ref={trapRef}
            className="impact-dialog decision-dialog dialogue-revision-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
          >
            <h3 id={titleId}>修订本段台词</h3>
            <p className="dialogue-revision-rule-hint">
              只改措辞，不能增删条数、不能改发声者；原句会留档供溯源。保存后：修订前采用的
              视频（没有采用版本时为上次修订保留的旧版）会转为<b>过期保留</b>（每镜最多 1 个，
              仅供对照、不可再采纳）；其余候选视频与参考图将被<b>永久删除、无法恢复</b>；
              具体数量以下方预览为准，保存后需重新生成本段。
            </p>
            {error && (
              <p className="field-error" role="alert">
                {error}
                {errorCode === 'STALE_EDIT_BASELINE' && '（本地草稿仍保留；该镜头在此期间有新内容变化，请核对无误后再继续，避免覆盖他人改动）'}
              </p>
            )}
            {error && !busy && (
              <button type="button" className="text-action" onClick={() => void acquireSession()}>
                重新获取编辑租约
              </button>
            )}
            {!session && busy && <p className="dialogue-revision-blocked-hint">正在准备编辑会话…</p>}
            {session && (
              <DialogueRevisionBody
                segment={segment} lines={lines} dirtyFlags={dirtyFlags} busy={busy}
                onChangeLine={updateLine} previewResult={previewResult}
              />
            )}
            {session && (
              <label className="dialogue-revision-reason">
                修订原因
                <input
                  value={reason} disabled={busy} placeholder="供应商合规拒收，改措辞"
                  onChange={event => setReason(event.target.value)}
                />
              </label>
            )}
            <div className="dialog-actions">
              <button type="button" className="btn" disabled={busy} onClick={closeDialog}>取消</button>
              <button type="button" className="btn" disabled={!canPreview} onClick={() => void runPreview()}>
                校验并预览影响
              </button>
              <button type="button" className="btn danger" disabled={!canSave} onClick={() => void runSave()}>
                确认保存
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}

function DialogueRevisionBody({ segment, lines, dirtyFlags, busy, onChangeLine, previewResult }: {
  segment: StoryboardPackSegment
  lines: string[]
  dirtyFlags: boolean[]
  busy: boolean
  onChangeLine: (index: number, value: string) => void
  previewResult: ShotEditImpactChanged | null
}) {
  // 删除数/保留数一律读后端算好的值（app/domain/storyboard_ops/shot_edit_session.py
  // 与保存路径共用同一个 dialogue_revision_preserved_version 判据），前端不重新推算——
  // 前端猜的话，「没有采用版本但有上一次修订保留版本」这类分支必然猜错（CLAUDE.md
  // 「界面承诺必须与实际行为一致」）。
  const deletedVideoCount = previewResult?.by_artifact_type['视频版本'] ?? 0
  const retainedVideoCount = previewResult?.by_artifact_type['保留视频版本'] ?? 0
  return (
    <>
      <ul className="dialogue-revision-list">
        {segment.dialogue.map((line, i) => (
          <li key={line.utterance_id || i} className={`dialogue-revision-card${dirtyFlags[i] ? ' dirty' : ''}`}>
            <div className="dialogue-revision-card-head">
              <span className="dialogue-revision-speaker">{speakerDisplayName(segment, line.speaker_identity_id)}</span>
              <span className="dialogue-revision-source">原文第 {line.source_segment_index} 段</span>
              {dirtyFlags[i] && <span className="dialogue-revision-badge">已改</span>}
            </div>
            <textarea
              className="dialogue-revision-textarea" value={lines[i]} disabled={busy}
              onChange={event => onChangeLine(i, event.target.value)}
            />
            {dirtyFlags[i] && (
              <button type="button" className="text-action" disabled={busy} onClick={() => onChangeLine(i, line.line)}>
                还原本句
              </button>
            )}
          </li>
        ))}
      </ul>
      {previewResult && (
        <div className="review-impact danger">
          <b>影响预览</b>
          <ul>
            <li>参考图将被永久删除 {previewResult.by_artifact_type['参考图'] ?? 0} 项</li>
            <li>候选视频版本将被永久删除 {deletedVideoCount} 项</li>
            {retainedVideoCount > 0 && <li>视频版本将转为过期保留 {retainedVideoCount} 项（仅供对照，不可再采纳）</li>}
            <li>证据链下游会失效 {previewResult.by_artifact_type['证据链'] ?? 0} 项</li>
          </ul>
          {deletedVideoCount > 0 && (
            <p>
              本段候选视频版本（{deletedVideoCount} 个）将被
              <b>永久删除，无法恢复</b>。
            </p>
          )}
        </div>
      )}
    </>
  )
}
