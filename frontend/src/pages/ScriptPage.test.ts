import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import {
  ScreenplayResumeButton,
  ScreenplayWriteError,
  classifyScreenplayWriteError,
  screenplayGeneratePayload,
  screenplayRepairDraftPayload,
  screenplayResumeActionLabel,
  screenplayResumeOutcomeSummary,
} from './ScriptPage'

const writeError = (
  status: number,
  message: string,
  detail?: any,
): ScreenplayWriteError => Object.assign(new Error(message), { status, detail })

const rebuildProduction = {
  operation: 'baseline_rebuild' as const,
  mode: 'baseline_rebuild' as const,
  mode_label: '按新合同重建剧本',
  phase: 'BLUEPRINT_GENERATION',
  baseline_done: false,
  first_evaluation_done: false,
  task_active: false,
  can_resume_baseline: true,
  can_resume_repair: false,
}

describe('screenplayResumeActionLabel', () => {
  it('uses the backend baseline rebuild label', () => {
    expect(screenplayResumeActionLabel(rebuildProduction)).toBe('按新合同重建剧本')
  })

  it('keeps the compatibility label only for older responses', () => {
    expect(screenplayResumeActionLabel({
      operation: 'baseline',
      phase: 'SCENE_SHARD_GENERATION',
      baseline_done: false,
      first_evaluation_done: false,
      task_active: false,
      can_resume_baseline: true,
      can_resume_repair: false,
    })).toBe('继续首版场次生成')
  })
})

describe('ScreenplayResumeButton', () => {
  it('renders the backend mode label and dispatches the mounted button click', () => {
    const onResume = vi.fn()
    const button = ScreenplayResumeButton({
      production: rebuildProduction,
      busy: false,
      onResume,
    })

    expect(renderToStaticMarkup(button)).toContain('按新合同重建剧本')
    button.props.onClick()

    expect(onResume).toHaveBeenCalledOnce()
  })
})

describe('screenplayResumeOutcomeSummary', () => {
  it('uses the server receipt summary instead of a fixed resume toast', () => {
    expect(screenplayResumeOutcomeSummary({
      mode: 'baseline_rebuild',
      summary: '服务端：已启动兼容合同重建',
    })).toBe('服务端：已启动兼容合同重建')
  })

  it('falls back to the server mode when an older receipt has no summary', () => {
    expect(screenplayResumeOutcomeSummary({ mode: 'baseline_rebuild' }))
      .toBe('已按当前合同启动剧本基线重建')
  })
})

describe('screenplayGeneratePayload', () => {
  it('sends only the idempotency key when no retry fence is active', () => {
    expect(screenplayGeneratePayload('key-1', undefined)).toEqual({
      idempotency_key: 'key-1',
    })
    expect(
      screenplayGeneratePayload('key-1', { requires_fresh_retry_grant: false }),
    ).toEqual({ idempotency_key: 'key-1' })
  })

  it('authorizes the paid retry with the expected unknown receipts when fenced', () => {
    const receipts = [{ call_id: 61640 }, { call_id: 61641 }]
    expect(
      screenplayGeneratePayload('key-2', {
        requires_fresh_retry_grant: true,
        unknown_receipts: receipts,
      }),
    ).toEqual({
      idempotency_key: 'key-2',
      authorize_blueprint_retry: true,
      expected_blueprint_unknown_receipts: receipts,
    })
  })

  it('defaults unknown receipts to an empty list when the fence lacks them', () => {
    expect(
      screenplayGeneratePayload('key-3', { requires_fresh_retry_grant: true }),
    ).toEqual({
      idempotency_key: 'key-3',
      authorize_blueprint_retry: true,
      expected_blueprint_unknown_receipts: [],
    })
  })
})

describe('screenplayRepairDraftPayload', () => {
  const draft = { title: '第一集', full_script_text: '正文' } as any

  it('assembles the repair-draft body aligned with the backend contract', () => {
    expect(screenplayRepairDraftPayload('repair-key', draft, 'artifact-7')).toEqual({
      screenplay: draft,
      expected_version: 'artifact-7',
      idempotency_key: 'repair-key',
    })
  })

  it('carries a null baseline verbatim so the backend treats it as no expected version', () => {
    expect(screenplayRepairDraftPayload('repair-key', draft, null)).toEqual({
      screenplay: draft,
      expected_version: null,
      idempotency_key: 'repair-key',
    })
  })
})

describe('classifyScreenplayWriteError', () => {
  it('classifies both version conflict codes as a conflict carrying the detail', () => {
    const detail = { code: 'screenplay_version_conflict', current_version: 'v2', diff: [] }
    expect(classifyScreenplayWriteError(writeError(409, '冲突', detail))).toEqual({
      kind: 'conflict',
      detail,
    })
    const legacy = { code: 'version_conflict' }
    expect(classifyScreenplayWriteError(writeError(409, '冲突', legacy))).toEqual({
      kind: 'conflict',
      detail: legacy,
    })
  })

  it('maps qa_already_passed (409) to a distinct already_passed decision', () => {
    expect(
      classifyScreenplayWriteError(
        writeError(409, '已过 QA', { code: 'screenplay_qa_already_passed', message: '已通过' }),
      ),
    ).toEqual({ kind: 'already_passed', message: '已通过' })
  })

  it('extracts qa failure score and errors, falling back to issues then message', () => {
    expect(
      classifyScreenplayWriteError(
        writeError(422, '结构失败', {
          code: 'screenplay_qa_failed',
          score: 42,
          errors: ['缺少结尾钩子'],
        }),
      ),
    ).toEqual({ kind: 'qa', failure: { score: 42, errors: ['缺少结尾钩子'] } })

    expect(
      classifyScreenplayWriteError(
        writeError(422, '结构失败', {
          code: 'screenplay_qa_failed',
          issues: [{ message: '第 2 场未闭环' }],
        }),
      ),
    ).toEqual({ kind: 'qa', failure: { score: undefined, errors: ['第 2 场未闭环'] } })

    expect(
      classifyScreenplayWriteError(
        writeError(422, '兜底文案', { code: 'screenplay_qa_failed' }),
      ),
    ).toEqual({ kind: 'qa', failure: { score: undefined, errors: ['兜底文案'] } })
  })

  it('routes both identity error codes to an identity decision with a toast', () => {
    const decision = classifyScreenplayWriteError(
      writeError(422, '身份未决', {
        code: 'screenplay_character_identity_unresolved',
        errors: ['人物 A 未匹配'],
      }),
    )
    expect(decision).toEqual({
      kind: 'identity',
      failure: { errors: ['人物 A 未匹配'] },
      toast: '人物身份预检未通过，草稿与现有分镜均已保留',
    })

    expect(
      classifyScreenplayWriteError(
        writeError(422, '发现失败', { code: 'screenplay_character_discovery_failed' }),
      ).kind,
    ).toBe('identity')
  })

  it('recognizes the cancelled operation from the 403 message', () => {
    expect(classifyScreenplayWriteError(writeError(403, '已取消操作'))).toEqual({
      kind: 'cancelled',
      message: '已取消操作',
    })
  })

  it('falls back to a plain toast for unclassified errors', () => {
    expect(classifyScreenplayWriteError(writeError(500, '服务异常'))).toEqual({
      kind: 'toast',
      message: '服务异常',
    })
    // 403 without the cancellation phrase must not be mistaken for a cancellation.
    expect(classifyScreenplayWriteError(writeError(403, '无权限')).kind).toBe('toast')
    // A conflict on the wrong status must not be swallowed as a version conflict.
    expect(
      classifyScreenplayWriteError(writeError(500, 'x', { code: 'screenplay_version_conflict' })).kind,
    ).toBe('toast')
  })
})
