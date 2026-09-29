import { describe, expect, it } from 'vitest'
import { extraSpeechRows, missingSubtitleActionHint, missingSubtitleRows, subtitleSummaryLine } from './subtitleSummary'

describe('subtitleSummaryLine', () => {
  it.each([
    ['报告为 null', null, null],
    ['报告不是对象', 'not-an-object', null],
    ['报告没有 subtitles 键（旧报告）', { ok: true }, null],
    ['subtitles 不是对象', { subtitles: 'nope' }, null],
    ['enabled 字段类型不对（畸形）', { subtitles: { enabled: 'yes' } }, null],
    ['未开启', { subtitles: { enabled: false } }, '字幕嵌入未开启（可在监制房系统设置中开启）'],
    ['本集没有台词', { subtitles: { enabled: true, lines_total: 0, lines_aligned: 0, lines_missing: 0 } }, '字幕：本集没有台词'],
    [
      '全部对齐',
      { subtitles: { enabled: true, lines_total: 31, lines_aligned: 31, lines_missing: 0 } },
      '字幕：31/31 句已对齐',
    ],
    [
      '部分缺失，带镜号且去重排序',
      {
        subtitles: {
          enabled: true,
          lines_total: 31,
          lines_aligned: 29,
          lines_missing: 2,
          missing: [
            { shot_no: 20, utterance_id: 'U01', line: 'x', match_ratio: 0.1, reason: 'not_found' },
            { shot_no: 12, utterance_id: 'U02', line: 'y', match_ratio: 0.1, reason: 'no_audio' },
          ],
        },
      },
      '字幕：29/31 句已对齐，2 句未出声（第 12、20 镜）',
    ],
    ['lines_total 类型不对（畸形）', { subtitles: { enabled: true, lines_total: 'many' } }, null],
    ['lines_aligned 缺失（畸形）', { subtitles: { enabled: true, lines_total: 10 } }, null],
    ['lines_missing 缺失（畸形）', { subtitles: { enabled: true, lines_total: 10, lines_aligned: 10 } }, null],
    [
      '估计时间的行不算「未出声」：已经烧出字幕了，只是时间是估计的，不应混进真缺失的计数',
      {
        subtitles: {
          enabled: true,
          lines_total: 31,
          lines_aligned: 29,
          lines_missing: 2,
          missing: [
            { shot_no: 12, utterance_id: 'U01', line: '守……印……人……', match_ratio: 0.3, reason: 'short_line_partial', status: 'estimated' },
            { shot_no: 20, utterance_id: 'U02', line: '真的没识别到', match_ratio: 0, reason: 'not_found', status: 'missing' },
          ],
        },
      },
      '字幕：29/31 句已对齐，1 句未出声，1 句按估计时间显示（第 12、20 镜）',
    ],
    [
      'lines_partial 非零时补一句部分命中计数',
      {
        subtitles: {
          enabled: true, lines_total: 31, lines_aligned: 28, lines_partial: 3, lines_missing: 0,
        },
      },
      '字幕：28/31 句已对齐，3 句部分命中',
    ],
    [
      '账本为空但检测到账本外人声：不得谎称没有台词',
      {
        subtitles: {
          enabled: true, lines_total: 0, lines_aligned: 0, lines_missing: 0,
          extra_speech: [
            { shot_no: 3, text: '姑娘想吃点啥', start_s: 1.2, end_s: 2.5 },
            { shot_no: 5, text: '好嘞您稍等', start_s: 0.5, end_s: 1.1 },
          ],
        },
      },
      '字幕：台词账本为空，但成片里检测到 2 处人声，未生成字幕（这些人声不是分镜登记的台词）',
    ],
    [
      '账本为空且 extra_speech 是显式空数组（反向断言）：仍是没有台词',
      { subtitles: { enabled: true, lines_total: 0, lines_aligned: 0, lines_missing: 0, extra_speech: [] } },
      '字幕：本集没有台词',
    ],
    [
      '账本为空且 extra_speech 字段类型不对（畸形，反向断言）：按无人声处理',
      { subtitles: { enabled: true, lines_total: 0, lines_aligned: 0, lines_missing: 0, extra_speech: 'nope' } },
      '字幕：本集没有台词',
    ],
    [
      '账本为空，extra_speech 里混了畸形条目：跳过畸形项、只数有效项，不崩',
      {
        subtitles: {
          enabled: true, lines_total: 0, lines_aligned: 0, lines_missing: 0,
          extra_speech: [
            { shot_no: 3, text: '有效条目', start_s: 1, end_s: 2 },
            { shot_no: 'bad', text: '镜号畸形', start_s: 1, end_s: 2 },
            { shot_no: 4, text: 123, start_s: 1, end_s: 2 },
            'not-an-object',
          ],
        },
      },
      '字幕：台词账本为空，但成片里检测到 1 处人声，未生成字幕（这些人声不是分镜登记的台词）',
    ],
    [
      '全部对齐同时有账本外人声：既有文案结构不变，补一句计数',
      {
        subtitles: {
          enabled: true, lines_total: 31, lines_aligned: 31, lines_missing: 0,
          extra_speech: [{ shot_no: 2, text: '多说的一句', start_s: 0, end_s: 1 }],
        },
      },
      '字幕：31/31 句已对齐，另检测到 1 处账本外人声',
    ],
    [
      '部分缺失同时有账本外人声：两句计数都要出现',
      {
        subtitles: {
          enabled: true, lines_total: 31, lines_aligned: 29, lines_missing: 2,
          missing: [
            { shot_no: 20, utterance_id: 'U01', line: 'x', match_ratio: 0.1, reason: 'not_found' },
            { shot_no: 12, utterance_id: 'U02', line: 'y', match_ratio: 0.1, reason: 'no_audio' },
          ],
          extra_speech: [
            { shot_no: 6, text: '甲', start_s: 0, end_s: 1 },
            { shot_no: 7, text: '乙', start_s: 0, end_s: 1 },
          ],
        },
      },
      '字幕：29/31 句已对齐，2 句未出声（第 12、20 镜），另检测到 2 处账本外人声',
    ],
  ] as const)('%s', (_label, report, expected) => {
    expect(subtitleSummaryLine(report)).toBe(expected)
  })
})

describe('missingSubtitleRows', () => {
  it('按镜号排序、原话超 40 字截断、reason 映射为完整正面陈述', () => {
    const longLine = 'a'.repeat(45)
    const rows = missingSubtitleRows({
      subtitles: {
        enabled: true,
        missing: [
          { shot_no: 20, utterance_id: 'U03', line: longLine, match_ratio: 0.12, reason: 'not_found' },
          { shot_no: 12, utterance_id: 'U01', line: '短句', match_ratio: 0.3333, reason: 'no_audio' },
          { shot_no: 15, utterance_id: 'U02', line: '走', match_ratio: 1, reason: 'short_line_partial' },
        ],
      },
    })
    expect(rows.map(row => row.shotNo)).toEqual([12, 15, 20])
    expect(rows[0].reasonLabel).toBe('该镜视频没有音轨')
    expect(rows[1].reasonLabel).toBe('短句只念出了一部分')
    expect(rows[2].reasonLabel).toBe('音轨里没有找到这句台词')
    expect(rows[2].line).toBe(`${'a'.repeat(40)}…`)
    expect(rows[0].ratioText).toBe('33%')
  })

  it('未知 reason 原样显示，不做黑名单兜底成空', () => {
    const rows = missingSubtitleRows({
      subtitles: {
        enabled: true,
        missing: [{ shot_no: 1, utterance_id: 'U01', line: '台词', match_ratio: 0.5, reason: 'cross_cut_weird_case' }],
      },
    })
    expect(rows[0].reasonLabel).toBe('cross_cut_weird_case')
  })

  it('单条畸形数据跳过，不丢弃整份列表；输入畸形整体返回空数组', () => {
    const rows = missingSubtitleRows({
      subtitles: {
        enabled: true,
        missing: [
          { shot_no: 1, utterance_id: 'U01', line: '好句子', match_ratio: 0.5, reason: 'not_found' },
          { shot_no: 'bad', utterance_id: 'U02', line: '坏句子', match_ratio: 0.5, reason: 'not_found' },
          'not-an-object',
        ],
      },
    })
    expect(rows).toHaveLength(1)
    expect(rows[0].utteranceId).toBe('U01')
    expect(missingSubtitleRows(null)).toEqual([])
    expect(missingSubtitleRows({ subtitles: { enabled: true } })).toEqual([])
    expect(missingSubtitleRows({ subtitles: { enabled: false } })).toEqual([])
  })
})

describe('missingSubtitleRows：estimated 字段', () => {
  it('status=estimated 的行标记为 estimated=true，其余（含旧报告没有 status 字段）为 false', () => {
    const rows = missingSubtitleRows({
      subtitles: {
        enabled: true,
        missing: [
          { shot_no: 1, utterance_id: 'U01', line: '按估计时间显示的句子', match_ratio: 0, reason: 'not_found', status: 'estimated' },
          { shot_no: 1, utterance_id: 'U02', line: '真的没有字幕', match_ratio: 0, reason: 'not_found', status: 'missing' },
          { shot_no: 1, utterance_id: 'U03', line: '旧报告没有 status 字段', match_ratio: 0, reason: 'not_found' },
        ],
      },
    })
    const byId = Object.fromEntries(rows.map(row => [row.utteranceId, row]))
    expect(byId.U01.estimated).toBe(true)
    expect(byId.U02.estimated).toBe(false)
    expect(byId.U03.estimated).toBe(false)
  })
})

describe('missingSubtitleActionHint', () => {
  it('只有真缺失时只给"去生成台重做"的出路', () => {
    const rows = missingSubtitleRows({
      subtitles: { enabled: true, missing: [{ shot_no: 1, utterance_id: 'U01', line: 'x', match_ratio: 0, reason: 'not_found', status: 'missing' }] },
    })
    expect(missingSubtitleActionHint(rows)).toBe('完全没有字幕的句子：去生成台重新生成对应镜头后重新合成即可补上')
  })

  it('只有估计时间时只给"人工核对时机"的提示', () => {
    const rows = missingSubtitleRows({
      subtitles: { enabled: true, missing: [{ shot_no: 1, utterance_id: 'U01', line: 'x', match_ratio: 0, reason: 'not_found', status: 'estimated' }] },
    })
    expect(missingSubtitleActionHint(rows)).toBe(
      '标了「估计时间」的句子已经烧出字幕，只是显示时刻是按同镜头前后台词估算的，建议人工核对画面时机是否准确',
    )
  })

  it('两种都有时两句话都给，且拦人必须给出路（不能是空字符串）', () => {
    const rows = missingSubtitleRows({
      subtitles: {
        enabled: true,
        missing: [
          { shot_no: 1, utterance_id: 'U01', line: 'x', match_ratio: 0, reason: 'not_found', status: 'missing' },
          { shot_no: 1, utterance_id: 'U02', line: 'y', match_ratio: 0, reason: 'not_found', status: 'estimated' },
        ],
      },
    })
    const hint = missingSubtitleActionHint(rows)
    expect(hint).toContain('去生成台重新生成')
    expect(hint).toContain('人工核对画面时机')
    expect(hint.length).toBeGreaterThan(0)
  })

  it('空列表返回空字符串，不假装有内容', () => {
    expect(missingSubtitleActionHint([])).toBe('')
  })
})

describe('extraSpeechRows', () => {
  it('格式化时间区间并按镜号排序', () => {
    const rows = extraSpeechRows({
      subtitles: {
        enabled: true,
        extra_speech: [
          { shot_no: 5, text: '识别出的多余语音', start_s: 3.2, end_s: 5.9 },
          { shot_no: 2, text: '另一段', start_s: 1, end_s: 2.456 },
        ],
      },
    })
    expect(rows.map(row => row.shotNo)).toEqual([2, 5])
    expect(rows[1].range).toBe('3.2s–5.9s')
    expect(rows[0].range).toBe('1.0s–2.5s')
  })

  it('畸形输入返回空数组', () => {
    expect(extraSpeechRows(undefined)).toEqual([])
    expect(extraSpeechRows({ subtitles: { enabled: true, extra_speech: 'nope' } })).toEqual([])
  })
})
