import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 用户实测（2026-08-30）：回收站模态（.dialog-backdrop）里点"清空回收站"或
// "彻底删除"，确认弹窗（DeleteConfirmDialog：.evidence-backdrop + .impact-dialog）
// 出现在回收站背后，必须先关掉回收站才能点到确认按钮。DOM 顺序本来就对
// （RecycleBinDialog 排在 deleteConfirm.dialog 之前），纯粹是 CSS 层级倒挂：
// .dialog-backdrop 的 z-index:200 比确认弹窗角色当年继承的 180 更高。
//
// 修法（src/index.css）：给全站"确认/决策弹窗"共用的既有选择器
// `.evidence-backdrop:has(> .impact-dialog)`（DeleteConfirmDialog / DecisionDialog /
// ImpactDialog 等都靠子元素 .impact-dialog 落进这条规则）单独声明 z-index:210，
// 压过 .dialog-backdrop 与 .monitor-drawer-backdrop，但仍留在
// .capability-approval-backdrop（1200，终审闸）之下。不新造类名、不新造模态体系。
//
// 这个测试钉住两件事，任何一件被悄悄改回去都要让 CI 变红：
//   1. 两个组件确实还在用报告里认定的那两个类名——不然下面的 CSS 数值比较就是
//      在比较错误的选择器，看起来钉住了其实什么都没验证；
//   2. index.css 里对应规则的 z-index 数值关系没有被改回去。
// react-test-renderer 跑在 vitest environment:'node' 下，没有真实 CSSOM 算不出
// computed style，所以第 2 步直接读源码文本解析数值（照抄 check_css_classes_defined.py
// 的思路：判不了 computed style 就退回判源码，不装作判了 computed style）。

vi.mock('../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import { RecycleBinDialog } from './RecycleBinDialog'
// eslint-disable-next-line import/first
import DeleteConfirmDialog from './DeleteConfirmDialog'

function hasClass(node: TestRenderer.ReactTestInstance, cls: string): boolean {
  const raw = node.props.className
  return typeof raw === 'string' && raw.split(/\s+/).includes(cls)
}

/** 从 index.css 源文本里摘出某条规则块声明的 z-index 数值；选择器必须精确
 *  写到 `{`，避免同名前缀选择器（如 .evidence-backdrop 和
 *  .evidence-backdrop:has(...)）互相误命中。 */
function extractZIndex(css: string, exactSelectorWithBrace: string): number {
  const start = css.indexOf(exactSelectorWithBrace)
  if (start < 0) throw new Error(`index.css 里找不到选择器：${exactSelectorWithBrace}`)
  const braceStart = start + exactSelectorWithBrace.indexOf('{')
  const braceEnd = css.indexOf('}', braceStart)
  const block = css.slice(braceStart, braceEnd)
  const m = block.match(/z-index:\s*(\d+)/)
  if (!m) throw new Error(`选择器块里没有 z-index 声明：${exactSelectorWithBrace}`)
  return Number(m[1])
}

// RecycleBinDialog 挂载时用 window.addEventListener 装 Esc 监听（vitest
// environment:'node' 没有全局 window），装一个最小 stub，不需要完整 DOM。
function installWindowStub() {
  ;(globalThis as { window?: unknown }).window = {
    addEventListener: () => {},
    removeEventListener: () => {},
  }
}
function uninstallWindowStub() {
  delete (globalThis as { window?: unknown }).window
}

describe('模态叠模态：确认弹窗必须盖住发起它的模态（2026-08-30 用户实测回归）', () => {
  beforeEach(installWindowStub)
  afterEach(uninstallWindowStub)

  it('RecycleBinDialog 用 .dialog-backdrop；DeleteConfirmDialog 用 .evidence-backdrop 承载 .impact-dialog', async () => {
    let recycleRenderer!: TestRenderer.ReactTestRenderer
    await act(async () => {
      recycleRenderer = TestRenderer.create(
        React.createElement(RecycleBinDialog, {
          deletedProjects: [],
          deletedCount: 0,
          deletedLoading: false,
          deletedError: null,
          busyId: null,
          purgingAll: false,
          onRestore: () => {},
          onPurge: () => {},
          onPurgeAll: () => {},
          onClose: () => {},
          onRefresh: () => {},
        }),
      )
    })
    const recycleBackdrop = recycleRenderer.root.findAllByProps({ role: 'dialog' })[0]
    expect(hasClass(recycleBackdrop, 'dialog-backdrop')).toBe(true)

    let confirmRenderer!: TestRenderer.ReactTestRenderer
    await act(async () => {
      confirmRenderer = TestRenderer.create(
        React.createElement(DeleteConfirmDialog, {
          pending: { summary: '将清空回收站' },
          busy: false,
          onCancel: () => {},
          onConfirm: () => {},
        }),
      )
    })
    const confirmBackdrop = confirmRenderer.root.findAllByProps({ role: 'presentation' })[0]
    expect(hasClass(confirmBackdrop, 'evidence-backdrop')).toBe(true)
    const confirmDialog = confirmRenderer.root.findAllByProps({ role: 'dialog' })[0]
    expect(hasClass(confirmDialog, 'impact-dialog')).toBe(true)
  })

  it('index.css：确认弹窗角色（.evidence-backdrop:has(> .impact-dialog)）的 z-index 必须高于 .dialog-backdrop', () => {
    const cssPath = resolve(dirname(fileURLToPath(import.meta.url)), '../index.css')
    const css = readFileSync(cssPath, 'utf8')

    const dialogBackdropZ = extractZIndex(css, '.dialog-backdrop {')
    const confirmDialogZ = extractZIndex(css, '.evidence-backdrop:has(> .impact-dialog) {')

    // 倒过来就是本次修的那个 bug：从 .dialog-backdrop（回收站等模态）里弹出的
    // 确认框会被压在背后，用户点不到确认/取消按钮。
    expect(confirmDialogZ).toBeGreaterThan(dialogBackdropZ)
  })
})
