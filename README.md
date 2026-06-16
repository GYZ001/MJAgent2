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

### 0. 推荐：一键常驻启动（不随终端关闭而退出）

```bash
scripts/dev.sh start      # 启动前后端
scripts/dev.sh status     # 查看是否在运行
scripts/dev.sh restart    # 重启
scripts/dev.sh stop       # 停止
```

`scripts/dev.sh` 用独立会话（`start_new_session`）把前后端拉起为后台常驻进程：父进程/终端退出后
由 `init` 接管，**不会随终端关闭、IDE 退出或父进程被杀而消失——只有手动 `scripts/dev.sh stop`
或 `kill` 对应端口进程才会停**。日志写到 `/tmp/manju2_backend.log`、`/tmp/manju2_frontend.log`。
脚本只清理本项目自己的 `8230 / 5230` 端口，不会动到其它项目（例如 `:5173` 的另一套前端）。

若想在前台跟实时日志、手动管理，仍可用下面的分终端启动方式。

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

一键方式：`scripts/dev.sh restart`。

或分别在后端和前端终端按 `Ctrl+C` 停止进程，然后重新执行上面的启动命令。

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

### 6. Windows 环境启动（PowerShell）

`scripts/dev.sh` 是 bash 脚本，Windows 下无法直接使用。以下为在 Windows + PowerShell 下以
**自动刷新模式**重启前后端的实测步骤与踩坑记录。

#### 6.1 停掉占用端口的旧进程

```powershell
$ports = 8230, 5230
foreach ($p in $ports) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) {
    try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop; "Killed pid $($c.OwningProcess) on port $p" }
    catch { "No process on port $p or kill failed" }
  }
}
```

> 不要用 `lsof`（macOS/Linux 专属），Windows 用 `Get-NetTCPConnection`。

#### 6.2 启动后端（uvicorn --reload）

```powershell
# uvicorn.exe 通常在用户级 Python 的 Scripts 目录下，按实际路径替换
& 'C:\Users\<用户名>\AppData\Local\Programs\Python\Python310\Scripts\uvicorn.exe' `
    app.main:app --host 127.0.0.1 --port 8230 --reload `
    --reload-dir app `
    --reload-exclude '__pycache__' --reload-exclude '*.pyc' `
    --reload-delay 2
```

后端地址：`http://127.0.0.1:8230`，改动 `app/` 下代码 → 自动热重载。

**踩坑（重要）**：

- **不要让 `--reload` 默认监听整个项目根目录**。uvicorn 默认用 `WatchFiles` 监听 cwd，
  会把 `tests/`、`__pycache__/`、`.pyc` 等都纳入监视。实测在 Windows 上：
  - `tests/test_validators.py` 被改动（或 pytest 运行时写入缓存）会触发 reload，
    但 reloader 在 Windows 下重启子进程时**偶发直接退出（exit code -1）而不拉起新进程**，
    导致后端静默挂掉、端口不再监听。
  - `__pycache__` 的 `.pyc` 在导入时生成，会引发**频繁误重载**。
- **必须**用 `--reload-dir app` 把监视范围缩到 `app/`，并加 `--reload-exclude '__pycache__'`
  与 `--reload-exclude '*.pyc'` 排除编译缓存。`--reload-delay 2` 进一步抑制抖动。
- 启动前可先清一遍 `app/` 下的 `__pycache__`：
  ```powershell
  Get-ChildItem -Path 'app' -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  ```

#### 6.3 启动前端（vite HMR）

```powershell
cd frontend
npx vite --host 127.0.0.1
```

前端地址：`http://127.0.0.1:5230/`，改动前端代码 → 浏览器 HMR 自动刷新。

> 注意：`npm run dev -- --host 127.0.0.1` 在某些 npm 版本下 `--host` 不会透传给 vite，
> 会回退到默认 `5173` 端口且不绑 host。直接用 `npx vite --host 127.0.0.1` 最稳，
> 端口由 `frontend/vite.config.ts` 固定为 `5230`，`/api`、`/media` 反代到后端 `:8230`。

#### 6.4 验证两端都在监听

```powershell
$ports = 8230, 5230
foreach ($p in $ports) {
  $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($c) { "Port $p -> LISTENING (pid $($c.OwningProcess -join ','))" }
  else     { "Port $p -> NOT listening" }
}
```

两端都应输出 `LISTENING`。后端日志若持续出现 `GET /api/... 200 OK` 即说明服务正常。

## 定妆照：按集反应式生成与漂移重绘（跨集一致性）

定妆照（角色一致性锚点）**不再做"每 20 集全量轮询"**，改为在**分镜阶段按集反应式**维护，
全部按需触发、不为还没出片的集提前花钱。核心代码见 [app/portraits.py](app/portraits.py)。

### 数据模型

定妆照按"适用集区间"分段存于 `character_portraits` 表：

- `ep_start` — 适用集左区间（含），即这一版定妆照从第几集起生效；
- `ep_end` — 右区间（含）；`NULL` 表示**开区间 = 当前最新版**，自然向后覆盖；
- `appearance` — 该段对应的外观锚点串；`image_path` — 落盘图；`base_portrait_id` — 图生图血缘。

> 上界不在生成时写死：一个造型能撑多少集由剧情决定，所以新段建出来就是开区间，只有被**下一版顶替**时
> 才把旧段 `ep_end` 回填为"新版起始 - 1"。这样按集读取永远有图兜底，不会出现"超出预设区间→查不到图"的空档。

### 两条产生路径（都在分镜展开前，`ensure_cards_for_screenplay`）

1. **新角色发现**：剧本里出现、人物谱里没有、且戏份够的角色 → 建卡 + 定妆，适用集从**首次出场那集**起开放。
2. **已有角色按集漂移**：剧本里出现、且本集之前就已有定妆照的角色 → 用**本集源文**一次性批量判定
   （`screen_appearance_changes`，每集一次模型调用、覆盖本集全部出场角色）外观是否相比**当前锚点**明显变化：
   - 变化不大 → 沿用当前定妆照（开区间自然覆盖），不重绘、不花钱；
   - 变化很大 → 关闭当前段右区间（= 本集 - 1），以当前定妆照为底**图生图**重绘新段（左区间 = 本集、右区间开放）。

因为外观变化一定写在它发生那一章的原文里，而每集映射到固定源章节，所以"逐集对本集源文判定"能在**变化发生的当集即时捕捉**，无滞后。判定/重绘失败都不阻断分镜。并发出片用 `(project, name)` 锁去重幂等。

### 按集渲染：图与文字锚点同段同源

关键帧/视频生成时，**同一集永远用同一套外观**：

- 参考图：`portrait_for_episode` 选覆盖该集的分段定妆照；
- 文字锚点：`bible_for_episode` 把 bible 换成"本集视图"（每个角色的 `appearance_canonical` / `ref_image_path`
  按覆盖该集的分段取）。

漂移重绘后会把 bible 里该角色锚点同步成最新版**仅供人物谱 UI 展示**；真正驱动按集渲染的是分段表 +
本集视图，所以即使回头重做早集也不会出现"早期图配晚期文字描述"的错位。

## 下一步

按 PRD §7 执行 **M0 集成验证日**：用脚本直连 HiAgent 验证 Seedance 2.0 的真实 API 形态，
回填 [docs/HIAGENT_INTEGRATION.md](docs/HIAGENT_INTEGRATION.md) 中全部 ⚠️ 待验证项，然后开工 M1 垂直切片。
