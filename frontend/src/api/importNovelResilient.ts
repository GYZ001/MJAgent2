// 小说导入的网络容错重放：POST /projects/import 在真实环境里实测会被浏览器
// 在请求发出后几十毫秒内断开连接（2026-10-08 生产实况：用户在 iPhone Safari
// 导入小说，后端照常把项目建好，前端却因为连接中断判成失败），而后端早已对
// 同一个 attachment_token 做了幂等（app/domain/projects/create.py::
// create_project_from_attachment 把 idempotency_key 定为
// f"novel-import:{attachment_token}"）。这里只封装一件事：用同一份请求体
// （同一个 attachment_token）重放 importProject，直到拿到带 project_id 的
// 终态结果，网络之外的业务错误原样抛出。
//
// 命令总线对同一幂等键的重放有两种形态（app/capabilities/idempotency.py::
// lookup + app/capabilities/dispatch.py::respond_ui/result_http_payload）：
//   - 上一次仍在执行中：lookup 命中 running 槽位，返回
//     status=ACCEPTED、error_code="idempotency_in_progress"；respond_ui 把它
//     当 202 返回，报文里没有 project_id——这里只要看「没有 project_id」就
//     知道还不是终态，不需要单独认 idempotency_in_progress 这个字段。
//   - 上一次已完成：lookup 命中缓存的终态结果（status=SUCCEEDED，data 里带
//     project_id/ingestion/...），respond_ui 原样把当年存的成功报文当 200
//     返回，和首次成功完全同形——两种「重放」里，只有这一种该被当作成功。
import { ApiError } from "./client";
import { importProject } from "./projects";

type ImportProjectBody = Parameters<typeof importProject>[0];
type ImportProjectResult = Awaited<ReturnType<typeof importProject>>;

// 重放退避间隔：2s / 4s / 8s，累计等待 14s，符合「总共不超过约 30 秒」的上限。
const RETRY_DELAYS_MS = [2000, 4000, 8000];

function wait(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** 网络类失败才值得用同一份请求体重放；附件凭证失效、参数不合法等业务错误
 *  重放也不会变成功，必须原样向上抛出。 */
function isRetryableNetworkFailure(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "BACKEND_UNAVAILABLE" || error.status === 0);
}

/**
 * 用同一份请求体调用 importProject；网络中断或命中「仍在执行中」的重放响应
 * 时按退避间隔重试，直到拿到带 project_id 的成功结果。重试用尽仍未拿到结果
 * 时，抛出一条说明「项目可能已经建好」的中文提示，而不是原样抛出最后一次
 * 的网络异常——此时用户该做的是去刷新项目列表，不是继续等一个已经确定拿不
 * 到响应的请求。
 */
export async function importNovelResilient(body: ImportProjectBody): Promise<ImportProjectResult> {
  const maxAttempts = RETRY_DELAYS_MS.length + 1;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    let result: ImportProjectResult | undefined;
    try {
      result = await importProject(body);
    } catch (error) {
      if (!isRetryableNetworkFailure(error)) throw error;
    }
    if (result?.project_id) return result;
    if (attempt < maxAttempts - 1) await wait(RETRY_DELAYS_MS[attempt]);
  }
  throw new Error(
    "网络连接中断，重试已用尽；项目可能已经建好，请先刷新项目列表查看。"
      + "如果列表里没有这个项目，再点一次「重试导入这份文件」"
      + "（同一份上传不会重复建项目）。",
  );
}
