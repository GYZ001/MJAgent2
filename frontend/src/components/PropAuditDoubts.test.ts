import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { PropAuditDoubt } from '../api'
import { doubtReasonLabel, doubtSubjectLabel } from './PropAuditDoubts'

const propsPageSource = readFileSync(fileURLToPath(new URL('../pages/PropsPage.tsx', import.meta.url)), 'utf-8')

const clauseDoubt = (over: Partial<PropAuditDoubt> = {}): PropAuditDoubt => ({
  kind: 'clause', text: '瓷瓶外观为青花纹样', doubt_type: 'owner_without_card',
  reason_a: '容器外观', reason_b: '', owner: '瓷瓶', ...over,
})

// 道具卡复核「待你确认」存疑文案与按钮接线（2026-10-03-v2）——界面要把三类
// 存疑来源说清楚：两次判定不一致 / 归属没有自己的卡 / 模型自述拿不准。
describe('道具卡复核存疑文案', () => {
  it('归属没有自己的卡：说明这件东西没有卡，删掉会消失', () => {
    const text = doubtReasonLabel(clauseDoubt())
    expect(text).toContain('瓷瓶')
    expect(text).toContain('没有它的道具卡')
  })

  it('模型自述拿不准：原样带出犹豫理由', () => {
    const text = doubtReasonLabel(clauseDoubt({ doubt_type: 'model_self_doubt', reason_a: '分不清是容器还是本体' }))
    expect(text).toContain('拿不准')
    expect(text).toContain('分不清是容器还是本体')
  })

  it('两次判定不一致：两次理由都要出现', () => {
    const text = doubtReasonLabel(clauseDoubt({
      doubt_type: 'inconsistent_between_two_judgments', reason_a: '已开盖', reason_b: '未提及',
    }))
    expect(text).toContain('已开盖')
    expect(text).toContain('未提及')
  })

  it('类别不合法：说明模型给的类别不在约定三类之内、无法自动采信', () => {
    const text = doubtReasonLabel(clauseDoubt({ doubt_type: 'invalid_category' }))
    expect(text).toContain('不是约定的三类之一')
  })

  it('owner 未共现：说明两者在分镜里从未同时出现过（2026-10-03-v3）', () => {
    const text = doubtReasonLabel(clauseDoubt({ doubt_type: 'owner_not_cooccurring', owner: '白色陶瓷杯' }))
    expect(text).toContain('白色陶瓷杯')
    expect(text).toContain('从未同时出现')
  })

  it('keep_fragment 不一致：两次保留的片段都要出现（2026-10-03-v3）', () => {
    const text = doubtReasonLabel(clauseDoubt({
      doubt_type: 'keep_fragment_mismatch', keep_fragment_a: '心形翠绿色叶片', keep_fragment_b: '',
    }))
    expect(text).toContain('心形翠绿色叶片')
    expect(text).toContain('整句删除')
  })

  it('keep_fragment 不一致：必须说明「确认删除」不会采纳任一候选片段（审查发现，界面承诺必须与实际行为一致）', () => {
    const text = doubtReasonLabel(clauseDoubt({
      doubt_type: 'keep_fragment_mismatch', keep_fragment_a: '心形翠绿色叶片', keep_fragment_b: '',
    }))
    expect(text).toContain('确认删除')
    expect(text).toContain('不会自动采纳')
  })

  it('子句存疑标注外观原文，别名存疑标注别名', () => {
    expect(doubtSubjectLabel(clauseDoubt())).toBe('外观「瓷瓶外观为青花纹样」')
    expect(doubtSubjectLabel(clauseDoubt({ kind: 'alias', alias: '椅子' }))).toBe('别名「椅子」')
  })

  it('没有存疑时不渲染任何内容（由父组件判断是否挂载本组件，这里只守函数）', () => {
    // PropAuditDoubts 组件本体在 doubts 为空数组时直接返回 null——没有渲染
    // 测试基建（同 PropsPage.test.ts 顶部说明），这条用静态扫描守住这一行为。
    const source = readFileSync(fileURLToPath(new URL('./PropAuditDoubts.tsx', import.meta.url)), 'utf-8')
    expect(source).toMatch(/if \(!doubts\.length\) return null/)
  })
})

// 手动入口接线：PropsPage 把 audit.doubts 非空时才挂载 PropAuditDoubts，并传入
// 确认删除/保留的回调，接到 api.confirmPropAuditDoubt/api.keepPropAuditDoubt。
describe('道具库——存疑确认/保留接线', () => {
  it('PropsPage 引入并按条件挂载 PropAuditDoubts', () => {
    expect(propsPageSource).toMatch(/import PropAuditDoubts from '..\/components\/PropAuditDoubts'/)
    expect(propsPageSource).toMatch(/audit\.doubts\.length > 0/)
    expect(propsPageSource).toMatch(/<PropAuditDoubts/)
  })

  it('PropAuditDoubts 本体调用确认删除与保留两个 API', () => {
    const source = readFileSync(fileURLToPath(new URL('./PropAuditDoubts.tsx', import.meta.url)), 'utf-8')
    expect(source).toMatch(/api\.confirmPropAuditDoubt/)
    expect(source).toMatch(/api\.keepPropAuditDoubt/)
    expect(source).toMatch(/确认删除/)
    expect(source).toMatch(/保留/)
  })
})
