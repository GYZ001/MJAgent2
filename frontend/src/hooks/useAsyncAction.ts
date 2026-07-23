import { useCallback, useRef, useState } from 'react'

/** 通用异步动作：提交锁，失败返回 undefined，成功返回结果。 */
export function useAsyncAction() {
  const [busy, setBusy] = useState(false)
  const locked = useRef(false)

  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    if (locked.current) return undefined
    locked.current = true
    setBusy(true)
    try {
      return await fn()
    } catch (e: unknown) {
      throw e
    } finally {
      locked.current = false
      setBusy(false)
    }
  }, [])

  return { busy, run }
}
