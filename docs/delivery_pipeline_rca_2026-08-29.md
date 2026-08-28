# 成片交付链路三处缺陷根因报告（2026-08-29）

> 背景：对「我欲封天」项目前 10 集做全链路视频生成压测（剧本→分镜→确认门→付费视频补齐→逐镜采纳→合成成片），成功率 4/10。在 6 集失败中，按「产品原因 vs 模型/供应商原因」分类后，锁定 **2 处产品侧代码缺陷**（EP3 合片时长膨胀、EP8 合法规划无法编译成有效计划）；此外用户反馈「成片台音画不同步」，经查与 EP3 **同源**。本报告把三处问题挖到最底层，给出实测证据链、影响面与修复方向。
>
> 本报告只做只读诊断（数据库一律 `mode=ro`，源片段只读、实验产物写临时目录），未改动任何生产数据、成片或数据库。

---

## 目录

- [问题一 / 问题三（同源）：draft 直粘不归一化音频 → EP3 时长膨胀 + 全体音画不同步](#问题一问题三同源draft-直粘不归一化音频--ep3-时长膨胀--全体音画不同步)
- [问题二：合法视频规划被拒且明细未落库 → EP8 卡 WAITING_AUTHORIZATION](#问题二合法视频规划被拒且明细未落库--ep8-卡-waiting_authorization)
- [失败集归因总表](#附录a10-集失败集归因总表)
- [复现命令](#附录b关键复现命令只读)

---

## 问题一/问题三（同源）：draft 直粘不归一化音频 → EP3 时长膨胀 + 全体音画不同步

### 结论先行

`app/media_exec/concat.py` 的 **draft 快速合成路径（`draft_concat`）用 `ffmpeg -f concat -c copy` 直粘片段，完全不对音频做重采样/时间戳归一**。当各镜源片段的**音频采样率不一致**时，concat demuxer 以首段的音频 timebase 解释所有后续片段的音频包，导致音频时钟被错误拉伸，产生两个可见症状：

1. **时长膨胀（EP3 现象）**：音频流时长被拉长，容器时长（取音视频最长者）随之虚高，被时长守门 `CON-409` 拦截；
2. **音画不同步（成片台现象）**：即使采样率一致、能通过时长门的成片（EP2/9/10），音频流仍系统性比视频流长数十毫秒，且逐镜累积。

**时长守门本身是对的**——它成功拦下了 EP3 的坏产物；缺陷在被守门的**合成算法**：draft 路径根本没有音频对齐逻辑。相邻的 `final_edit` 增强路径**做了**正确的音频归一（`aresample` + `asetpts=PTS-STARTPTS`），但它默认不触发（见下）。

### 证据链（EP3，只读复现）

**（1）源片段：视频流恒定、音频流不齐，且采样率混用**

| 镜 | 容器时长 | 视频流时长 | 音频流时长 | 音频采样率 |
|----|---------|-----------|-----------|-----------|
| 1  | 15.104s | 15.041667s | 15.104s | **32000Hz** |
| 2  | 15.093s | 15.041667s | 15.093s | 44100Hz |
| 3–11 | 15.069–15.093s | 15.041667s | 15.069–15.093s | 44100Hz |
| 12 | 15.104s | 15.041667s | 15.104s | **32000Hz** |
| 13 | 15.104s | 15.041667s | 15.104s | **32000Hz** |

- 13 个视频流**全部**是 15.041667s（24fps×361 帧），完全正常；
- 音频采样率**混用**：镜 1/12/13 为 32000Hz，其余为 44100Hz；
- 音频是**真实声音**（`volumedetect` mean_volume ≈ -18.9dB，非静音），系视频模型（Seedance）产出的原生音轨。

**（2）用 draft 路径的 `-c copy` 直粘复现（只读源 → 临时输出）**

```
concat -c copy 输出：
  容器时长  = 228.712625s   ← 与线上 CON-409 报错「实测 228.713s」完全一致
  视频流时长 = 196.059570s   ← 正确（≈13×15.04）
  音频流时长 = 228.712625s   ← 被拉伸 +32.6s，正是膨胀来源
```

对照：源片段各容器时长之和 = 196.122s（即线上「预期 196.122s」的来源，见 `concat.py` 用 `format=duration` 逐段累加）。**视频没问题，多出来的 32.6s 全部在音频流。**

**（3）定量归因：采样率错配拉伸**

concat demuxer `-c copy` 不重排时间戳，用**首段**（镜 1 = 32000Hz，`time_base=1/32000`）的音频 timebase 解释后续所有音频包。44100Hz 的 AAC 帧（1024 samples，真实时长 1024/44100≈0.02322s）被当作 1024/32000=0.032s 播放，音频时钟被系统性拉慢，倍率约 44100/32000≈1.378。这解释了「只有 EP3 爆炸、其余集正常」——**唯一区分变量就是采样率是否混用**。

**（4）对照：成功集的音画漂移是系统性的（问题三本体）**

| 集 | 合成模式 | 视频流 | 音频流 | 音频比视频长 |
|----|---------|--------|--------|-------------|
| EP2  | draft_concat | 75.366s | 75.417s | +0.051s |
| EP9  | draft_concat | 105.504s | 105.578s | +0.074s |
| EP10 | draft_concat | 150.759s | 150.833s | +0.075s |

这三集采样率恰好统一（44100Hz），没触发膨胀、通过了时长门并已交付；但音频流**仍比视频流长约 50–75ms**（单镜源片段音频就比视频长 ~30–60ms，逐镜累积）。draft 直粘既不裁齐每镜音视频，也不重置 PTS，**音画从第一镜就开始漂移，越往后越明显**——这正是成片台观感上的「音画不同步」。EP3 只是把同一个缺陷放大到时长门能拦下的量级。

### 根因定位（代码）

- draft 合成主逻辑：[concat.py:1150-1172](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/media_exec/concat.py#L1150-L1172)
  - 优先 `-c copy` 直粘：`concat_in + ["-c", "copy", ...]`，**音频原样复制、不归一**；
  - 失败才回退 `-c:v libx264` 重编码，但**仍只重编码视频，未对音频做 `aresample`/`asetpts`**。
- 变量命名 `silent_video`（[concat.py:1149](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/media_exec/concat.py#L1149)）暴露了**设计假设「模型视频无音轨」**，与实测「每段都带真实 AAC 音轨」不符——这是缺陷的认知根源。
- 预期时长来自各段容器 `format=duration` 之和（[concat.py:1096-1104](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/media_exec/concat.py#L1096-L1104)、[_probe_concat_media](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/media_exec/concat.py#L595-L637)），`format=duration` 取音视频流较长者，本身已被不齐的音频污染。
- 正确样板就在同文件相邻路径 `final_edit`：[final_edit.py:294-309](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/final_edit.py#L294-L309) 与 [final_edit.py:399](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/final_edit.py#L399) 对每段做 `aresample={FINAL_AUDIO_RATE}`（48000Hz）+ `asetpts=PTS-STARTPTS` 统一音频规格。但 `_final_edit_decision`（[concat.py:845-884](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/media_exec/concat.py#L845-L884)）在「简单时间线（无确定性文字、无增强转场）」时返回 `False`，前 10 集**全部**走了 `draft_concat`（`decision_reason=simple_timeline_fast_concat`），因此从未享受到音频归一。

### 影响面

- **所有走 draft 路径的成片**（当前几乎 100%）都存在音画不同步；采样率一旦混用则时长膨胀、直接交付失败。
- 采样率混用由上游视频模型产出决定，不可控；**下游合成必须自己归一**，不能假设输入规格一致。

### 修复方向（不做白名单/兜底，从根上对齐音视频）

1. **draft 路径统一音频规格**：拼接前对每段音频强制 `aresample=<统一采样率>`（对齐 `FINAL_AUDIO_RATE=48000`）并 `asetpts=PTS-STARTPTS`，消除采样率错配导致的时钟拉伸。
2. **每镜音视频裁齐**：以视频流时长为权威，对音频做 `apad`/`atrim` 对齐到同一时长，消除逐镜累积的 A/V 漂移（根治问题三）。
3. **预期时长以视频流为准**：`_probe_concat_media` 增加视频流时长探测，时长门用**视频流时长之和**作为预期基准，而非受音频污染的容器时长。
4. **纠正设计假设**：去掉 `silent_video` 的隐含前提，把音频作为一等公民纳入合成契约；draft 与 final_edit 两条路径共用同一套音频归一逻辑，避免只有增强路径正确。
5. 修复须对齐上下游数据流：合成输出的 `total_duration_s`/`edit_report.timeline` 与 `episode_mix_status` 的 `effective_duration_s` 口径需一致（当前 `effective_duration_s` 按 `duration_s/playback_rate` 估算，见 [concat.py:758](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/media_exec/concat.py#L758)）。

---

## 问题二：合法视频规划被拒且明细未落库 → EP8 卡 WAITING_AUTHORIZATION

### 结论先行

EP8 的视频补齐在启动 30s 内即落入 `WAITING_AUTHORIZATION`，run_events 只留下一条 `RUN_PARTIAL :: VIDEO_PLAN_INVALID`（payload 为空 `{}`）。深挖发现：

1. **规划模型侧没有问题**——LLM 返回了**完整合法的 JSON**（10 镜齐全、`finish_reason=stop`、未截断），程序还成功做了 4 次 mode 归一化；
2. **失败发生在模型产出之后的「计划编译/校验」环节**：`generate_episode_plan` 抛出 `VideoPlanValidationError`/`ValueError`，被 [video_supervisor.py:519-520](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_supervisor.py#L519-L520) 包装为 `GrantValidationError("VIDEO_PLAN_INVALID", str(exc))`；
3. **可观测性缺口**：处理该异常时（[video_supervisor.py:3304-3308](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_supervisor.py#L3304-L3308)）**只把 `exc.code` 写进 `cp.outcome`，`str(exc)` 中携带的完整 issues 明细被整段丢弃**，既没写 run_events 也没写 error_logs。结果是一个无法定位的孤立 `VIDEO_PLAN_INVALID`。

**这是产品侧缺陷**：合法的模型规划被产品逻辑拒绝，且拒绝的真实原因被吞掉，无法 RCA。

### 证据链

**（1）模型产出合法**——provider_call `id=65417`（run=`run_57275cf32078`）：

```
model=doubao-seed-2-0-pro-260215  http=200  finish_reason=stop
usage: prompt=9306 completion=1635  （max_tokens=9000，未截断）
返回 shots=10，shot_id 全部命中 DB 分镜，顺序一致
```

只读比对：EP8 数据库 10 个 shot 与 planner 输出的 10 个 shot_id **完全一致**（无 missing / 无 extra / 顺序一致）；每镜 `shot_contract_json` 齐全（3000–3300 字节）、`duration_s=15`。

**（2）落库结果为空**——`episode_video_generation_plans` 表中 EP8 **0 行**（对照成功集 EP10 有 `evp_...status=valid`）。说明 `generate_episode_plan` 在 `publish_plan` 之前就抛错返回。

**（3）事件链只剩一个空壳**——run=`run_57275cf32078` 的全部 run_events：

```
RUN_STARTED
VIDEO_SUPERVISOR_CHECKPOINT  phase=WAITING_AUTHORIZATION  coverage={C:10, total:10, fallback_quota:2}
RUN_CREATED
RUN_PARTIAL  [warning]  VIDEO_PLAN_INVALID   payload={}
```

该时间窗内 `error_logs` **无任何 plan 相关记录**——异常明细确实丢了。

**（4）非结构性、非确定性**——EP8 与成功集 EP10 的镜数（10）、投影时长（150s）、契约完整度**完全一致**，排除了「大纲授权时长漂移 / 镜数不符 / 契约缺失」这类稳定复现的结构问题。结合「该 run 仅 1 次 chat 且成功、无 error_log」，`VIDEO_PLAN_INVALID` 高度指向**运行时的瞬时因素**——最可能是资产解析 `resolve_shot_asset_dependencies` 在那一刻抛错（`ASSET_REVISION_RESOLUTION_FAILED` / `ASSET_REVISION_NOT_READY`，见 [video_plan.py:1702-1733](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_plan.py#L1702-L1733)），或计划时效校验竞态（`STORYBOARD_RELEASE_MANIFEST_STALE` / `SHOT_CONTRACT_FINGERPRINT_STALE`，见 [video_plan.py:1154-1176](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_plan.py#L1154-L1176)、[1434-1442](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_plan.py#L1434-L1442)）。**但正因为明细未落库，无法在事后确证是哪一个**——这本身就是首要待修的缺陷。

### 根因定位（代码）

- 异常包装点：[video_plan.py:513-520](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_plan.py#L513-L520) —— `except (ValueError, VideoPlanValidationError) as exc: raise GrantValidationError("VIDEO_PLAN_INVALID", str(exc))`。
- `VideoPlanValidationError` 本身**携带完整 issues**：[video_plan.py:468-470](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_plan.py#L468-L470) 把 `issues` JSON 作为 message；
- `GrantValidationError(code, message)` 也保留了 message：[completion_grant.py:2630-2633](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/completion_grant.py#L2630-L2633)；
- **信息在这里丢失**：[video_supervisor.py:3304-3308](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_supervisor.py#L3304-L3308) 只用 `cp.outcome = exc.code`，从未读取/记录 `str(exc)`；`RUN_PARTIAL` 事件 payload 为空，明细整段蒸发。

### 影响面

- 任何一次 `VIDEO_PLAN_INVALID`（无论根因是资产未就绪、契约漂移还是真正的计划非法）在线上都表现为**同一个无差别的 `WAITING_AUTHORIZATION`**，运维/研发无法区分「该重试」还是「该修数据」还是「该改逻辑」，直接拉低可诊断性与自动恢复能力。

### 修复方向

1. **补齐可观测性（最高优先、低风险）**：在 [video_supervisor.py:3304-3308](file:///Users/bytedance/Desktop/漫剧Agent2.0/app/video_supervisor.py#L3304-L3308)（及 3296、3324 等同类分支）落库时，把 `str(exc)`（含 `VideoPlanValidationError.issues` 全量）写入 `RUN_PARTIAL` 事件 payload 与 `error_logs`，消除黑盒。这符合项目「暴露真实问题、禁止静默兜底」原则。
2. **据落库明细二次定位**：拿到真实 issue code 后，再判断是「瞬时资产解析失败应触发重采样/重试」还是「确定性契约校验需修数据/逻辑」，对症修复；不预设白名单分类。
3. **区分终态语义**：瞬时可恢复失败（资产服务抖动）与真正需人工授权的失败（计划非法）不应共用 `WAITING_AUTHORIZATION`，避免把可自动恢复的场景误导到人工门。

---

## 附录A：10 集失败集归因总表

| 集 | 失败阶段 | 归因 | 关键证据 |
|----|---------|------|---------|
| EP1 | 视频 | 模型/供应商 | Seedance `video_poll` 返回 `TASK_FAILED: copyright restrictions` |
| EP3 | 成片 | **产品（问题一/三）** | 视频流 196.06s 正确，音频流被拉伸到 228.71s；采样率 32000/44100 混用 |
| EP4 | 视频 | 模型/供应商 | 同 EP1，`copyright restrictions` |
| EP5 | 剧本 | 模型 | 质量门 `StructuredSemanticError`：current functional 冒用登记身份称谓「老者」 |
| EP7 | 分镜 | 网络/供应商传输 | 流式 `ReadError`（latency 903578ms 后中断），fail-closed 禁自动重试 |
| EP8 | 视频 | **产品（问题二）** | 规划模型返回合法 10 镜（finish_reason=stop），计划落库 0 行、明细被吞 |

成功集：EP2、EP6、EP9、EP10（但 EP2/9/10 的成片均存在问题三所述的音画漂移，只是量级在时长门容差内）。

## 附录B：关键复现命令（只读）

**问题一/三 —— 逐流时长与采样率：**

```bash
for i in $(seq 1 13); do
  p="projects/proj_4c21fc3ce76a/episodes/3/shots/$i/v1.mp4"
  ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=nw=1:nk=1 "$p"   # 视频流
  ffprobe -v error -select_streams a:0 -show_entries stream=duration,sample_rate -of default=nw=1 "$p"  # 音频流+采样率
  ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$p"                      # 容器
done
```

**问题一/三 —— 复现膨胀（源只读，输出临时目录）：**

```bash
TD=$(mktemp -d); LIST="$TD/list.txt"
for i in $(seq 1 13); do echo "file '$(pwd)/projects/proj_4c21fc3ce76a/episodes/3/shots/$i/v1.mp4'" >> "$LIST"; done
ffmpeg -y -loglevel error -f concat -safe 0 -i "$LIST" -c copy "$TD/out.mp4"
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$TD/out.mp4"                 # 228.712625
ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=nw=1:nk=1 "$TD/out.mp4"  # 196.06
ffprobe -v error -select_streams a:0 -show_entries stream=duration -of default=nw=1:nk=1 "$TD/out.mp4"  # 228.71
```

**问题二 —— 事件链与落库结果（只读 DB）：**

```sql
-- run_events 只剩 RUN_PARTIAL/VIDEO_PLAN_INVALID，payload 为空
SELECT event_type,severity,message,payload_json FROM run_events WHERE run_id='run_57275cf32078' ORDER BY id;
-- 计划表 0 行（对照成功集 ep_abdda0c52af7 有 status=valid）
SELECT id,status FROM episode_video_generation_plans WHERE episode_id='ep_6a03165f7929';
-- 规划模型产出合法（provider_call 65417，finish_reason=stop，shots=10）
SELECT status,http_status,response_json FROM provider_calls WHERE id=65417;
```
