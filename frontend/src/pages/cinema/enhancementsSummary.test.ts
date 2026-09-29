import { describe, expect, it } from 'vitest'
import { enhancementFeatureSummaries } from './enhancementsSummary'

describe('enhancementFeatureSummaries', () => {
  it.each([
    ['报告为 null', null, []],
    ['报告不是对象', 'nope', []],
    ['没有 enhancements 键（旧报告）', { ok: true }, []],
    ['enhancements 不是对象', { enhancements: 'nope' }, []],
  ])('%s', (_label, report, expected) => {
    expect(enhancementFeatureSummaries(report)).toEqual(expected)
  })

  it('三项都跳过时逐项给出中文原因', () => {
    const report = {
      enhancements: {
        music_bed: { applied: false, reason: '项目未开启统一配乐' },
        teaser: { applied: false, reason: '项目未开启片头预告' },
        monologue: { applied: false, reason: '项目未开启主角内心独白' },
      },
    }
    const summaries = enhancementFeatureSummaries(report)
    expect(summaries).toEqual([
      { key: 'music_bed', label: '统一配乐', applied: false, reason: '项目未开启统一配乐', details: [] },
      { key: 'teaser', label: '片头预告', applied: false, reason: '项目未开启片头预告', details: [] },
      { key: 'monologue', label: '主角内心独白', applied: false, reason: '项目未开启主角内心独白', details: [] },
    ])
  })

  it('配乐应用时列出使用曲目', () => {
    const report = {
      enhancements: {
        music_bed: { applied: true, reason: '', tracks: [{ track_id: 't1', title: '月光曲' }, { track_id: 't2', title: '夜想曲' }] },
      },
    }
    const [summary] = enhancementFeatureSummaries(report)
    expect(summary.applied).toBe(true)
    expect(summary.details).toEqual(['使用曲目：月光曲、夜想曲'])
  })

  it('预告应用时列出片段与总长', () => {
    const report = {
      enhancements: {
        teaser: {
          applied: true, reason: '', duration_s: 9.5,
          clips: [{ shot_no: 3, start_s: 1.2, end_s: 3.5, reason: '心动瞬间' }],
        },
      },
    }
    const [summary] = enhancementFeatureSummaries(report)
    expect(summary.details).toEqual(['预告总长 9.5 秒', '第 3 段 1.2–3.5s：心动瞬间'])
  })

  it('独白应用时列出角色与原句，并提示跳过条数', () => {
    const report = {
      enhancements: {
        monologue: {
          applied: true, reason: '',
          lines: [{ character_name: '顾屿', text: '我不会认输', start_s: 12.0 }],
          skipped: [{ item: {}, reason: '角色未设置固定音色' }],
        },
      },
    }
    const [summary] = enhancementFeatureSummaries(report)
    expect(summary.details).toEqual(['顾屿：「我不会认输」', '另有 1 句独白候选未采用（见理由）'])
  })

  it('模型提名被丢弃的条目即使该项未应用也要展示理由', () => {
    const report = {
      enhancements: {
        music_bed: {
          applied: false, reason: '编排计划未给出任何可用的配乐提名',
          rejected: [{ item: { track_id: 'ghost' }, reason: '曲目 ID「ghost」不在曲库清单中' }],
        },
      },
    }
    const [summary] = enhancementFeatureSummaries(report)
    expect(summary.applied).toBe(false)
    expect(summary.details).toEqual(['模型提名未采用：曲目 ID「ghost」不在曲库清单中'])
  })

  it('没有 rejected 字段时不产生多余详情', () => {
    const report = { enhancements: { teaser: { applied: false, reason: '项目未开启片头预告' } } }
    const [summary] = enhancementFeatureSummaries(report)
    expect(summary.details).toEqual([])
  })

  it('畸形字段（详情数组里混入非对象）不崩溃、逐条跳过', () => {
    const report = { enhancements: { music_bed: { applied: true, reason: '', tracks: ['not-an-object', { title: '月光曲' }] } } }
    const [summary] = enhancementFeatureSummaries(report)
    expect(summary.details).toEqual(['使用曲目：月光曲'])
  })
})
