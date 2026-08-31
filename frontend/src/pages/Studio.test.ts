import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const source = readFileSync(fileURLToPath(new URL('./Studio.tsx', import.meta.url)), 'utf-8')

// 2026-08-31 用户拍板：画风挪到导入项目时一次性选定，复用人物谱/场景库共用
// 的 VisualStyleDialog/useVisualStyleDialog，不新造一套。同一套静态扫描守法
// 见 BiblePage.test.ts 顶部注释（本仓库无组件渲染测试基建）。
describe('导入面板复用统一画风弹窗，并把选定结果带进创建请求', () => {
  it('复用既有的 VisualStyleDialog / useVisualStyleDialog，没有另起一套', () => {
    expect(source).toMatch(/import VisualStyleDialog from '..\/components\/VisualStyleDialog'/)
    expect(source).toMatch(/import \{ useVisualStyleDialog \} from '..\/hooks\/useVisualStyleDialog'/)
  })

  it('useVisualStyleDialog 以 null 项目态调用（项目尚未创建）', () => {
    expect(source).toMatch(/useVisualStyleDialog\(null\)/)
  })

  it('确认导入时把选定的 style_name 带进 importProject 请求体', () => {
    expect(source).toMatch(/api\.importProject\(\{[\s\S]{0,200}style_name: styleName \|\| undefined/)
  })
})
