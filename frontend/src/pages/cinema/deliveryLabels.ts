/**
 * 从 CinemaPage.tsx 搬出（U4，见 CLAUDE.md「架构与文件规范」：CinemaPage.tsx
 * 基线 931 行不能再涨）。CinemaPage.tsx 用
 * `export { deliveryWarningLabel } from './cinema/deliveryLabels'` 保住既有
 * 导入路径，CinemaPage.test.ts 仍从 './CinemaPage' 导入它。
 */
export function deliveryWarningLabel(value: string): string {
  const translated = value
    .replace(/Duplicate frames(?:\s*\(frame \d+ and frame \d+\))?/gi, '存在重复画面帧')
    .replace(/Missing start state of /gi, '未呈现预期起始状态：')
    .replace(/End state mismatch:\s*/gi, '结束状态不符合预期：')
    .replace(/Mismatched starting state/gi, '起始状态不符合预期')
    .replace(/Start state partially mismatched:\s*/gi, '起始状态部分不符合预期：')
    .replace(/Character outfit does not match the expected design(?:\s*\([^)]*\))?/gi, '角色服装与预期设计不一致')
    .replace(/The expected core action is not fully completed/gi, '预期核心动作未完整完成')
    .replace(/Some character faces do not match the provided character anchors/gi, '部分角色面部与人物设定不一致')
    .replace(/target character/gi, '目标角色')
    .replace(/is not present in the scene/gi, '未出现在画面中')
    .replace(/the girl is bowing instead of standing calmly as expected/gi, '角色正在鞠躬，而预期为平静站立')
    .replace(/'s outfit has incorrect accessory\s*/gi, '的服装配饰与人物设定不一致：')
    .replace(/'s outfit does not match the character anchor/gi, '的服装与人物设定不一致')
    .replace(/'s outfit does not match the expected light green top and tight pants, instead wearing a purple dress/gi, '的服装不符合预期：应为淡绿色上衣搭配紧腿长裤，实际为紫色连衣裙')
    .replace(/has raised his head instead of not responding yet/gi, '已抬头回应，而预期仍未作出反应')
    .replace(/preparing to approach/gi, '准备走向')
    .replaceAll('角色锚点', '人物设定')
    .replaceAll('锚点', '设定参考')
    .replaceAll('AI生成', '生成工具')
    .replace(/(\d+)s\b/gi, '$1 秒')
  return /[A-Za-z]{3}/.test(translated)
    ? '画面状态或人物一致性与预期不符，请结合对应镜头人工复验'
    : translated
}
