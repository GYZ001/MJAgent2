// 人物谱域的桶文件：核心世界书 / 人物与定妆照 / 场景与场景图三块子域各自成文件
// （各自不到 300 行），这里只做重新导出与 `api.<method>` 对象组装，不放业务逻辑。
import * as core from "./core";
import * as characters from "./characters";
import * as scenes from "./scenes";
import * as manual from "./manual";

export * from "./core";
export * from "./characters";
export * from "./scenes";
export * from "./manual";

export const api_bible = {
  // core
  bibleImpactPreview: core.bibleImpactPreview,
  bibleVisualStyles: core.bibleVisualStyles,
  bibleVisualStylesUnscoped: core.bibleVisualStylesUnscoped,
  setBibleStyle: core.setBibleStyle,
  bibleGeneratePrecheck: core.bibleGeneratePrecheck,
  saveBibleDraft: core.saveBibleDraft,
  getBibleDraft: core.getBibleDraft,
  // characters
  regenerateCharacterView: characters.regenerateCharacterView,
  refsPrecheck: characters.refsPrecheck,
  refsGaps: characters.refsGaps,
  refsProgress: characters.refsProgress,
  saveCharacter: characters.saveCharacter,
  listPortraitCandidates: characters.listPortraitCandidates,
  adoptPortraitCandidate: characters.adoptPortraitCandidate,
  rollbackPortraitCandidate: characters.rollbackPortraitCandidate,
  // scenes
  sceneBiblePreview: scenes.sceneBiblePreview,
  sceneBiblePrecheck: scenes.sceneBiblePrecheck,
  genSceneBible: scenes.genSceneBible,
  sceneRefsPrecheck: scenes.sceneRefsPrecheck,
  sceneRefsGaps: scenes.sceneRefsGaps,
  sceneRefsProgress: scenes.sceneRefsProgress,
  genSceneRefs: scenes.genSceneRefs,
  cancelSceneRefs: scenes.cancelSceneRefs,
  editScenePrompt: scenes.editScenePrompt,
  editSceneAnchor: scenes.editSceneAnchor,
  regenerateSceneView: scenes.regenerateSceneView,
  rollbackSceneReference: scenes.rollbackSceneReference,
  // manual（完全手动新增/替换，不走模型）
  addManualCharacter: manual.addManualCharacter,
  replaceCharacterPortraitImage: manual.replaceCharacterPortraitImage,
  addManualScene: manual.addManualScene,
  replaceSceneImage: manual.replaceSceneImage,
  rollbackManualSceneImage: manual.rollbackManualSceneImage,
};
