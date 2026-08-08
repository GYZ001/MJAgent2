import { useCallback, useLayoutEffect, useState } from 'react'

export interface FillPageSizeOptions {
  minCardWidth: number
  gap?: number
  rows?: number
  floor?: number
  ceiling?: number
}

export const SINGLE_ROW_ASSET_PAGE: FillPageSizeOptions = {
  minCardWidth: 270,
  rows: 1,
  floor: 1,
  ceiling: 8,
}

export function pageSizeForWidth({
  available,
  minCardWidth,
  gap = 14,
  rows = 3,
  floor = 8,
  ceiling = 36,
}: FillPageSizeOptions & { available: number }): number {
  const cols = Math.max(1, Math.floor((available + gap) / (minCardWidth + gap)))
  return Math.max(floor, Math.min(ceiling, cols * rows))
}

/** 按网格的实际内容宽度计算每页条数；返回的 ref 必须挂在对应网格上。 */
export function useFillPageSize({
  minCardWidth,
  gap = 14,
  rows = 3,
  floor = 8,
  ceiling = 36,
}: FillPageSizeOptions) {
  const [container, setContainer] = useState<HTMLElement | null>(null)
  const [pageSize, setPageSize] = useState(0)
  const containerRef = useCallback((node: HTMLElement | null) => setContainer(node), [])

  useLayoutEffect(() => {
    if (!container) return
    const calc = () => {
      const measuredGap = Number.parseFloat(window.getComputedStyle(container).columnGap)
      const available = container.clientWidth
      if (available <= 0) return
      const next = pageSizeForWidth({
        available,
        minCardWidth,
        gap: Number.isFinite(measuredGap) ? measuredGap : gap,
        rows,
        floor,
        ceiling,
      })
      setPageSize(current => current === next ? current : next)
    }
    calc()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(calc)
    observer?.observe(container)
    window.addEventListener('resize', calc)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', calc)
    }
  }, [container, minCardWidth, gap, rows, floor, ceiling])

  return [pageSize, containerRef] as const
}
