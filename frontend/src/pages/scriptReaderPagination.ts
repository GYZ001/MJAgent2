import type { PlotSpine, PlotSpineBeat } from '../api'

export type SpineReaderItem =
  | { kind: 'premise'; text: string }
  | { kind: 'beat'; beat: PlotSpineBeat; index: number }
  | { kind: 'ending'; text: string }
  | { kind: 'drop'; text: string; index: number }

export function paginateItems<T>(items: T[], pageSize: number): T[][] {
  if (!items.length) return []
  const safePageSize = Math.max(1, Math.floor(pageSize))
  const pages: T[][] = []
  for (let index = 0; index < items.length; index += safePageSize) {
    pages.push(items.slice(index, index + safePageSize))
  }
  return pages
}

export function paginateSpine(spine: PlotSpine, pageSize = 5): SpineReaderItem[][] {
  const items: SpineReaderItem[] = []
  if (spine.episode_premise) items.push({ kind: 'premise', text: spine.episode_premise })
  ;(spine.spine_beats ?? []).forEach((beat, index) => items.push({ kind: 'beat', beat, index }))
  if (spine.must_keep_ending) items.push({ kind: 'ending', text: spine.must_keep_ending })
  ;(spine.drop_list ?? []).forEach((text, index) => items.push({ kind: 'drop', text, index }))
  return paginateItems(items, pageSize)
}

export function paginateManuscript(text: string, charBudget = 700): string[] {
  if (!text) return []
  const safeBudget = Math.max(1, Math.floor(charBudget))
  const pages: string[] = []
  let start = 0

  while (start < text.length) {
    const target = Math.min(start + safeBudget, text.length)
    if (target === text.length) {
      pages.push(text.slice(start))
      break
    }

    const preferredStart = start + Math.floor(safeBudget * 0.55)
    const lineBreak = text.lastIndexOf('\n', target - 1)
    let end = lineBreak >= preferredStart ? lineBreak + 1 : target

    if (end === target) {
      for (let index = target - 1; index >= preferredStart; index -= 1) {
        if ('。！？；'.includes(text[index])) {
          end = index + 1
          break
        }
      }
    }

    pages.push(text.slice(start, end))
    start = end
  }

  return pages
}
