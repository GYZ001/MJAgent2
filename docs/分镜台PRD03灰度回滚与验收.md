# 分镜台 PRD-03 灰度、监控与回滚记录

## 发布开关

以下开关位于 `settings`，默认值写入 `app/config.py`，可在监制房即时切换：

| 开关 | 默认 | 关闭/开启后的行为 |
|---|---:|---|
| `storyboard_workspace_safe_readonly` | `false` | 紧急开启后保留浏览、筛选和导航，隐藏/禁用编辑与确认；P0 服务端防线继续生效。 |
| `storyboard_structure_edit_enabled` | `true` | 关闭后隐藏新增、复制、删除、移动入口，服务端同时拒绝结构写入；提示只承诺修改现有镜或继续 Agent 修复。 |
| `storyboard_source_rebind_enabled` | `true` | 关闭后现有证据继续只读展示，不提供原文重绑定；服务端不返回可框选正文。 |

开关回滚不得关闭以下服务端约束：编辑租约与基线哈希、旧运行写入栅栏、no-op 幂等、授权原文校验、确认完整终态及预览令牌。

## 灰度顺序

1. 预发开启全部能力，执行 `tests/test_storyboard_workspace_prd.py`、前端组件测试和 390×844 视觉/键盘抽检。
2. 生产先保持 `storyboard_workspace_safe_readonly=true` 验证状态快照，再关闭只读并开放编辑。
3. 原文重绑定与结构编辑按项目逐项开启；任一能力异常只关闭对应开关，不回滚 P0 数据表或服务端门禁。
4. 连续观察陈旧草稿、证据、确认、no-op 和预览过期指标后扩大范围。

## 仪表盘与告警口径

无正文指标写入 `provider_calls(kind='val422_metric')`，通过 `GET /api/system/val422-metrics` 聚合。仪表盘至少展示：

- `storyboard_stale_edit_blocked_total`：陈旧基线已被阻断；若审计发现对应发布成功，立即 P0 告警。
- `storyboard_source_evidence_rejected_total`：不可验证证据已被阻断；发布版缺少有效绑定立即 P0 告警。
- `storyboard_confirm_preview_total{passed=false}`：非完整终态/门禁失败已被阻断；对应确认成功立即 P0 告警。
- `storyboard_preview_rejected_total{reason}`：过期、已消费或状态漂移预览被拒绝。
- `storyboard_save_noop_total`：no-op 请求被安全短路；同请求若新增 Artifact 或失效媒体立即 P0 告警。
- `storyboard_save_result_total{validation,source}`：人工保存、口播冲突修复与草稿结果漏斗。

所有标签只含项目/分集/镜头稳定 ID、动作、结果和版本，不记录原文、剧本或台词正文。错误详情继续使用错误 ID 关联 `error_logs`。

## 回滚演练

1. 开启安全只读：确认页面仍能选集、选镜、筛选、切页签，编辑与确认不可提交。
2. 关闭结构编辑：确认入口消失，直接请求返回可执行的人话提示；现有发布版、草稿和媒体不变。
3. 关闭原文重绑定：确认历史证据仍可读，可框选正文不再下发；已绑定偏移与哈希不删除。
4. 恢复三个默认值并刷新快照：编辑租约只对刷新后新会话签发，旧预览令牌仍按原基线校验。

数据兼容采用新增表和向后兼容读取：历史合法 `source_excerpt` 仅在能精确定位本集连续原文时懒迁移；新字段不通过删列回滚，失败草稿与发布版始终隔离。

## 2026-07-27 技术验收记录

### PRD 能力对照

| 需求组 | 结论 | 主要证据 |
|---|---|---|
| SB-FR-01～05（P0 状态、并发、原文、确认） | 通过 | 单调快照/唯一动作、运行态编辑租约、基线哈希、授权章节偏移绑定、确认预览与完整终态均由服务端强制；自动确认遇人工 warning 不再伪报生成失败。 |
| SB-FR-06～13（P1 编辑、结构、预览、恢复） | 通过 | 结构化 diff、口播即时计数、台词/时间轴单一语义、角色选择、新增/复制/删除/移动影响预览、no-op、失败草稿、人话错误与真实恢复动作已覆盖。 |
| SB-FR-14～17（P2 导航、窄屏、无障碍、交接） | 通过 | 问题镜筛选、表单输入防劫持、当前镜/问题数可访问名、确认后评审墙入口、移动导航可访问性收口已验证。 |
| SB-AT-01～21 | 通过 | `tests/test_storyboard_workspace_prd.py`、Supervisor/VAL-422/恢复/数据迁移集成测试与 `frontend/src/pages/BoardPage.test.ts` 等组件契约测试。 |

### 发布级校验

- `scripts/verify.py --full`：通过。Ruff、契约面、Capability 覆盖、Python 编译全部通过。
- 后端：`723 passed, 1 skipped`；唯一 warning 是 Starlette TestClient 的上游弃用提示，无业务失败。
- 前端：`12` 个测试文件、`52 passed`；TypeScript 与 Vite 生产构建通过。
- 390×844 真机视口抽检：`documentScrollWidth=375 < innerWidth=390`，无页面横溢；顶部集选、确认态主动作、筛选、镜头轨道和删除/修改入口可达。
- 键盘/读屏抽检：方向键可从镜 01 切到镜 02；隐藏的移动侧栏不再出现在可访问树，打开后才暴露导航控件；表单、轨道、删除和评审墙入口具有唯一可读名称。
- 真实业务验收：项目 `proj_03f88c44dc31` 第一集完成 `15/15` 镜、`96s`、`0` 问题镜、最终镜有效、15/15 原文证据绑定，状态为 `confirmed`，Supervisor 为 `SUCCEEDED_CONFIRMED`，新浏览器会话控制台 warning/error 均为 0。
- 删除镜头闭环实测：打开镜 01 删除预览，服务端正确给出 `15 → 14`、镜号重排、相邻重验、最终镜唯一与下游失效数；抽检后取消，未改动已确认版。

技术自检结论：PRD-03 的 P0/P1/P2 功能、数据边界、恢复语义、可观测性、灰度/回滚和真实分镜业务闭环均已通过，无 P0/P1 豁免项。
