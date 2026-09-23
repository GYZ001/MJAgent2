import { useCallback, useEffect, useRef, useState } from 'react'
import { api, CustomerFeedbackRecord } from '../../api'
import DecisionDialog from '../../components/DecisionDialog'

interface CustomerFeedbackPanelProps {
  episodeId: string
  episodeNo: number
  reviewer: string
  toast: (msg: string, isErr?: boolean) => void
  onNavigateToBoard: () => void
}

export function feedbackSubmitDisabledReason(busy: boolean, feedback: string): string {
  if (busy) return '正在提交上一条反馈'
  if (!feedback.trim()) return '请先填写反馈内容'
  return ''
}

export function formatFeedbackTime(value: number): string {
  const ms = value < 1_000_000_000_000 ? value * 1000 : value
  return new Date(ms).toLocaleString('zh-CN')
}

/**
 * 成片台客户反馈区：只做记录，不创建修订任务（2026-09-23 退场
 * delivery_revision——那条 workflow_run 从没有执行者推进过，界面却承诺"创建
 * 修订任务"是空头支票；建的 run 永远停在 CREATED，还会被部署闸门当成"在途"
 * 拦住生产部署）。从 CinemaPage.tsx 搬出，连同新的只读反馈列表一起自成一区，
 * 让 CinemaPage 变短而不是变长。需要修改本集内容走"去分镜台修订本集"入口。
 */
export default function CustomerFeedbackPanel({
  episodeId, episodeNo, reviewer, toast, onNavigateToBoard,
}: CustomerFeedbackPanelProps) {
  const [feedback, setFeedback] = useState('')
  const [feedbackBusy, setFeedbackBusy] = useState(false)
  const [feedbackConfirmOpen, setFeedbackConfirmOpen] = useState(false)
  const [items, setItems] = useState<CustomerFeedbackRecord[]>([])
  const [listError, setListError] = useState<string | null>(null)
  const dialogTriggerRef = useRef<HTMLElement | null>(null)

  const loadFeedback = useCallback(async () => {
    try {
      setItems(await api.getCustomerFeedback(episodeId))
      setListError(null)
    } catch (e) {
      setListError((e as Error).message)
    }
  }, [episodeId])

  useEffect(() => { void loadFeedback() }, [loadFeedback])

  const submitFeedback = async () => {
    const message = feedback.trim()
    if (!message) return
    setFeedbackBusy(true)
    try {
      await api.submitCustomerFeedback(episodeId, { message, created_by: reviewer.trim() || 'customer' })
      setFeedback('')
      toast('反馈已记录')
      await loadFeedback()
    } catch (e) {
      toast((e as Error).message, true)
    } finally {
      setFeedbackBusy(false)
    }
  }

  const disabledReason = feedbackSubmitDisabledReason(feedbackBusy, feedback)

  return (
    <div className="customer-feedback">
      <p className="hint">
        反馈会记录在本集交付记录下，不会自动创建修订任务；需要修改本集内容，请到分镜台或生成台处理。
        {' '}
        <button type="button" className="btn ghost small" onClick={onNavigateToBoard}>
          去分镜台修订本集
        </button>
      </p>
      {listError && <p className="hint" role="alert">反馈记录加载失败：{listError}</p>}
      {items.length > 0 && (
        <ul>
          {items.map(item => (
            <li key={item.id}>
              <span>{formatFeedbackTime(item.created_at)}</span>{' '}
              <b>{item.created_by}</b>{' '}
              {item.rating != null && <span className="stamp gold">{item.rating} 分</span>}
              <p>{item.message}</p>
            </li>
          ))}
        </ul>
      )}
      <input
        disabled={feedbackBusy}
        aria-label={disabledReason ? `客户反馈，暂不可用：${disabledReason}` : '客户反馈'}
        value={feedback}
        onChange={event => setFeedback(event.target.value)}
        placeholder="输入客户反馈，会记录在本集交付记录下"
      />
      <button
        className="btn primary small"
        disabled={Boolean(disabledReason)}
        aria-label={disabledReason ? `提交客户反馈，暂不可用：${disabledReason}` : '提交客户反馈'}
        onClick={event => {
          dialogTriggerRef.current = event.currentTarget
          setFeedbackConfirmOpen(true)
        }}
      >{feedbackBusy ? '提交中…' : '提交反馈'}</button>
      {feedbackConfirmOpen && (
        <DecisionDialog
          title="提交这条客户反馈？"
          summary={`第 ${episodeNo} 集 · ${feedback.trim()}`}
          message="确认后会把这条反馈记录在本集交付记录下，不会自动创建修订任务。如需修改本集内容，请到分镜台或生成台处理。"
          details={[
            '不会创建修订任务，也不会自动重新生成图片或视频',
            '当前成片、已交付快照和既有审核记录不会被覆盖',
            '反馈提交成功后会清空本次输入',
          ]}
          confirmLabel="确认提交反馈"
          cancelLabel="返回修改反馈"
          returnFocus={dialogTriggerRef.current}
          onClose={() => setFeedbackConfirmOpen(false)}
          onConfirm={() => {
            setFeedbackConfirmOpen(false)
            void submitFeedback()
          }}
        />
      )}
    </div>
  )
}
