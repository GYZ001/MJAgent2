import { useEffect, useState } from 'react'

/** 按当前视口估算网格每页条数，尽量铺满一屏再分页。 */
export function useFillPageSize({
  minCardWidth,
  gap = 14,
  rows = 3,
  deskMaxWidth = 1480,
  deskPaddingX = 76,
  floor = 8,
  ceiling = 36,
}: {
  minCardWidth: number
  gap?: number
  rows?: number
  deskMaxWidth?: number
  deskPaddingX?: number
  floor?: number
  ceiling?: number
}) {
  const [pageSize, setPageSize] = useState(() => Math.max(floor, Math.min(ceiling, 12)))

  useEffect(() => {
    const calc = () => {
      const narrow = window.matchMedia('(max-width: 1080px)').matches
      const mobile = window.matchMedia('(max-width: 720px)').matches
      const sidebarWidth = mobile ? 0 : narrow ? 76 : 224
      const available = Math.min(
        deskMaxWidth,
        Math.max(320, window.innerWidth - sidebarWidth - deskPaddingX),
      )
      const cols = Math.max(1, Math.floor((available + gap) / (minCardWidth + gap)))
      const next = Math.max(floor, Math.min(ceiling, cols * rows))
      setPageSize(next)
    }
    calc()
    window.addEventListener('resize', calc)
    return () => window.removeEventListener('resize', calc)
  }, [minCardWidth, gap, rows, deskMaxWidth, deskPaddingX, floor, ceiling])

  return pageSize
}
