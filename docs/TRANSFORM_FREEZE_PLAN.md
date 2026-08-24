# 全面转型改造 · 冻结方案 v1（过目稿）

日期：2026-08-24。状态：**已实施**——用户批准后 P0 已全部落地（后端契约 6.0.0/episode_prep_pack 1.3.0、
前端准备包视图、新角色发现三分流、驱动与监控适配），经独立 Code Review 后提交。本文保留为
决策依据与范围冻结的历史记录；实施中的偏差与教训见最终验收报告。

## 0. 已拍板的四条决策（本方案的边界，不再重议）

| # | 决策 | 落点 |
|---|------|------|
| ① | 使用参考图方式，舍弃首尾帧 | 视频生成统一 `REFERENCE_IMAGE_MODE`（生成台轮次落地） |
| ② | 保留"禁止静默删戏"硬门禁 | 门禁保留在剧本台，形态从"剧本全文覆盖"改为"事件链覆盖"（见 §3） |
| ③ | 三道人工确认门取消 | 实际生效路径：旧剧本"编辑→预览→确认发布"回路随重型流水线整体休眠、前端三道确认 UI 移除、S20 video.complete_episode 审批 ALWAYS→NEVER。（注：`requires_human_gate` 字段经查全仓从无读取方，flip 它只是声明性动作，勿误以为该字段是生效开关；S30 delivery.review 属破坏性交付放行门，保留人工审批） |
| ④ | 开发期 HiAgent 主力，版权问题再切私有部署 | 默认路径/回归/验收全走 HiAgent；provider 抽象层保持可切换，切换由用户拍板 |

## 1. 目标架构（用户原话的工程化表述）

```
剧本台   = 轻量分集准备：分集映射 + 本集事件链(带原文证据) + 本集资源图片清单
分镜台   = 输出最终提示词脚本：事件 → 镜头 → 可直接发给供应商的 prompt + 参考图选择
生成台   = 只发送 prompt+参考图、轮询查看视频结果
```

## 2. 每条关键约束的实测依据（无"我猜"）

| 约束 | 依据 | 状态 |
|------|------|------|
| HiAgent 支持 15s 一次生成含多镜头硬切 | c2：15s+2 参考图 ok，359.2s，720×1280，3.76Mbps，切点 4.875/7.0/11.375 | ✅ 实测 |
| 参考图注入有效（2 张同时绑定 2 角色，服装发型体型一致） | c2 目视比对 | ✅ 实测 |
| 5s 单镜头指令可被遵守为 0 切点 | c4 负对照 | ✅ 实测 |
| 10s+1 参考图可行 | c3：205.7s | ✅ 实测 |
| 版权风控存在（无参考图 15s 被拒 copyright，316.8s 生成期拦截） | c1 单样本，因果未证实 | ⚠️ 已知风险，决策④已覆盖 |
| 私有部署可跑通但全面弱于 HiAgent | exp01-04：657s、1.21Mbps、切点挤末尾、疑似单 GPU 串行 | ✅ 实测（备胎） |
| 参考图上限 9 张 | 代码声称，**未实测** | ❌ 未验证 → 分镜台轮次前补测 |
| 15s 是否硬上限 | **未测试** | ❌ 未验证 → 按 ≤15s 设计 |
| 供应商不精确对齐时间码 | c2 切点 vs 指令偏差 | ✅ 实测 → 接缝数用区间估计，不做确定性承诺 |

## 3. 剧本台新产物：分集准备包（episode_prep_pack）

替代现在的重型 episode_screenplay（蓝图→场次分片→编译→56 项校验→修复回路）。

```
episode_prep_pack v1.0.0
├── episode_scope        # 章节映射（沿用 episode_mapping，确定性，无模型调用）
├── event_chain[]        # 本集事件链，每个事件：
│   ├── event_id / summary / order
│   ├── source_evidence[]     # 原文段落索引+引文 —— 硬门禁的锚点
│   └── key_lines[]           # 关键台词按原文顺序保留（保护台词不被静默删）
├── asset_manifest       # 本集资源图片清单（新结构，集级）：
│   ├── characters[]: {identity_id → portrait_id, 出场事件列表}
│   └── scenes[]:     {scene_id → scene_reference_id, 出场事件列表}
└── hook / cliffhanger   # 集级叙事属性，保留
```

**硬门禁（决策②）的新形态**：每个已索引原文段必须被 event_chain 覆盖
（delivered / merged / retained-as-context / 证明为重复），缺一个 → publish 阻断。
这是现有 screenplay 契约 invariant 的直接移植，校验逻辑复用 `app/validators.py`
的覆盖账本，只是校验对象从剧本全文换成事件链。

**明确从剧本台删除**（移交或废弃）：
- 全文对白改编、场次 entry/exit 状态机 → 职责移交分镜台（P1 轮次）
- spine_beat 补丁层、ending_hook 窗口接地、结构修复回路（只允许一次的那个）→ 废弃
- 场次分片系统（`screenplay_scene_shards.py`，6518 行）→ 本轮休眠不删，分镜台轮次确认无依赖后再删

## 4. 优先级分级

### P0（本轮实现：剧本台前后端）
1. **契约**：`screenplay` 契约 5.1.0 → **6.0.0**，invariants 重写为 prep_pack 的 5 条
   （覆盖门禁、事件有证据、asset_manifest 完整解析、hook 非空接地到事件、无模型调用的部分标明确定性）
2. **后端生成器**：新的轻量生成流程（识别本集出场角色/场景 → 事件链抽取 → 资产映射），
   复用 Run/Step/Artifact harness、复用 portraits 身份体系（9 定妆照/27 场景参考图已有）
3. **硬门禁移植**：覆盖账本从 validators 现有逻辑迁移，红灯测试必须先行（故意删一个事件 → publish 必须阻断）
4. **三道门取消**：`requires_human_gate=False` + 审批自动放行
5. **前端 ScriptPage.tsx**（1528 行）改造：事件链视图（带原文引证）、资产映射网格（角色→定妆照缩略图、场景→参考图缩略图）、门禁状态灯；删除人工确认 UI
6. **监控阈值重标定**：`serial10_progress.py` 的 `SUSPICIOUS_DISTINCT_OPS=25` 基于重型流程实测（83 op/集），轻量流程 op 数会合法地大幅下降，需用新 EP1 实测值重定，否则全是误报

### P1（下一轮：分镜台，本轮不动）
- storyboard 契约输入从 episode_screenplay 换成 episode_prep_pack
- 提示词脚本输出（含 15s 多镜头支持：`app/compiler.py:183-189` 时长夹取、
  `app/config.py` `VIDEO_DURATION_MAX_S=10`、`app/video_prompt_profiles.py:24-28` 单镜头规则整段改写）
- 参考图上限补测（≤9 张假设的实测确认；"9"实为 Seedance 数字，两家均未验证过，
  实测仅到 HiAgent 2 张 / H3 4 张，见 docs/PROVIDER_CAPABILITY_NOTES.md）
- **场景参考图绑定在 HiAgent 上补测**：私有部署实测环境绑定无效（n=2，环境与参考图完全
  不符），主力侧未测。分镜台提示词设计必须保证环境描述在 prompt 内完备，场景参考图按
  增强项对待，实测通过后才能升级为依赖项

### P2（生成台轮次，本轮不动）
- `apply_scene_boundary_strategy` 双分支收敛为 `REFERENCE_IMAGE_MODE` 单分支（决策①落地点）
- 生成台前端简化为"发送 + 轮询 + 预览"
- 成片拼接按"一次生成=多镜头段"重排

## 5. 冻结项

| 类别 | 冻结内容 |
|------|----------|
| 依赖 | 不引入新框架/大依赖；前端仍 Vite+React，后端仍 FastAPI+SQLite |
| 数据结构 | `episode_prep_pack` v1.0.0 如 §3；`narrative_plan` 保留为存在开关（90+ 处下游引用，本轮不动其读方） |
| 模块边界 | 剧本台=准备包生产+硬门禁；分镜台=提示词脚本；生成台=收发与查看。台间只通过 Artifact 传递 |
| 关键常量 | screenplay 契约 6.0.0；SCREENPLAY_ENVELOPE_VERSION 随之 bump；`VIDEO_DURATION_MAX_S`、`VideoGenerationMode`、prompt profile **本轮一律不动** |
| 供应商 | 文本 `d8p318cv256o70qpgv90`、视频 `d7jf6nd5boeaebtfbdqg`（HiAgent）；私有部署仅作备胎档案 |

## 6. 本次明确不实现

1. 分镜台/生成台的任何代码改动（P1/P2 轮次）
2. 15s 多镜头支持（属分镜台轮次）
3. 首尾帧模式的删除（属生成台轮次；本轮只是不再有新调用走它）
4. 旧重型剧本代码的物理删除（休眠保留，防误删下游依赖）
5. 供应商自动切换/A-B 路由（决策④说切换由用户拍板）
6. 参考图 9 张上限的突破尝试

## 7. 每步验证（按 CLAUDE.md）

| 步骤 | 验证方式 |
|------|----------|
| 契约+生成器 | `py scripts/verify.py` + 重启后端（无热加载）+ EP1 真实生成一遍，核对 artifact 结构 |
| 硬门禁 | **红灯先行**：构造缺失事件的 pack → publish 必须阻断且报出具体缺失段索引 |
| 资产映射 | EP1 manifest 逐条对照 `character_portraits`/`scene_references` 表，出场角色 100% 解析到 portrait_id |
| 前端 | vite build 零错误 + 手工走 EP1 核心路径 + 确认三道门 UI 已消失 |
| 收尾回归 | 清剧本模块数据，EP1→EP10 严格串行重跑（预计单集时长大幅下降），`serial10_progress.py` 用新阈值盯 |

## 8. 遗留衔接

- 私有部署 turbo_profile/队列并发探测仍在跑，结果归档 #23（备胎能力档案），不阻塞本方案
- #19 测试沙箱泄漏在开始大规模改造前先修（防止改造期间再次满盘）
- 后端有改动必须手工重启才生效（无热加载、无托管）
