/**
 * 区间压缩：把段号/序号数组压成用户约定的展示格式（如 "1,3,5~7"）——连续 ≥2 个的
 * 数字合并成 "起~止"（用户明确要求 `~`，不是 `-`），孤立数字单独列出，段之间用
 * `,` 分隔、不加空格。内部先去重、按升序排列，调用方传入乱序或带重复的数组都
 * 得到同一个结果。空数组回退空串——调用方据此退回只展示"覆盖 N 段"，不渲染
 * 区间部分。
 *
 * 2.0.0（原名 compressEventOrders）：函数本身逐字节未变——它从来只是纯数字数组
 * 压缩，不含任何"事件"语义；重命名是因为调用方从换算"事件序号"改成直接使用
 * asset_manifest 条目自带的 segment_indexes（原文段号），不再需要 eventIdsToOrders
 * 这层换算，见 app/production/prep_pack.py 模块 docstring 的 2.0.0 说明。
 *
 * 提取到 lib/（原在 pages/ScriptPage.tsx）：分镜台 2.0.0 展示改造需要在
 * BoardPage.tsx 里展示 source_segment_indexes 的同一种压缩格式，跨页复用必须
 * 共享同一个真源，不得复制第二份实现——见 docs/STORYBOARD_PROMPT_IR_DESIGN.md。
 * ScriptPage.tsx 通过 `export { compressSegmentIndexes } from '../lib/segmentIndexes'`
 * 保持对外接口不变，函数体没有改动。
 */
export function compressSegmentIndexes(indexes: number[]): string {
  const sorted = Array.from(new Set(indexes)).sort((a, b) => a - b)
  if (!sorted.length) return ''
  const segments: string[] = []
  let start = sorted[0]
  let prev = sorted[0]
  for (let i = 1; i <= sorted.length; i++) {
    const current = sorted[i]
    if (current !== undefined && current === prev + 1) {
      prev = current
      continue
    }
    segments.push(start === prev ? `${start}` : `${start}~${prev}`)
    if (current !== undefined) {
      start = current
      prev = current
    }
  }
  return segments.join(',')
}
