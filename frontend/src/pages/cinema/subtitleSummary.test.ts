import { describe, expect, it } from 'vitest'
import { extraSpeechRows, missingSubtitleRows, subtitleSummaryLine } from './subtitleSummary'

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
