import { api, ApiError } from '../api'

export type StyleRegenOutcome =
  | { kind: 'unchanged' }
  | {
    kind: 'started'
    refsStarted: boolean
    refsError: string | null
    sceneRefsStarted: boolean
    sceneRefsError: string | null
    sceneBibleReady: boolean
  }
  | { kind: 'idempotent_replay' }

/**
 * 人物谱页与场景库页共用：切换项目统一画风（不改动人物设定本身）。
 *
 * 两段式走同一个后端路由（POST /projects/{id}/bible/style）：第一次不带
 * confirm，画风未变化时直接拿到结果（changed=false，幂等短路）；画风有
 * 变化时后端返回 409 + 合并报价（人物定妆照 + 场景图两条腿的费用），这里
 * 直接用报价里的 quote_id 发起第二次请求确认——不再弹窗等用户手动点
 * 「确认并开始」（2026-08-29 用户拍板：删除生成前的费用确认弹窗；模型与
 * 视频生成走公司自有服务，本来就不计费，这层确认对他是纯摩擦）。
 *
 * 后端在**同一次请求内**发起人物与场景两条生成线，不是这里自己在前端
 * 排队调用两个不同端点——那样任一步失败或页面被关掉，另一条线就发不出去。
 */
export async function applyStyleRegen(
  projectId: string,
  styleName: string,
  expectedVersion: number,
): Promise<StyleRegenOutcome> {
  try {
    const result = await api.setBibleStyle(projectId, { style_name: styleName, expected_version: expectedVersion })
    if (!result.changed) return { kind: 'unchanged' }
    // 后端在「未变化」之外理论上总是要求确认；真出现 changed=true 且无需确认，
    // 不该在这里悄悄吞掉，按已开始处理如实上报。
    return {
      kind: 'started',
      refsStarted: !!result.refs_started,
      refsError: result.refs_error ?? null,
      sceneRefsStarted: !!result.scene_refs_started,
      sceneRefsError: result.scene_refs_error ?? null,
      sceneBibleReady: !!result.scene_bible_ready,
    }
  } catch (e: unknown) {
    if (!(e instanceof ApiError) || e.code !== 'PAYMENT_CONFIRM_REQUIRED') throw e
    const quoteId = (e.detail as { precheck?: { quote_id?: string } } | undefined)?.precheck?.quote_id
    if (!quoteId) throw e
    const result = await api.setBibleStyle(projectId, {
      style_name: styleName, expected_version: expectedVersion, confirm: true, quote_id: quoteId,
    })
    if (result.idempotent_replay) return { kind: 'idempotent_replay' }
    return {
      kind: 'started',
      refsStarted: !!result.refs_started,
      refsError: result.refs_error ?? null,
      sceneRefsStarted: !!result.scene_refs_started,
      sceneRefsError: result.scene_refs_error ?? null,
      sceneBibleReady: !!result.scene_bible_ready,
    }
  }
}
