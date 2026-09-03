// 系统观测/管理域的桶文件：任务队列 / 调用日志 / 系统设置 / 模型中心 / 总览与埋点 /
// 账号管理 / 链路追踪七块子域各自成文件，这里只做重新导出与
// `api.<method>` 对象组装，不放业务逻辑。这些类型此前全部本地重声明在
// MonitorPage.tsx（24 个）里，后端改字段时前端不会编译报错；收进这里之后
// 调用方一律 `import type { ... } from '../api'`。
import * as jobs from "./jobs";
import * as calls from "./calls";
import * as settings from "./settings";
import * as models from "./models";
import * as overview from "./overview";
import * as admin from "./admin";
import * as trace from "./trace";
import * as audit from "./audit";

export * from "./jobs";
export * from "./calls";
export * from "./settings";
export * from "./models";
export * from "./overview";
export * from "./admin";
export * from "./trace";
export * from "./audit";

export const api_system = {
  // jobs
  getJobsSummary: jobs.getJobsSummary,
  getJobsPage: jobs.getJobsPage,
  getJobDetail: jobs.getJobDetail,
  runProjectObservabilityJobAction: jobs.runProjectObservabilityJobAction,
  runRunAction: jobs.runRunAction,
  cancelJob: jobs.cancelJob,
  retrySystemJob: jobs.retrySystemJob,
  getZeroCostCandidate: jobs.getZeroCostCandidate,
  releaseZeroCostJobs: jobs.releaseZeroCostJobs,
  // calls
  getCallsPage: calls.getCallsPage,
  getCallDetail: calls.getCallDetail,
  // settings
  getSettings: settings.getSettings,
  updateSettings: settings.updateSettings,
  // models
  getHealth: models.getHealth,
  getModelCatalog: models.getModelCatalog,
  testModel: models.testModel,
  testNewModel: models.testNewModel,
  saveModelCredentials: models.saveModelCredentials,
  createModel: models.createModel,
  updateModel: models.updateModel,
  deleteModel: models.deleteModel,
  // overview / telemetry
  getSystemOverview: overview.getSystemOverview,
  reportMonitorEvent: overview.reportMonitorEvent,
  // account admin
  listUsers: admin.listUsers,
  listDeletedUsers: admin.listDeletedUsers,
  createUser: admin.createUser,
  updateUser: admin.updateUser,
  deleteUser: admin.deleteUser,
  restoreUser: admin.restoreUser,
  grantVideoAddon: admin.grantVideoAddon,
  // trace
  getTraceView: trace.getTraceView,
  getTraceNodeDetail: trace.getTraceNodeDetail,
  // audit
  listAuditEvents: audit.listAuditEvents,
  getAuditEvent: audit.getAuditEvent,
  getAuditFacets: audit.getAuditFacets,
};
