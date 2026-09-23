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

// usePoll 每 6s 轮询一次项目列表（不像 useProject/useEpisode 那样随业务状态
// 停轮询），失败不清空 projects；已有数据后再失败的 error 此前只传给了
// QueryState，会被它的 hasData 分支无条件吞掉（同 orgs/resources 各面板
// 2c96b89c 修的那类问题）。无组件渲染测试基建（同 BiblePage.test.ts 顶部
// 注释），继续用源码静态扫描守住接线不回归。
describe('已有项目列表时后台轮询刷新失败不得被吞', () => {
  it('QueryState 的 error 由 projectsLoadFailed 门控，不是原始 error 直传', () => {
    expect(source).toMatch(/const projectsLoadFailed = !projects && !!error/)
    expect(source).toMatch(/<QueryState[^>]*error=\{projectsLoadFailed \? error : null\}/)
  })

  it('QueryState 之外单独渲染 StaleRefreshBanner，已有数据时展示刷新失败信号', () => {
    expect(source).toMatch(
      /<StaleRefreshBanner error=\{projectsLoadFailed \? null : error\} onRetry=\{refresh\} objectName="项目" \/>/,
    )
  })
})
