import { EvidenceIssue } from '../../api'

export default function IssueList({ issues }: { issues: EvidenceIssue[] }) {
  if (!issues.length) return <div className="issue-empty">没有未解决问题</div>
  return (
    <div className="issue-list">
      {issues.map((issue, index) => (
        <article className={'issue-item ' + issue.severity} key={issue.code + issue.subject + index}>
          <div>
            <span>{issue.severity === 'blocker' ? '阻塞' : issue.severity === 'warning' ? '警告' : '提示'}</span>
            <code>{issue.code}</code>
          </div>
          <b>{issue.message}</b>
          {issue.repair_hint && <p>{issue.repair_hint}</p>}
          <small>{issue.subject}</small>
        </article>
      ))}
    </div>
  )
}
