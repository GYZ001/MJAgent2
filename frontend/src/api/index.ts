// 前端 API 层入口。拆分后的目录结构：
//   client.ts    — 传输层：ApiError、会话管理、request/get/mutate/download（内部
//                  可见，不在这里对外导出；只有 get/post/put/del/upload 这几个
//                  逃生口作为 `api` 对象的方法保留，供确实无法收敛成具名方法的
//                  调用点使用）。
//   auth.ts      — 登录态：login/logout/me/changePassword，onUnauthenticated 信号。
//   common.ts    — 跨域共享的叶子类型（ArtifactEvidence/TaskTiming/...）与
//                  numToCn 工具。
//   bible/       — 人物谱域（世界书 / 人物与定妆照 / 场景与场景图）。
//   screenplay.ts— 映射台域（EpisodeScreenplay / EpisodePrepPack / ScreenplayState）。
//   storyboard/  — 分镜台域（Shot / ShotVersion / StoryboardPack / 分镜台任务状态）。
//   video.ts     — 视频生成域（ShotVideoGenerationPlan / ReviewWallContext）。
//   delivery.ts  — 成片与交付域（MixStatus / DeliveryReadiness / DeliveryPackageRecord）。
//   projects.ts  — 项目/分集聚合域（Project / Episode / ChapterContent）。
//   system/      — 观测台与系统管理域（任务队列/调用日志/系统设置/模型中心/
//                  总览/账号管理/链路追踪）。
//
// 硬约束：`api` 对象的方法名与对外形状不得因为这次重组而改变——全前端大量
// `import { api } from '../api'`，这里只是换文件组织，不是换 API。

import { download, get, mutate, request, ApiError, onUnauthenticated } from "./client";
import * as bibleApi from "./bible";
import * as screenplayApi from "./screenplay";
import * as storyboardApi from "./storyboard";
import * as videoApi from "./video";
import * as deliveryApi from "./delivery";
import * as projectsApi from "./projects";
import * as systemApi from "./system";
import { getArtifactLineage, numToCn } from "./common";

export type { AuthMeResponse, AuthLoginResponse } from "./auth";
export { login, logout, me, changePassword } from "./auth";
export { onUnauthenticated, ApiError };
export { numToCn };
export * from "./common";
export * from "./bible";
export * from "./screenplay";
export * from "./storyboard";
export * from "./video";
export * from "./delivery";
export * from "./projects";
export * from "./system";

export const api = {
  /* ── 泛型逃生口：只在动态拼 URL 或专为测试注入的场景保留，见各调用点注释 ── */
  get,
  post: (path: string, body?: unknown) => mutate("POST", path, body),
  put: (path: string, body: unknown) => mutate("PUT", path, body),
  del: (path: string) => mutate("DELETE", path),
  download,
  upload: (path: string, form: FormData) =>
    request("POST", path, undefined, { form }),

  /* ── 视频生成域 ── */
  episodeGenerate: videoApi.api_video.episodeGenerate,
  projectVideoCompletion: videoApi.api_video.projectVideoCompletion,
  shotGenerate: videoApi.api_video.shotGenerate,
  getReviewContext: videoApi.api_video.getReviewContext,

  /* ── 人物谱域 ── */
  sceneBiblePreview: bibleApi.api_bible.sceneBiblePreview,
  sceneBiblePrecheck: bibleApi.api_bible.sceneBiblePrecheck,
  genSceneBible: bibleApi.api_bible.genSceneBible,
  sceneRefsPrecheck: bibleApi.api_bible.sceneRefsPrecheck,
  sceneRefsGaps: bibleApi.api_bible.sceneRefsGaps,
  sceneRefsProgress: bibleApi.api_bible.sceneRefsProgress,
  genSceneRefs: bibleApi.api_bible.genSceneRefs,
  cancelSceneRefs: bibleApi.api_bible.cancelSceneRefs,
  editScenePrompt: bibleApi.api_bible.editScenePrompt,
  editSceneAnchor: bibleApi.api_bible.editSceneAnchor,
  regenerateCharacterView: bibleApi.api_bible.regenerateCharacterView,
  regenerateSceneView: bibleApi.api_bible.regenerateSceneView,
  adoptSceneCandidate: bibleApi.api_bible.adoptSceneCandidate,
  rollbackSceneReference: bibleApi.api_bible.rollbackSceneReference,
  bibleImpactPreview: bibleApi.api_bible.bibleImpactPreview,
  bibleVisualStyles: bibleApi.api_bible.bibleVisualStyles,
  setBibleStyle: bibleApi.api_bible.setBibleStyle,
  bibleGeneratePrecheck: bibleApi.api_bible.bibleGeneratePrecheck,
  refsPrecheck: bibleApi.api_bible.refsPrecheck,
  refsGaps: bibleApi.api_bible.refsGaps,
  refsProgress: bibleApi.api_bible.refsProgress,
  saveBibleDraft: bibleApi.api_bible.saveBibleDraft,
  getBibleDraft: bibleApi.api_bible.getBibleDraft,
  saveCharacter: bibleApi.api_bible.saveCharacter,
  listPortraitCandidates: bibleApi.api_bible.listPortraitCandidates,
  adoptPortraitCandidate: bibleApi.api_bible.adoptPortraitCandidate,
  rollbackPortraitCandidate: bibleApi.api_bible.rollbackPortraitCandidate,
  generateBible: bibleApi.generateBible,
  generateRefs: bibleApi.generateRefs,
  cancelBibleGeneration: bibleApi.cancelBibleGeneration,
  cancelRefsGeneration: bibleApi.cancelRefsGeneration,
  updateBible: bibleApi.updateBible,
  setCharacterPortraitPrompt: bibleApi.setCharacterPortraitPrompt,

  /* ── 映射台域 ── */
  screenplayPreflight: screenplayApi.screenplayPreflight,
  generateScreenplay: screenplayApi.generateScreenplay,
  resumeScreenplay: screenplayApi.resumeScreenplay,
  cancelScreenplay: screenplayApi.cancelScreenplay,
  deleteScreenplay: screenplayApi.deleteScreenplay,
  getScreenplayStatus: screenplayApi.getScreenplayStatus,

  /* ── 分镜台域 ── */
  getStoryboardStatus: storyboardApi.api_storyboard.getStoryboardStatus,
  storyboardPreflight: storyboardApi.api_storyboard.storyboardPreflight,
  startStoryboard: storyboardApi.api_storyboard.startStoryboard,
  confirmStoryboardPreview: storyboardApi.api_storyboard.confirmStoryboardPreview,
  confirmStoryboard: storyboardApi.api_storyboard.confirmStoryboard,
  previewClearStoryboard: storyboardApi.api_storyboard.previewClearStoryboard,
  clearStoryboard: storyboardApi.api_storyboard.clearStoryboard,
  reconcileProviderTasks: storyboardApi.api_storyboard.reconcileProviderTasks,
  cancelStoryboard: storyboardApi.api_storyboard.cancelStoryboard,
  setVideoModel: storyboardApi.api_storyboard.setVideoModel,
  getShotReview: storyboardApi.api_storyboard.getShotReview,

  /* ── 成片与交付域 ── */
  getMixStatus: deliveryApi.getMixStatus,
  getDeliveryReadiness: deliveryApi.getDeliveryReadiness,
  getDeliveryPackages: deliveryApi.getDeliveryPackages,
  approveDelivery: deliveryApi.approveDelivery,
  concatenateEpisode: deliveryApi.concatenateEpisode,
  createDeliveryPackage: deliveryApi.createDeliveryPackage,
  submitCustomerFeedback: deliveryApi.submitCustomerFeedback,
  downloadDeliveryFile: deliveryApi.downloadDeliveryFile,

  /* ── 项目/分集聚合域 ── */
  listProjects: projectsApi.listProjects,
  getProject: projectsApi.getProject,
  getEpisode: projectsApi.getEpisode,
  getChapter: projectsApi.getChapter,
  importProject: projectsApi.importProject,
  uploadNovelAttachment: projectsApi.uploadNovelAttachment,
  deleteProject: projectsApi.deleteProject,
  listDeletedProjects: projectsApi.listDeletedProjects,
  restoreProject: projectsApi.restoreProject,
  purgeProject: projectsApi.purgeProject,
  purgeAllDeletedProjects: projectsApi.purgeAllDeletedProjects,
  setStageTextModel: projectsApi.setStageTextModel,
  replanEpisodes: projectsApi.replanEpisodes,
  generateAllScreenplays: projectsApi.generateAllScreenplays,
  generateAllStoryboards: projectsApi.generateAllStoryboards,
  cancelAllScreenplays: projectsApi.cancelAllScreenplays,
  deleteEpisode: projectsApi.deleteEpisode,
  setEpisodeTargetDuration: projectsApi.setEpisodeTargetDuration,
  getStoryboardMetrics: projectsApi.getStoryboardMetrics,
  listEpisodesPage: projectsApi.listEpisodesPage,

  /* ── 观测台与系统管理域 ── */
  ...systemApi.api_system,

  /* ── 证据抽屉（跨域叶子） ── */
  getArtifactLineage,
};
