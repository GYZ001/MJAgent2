import { api, type ProviderTaskReconcileResult, type ReusedReason } from '../api'

/** 把"核对供应商任务状态"从一句兑现不了的文案接成真实动作。
 *
 * 真实入口：POST /episodes/{id}/provider-tasks/reconcile
 * （app/domain/video_ops/clear.py::reconcile_episode_provider_tasks），只需
 * manju:project-write（与生成视频同档，见 app/capabilities/exemptions.py），
 * 不要求系统管理员，也不需要先触发"清空视频提示词"等整集破坏性操作——分镜台
 * BoardPage.tsx 是复用同一接口的另一条（更绕的）入口，不是唯一入口。此前生成台
 * WallPage.tsx 只在 toast 里承诺"请核对供应商任务状态"却没有任何入口能做这件事
 * （CLAUDE.md「拦住用户时必须给出路」），GenerationPanel 现在直接调用它。
 */
export async function reconcileProviderTasksAndReport(
  episodeId: string,
  onToast: (message: string, isErr?: boolean) => void,
): Promise<void> {
  try {
    onToast(providerReconcileResultText(await api.reconcileProviderTasks(episodeId)))
  } catch (error) {
    onToast(error instanceof Error ? error.message : String(error), true)
  }
}

/** 核对结果的诚实转述：按实际发生的三类结果拼句子，不发生的类别不提。 */
export function providerReconcileResultText(result: ProviderTaskReconcileResult): string {
  const settled = result.provider_confirmed_terminal_job_ids.length
  const closed = result.superseded_jobs_closed_job_ids.length
  const remaining = result.clearance.blockers.length
  const parts: string[] = []
  if (settled) parts.push(`${settled} 个任务确认供应商终态`)
  if (closed) parts.push(`${closed} 个从未提交的过时任务已收口`)
  if (!parts.length) parts.push('未发现可结算的供应商任务')
  if (remaining) parts.push(`仍有 ${remaining} 个在供应商侧处理中，请稍后再核对`)
  return `已核对：${parts.join('，')}`
}

/** reused=true 时的诚实文案——不再说"输入未变化"，那句话从未真正比较过输入。
 *  按服务端回传的 reused_reason 如实转述命中记录的真实状态。 */
export function reusedReasonLabel(reason?: ReusedReason): string {
  switch (reason) {
    case 'succeeded':
      return '已有交付版本，未重新生成'
    case 'stuck_needs_human':
      return '现有任务卡在需要人工处理，未提交新任务；可在下方点击「核对供应商任务状态」'
    case 'in_flight':
    default:
      return '已有任务在处理中，未重复提交'
  }
}
