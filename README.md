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

## 启动与重启

项目需要同时启动后端 FastAPI 和前端 Vite。以下命令都从项目根目录
`/Users/bytedance/Desktop/漫剧Agent2.0` 执行。

### 1. 启动后端

打开第一个终端：

```bash
cd /Users/bytedance/Desktop/漫剧Agent2.0
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8230 --reload
```

后端地址：`http://127.0.0.1:8230`

### 2. 启动前端

打开第二个终端：

```bash
cd /Users/bytedance/Desktop/漫剧Agent2.0/frontend
npm run dev
```

前端地址：`http://127.0.0.1:5230`

前端开发服务器已在 `frontend/vite.config.ts` 中固定为 `5230`，并把 `/api`、`/media`
代理到后端 `http://127.0.0.1:8230`。

### 3. 正常重启

分别在后端和前端终端按 `Ctrl+C` 停止进程，然后重新执行上面的启动命令。

后端重启时会自动执行数据库初始化、恢复视频/关键帧队列，以及恢复中断的人物谱任务。

### 4. 端口被占用时

如果启动时报 `Address already in use`，先查占用端口的进程：

```bash
lsof -nP -iTCP:8230 -sTCP:LISTEN
lsof -nP -iTCP:5230 -sTCP:LISTEN
```

确认是旧的本项目进程后，用查到的 `PID` 停掉它：

```bash
kill <PID>
```

然后重新启动后端和前端。

### 5. 构建检查

修改前端后可用下面命令确认能正常打包：

```bash
cd /Users/bytedance/Desktop/漫剧Agent2.0/frontend
npm run build
```

## 下一步

按 PRD §7 执行 **M0 集成验证日**：用脚本直连 HiAgent 验证 Seedance 2.0 的真实 API 形态，
回填 [docs/HIAGENT_INTEGRATION.md](docs/HIAGENT_INTEGRATION.md) 中全部 ⚠️ 待验证项，然后开工 M1 垂直切片。
