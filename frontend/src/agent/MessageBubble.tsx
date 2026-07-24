/** 用户消息气泡：右对齐，原样文本（保留换行）。 */
export default function MessageBubble({ text }: { text: string }) {
  return (
    <div className="agent-msg agent-msg-user">
      <div className="agent-bubble">{text}</div>
    </div>
  )
}
