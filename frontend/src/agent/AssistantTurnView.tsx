import ApprovalCard from './ApprovalCard'
import EvidenceCitation from './EvidenceCitation'
import Markdown from './Markdown'
import RunProgressCard from './RunProgressCard'
import ThinkingBlock from './ThinkingBlock'
import type { AssistantTranscriptItem } from './transcript'

/** 渲染一条 assistant 消息：思考过程 + 正文 Markdown + 归属本轮的审批/Run/证据卡。 */
export default function AssistantTurnView({
  item,
  onApprove,
  onReject,
  onOpenRun,
  onOpenEvidence,
  onFollowIntent,
}: {
  item: AssistantTranscriptItem
  onApprove: (toolCallId: string, reason: string) => Promise<void> | void
  onReject: (toolCallId: string, reason: string) => Promise<void> | void
  onOpenRun: (runId: string) => void
  onOpenEvidence: (artifactId: string) => void
  onFollowIntent: () => void
}) {
  const streaming = item.status === 'streaming'

  return (
    <div className="agent-msg agent-msg-assistant">
      <ThinkingBlock text={item.thinking} streaming={streaming} />

      {item.answer.trim() ? (
        <div className="agent-answer">
          <Markdown text={item.answer} />
          {streaming && <span className="agent-caret" aria-hidden="true" />}
        </div>
      ) : null}

      {item.approvals.map(card => (
        <ApprovalCard
          key={card.tool_call_id}
          data={card}
          onApprove={reason => onApprove(card.tool_call_id, reason)}
          onReject={reason => onReject(card.tool_call_id, reason)}
        />
      ))}

      {item.runs.map(run => (
        <RunProgressCard
          key={run.runId}
          runId={run.runId}
          summary={run.summary}
          onOpen={() => onOpenRun(run.runId)}
        />
      ))}

      {item.citations.length > 0 && (
        <div className="agent-citations">
          {item.citations.map(id => (
            <EvidenceCitation key={id} artifactId={id} onOpen={() => onOpenEvidence(id)} />
          ))}
        </div>
      )}

      {item.intent && (
        <div className="agent-card agent-intent-card">
          <p>助手建议定位到相关页面</p>
          <button type="button" className="btn primary" onClick={onFollowIntent}>定位</button>
        </div>
      )}

      {item.error && item.status === 'failed' && (
        <div className="agent-error" role="alert">{item.error}</div>
      )}
    </div>
  )
}
