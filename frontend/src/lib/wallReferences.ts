import type { ReferenceImage, Shot } from '../api'

/**
 * 生成台「本次生成实际参考图」面板的两个纯判据（WallPage.tsx 消费）。
 *
 * 只有 GET /shots/{id}/review 带 image_inputs.reference_images（列表接口为控制体积
 * 不带），于是生成台同时吃两个新鲜度不同的数据源：分集轮询（4 秒一次，带出新版本
 * 与状态）和单镜详情（原先只在切段/手动刷新时取一次）。两者错位曾让界面撒谎——
 * 用户点「生成所有视频」时详情早已取过（那一刻这一段还没有任何版本），随后轮询
 * 带出「生成中」的版本，而参考图快照里根本没有这条版本，面板把「还不知道」当成
 * 「一张都没有」，整轮生成都挂着红色「参考图缺失」；实测 shot_6aa94fbb456a 的
 * image_inputs 里三张参考图（孟浩、小胖子、靠山宗杂役屋舍）一张不少。
 */

/** 按版本 id 摊平详情响应里的参考图。**键存在 = 这条版本的参考图已知**，此时空
 *  数组才是「真的一张都没带」；键缺失 = 本次快照没覆盖到它，不构成任何结论。
 *  image_inputs 因体积被后端裁掉（omitted_for_size）时同样按未知处理。 */
export function extractReferenceImagesByVersion(
  shot: Pick<Shot, 'versions'>,
): Record<string, ReferenceImage[]> {
  const map: Record<string, ReferenceImage[]> = {}
  for (const version of shot.versions ?? []) {
    const inputs = version.image_inputs
    if (!inputs || inputs.omitted_for_size) continue
    map[version.id] = inputs.reference_images ?? []
  }
  return map
}

/** 选中段在轮询数据里的版本指纹：版本集合、状态、供应商任务号任一变化，都说明这
 *  一段有了新的生成事实，详情快照必须跟着重取。故意不含 running_since 这类每轮
 *  轮询都在动的字段，否则 4 秒一次的轮询会变成 4 秒一次的详情请求。 */
export function shotVersionSignature(shot: Pick<Shot, 'versions'> | undefined): string {
  return (shot?.versions ?? [])
    .map(version => `${version.id}:${version.status}:${version.provider_task_id ?? ''}`)
    .join('|')
}
