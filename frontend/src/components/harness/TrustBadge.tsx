import { statusLabel } from '../../lib/statusLabels'

export default function TrustBadge({ level }: { level?: string | null }) {
  const value = level || 'T0'
  const label = statusLabel(value)
  const known = label !== '未知状态'
  return (
    <span
      className={'trust-badge trust-' + value.toLowerCase()}
      title={known ? label : '验证等级待确认'}
    >
      {known ? label : '验证等级待确认'}
    </span>
  )
}
