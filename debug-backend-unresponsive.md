# Debug Session: backend-unresponsive
- **Status**: [OPEN]
- **Issue**: 后端进程仍存活但接口超时，确认是否被外部程序终止以及实际阻塞位置。
- **Debug Server**: http://127.0.0.1:7778/event
- **Log File**: `.dbg/trae-debug-log-backend-unresponsive.ndjson`

## Reproduction Steps
1. 启动 `scripts/backend_cycle.py --interval 900`。
2. 调用 `/api/episodes/ep_0893abc3451e/video-completion`。
3. 观察后端 CPU 升高，随后 `/api/system/health` 请求超时。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | worker 被外部 SIGTERM/SIGKILL | High | Low | Confirmed：11:34:27 supervisor 收到 SIGTERM(15) |
| B | 同步 CPU 任务阻塞事件循环 | High | Medium | Confirmed：CPU 约 94%-131% 时健康接口超时，CPU 回落后恢复 |
| C | 数据库锁或线程锁死锁 | Low | Medium | Inconclusive：未采到阻塞栈，且服务自行恢复 |
| D | supervisor 提前触发周期重启 | Low | Low | Rejected：仅运行 202 秒，未到 900 秒周期 |
| E | 第二个进程抢占 8230 | Low | Low | Rejected：切换前后均只有一个 worker 监听 |

## Log Evidence
- pre-fix: supervisor PID 72351、worker PID 72352 持续存活。
- pre-fix: worker 启动命令不含 `--reload`。
- pre-fix: `/video-completion` 返回后 worker CPU 持续升高，健康接口超时。
- NDJSON 第 5 行：11:34:27 supervisor 收到外部 SIGTERM(15)，旧进程正常退出。
- NDJSON 第 6 行：旧 supervisor 仅运行 202 秒，排除 900 秒周期触发。
- 新 supervisor PID 73590、worker PID 73594 于 11:34:28 立即接管。
- 同时存在 `[OPEN]` 的 `storyboard-limit-stop` Trae 调试会话，其测试和后端重启时间线吻合。

## Verification Conclusion
存在两个独立现象：
1. `/video-completion` 后发生过短时 CPU 饱和，导致接口超时但进程未退出。
2. 11:34:27 确实有外部程序向 supervisor 发送 SIGTERM；该动作不是 15 分钟周期任务，
   而是并行 Trae 调试会话在验证新代码后执行的受控后端重启。
