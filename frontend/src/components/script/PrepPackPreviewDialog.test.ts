import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import PrepPackPreviewDialog from './PrepPackPreviewDialog'

// 这是映射台唯一一处"点下去会真正开始生成、会花钱"之前的确认点——新架构下
// 映射台是用户拿到角色卡的唯一入口，弹窗必须在这里说清楚点击后会发生什么
// （新角色/新场景自动建卡+生成定妆照，已有的自动匹配复用），不能只说
// "将展示输入范围"这种不涉及产出的技术性描述。

describe('PrepPackPreviewDialog', () => {
  it('renders nothing when there is no pending preview', () => {
    const html = renderToStaticMarkup(createElement(PrepPackPreviewDialog, {
      preview: null, onCancel: () => {}, onConfirm: () => {},
    }))
    expect(html).toBe('')
  })

  it('states the real outcome (auto card + portrait creation, auto reuse of existing ones) before the user commits', () => {
    const html = renderToStaticMarkup(createElement(PrepPackPreviewDialog, {
      preview: {
        title: '首次生成映射包预检',
        data: { input: { source_chars: 3200, source_chapters: [1, 2] } },
        idempotencyKey: 'k1',
      },
      onCancel: () => {}, onConfirm: () => {},
    }))
    expect(html).toContain('自动建卡并生成定妆照')
    expect(html).toContain('自动匹配复用')
    expect(html).toContain('3200')
    expect(html).toContain('启动首版映射包生成')
  })

  it('surfaces the known image cost before the launch button, and never fakes the unknowable part', () => {
    const html = renderToStaticMarkup(createElement(PrepPackPreviewDialog, {
      preview: {
        title: '首次生成映射包预检',
        data: {
          input: { source_chars: 500, source_chapters: [3] },
          cast_impact: {
            portrait_asset_stage: {
              known_pending_characters: ['李富贵'],
              known_pending_scenes: ['宗门广场'],
              known_image_count: 5,
              known_cost_cny: 1.0,
              estimated_images: null,
              estimated_cost_cny: null,
              note: '本集若出现尚未登记的新角色/新场景，会自动建卡/登记并生成参考图；具体新增数量在生成前无法确知，完整费用以生成后为准。',
            },
          },
        },
        idempotencyKey: 'k-cost',
      },
      onCancel: () => {}, onConfirm: () => {},
    }))
    expect(html).toContain('已知会出图')
    expect(html).toContain('¥1')
    // 已知部分必须排在启动按钮之前——用户点下去之前就该看到。
    expect(html.indexOf('已知会出图')).toBeLessThan(html.indexOf('启动首版映射包生成'))
    // 新发现部分必须如实标为不可预知，不得杜撰一个精确数字。
    expect(html).toContain('无法确知')
  })

  it('renders zero known cost as zero, not a fabricated default, when nothing is pending', () => {
    const html = renderToStaticMarkup(createElement(PrepPackPreviewDialog, {
      preview: {
        title: '首次生成映射包预检',
        data: {
          input: { source_chars: 200, source_chapters: [1] },
          cast_impact: {
            portrait_asset_stage: {
              known_pending_characters: [],
              known_pending_scenes: [],
              known_image_count: 0,
              known_cost_cny: 0,
              estimated_images: null,
              estimated_cost_cny: null,
            },
          },
        },
        idempotencyKey: 'k-zero',
      },
      onCancel: () => {}, onConfirm: () => {},
    }))
    expect(html).toContain('共 0 张')
    expect(html).toContain('¥0')
  })

  it('still surfaces the fresh-retry-grant warning when the backend flags an unknown prior receipt', () => {
    const html = renderToStaticMarkup(createElement(PrepPackPreviewDialog, {
      preview: {
        title: '首次生成映射包预检',
        data: {
          input: { source_chars: 100, source_chapters: [1] },
          blueprint_budget: { requires_fresh_retry_grant: true },
        },
        idempotencyKey: 'k2',
      },
      onCancel: () => {}, onConfirm: () => {},
    }))
    expect(html).toContain('结果未知')
    expect(html).toContain('授权并重试（可能重新计费）')
  })
})
