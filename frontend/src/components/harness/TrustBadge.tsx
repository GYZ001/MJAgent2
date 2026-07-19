const TRUST_LABELS: Record<string, string> = {
  T0: 'T0 仅生成',
  T1: 'T1 结构合法',
  T2: 'T2 规则已验证',
  T3: 'T3 独立评估通过',
  T4: 'T4 人工已确认',
  T5: 'T5 交付已验证',
}

export default function TrustBadge({ level }: { level?: string | null }) {
  const value = level || 'T0'
  return (
    <span className={'trust-badge trust-' + value.toLowerCase()} title={TRUST_LABELS[value] || value}>
      {TRUST_LABELS[value] || value}
    </span>
  )
}
