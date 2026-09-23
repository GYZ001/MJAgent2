import { createElement } from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type StoryboardPackSegment } from '../api'
import {
  previewShotEditImpact, startShotEditSession, updateShot,
} from '../api/storyboard/shotEditSession'

// 与 SegmentIdentityReview.test.ts 同一套写法：environment 是 'node'（无 jsdom），
// 不能用 @testing-library，靠 react-test-renderer 直接摸 props.onClick/onChange。
vi.mock('../api/storyboard/shotEditSession', () => ({
  startShotEditSession: vi.fn(),
  previewShotEditImpact: vi.fn(),
  updateShot: vi.fn(),
}))
// 本组件是真正的弹窗（useFocusTrap 摸 document），照抄 modalLayering.render.test.ts
// 的先例：vitest environment:'node' 没有 document，装一个空 ref 的 stub。mock 必须
// 排在 import 组件之前。
vi.mock('../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import SegmentDialogueRevision from './SegmentDialogueRevision'

afterEach(() => vi.resetAllMocks())

const SEGMENT: StoryboardPackSegment = {
  segment_no: 3, duration_s: 15, synopsis: '', source_segment_indexes: [1],
  speech_template: '镜头1：@听听 蹲在台边{{speech:U01}}\n镜头2：@龙猫 答{{speech:U02}}',
  prompt_text: '提示词', shot_count: 1,
  dialogue: [
    { utterance_id: 'U01', speaker_identity_id: 'bible:听听', line: '那根越来越粗了。', source_segment_index: 1, delivery: 'spoken_dialogue' },
    { utterance_id: 'U02', speaker_identity_id: 'bible:龙猫', line: '还不是时候。', source_segment_index: 2, delivery: 'spoken_dialogue' },
  ],
  resources: {
    characters: [
      { identity_id: 'bible:听听', display_name: '听听' },
      { identity_id: 'bible:龙猫', display_name: '龙猫' },
    ],
    scenes: [], props: [],
  },
  degraded_capabilities: [], beats: [], beat_ids: [], target_model: 'seedance_2', storyboard_version: '2.4.1',
}

// 刻意不是默认的「平静」：修订只改 line，emotion/delivery 必须原样回传，
// 否则 PUT 整列替换 dialogues 会把别处写进去的真实值悄悄抹平。
const STORED_DIALOGUES = [
  { speaker: '听听', line: '那根越来越粗了。', emotion: '急切', delivery: 'spoken_dialogue' },
  { speaker: '龙猫', line: '还不是时候。', emotion: '低沉', delivery: 'offscreen_voice' },
]

const SESSION = { edit_session_token: 'tok-1', baseline_artifact_id: 'art-1', baseline_content_hash: 'hash-1', lease_expires_at: 999 }
const PREVIEW = {
  unchanged: false as const, changed_fields: ['dialogues'], normalized_changes: {},
  baseline_artifact_id: 'art-1', baseline_content_hash: 'hash-1', requires_reconfirm: true,
  paid_media_invalidated: true, stale_descendant_ids: ['d1', 'd2'], stale_count: 2,
  by_artifact_type: { 参考图: 1, 视频版本: 2, 证据链: 2 },
  preview_token: 'ptok-1', preview_expires_at: 999,
}

function mount(segment: StoryboardPackSegment) {
  const notify = vi.fn()
  const onSaved = vi.fn()
  let view!: TestRenderer.ReactTestRenderer
  act(() => {
    view = TestRenderer.create(createElement(SegmentDialogueRevision, {
      shotId: 's1', segment, shotDialogues: STORED_DIALOGUES, expectedVersion: 'art-1', notify, onSaved,
    }))
  })
  return { view, notify, onSaved }
}

function buttonText(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === 'string' ? c : '')).join('').trim()
}
/** 提示文案里混了 <b> 强调，不能只取字符串子节点，要递归拼出全部文本。 */
function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === 'string' ? c : textOf(c))).join('')
}
function findButtons(view: TestRenderer.ReactTestRenderer, label: string) {
  return view.root.findAll(n => n.type === 'button' && buttonText(n) === label)
}
async function openDialog(view: TestRenderer.ReactTestRenderer) {
  const button = findButtons(view, '修订台词')[0]
  await act(async () => { button.props.onClick(); await Promise.resolve() })
}
async function clickButton(view: TestRenderer.ReactTestRenderer, label: string) {
  const button = findButtons(view, label)[0]
  expect(button, `按钮「${label}」应存在`).toBeDefined()
  await act(async () => { button.props.onClick(); await Promise.resolve() })
}
function editLine(view: TestRenderer.ReactTestRenderer, index: number, value: string) {
  const textarea = view.root.findAllByType('textarea')[index]
  act(() => { textarea.props.onChange({ target: { value } }) })
}
function setReason(view: TestRenderer.ReactTestRenderer, value: string) {
  const input = view.root.findAllByType('input')[0]
  act(() => { input.props.onChange({ target: { value } }) })
}

describe('段落没有台词模板（旧产物）', () => {
  it('修订台词按钮禁用，并逐字给出后端那句拒绝原因', () => {
    const { view } = mount({ ...SEGMENT, speech_template: '' })
    const button = findButtons(view, '修订台词')[0]
    expect(button.props.disabled).toBe(true)
    const hint = view.root.findAll(n => n.props.className === 'dialogue-revision-blocked-hint')[0]
    expect(hint.children.join('')).toBe('本段没有台词模板（旧产物），不支持台词修订，请重新生成本段分镜')
    act(() => view.unmount())
  })
})

describe('预览与保存的两段式门禁', () => {
  it('改过一句但还没预览时，确认删除并保存保持禁用', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    const { view } = mount(SEGMENT)
    await openDialog(view)
    expect(findButtons(view, '确认删除并保存')[0].props.disabled).toBe(true)
    setReason(view, '供应商合规拒收')
    editLine(view, 0, '那根线越来越粗了。')
    expect(findButtons(view, '确认删除并保存')[0].props.disabled).toBe(true)
    act(() => view.unmount())
  })

  it('预览通过后再改一句，预览作废、确认删除并保存重新变禁用', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    vi.mocked(previewShotEditImpact).mockResolvedValue(PREVIEW)
    const { view } = mount(SEGMENT)
    await openDialog(view)
    setReason(view, '供应商合规拒收')
    editLine(view, 0, '那根线越来越粗了。')
    await clickButton(view, '校验并预览影响')
    expect(findButtons(view, '确认删除并保存')[0].props.disabled).toBe(false)
    editLine(view, 1, '还不是时机。')
    expect(findButtons(view, '确认删除并保存')[0].props.disabled).toBe(true)
    act(() => view.unmount())
  })
})

describe('409 防护：保存的 patch 必须与预览的 changes 逐字相同', () => {
  it('PUT 的 dialogues 与 POST 的 changes.dialogues 深度相等', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    vi.mocked(previewShotEditImpact).mockResolvedValue(PREVIEW)
    vi.mocked(updateShot).mockResolvedValue({ ok: true, artifact_id: 'art-2', impact: {} })
    const { view, notify, onSaved } = mount(SEGMENT)
    await openDialog(view)
    setReason(view, '供应商合规拒收')
    editLine(view, 0, '那根线越来越粗了。')
    await clickButton(view, '校验并预览影响')
    await clickButton(view, '确认删除并保存')

    const previewCall = vi.mocked(previewShotEditImpact).mock.calls[0]
    const updateCall = vi.mocked(updateShot).mock.calls[0]
    expect(updateCall[1].dialogues).toEqual(previewCall[1].changes.dialogues)
    // 发声者必须是展示名（听听/龙猫），不是段落里的 identity_id（bible:听听）——
    // 这是 revisions_from_dialogues 的发声者一致性判据要求的形状。
    // emotion/delivery 取落库值（急切/低沉、offscreen_voice）原样回传，不是前端
    // 现编的默认值：PUT 会整列替换 dialogues，兜底填充等于静默抹平真实数据。
    expect(previewCall[1].changes.dialogues).toEqual([
      { speaker: '听听', line: '那根线越来越粗了。', emotion: '急切', delivery: 'spoken_dialogue' },
      { speaker: '龙猫', line: '还不是时候。', emotion: '低沉', delivery: 'offscreen_voice' },
    ])
    expect(updateCall[1].preview_token).toBe('ptok-1')
    expect(updateCall[1].baseline_content_hash).toBe('hash-1')
    expect(updateCall[1].revision_reason).toBe('供应商合规拒收')
    expect(notify).toHaveBeenCalledWith('本段台词已修订，原句已留档；可回生成台重新生成本段视频')
    expect(onSaved).toHaveBeenCalledOnce()
    act(() => view.unmount())
  })
})

describe('P0：确认框措辞与实际删除行为一致', () => {
  it('打开即可见的规则提示说的是永久删除，不是「失效」', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    const { view } = mount(SEGMENT)
    await openDialog(view)
    const hint = view.root.findAll(n => n.props.className === 'dialogue-revision-rule-hint')[0]
    expect(textOf(hint)).toContain('永久删除、无法恢复')
    expect(textOf(hint)).not.toContain('失效')
    act(() => view.unmount())
  })

  it('预览影响改用「将被永久删除」，付费视频提示带上具体版本数', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    vi.mocked(previewShotEditImpact).mockResolvedValue(PREVIEW)
    const { view } = mount(SEGMENT)
    await openDialog(view)
    setReason(view, '供应商合规拒收')
    editLine(view, 0, '那根线越来越粗了。')
    await clickButton(view, '校验并预览影响')

    const impact = view.root.findAll(n => n.props.className === 'review-impact danger')[0]
    const text = textOf(impact)
    expect(text).toContain('参考图将被永久删除 1 项')
    expect(text).toContain('视频版本将被永久删除 2 项')
    // 证据链下游只是标 stale、不删记录（app/evidence/repository.py），措辞不能跟着改。
    expect(text).toContain('证据链下游会失效 2 项')
    expect(text).toContain('本段已生成的视频版本（2 个）将被')
    expect(text).toContain('永久删除，无法恢复')
    act(() => view.unmount())
  })

  it('确认按钮文案体现后果，不再是中性的「确认保存」', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    const { view } = mount(SEGMENT)
    await openDialog(view)
    expect(findButtons(view, '确认保存').length).toBe(0)
    expect(findButtons(view, '确认删除并保存').length).toBe(1)
    act(() => view.unmount())
  })
})

describe('P1：三步握手失败后本地草稿必须原样保留', () => {
  it('编辑租约过期时，点「重新获取编辑租约」不会清空已改的台词', async () => {
    vi.mocked(startShotEditSession)
      .mockResolvedValueOnce(SESSION)
      .mockResolvedValueOnce({ ...SESSION, edit_session_token: 'tok-2' })
    vi.mocked(previewShotEditImpact).mockRejectedValueOnce(
      new ApiError(409, '编辑租约已过期；本地内容仍保留，请重新取得编辑基线'),
    )
    const { view } = mount(SEGMENT)
    await openDialog(view)
    setReason(view, '供应商合规拒收')
    editLine(view, 0, '那根线越来越粗了。')
    await clickButton(view, '校验并预览影响')

    const errorPara = view.root.findAll(n => n.props.role === 'alert')[0]
    expect(textOf(errorPara)).toContain('编辑租约已过期')
    expect(textOf(errorPara)).not.toContain('避免覆盖他人改动')
    expect(findButtons(view, '重新获取编辑租约').length).toBe(1)

    await clickButton(view, '重新获取编辑租约')

    expect(startShotEditSession).toHaveBeenCalledTimes(2)
    expect(view.root.findAllByType('textarea')[0].props.value).toBe('那根线越来越粗了。')
    act(() => view.unmount())
  })

  it('撞上新基线（STALE_EDIT_BASELINE）时额外提示核对内容，避免覆盖他人改动', async () => {
    vi.mocked(startShotEditSession).mockResolvedValue(SESSION)
    vi.mocked(previewShotEditImpact).mockRejectedValueOnce(
      new ApiError(
        409,
        '编辑期间出现了新版本，发布已冻结；本地草稿仍保留，请核对内容后重新获取编辑基线再试',
        'STALE_EDIT_BASELINE',
      ),
    )
    const { view } = mount(SEGMENT)
    await openDialog(view)
    setReason(view, '供应商合规拒收')
    editLine(view, 0, '那根线越来越粗了。')
    await clickButton(view, '校验并预览影响')

    const errorPara = view.root.findAll(n => n.props.role === 'alert')[0]
    expect(textOf(errorPara)).toContain('本地草稿仍保留')
    expect(textOf(errorPara)).toContain('避免覆盖他人改动')
    // 撞新基线也走同一个不清空草稿的重试入口，不是另外一套「迁移草稿」流程
    // （前端压根没有这个接口，见 app/storyboard_workspace.py 的文案改法）。
    expect(findButtons(view, '重新获取编辑租约').length).toBe(1)
    act(() => view.unmount())
  })
})
