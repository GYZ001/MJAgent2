export default function EvidenceCitation({
  artifactId,
  label,
  onOpen,
}: {
  artifactId: string
  label?: string
  onOpen?: (id: string) => void
}) {
  return (
    <button
      type="button"
      className="agent-evidence"
      onClick={() => onOpen?.(artifactId)}
    >
      {label || `证据 ${artifactId.slice(0, 10)}`}
    </button>
  )
}
