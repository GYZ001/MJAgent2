import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import {
  requestCapabilityApproval,
  resolveCapabilityApproval,
} from '../capabilityApproval'
import CapabilityApprovalHost from './CapabilityApprovalHost'

describe('CapabilityApprovalHost', () => {
  it('shows the screenplay rebuild approval card before resolving the request', async () => {
    const decision = requestCapabilityApproval({
      status: 'waiting_approval',
      command: 'screenplay.resume',
      approval_id: 'appr-rebuild',
      approval_token: 'appr-rebuild.signature',
      preflight: {
        summary: '按当前合同重建剧本基线',
        risk: 'R2',
      },
    })

    const html = renderToStaticMarkup(createElement(CapabilityApprovalHost))

    expect(html).toContain('批准：screenplay.resume')
    expect(html).toContain('按当前合同重建剧本基线')
    expect(html).toContain('风险等级：R2')
    expect(html).toContain('批准一次')

    resolveCapabilityApproval(false)
    await expect(decision).resolves.toBe(false)
  })
})
