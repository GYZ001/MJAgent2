# 死代码具体位置清单

- 基线 commit：`639fe6f`
- 扫描日期：2026-08-30
- 说明：本清单只列**死代码的具体位置**（文件 + 符号 + 行号），供后续删除定位使用。位置为 `639fe6f` 实测；行号会随后续改动漂移，删除时以**符号名**为准。
- 判定：仅收录静态分析确认「生产不可达 / 仅测试保活 / 恒值无消费 / 重复定义被覆盖」的符号。仍被生产调用的符号一律不收。

---

## 执行状态（复核于 `70cecd4`，2026-08-30）

本清单第一节（后端 55 项）、第二节（前端全部）、第三/四节大部分，**已被 commit `52eba2b`「按 639fe6f 死代码清单删除后端/前端/脚本死代码」执行删除**，随后叠加了 exec 外观拆除（`1f7496b`）、bible_ops 等巨型模块拆包、MonitorPage/api.ts 重构（`654000a`）、软删除回收站（`70cecd4`）等。在 `70cecd4` 下用 5 个 subagent 按符号名逐项复核后：

- **一、后端 55 项**：全部已删除/已消除，无一残留、无一复活 → 整节完成。
- **二、前端全部**：已清除；保留的 `projectVideoCompletion`/`getReviewContext`/`DeliveryPackageRecord` 随 api.ts 拆包迁移到 `frontend/src/api/` 分包，仍现役。
- **三、脚本 12 项**：已删 9 项；`yyft_serial10.py`、`verify_episode_binding.py`（团队标注误判保留）、`backfill_character_aliases.py`（观察项）仍在。
- **四、历史事故测试**：已删。
- 遗留几处**注释/docstring 文字**（`run_screenplay_production` docstring、`compute_narrative_metrics` 注释、`VIDEO_POLL_BUDGET` 注释）不影响运行，属可选清理。

`70cecd4` 下**新发现的死代码候选**见文末【六、70cecd4 复核新增候选】。

---

## 一、后端 Python

### 1.1 重复定义被覆盖

| 符号 | 位置 | 说明 |
|---|---|---|
| `_SCENE_SHARD_UNIT_ORDINAL_RE` | `app/screenplay_scene_shards.py:923` | 首份定义在模块加载时被 `:969` 无条件覆盖 |
| `_scene_shard_canonicalize_review_unit_references` | `app/screenplay_scene_shards.py:926` | 首份定义被 `:972` 覆盖；唯一调用（:5367）指向后者 |

### 1.2 无任何调用（定义即死）

| 符号 | 位置 |
|---|---|
| `MessageOut` | `app/agent/schemas.py:28` |
| `StoryboardPatchShotInput` | `app/capabilities/inputs.py:179`（已被 `ShotUpdateInput` 替代） |
| `decide_approval` | `app/capabilities/policy.py:160` |
| `_identity_source_label_schema` | `app/portraits.py:3532` |
| `generic_functional_extra_role` | `app/character_policy.py:54` |
| `functional_extra_policy_text` | `app/character_policy.py:118` |
| `enrich_ref_dict_metadata` | `app/multiview.py:1901` |
| `is_plot_key_frame` | `app/multiview.py:1977` |
| `video_qa_sample_positions` | `app/multiview.py:2285`（质检下线后消费者消失） |
| `visual_style_names` | `app/visual_styles.py:57` |
| `_track_bible_task` | `app/domain/common.py:113` |
| `_sync_storyboard_shot_timing` | `app/domain/storyboard_ops.py:196` |
| `_uses_previous_tail_frame_for_model` | `app/domain/video_ops.py:23` |
| `_storyboard_residual_hint` | `app/domain/storyboard_ops.py:5429` |
| `_media_semaphore` | `app/hiagent.py:522` |
| `_model_route` | `app/hiagent.py:790` |
| `_model_setting` | `app/hiagent.py:810` |
| `_append_reference_notes_from_dicts` | `app/media_exec/enqueue.py:647` |
| `narrative_keyframe_slot_index` | `app/video_modes.py:619` |
| `_is_character_bearing` | `app/video_modes.py:3547` |
| `_is_character_bearing_ref` | `app/video_modes.py:3552` |
| `_suppress_character_anchors_covered_by_keyframes` | `app/video_modes.py:3643` |
| `_keyframe_identity_names` | `app/video_modes.py:3629`（仅被上一行的函数调用，随删） |
| `load_reference_set` | `app/media_pipeline/reference_store.py:208` |
| `episode_pipeline_summary` | `app/media_pipeline/status.py:479` |
| `is_stalled` | `app/video_supervisor.py:207` |
| `_amend_storyboard_duration` | `app/video_supervisor.py:2677`（勿与现役 `_amend_storyboard`:2536 混淆） |
| `_assert_storyboard_version` | `app/video_supervisor.py:3114` |

### 1.3 孤立互调对（互相调用，外部零引用）

| 符号 | 位置 | 说明 |
|---|---|---|
| `_reserve_video_resubmit` | `app/media_exec/run_job.py:1769` | 仅被 `_reserve_or_pause_video_resubmit` 调用 |
| `_reserve_or_pause_video_resubmit` | `app/media_exec/run_job.py:1781` | 全仓零调用 |

### 1.4 仅测试保活（生产不可达）

| 符号 | 位置 | 唯一调用方 |
|---|---|---|
| `_source_identity_contexts` | `app/portraits.py:925` | `tests/test_character_discovery.py` |
| `CHARACTER_SUBJECT_KINDS` | `app/portraits.py:9192` | 同上（断言） |
| `record_visual_entity_merge` | `app/db.py:2781` | `tests/test_db_visual_entity_migration.py` 等（生产用 `portraits.py:2486` 的 `_record_visual_entity_merge`） |
| `_plan_one_shot` | `app/domain/storyboard_ops.py:4228` | 经 `_ensure_shot_mode_plan` |
| `_ensure_shot_mode_plan` | `app/domain/storyboard_ops.py:4243` | `tests/test_review_wall_prd.py`、`tests/test_keyframe_chain.py` |
| `_storyboard_loop_exit_text` | `app/domain/storyboard_ops.py:5444` | `tests/test_agent_loop.py` |
| `run_screenplay_production` | `app/production/screenplay_repair.py:3514` | 仅测试 |
| `generate_screenplay` | `app/stages.py:12288` | 旧子树互调 + 测试 |
| `generate_screenplay_baseline` | `app/stages.py:12516` | 旧子树互调 + 测试 |
| `_persist_video_resubmit` | `app/media_exec/run_job.py:1813` | `tests/test_media_job_recovery.py` |
| `mark_provider_video_budget_claim` | `app/completion_grant.py:1732` | `tests/test_video_completion_authority.py`、`tests/test_media_scheduler.py` |

### 1.5 冷观众指标子树（生产固定关闭，仅互调/测试）

生产侧 `app/domain/storyboard_ops.py:4571` 固定 `narrative_metrics=None`；以下函数仅子树互调或测试引用：

| 符号 | 位置 |
|---|---|
| `validate_blind_review` | `app/narrative.py:3425` |
| `compute_narrative_metrics` | `app/narrative.py:3601` |
| `blind_ai_human_comprehension_correlation` | `app/narrative.py:3945` |
| `audience_perceptual_surface` | `app/narrative.py:4046` |
| `blind_reader_payload` | `app/narrative.py:4130` |

> 注：`narrative.py` 其余函数（`validate_screenplay_narrative` 等）现役，删除须按函数级切割，勿整模块删。

### 1.6 恒值空壳（返回恒定值 / 恒抛错，无调用）

| 符号 | 位置 | 行为 |
|---|---|---|
| `scene_generation_kinds` | `app/media_exec/enqueue.py:659` | 恒 `raise ValueError("关键帧功能已下线…")` |
| `shot_keyframes_ready` | `app/media_exec/enqueue.py:664` | 恒 `return False` |
| `reusable_previous_assets` | `app/video_modes.py:403` | 恒 `return []` |

### 1.7 纯墓碑（仅抛异常 / no-op，零调用）

| 符号 | 位置 |
|---|---|
| `refresh_video_grant_storyboard_artifact` | `app/completion_grant.py:2767`（`raise GrantValidationError`） |
| `merge_observed_state_out_into_shot_contract` | `app/evidence/media.py:200`（`raise ValueError`） |
| `preferred_level_for_code` | `app/repair_router.py:80` |
| `preferred_level_for_code` | `app/video_repair_router.py:46` |

### 1.8 无消费常量

| 符号 | 位置 |
|---|---|
| `VIDEO_POLL_BUDGET` | `app/config.py:325` |
| `EPISODE_TARGET_MAX_S` | `app/config.py:416` |
| `STORYBOARD_MAX_SHOTS` | `app/config.py:420` |
| `SHOT_HARD_MAX` | `app/renderability.py:12` |
| `SPINE_BEATS_MAX` | `app/renderability.py:15` |
| `DROP_LIST_MIN` | `app/renderability.py:16` |
| `KEY_PLOT_POINTS_MAX` | `app/renderability.py:83` |
| `SCENE_OUTLINE_MAX` | `app/renderability.py:91` |

### 1.9 未使用 import

| 符号 | 位置 |
|---|---|
| `generate_screenplay`（import 未使用） | `app/domain/common.py:42` |

---

## 二、前端 TypeScript

### 2.1 孤儿文件（无任何页面/生产引用）

| 文件 | 说明 |
|---|---|
| `frontend/src/components/AsyncButton.tsx` | 仅有 `export default`，全 src 无 import |
| `frontend/src/components/AutoChangeQueue.tsx` | 未被任何页面挂载 |
| `frontend/src/components/AutoChangeQueue.test.ts` | AutoChangeQueue 专属测试 |
| `frontend/src/pages/scriptReaderPagination.ts` | 仅被同名 `.test.ts` 引用 |
| `frontend/src/pages/scriptReaderPagination.test.ts` | 上者专属测试 |

### 2.2 局部死代码

| 符号 | 位置 | 说明 |
|---|---|---|
| `unsavedDraft` | `frontend/src/App.tsx:370` | 只写不读 |
| `routeRevision >= 0` | `frontend/src/App.tsx:774` | 恒真 |
| `updateEditing` | `frontend/src/pages/BiblePage.tsx:684` | 定义后无调用 |
| `legacy` mode | `frontend/src/pages/MonitorPage.tsx:29,1311,2982` | `App.tsx` 只挂载 `project`/`system` |

### 2.3 仅测试保活的导出

| 符号 | 位置 |
|---|---|
| `resolveRoutedEpisodeId` | `frontend/src/pages/episodePicker.ts:67` |
| `filterEpisodeOptions` | `frontend/src/pages/episodePicker.ts:75` |
| `scopesFor` | `frontend/src/auth/session.ts:85` |
| `isSystemAdmin`（函数，非 `AuthState` 属性） | `frontend/src/auth/session.ts:93` |
| `shotVideoState` | `frontend/src/shotStatus.ts:51` |
| `isStoryboardPackSegmentShot` | `frontend/src/pages/BoardPage.tsx:240` |

### 2.4 无调用的 api.ts 命名 wrapper

`frontend/src/api.ts` 中以下方法无任何生产/测试调用（对应端点走通用 `api.get/post/del`；**只删 wrapper，不动后端端点**）：

- 视频计划/集完成组（`:321-379`）：`createVideoGenerationPlan`、`getVideoGenerationPlan`、`validateVideoGenerationPlan`、`reconcileVideoGenerationPlan`、`overrideVideoGenerationPlan`、`executeVideoGenerationPlan`、`stopEpisodeVideo`、`resumeEpisodeVideo`、`episodeVideoCompletion`、`getVideoCompletion`、`resetVideoCompletion`（**保留 `projectVideoCompletion`**）
- 镜头/版本/清理组（`:397-450`）：`stopShotVideo`、`adoptVersion`、`cancelShotAdoption`、`deleteVersion`、`discardReferenceImage`、`restoreReferenceImage`、`clearEpisodeArtifacts`、`clearShotArtifacts`、`clearEpisodeVideos`、`clearShotReferences`、`clearShotVideos`、`archiveVersion`、`unarchiveVersion`（**保留 `getReviewContext`**）
- scene-review / stale-assets：`cancelSceneViewRegeneration`（:568）、`startSceneReview`（:578）、`listSceneReviews`（:587）、`getSceneReview`（:592）、`disposeSceneReviewItem`（:597）、`staleAssetsPreview`（:829）、`repairStaleAssets`（:859）
- 后端 auto-change 组件删除后连带：`listAutoChanges`（:806）、`decideAutoChange`（:810）

### 2.5 无引用类型

| 类型 | 位置 |
|---|---|
| `RunSummary` | `frontend/src/api.ts:1039` |
| `StepRun` | `frontend/src/api.ts:1056` |
| `RunEvent` | `frontend/src/api.ts:1071` |
| `PrepPackCharacterProvenance` | `frontend/src/api.ts:1319` |
| `DeliveryPackage` | `frontend/src/api.ts:2387`（**保留 `DeliveryPackageRecord`:2398**） |

### 2.6 旧 URL 重定向（无按钮入口）

| 符号 | 位置 |
|---|---|
| `LegacyMonitorRedirect` | `frontend/src/App.tsx:1267`（挂载 :1082，解析 :237-239） |

---

## 三、脚本（硬编码失效 ID / 已被替代）

以下脚本仍硬编码已失效的 `proj_3ac0b627fa46` / `data/manju.db`，或已有现役替代，删除/归档前请先确认无外部 cron/launchd 调用：

| 文件 |
|---|
| `scripts/yyft_clear_first5.sh` |
| `scripts/yyft_serial_monitor.sh` |
| `scripts/yyft_first5.py` |
| `scripts/auto_commit_push.sh` |
| `scripts/yyft_serial10.py` |
| `scripts/yyft_pipeline10.py` |
| `scripts/serial10_progress.py` |
| `scripts/scope_snapshot.py` |
| `scripts/verify_episode_binding.py` |

一次性迁移/事故工具（完成使命后归档）：`scripts/verify_identity_conflict_history.py`、`scripts/regression_diff.py`；`scripts/backfill_character_aliases.py` 仍留 `DEFAULT_PROJECT_ID`（:49）。

---

## 四、历史事故测试

| 文件 | 说明 |
|---|---|
| `tests/test_run_9063_environment_recompile.py` | 固定旧 `data/manju.db`（:24）、常规环境只能 skip（:44） |

---

## 五、不属于"死代码"、需单独决策（不在删除范围）

以下项**运行时仍在执行**，只是遗留/兼容性质，列此仅为避免误删：

- `app/domain/__init__.py`、`app/media_exec/__init__.py`、`app/api.py`、`app/worker.py` 的 `exec()` 聚合外观 —— 运行时活跃，且团队 `docs/coupling_review_2026-08-29.md` 将其列为 P0 正确性问题需重构，**不是可直接删的死代码**。
- `app/domain/screenplay_ops.py` 的旧 blueprint budget/grant（`_screenplay_blueprint_budget_projection`、`_abandon_orphaned_blueprint_receipts`）—— 仍在执行链上，保护历史核账。
- `app/capabilities/handlers/domain.py` 空 `HANDLER_MAP`、`app/agent/**` 对话后端 —— 恒空/后端仍可达，属产品决策。
- `plan_screenplay_patch`（`screenplay_repair.py:642`）、`scene_name_visual_constraints`（`scenes.py:151`）—— 仍被现行函数调用。

---

## 六、70cecd4 复核新增候选

在 `70cecd4`（清单已被 `52eba2b` 执行删除后）复核发现的、清单外的死代码，均已交叉验证。行号为 `70cecd4` 实测。

### 6.1 后端

| 符号 | 位置 | 证据 |
|---|---|---|
| `registered_names` | `app/db_schema.py:56` | 全仓仅此一处 `def`，无任何调用方（同名字样仅在 `production/prep_pack/contracts.py:471` 一句注释里指向已删的另一符号）。同模块 `register_table`/`get`/`run` 均现役，唯此函数零消费。属拆包新注册表模块里的孤立 helper。低风险，删前建议确认无预留自省用途。 |

### 6.2 前端

| 符号 | 位置 | 证据 |
|---|---|---|
| `routeRevision` | `frontend/src/App.tsx:358` | 清单 2.2 里的"`>= 0` 恒真判定"已随删除消失，但变量本体残留：`useState` 定义 + `setRouteRevision`(:494/:664) 只写，全 src 无读取。可能为强制重渲染而有意保留，删前需确认。 |
| `resolveEpisodeId` | `frontend/src/episodePicker.ts:22` | 仅 `episodePicker.test.ts` 引用，无生产调用。与清单 2.3 已删的 `resolveRoutedEpisodeId`/`filterEpisodeOptions` 同文件同模式，属漏收。 |
| `EpisodeOption`（类型） | `frontend/src/episodePicker.ts:3` | 同上，仅测试引用。同文件 `episodeProductionStatus`/`pickerWindowParams`/`resolveWindowedEpisodeId`/`EpisodeProductionFilter` 现役，勿误删。 |

### 6.3 脚本（清单外同类硬编码残留）

| 文件 | 证据 |
|---|---|
| `scripts/episode_source_audit.py` | 硬编码失效 `DEFAULT_PROJECT_ID = "proj_3ac0b627fa46"`(:105)、`data/manju.db`(:104)，`--project` 可覆盖(:1460)。形态与 `verify_episode_binding.py` 完全一致，清单未收录。 |

### 6.4 可选清理（注释/docstring 文字残留，不影响运行）

- `tests/test_published_screenplay_revalidation_eligibility.py:14`：docstring 提及已删的 `run_screenplay_production`。
- `app/domain/storyboard_ops/episode_detail.py:229`：注释提及已删的 `compute_narrative_metrics`。
- `app/media_exec/run_job.py:670`：注释提及已删的 `VIDEO_POLL_BUDGET`。

---

## 七、2026-09-01 复核：第三节「团队标注误判保留」/「观察项」脚本处置

复核第三节列出的 `scripts/yyft_serial10.py`（团队标注误判保留）、
`scripts/verify_episode_binding.py`（团队标注误判保留）、
`scripts/backfill_character_aliases.py`（观察项），以及本文档 6.3 已记录的
`scripts/episode_source_audit.py`，四者都硬编码失效的 `proj_3ac0b627fa46`
（或该项目下的 `ep_*` id）。本轮逐一核实并处置：

- **`scripts/yyft_serial10.py`：确认「团队标注误判保留」成立，不删。** 全仓 grep
  发现 `tests/test_yyft_serial10_failure_triage.py`、
  `tests/test_yyft_serial10_auto_cycle.py`、
  `tests/test_yyft_serial10_code_fingerprint.py`、
  `tests/test_yyft_serial10_retry_grant.py` 四个文件、合计 1504 行，直接
  `from scripts import yyft_serial10` 并调用其纯逻辑（`classify_failure_family`
  失败三族分诊、`cmd_run` 的重试阶梯/自动重启循环、`restart_backend` 的代码指纹
  自愈护栏、`is_retry_grant_category`），全部通过 `monkeypatch` 隔离，与
  `PROJECT_ID`/`EPISODES` 的具体取值无关——这与派单原假设「完全跑不起来、
  run_first10_videos.py 已现役替代、可整文件删除」矛盾：删除会打断这 1504 行
  测试（且这些测试不在本轮任何代理的文件所有权范围内），而 `run_first10_videos.py`
  也不具备 yyft_serial10.py 的失败分诊三族/自动重启+清库+重跑循环/
  GEN-RETRY-GRANT 识别这套协议，二者并非同类替代关系。真正的缺陷范围更窄：
  未使用的 `PROJECT_ID` 常量（已删，全仓零引用）、以及 `main()` 里没有
  `--project` 覆盖入口（已加：新增 `resolve_project_episodes()` 运行时按项目名/id
  解析 EP1-EP10，`main()` 用同模块 `global EPISODES` 重绑定整体替换模块级硬编码
  列表；`--project` 现为必填，缺省时打印中文用法说明并 `SystemExit(2)`）。四个
  测试文件全部通过 `cmd_run(SimpleNamespace(...))` 直接调用、不经过 `main()`，
  改动不影响它们。
- **`scripts/verify_episode_binding.py`：删除 `DEFAULT_PROJECT_ID`，`--project`
  改为必填**（缺省时打印中文用法说明 + `SystemExit(2)`）。`--episodes`/`--expect`
  的默认值（EP1/EP2/EP5/EP6、许清/李富贵）仍是历史项目的人设，已在 docstring
  标注换项目时需一并用 `--episodes`/`--expect` 覆盖，本轮不改这两个默认值本身
  （不在派单范围内，且它们本身不是「失效硬编码」，只是与失效项目绑定的示例值）。
- **`scripts/backfill_character_aliases.py`：删除 `DEFAULT_PROJECT_ID`，`--project`
  改为必填**，观察项状态解除。
- **`scripts/episode_source_audit.py`：同上处置**，`DEFAULT_PROJECT_ID` 已删、
  `--project` 改为必填。
- **`scripts/run_ep1_all_projects.py`：硬编码的四组
  `(项目名, project_id, episode_id)` 全部失效**（"王六郎"/"罗刹海市"/"黄英"
  在当前库里完全不存在，"我欲封天" 的 id 也已随重建改变），派单要求「若脚本
  其余逻辑依赖已删符号无法运行，则整文件删除」，但实测脚本主体逻辑（人物谱→
  定妆→场景库→场景图→映射台→分镜台→生成台七阶段驱动、限流分类重试、状态文件
  合并）不依赖任何已删的 app 符号，只是入口处的项目/分集列表写死——因此改为
  运行时按项目名/id 解析（新增 `resolve_projects()`），`run`/`status`/`report`
  三个子命令的 `--projects` 均改为必填（无默认值，避免再写死一份「当前四个
  项目」）。已用现役项目 `我欲封天`（`proj_f8cf2eeb2e66`）实测 `status` 子命令
  端到端跑通。
- **`scripts/yyft_serial10.py`（补充）与 `scripts/watch_bible_run.py`**：
  `watch_bible_run.py:11` 的 `DB_PATH = "data/manju.db"` 改为
  `from app.config import DB_PATH`，不再另起一份硬编码路径；已用现役项目实测
  `python scripts/watch_bible_run.py proj_f8cf2eeb2e66` 正常输出。

### 说明：本轮重构未发现新增死代码的范围

软删除/回收站（后端路由+handler+core+自动清理 loop+前端 Studio 回收站按钮全链路接线完整）、架构度量与三道闸门工具（`arch_graph.py`/`check_file_conventions.py` 均由 `verify.py --full` 可达）、四个 domain 真包拆分（`bible_ops`/`screenplay_ops`/`storyboard_ops`/`video_ops` 共 495 个 re-export 两轮 AST 扫描 0 孤儿）、费用确认弹窗前端下线（后端两段式报价确认端点仍在用，前端替代逻辑已接线）——均未产生新死代码。
