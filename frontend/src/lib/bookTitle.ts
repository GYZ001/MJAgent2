/** 书名号规范化：已有完整《》时不重复包裹。 */
export function formatBookTitle(name: string | null | undefined): string {
  const trimmed = (name || '').trim()
  if (!trimmed) return '《未命名》'
  if (trimmed.startsWith('《') && trimmed.endsWith('》') && trimmed.length >= 2) {
    return trimmed
  }
  // 半边书名号：去掉残缺侧后统一包裹
  let core = trimmed
  if (core.startsWith('《')) core = core.slice(1)
  if (core.endsWith('》')) core = core.slice(0, -1)
  core = core.trim() || '未命名'
  return `《${core}》`
}
