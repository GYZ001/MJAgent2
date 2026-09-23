import { describe, expect, it } from 'vitest'
import type { ProviderTaskBlocker, ProviderTaskReconcileResult } from '../api'
import { providerReconcileResultText, reusedReasonLabel } from './providerTaskRecovery'

const blocker = (job_id: string): ProviderTaskBlocker => ({
  job_id, shot_id: null, version_id: null, job_status: 'waiting_provider',
  provider_operation_id: null, provider_task_id: null, provider_create_state: 'accepted',
  claim_status: null, recovery_status: 'waiting_provider', recovery_action: 'continue_provider_poll',
})

const result = (overrides: Partial<ProviderTaskReconcileResult>): ProviderTaskReconcileResult => ({
  episode_id: 'ep1', blockers_before: 0,
  provider_confirmed_terminal_job_ids: [], superseded_jobs_closed_job_ids: [],
  clearance: { safe_to_clear: true, resume_supported: true, blockers: [] },
  ...overrides,
})

describe('providerReconcileResultText', () => {
  it('全部结清且没有残留阻塞时不提"仍有"分句', () => {
    const text = providerReconcileResultText(result({
      provider_confirmed_terminal_job_ids: ['j1', 'j2'],
    }))
    expect(text).toBe('已核对：2 个任务确认供应商终态')
  })

  it('过时任务收口与终态确认同时发生时两句都要有', () => {
    const text = providerReconcileResultText(result({
      provider_confirmed_terminal_job_ids: ['j1'],
      superseded_jobs_closed_job_ids: ['j2', 'j3'],
    }))
    expect(text).toContain('1 个任务确认供应商终态')
    expect(text).toContain('2 个从未提交的过时任务已收口')
  })

  it('仍有供应商侧在途任务时如实告知需要稍后再核对', () => {
    const text = providerReconcileResultText(result({
      clearance: { safe_to_clear: false, resume_supported: true, blockers: [blocker('j9')] },
    }))
    expect(text).toBe('已核对：未发现可结算的供应商任务，仍有 1 个在供应商侧处理中，请稍后再核对')
  })

  it('什么都没发生时不编造结果', () => {
    expect(providerReconcileResultText(result({}))).toBe('已核对：未发现可结算的供应商任务')
  })
})

describe('reusedReasonLabel', () => {
  it('stuck_needs_human 指向真实存在的核对按钮，不是空口承诺', () => {
    expect(reusedReasonLabel('stuck_needs_human')).toBe(
      '现有任务卡在需要人工处理，未提交新任务；可在下方点击「核对供应商任务状态」',
    )
  })

  it('succeeded 如实说明已有交付版本', () => {
    expect(reusedReasonLabel('succeeded')).toBe('已有交付版本，未重新生成')
  })

  it('in_flight 与未知原因都归到"处理中，未重复提交"', () => {
    expect(reusedReasonLabel('in_flight')).toBe('已有任务在处理中，未重复提交')
    expect(reusedReasonLabel(undefined)).toBe('已有任务在处理中，未重复提交')
  })
})
