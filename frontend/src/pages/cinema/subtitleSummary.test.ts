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
