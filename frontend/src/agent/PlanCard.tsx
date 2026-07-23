export default function PlanCard({ steps }: { steps: string[] }) {
  if (!steps.length) return null
  return (
    <section className="agent-card plan-card">
      <h4>计划</h4>
      <ol>
        {steps.slice(0, 5).map((step, idx) => (
          <li key={`${idx}-${step}`}>{step}</li>
        ))}
      </ol>
    </section>
  )
}
