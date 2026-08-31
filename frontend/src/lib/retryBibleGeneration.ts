import { api, Project } from '../api'

/** 人物谱世界观写入失败时的自助修复：只重写 world.visual_style_canonical，不再
 * 调用外部模型判断年代/题材（2026-08-31 二次拍板：那次多余调用曾在真实项目上
 * 触发内容审核，把用户拦在 bible_status=failed 且没有出路），重试基本不会再
 * 失败。BiblePage.tsx 与 ScenesPage.tsx 共用同一套判据，抽成工厂函数避免两处
 * 各写一遍；沿用创建项目时选定并落库的 bible_style_name，不要求用户重选画风。 */
export function retryBibleGenerationAction(
  project: Pick<Project, 'id' | 'bible_style_name'>,
  act: (fn: () => Promise<unknown>, doneMsg?: string) => Promise<unknown>,
) {
  return () => act(async () => {
    const styleName = project.bible_style_name || undefined
    const quote = await api.bibleGeneratePrecheck(project.id, { style_name: styleName })
    await api.generateBible(project.id, {
      confirm: true, quote_id: quote.quote_id, idempotency_key: quote.quote_id, style_name: styleName || '',
    })
  }, '人物谱已重新生成')
}
