import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { CharacterLooksStatus } from '../api/storyboard/characterLooks'

/**
 * 人物造型照状态与补齐入口（2026-10-02）：非默认造型段没有造型照时会退回全身
 * 定妆照、服装可能被带偏（见后端 app.video_modes.character_look_views.
 * look_fallback_notice）；这里给出就绪/生成中/失败/待生成计数与「补齐造型照」
 * 按钮，让人工核查时能看见、能补——不把人晾在原地（CLAUDE.md「拦住用户时必须
 * 给出路」）。
 *
 * 轮询只在「点了补齐」之后进行，不常驻轮询整个分镜台——造型照生成是分钟级供应商
 * 调用，没有正在生成时没有必要每次进页面都发一次状态查询。
 */

const POLL_INTERVAL_MS = 4000
const MAX_POLLS = 20

export default function CharacterLooksPanel({ episodeId }: { episodeId: string }) {
  const [status, setStatus] = useState<CharacterLooksStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const pollCountRef = useRef(0)

  const load = useCallback(async () => {
    try {
      const result = await api.getCharacterLooks(episodeId)
      setStatus(result)
      setError(null)
      return result
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : '造型照状态查询失败')
      return null
    }
  }, [episodeId])

  useEffect(() => {
    void load()
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [load])

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const startPolling = useCallback(() => {
    stopPolling()
    pollCountRef.current = 0
    pollRef.current = setInterval(() => {
      pollCountRef.current += 1
      void load().then(result => {
        const stillGenerating = (result?.summary.generating ?? 0) > 0
        if (!stillGenerating || pollCountRef.current >= MAX_POLLS) stopPolling()
      })
    }, POLL_INTERVAL_MS)
  }, [load, stopPolling])

  async function onStart() {
    setBusy(true)
    try {
      await api.startCharacterLooks(episodeId)
      await load()
      startPolling()
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : '补齐造型照未能受理')
    } finally {
      setBusy(false)
    }
  }

  if (!status) return null
  const { ready, generating, failed, missing } = status.summary
  const needsAttention = missing + failed > 0
  if (ready + generating + failed + missing === 0) return null

  return (
    <div className="character-looks-panel" role="status">
      <span>
        人物造型照：就绪 <b>{ready}</b> · 生成中 <b>{generating}</b> · 失败{' '}
        <b className={failed > 0 ? 'character-looks-count-bad' : ''}>{failed}</b> · 待生成{' '}
        <b className={missing > 0 ? 'character-looks-count-bad' : ''}>{missing}</b>
      </span>
      {needsAttention && (
        <button
          type="button"
          className="btn small"
          disabled={busy || generating > 0}
          title="以人物定妆照为基础，按本段服装描述补齐造型照，避免非默认造型段退回定妆照带偏服装"
          onClick={() => void onStart()}
        >
          {busy || generating > 0 ? '正在补齐…' : '补齐造型照'}
        </button>
      )}
      {error && <small className="character-looks-error">{error}</small>}
    </div>
  )
}
