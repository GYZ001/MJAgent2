import { useEffect, type RefObject } from 'react'

/**
 * 把滚轮限制在容器内：内部可滚动区域滚到顶/底，或落在非滚动区域时，
 * 不再把滚动链到页面 / 其它面板。
 */
export function useScrollContainment(
  ref: RefObject<HTMLElement | null>,
  enabled = true,
) {
  useEffect(() => {
    const root = ref.current
    if (!root || !enabled) return

    const findScroller = (start: EventTarget | null): HTMLElement | null => {
      let node = start as HTMLElement | null
      while (node && node !== root) {
        if (node instanceof HTMLElement) {
          const style = getComputedStyle(node)
          const canY = /(auto|scroll|overlay)/.test(style.overflowY)
          if (canY && node.scrollHeight > node.clientHeight + 1) return node
        }
        node = node.parentElement
      }
      if (root instanceof HTMLElement) {
        const style = getComputedStyle(root)
        const canY = /(auto|scroll|overlay)/.test(style.overflowY)
        if (canY && root.scrollHeight > root.clientHeight + 1) return root
      }
      return null
    }

    const onWheel = (event: WheelEvent) => {
      const scroller = findScroller(event.target)
      if (!scroller) {
        event.preventDefault()
        return
      }
      const dy = event.deltaY
      if (dy === 0) return
      const top = scroller.scrollTop
      const max = scroller.scrollHeight - scroller.clientHeight
      const atTop = top <= 0
      const atBottom = top >= max - 1
      if ((dy < 0 && atTop) || (dy > 0 && atBottom)) {
        event.preventDefault()
      }
    }

    root.addEventListener('wheel', onWheel, { passive: false })
    return () => root.removeEventListener('wheel', onWheel)
  }, [enabled, ref])
}
