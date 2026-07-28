import { useEffect, useRef } from 'react'

/** 弹窗焦点圈定：首焦点、Tab 不逃出、Esc 关闭、关闭后可恢复触发点。 */
export function useFocusTrap(
  active: boolean,
  onClose: () => void,
  options?: {
    dirty?: boolean
    onDirtyClose?: () => void
    returnFocus?: HTMLElement | null
    suspended?: boolean
  },
) {
  const containerRef = useRef<HTMLElement | null>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const onCloseRef = useRef(onClose)
  const optionsRef = useRef(options)
  onCloseRef.current = onClose
  optionsRef.current = options

  useEffect(() => {
    if (!active) return
    previousFocus.current = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const node = containerRef.current
    const focusables = () => {
      if (!node) return [] as HTMLElement[]
      return Array.from(node.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter(el => !el.hasAttribute('disabled') && el.offsetParent !== null)
    }
    const initial = focusables()[0]
    initial?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (optionsRef.current?.suspended) return
      if (event.key === 'Escape') {
        event.preventDefault()
        const latestOptions = optionsRef.current
        if (latestOptions?.dirty && latestOptions.onDirtyClose) latestOptions.onDirtyClose()
        else onCloseRef.current()
        return
      }
      if (event.key !== 'Tab' || !node) return
      const items = focusables()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
      const previous = previousFocus.current
      const fallback = optionsRef.current?.returnFocus
      const previousUsable = previous
        && previous !== document.body
        && previous.isConnected
        && previous.getClientRects().length > 0
      const fallbackUsable = fallback
        && fallback.isConnected
        && fallback.getClientRects().length > 0
      const target = fallbackUsable ? fallback : previousUsable ? previous : previous
      target?.focus?.()
    }
  }, [active])

  return containerRef
}
