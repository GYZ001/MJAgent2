import { describe, expect, it } from 'vitest'
import type { ArtifactEvidence } from '../../api'
import { matchesEvidenceRelation } from './EvidenceDrawer'

const relation = {
  id: 'artifact_internal_123',
  type: 'shot_video',
  status: 'ready',
} as ArtifactEvidence

describe('质检依据关联筛选', () => {
  it('支持按业务类型和业务状态筛选', () => {
    expect(matchesEvidenceRelation(relation, '镜头视频')).toBe(true)
    expect(matchesEvidenceRelation(relation, '就绪')).toBe(true)
  })

  it('无匹配时返回 false，内部标识仍可用于精确定位', () => {
    expect(matchesEvidenceRelation(relation, '人物谱')).toBe(false)
    expect(matchesEvidenceRelation(relation, 'internal_123')).toBe(true)
  })
})
