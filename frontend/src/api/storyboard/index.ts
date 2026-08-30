// 分镜台域的桶文件：镜头形状 / 视频版本与流水线状态 / 分镜包契约 / 分镜台任务状态与
// 各类预检响应 / 具名方法，五块子域各自成文件（各自远小于单文件行数上限），这里只做
// 重新导出与 `api.<method>` 对象组装，不放业务逻辑。
import * as methods from "./methods";

export * from "./shot";
export * from "./versions";
export * from "./pack";
export * from "./status";
export * from "./methods";

export const api_storyboard = {
  getStoryboardStatus: methods.getStoryboardStatus,
  storyboardPreflight: methods.storyboardPreflight,
  startStoryboard: methods.startStoryboard,
  confirmStoryboardPreview: methods.confirmStoryboardPreview,
  confirmStoryboard: methods.confirmStoryboard,
  previewClearStoryboard: methods.previewClearStoryboard,
  clearStoryboard: methods.clearStoryboard,
  reconcileProviderTasks: methods.reconcileProviderTasks,
  cancelStoryboard: methods.cancelStoryboard,
  setVideoModel: methods.setVideoModel,
  getShotReview: methods.getShotReview,
};
