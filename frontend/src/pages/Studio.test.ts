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

// 真实案例（2026-08-31）：同一项目同一摄影类画风下 8/10 集视频阶段被供应商
// 隐私政策拒收。导入面板必须在选画风时如实提示——不禁止选择，只是不再沉默。
describe('摄影类画风在导入面板给出可见提示，非摄影类不提示', () => {
  it('提示按已选画风的 photographic 标记门控，不是无条件展示', () => {
    expect(source).toMatch(
      /styleDialog\.styleOptions\.find\(o => o\.name === styleName\)\?\.photographic && <p className="warning-banner"/,
    )
  })

  it('提示文案指向真实供应商风险与出路，不是空话', () => {
    expect(source).toContain('视频生成阶段有较高概率被供应商隐私政策判定疑似真人而拒收')
    expect(source).toContain('或改选其它画风')
  })
})
