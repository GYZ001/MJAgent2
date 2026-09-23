import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import EpisodeBatchConfirmDialog, { type BatchAction } from './EpisodeBatchConfirmDialog'

// P1-1 回归（界面承诺必须与实际行为一致）：三个批量操作的确认框此前统一声称
// 会「占用会员时长」，但 app/planning.py 的重新分集是纯正则拆章（无模型调用），
// 映射包/分镜批量生成虽会调用文本模型，HiAgent 文本调用不计费（本仓「文本模型
// 调用不计费」口径），三者都不会启动付费视频、不消耗视频额度。这里断言旧的
// 失实文案不再出现，且按动作分别给出真实后果。

const noop = () => {}

function renderDialog(action: BatchAction, overrides: {
  totalEpisodes?: number
  screenplayTodoCount?: number
  storyboardTodoCount?: number
  busy?: boolean
} = {}): string {
  return renderToStaticMarkup(createElement(EpisodeBatchConfirmDialog, {
    action,
    projectName: '测试项目',
    totalEpisodes: 0,
    screenplayTodoCount: 0,
    storyboardTodoCount: 0,
    busy: false,
    onClose: noop,
    onConfirm: noop,
    ...overrides,
  }))
}

describe('EpisodeBatchConfirmDialog — 费用与额度文案', () => {
  it('从不出现旧的失实措辞「会员时长」，不管哪个动作', () => {
    for (const action of ['replan', 'screenplay', 'storyboard'] as const) {
      const html = renderDialog(action, { totalEpisodes: 5, screenplayTodoCount: 5, storyboardTodoCount: 5 })
      expect(html).not.toContain('会员时长')
    }
  })

  it('重新规划全部分集：不声称调用模型，改为如实说明视频额度不退回', () => {
    const html = renderDialog('replan', { totalEpisodes: 5 })
    expect(html).not.toContain('调用文本模型')
    expect(html).toContain('不调用任何模型')
    expect(html).toContain('视频额度不会退回')
  })

  it('首次开始分集（totalEpisodes=0）：同样不调用模型，也没有可清空的视频', () => {
    const html = renderDialog('replan', { totalEpisodes: 0 })
    expect(html).toContain('不调用任何模型')
    expect(html).not.toContain('视频额度不会退回')
  })

  it('批量生成待办映射包：如实说明会调用文本模型，且不计费不耗视频额度', () => {
    const html = renderDialog('screenplay', { screenplayTodoCount: 3 })
    expect(html).toContain('调用文本模型')
    expect(html).toContain('不产生费用')
    expect(html).toContain('不消耗视频额度')
  })

  it('批量生成待办分镜：如实说明会调用文本模型，且不计费不耗视频额度', () => {
    const html = renderDialog('storyboard', { storyboardTodoCount: 4 })
    expect(html).toContain('调用文本模型')
    expect(html).toContain('不产生费用')
    expect(html).toContain('不消耗视频额度')
  })

  it('busy=true 时仍可渲染（覆盖提交中态，避免文案改动引入渲染异常）', () => {
    expect(() => renderDialog('screenplay', { busy: true })).not.toThrow()
  })
})
