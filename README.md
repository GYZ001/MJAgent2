# 漫剧 Agent 2.0

上传小说 → 生成分镜脚本（画面描述/旁白/台词）→ 调用 HiAgent 上的 Seedance 2.0 生成竖屏漫剧镜头视频。

本项目是 1.0（`~/Desktop/漫剧Agent`）的重新设计版。1.0 失败复盘与 2.0 全部设计见：

- **[PRD.md](PRD.md)** — 唯一主文档：失败复盘、目标与 DoD、功能需求、架构、里程碑
- [docs/HIAGENT_INTEGRATION.md](docs/HIAGENT_INTEGRATION.md) — HiAgent 网关集成规范（含 1.0 已验证的真实 API 形态与延迟数据）
- [docs/PROMPT_SPEC.md](docs/PROMPT_SPEC.md) — 提示词链、JSON Schema、校验与修复回路、金样回归

## 三条铁律（来自 1.0 的血泪）

1. **禁止 mock**：任何 provider 适配器必须打真实 API，第一周就要产出真实视频文件。
2. **禁止静默兜底**：模型调用失败 = 任务失败 = UI 红色可见 + 原始报文。
3. **贵的环节前人工把关**：分镜脚本人工确认后，才允许花钱调 Seedance（¥0.8/秒）。

## 下一步

按 PRD §7 执行 **M0 集成验证日**：用脚本直连 HiAgent 验证 Seedance 2.0 的真实 API 形态，
回填 [docs/HIAGENT_INTEGRATION.md](docs/HIAGENT_INTEGRATION.md) 中全部 ⚠️ 待验证项，然后开工 M1 垂直切片。
