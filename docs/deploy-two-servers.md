# 双机部署：入口机 A + 计算服务器 B

2026-09-02 落地。用户仍经 `https://automanju.com/`（及 `https://43.153.78.247/`）访问，
项目本体、后端、数据库、媒体与全部计算跑在 B；A 只做 TLS 终止、nginx 转发与隧道入口。

## 拓扑

```
用户 ──HTTPS──▶ A nginx(:443) ──▶ 127.0.0.1:18230 ══SSH 反向隧道══▶ B 127.0.0.1:8230 uvicorn
                                 A 127.0.0.1:2222  ══（同一条隧道）══▶ B :22   （A 运维 B 用）
```

- **隧道方向是 B → A**，不是 A → B。实测 A 主动连 `115.191.42.201:22` 全部超时（B 的
  安全组没放行 A），而 B 出站到 A:22 是通的。反向隧道只要求 B 能出站，B 不新增任何
  公网入站端口。
- A（43.153.78.247，腾讯云新加坡，Ubuntu 24.04，2 核）：nginx、lego 证书续期、sshd 的
  `mjtunnel` 账号、每日凌晨部署定时器、开发工作区（本机 :8230 只绑 127.0.0.1，纯调试）。
- B（115.191.42.201，火山引擎北京，CentOS 8，8 核 30G）：`/root/MJAgent2` 仓库、
  `.venv`（Python 3.11.15，uv 提供）、SQLite `data/manju.db`、`projects/` 媒体、
  静态 ffmpeg/ffprobe 7.0.2（`/opt/ffmpeg-static`）、CJK 字体、systemd 单元。
  B 没有 Docker 也没有 nginx；项目本身不用容器，原生 systemd 跑。

## 隧道映射

B 上 `mjagent2-tunnel.service`（autossh -M 0，`Restart=always`，`RestartSec=5`，
`ServerAliveInterval=30/CountMax=3`，`ExitOnForwardFailure=yes`）：

| A 侧监听（仅回环） | B 侧目标 | 用途 |
|---|---|---|
| `127.0.0.1:18230` | `127.0.0.1:8230` | nginx 上游（应用流量：HTTP/SSE/WebSocket） |
| `127.0.0.1:2222`  | `127.0.0.1:22`   | A 运维 B：`ssh mjb`（`~/.ssh/config` 别名） |

A 侧 sshd 对 `mjtunnel` 的限制（`/etc/ssh/sshd_config` 末尾 `Match User mjtunnel`，
`/var/lib/mjtunnel/.ssh/authorized_keys` 带 `restrict,port-forwarding,permitlisten=...`）：
只许远程转发到 18230/2222，无 shell/pty/agent/X11，密码登录关闭。要 shell 会被拒，
转发别的端口会被拒（已实测）。

## 配置位置

| 东西 | 位置 |
|---|---|
| nginx server 块 | A `/etc/nginx/sites-available/mjagent2` |
| nginx 上游（指向 18230） | A `/etc/nginx/conf.d/mjagent2-upstream.conf` |
| nginx 反代参数（Host 透传、Upgrade/Connection、超时） | A `/etc/nginx/snippets/mjagent2-proxy.conf` |
| WebSocket 升级 map | A `/etc/nginx/conf.d/mjagent2-websocket.conf` |
| 应用 location（/media 缓存、/events 关缓冲、隧道 502 页） | A `/etc/nginx/snippets/mjagent2-app.conf`，页面 `/var/www/html/mjagent2-upstream-down.html` |
| 隧道单元 | B `/etc/systemd/system/mjagent2-tunnel.service`（模板 `scripts/deploy/bootstrap_b.sh.tmpl`） |
| 隧道私钥 | B `/root/.ssh/mjagent2_tunnel_ed25519`；A 侧留档 `/root/mjagent2-deploy/b_tunnel_key` |
| 后端单元 | B `/etc/systemd/system/mjagent2-backend.service`（源 `scripts/deploy/mjagent2-backend.service`） |
| 备份定时器 | B `mjagent2-backup.timer`（每天 03:17 B 本地时间，`scripts/backup_manju_db.py`） |
| 每日部署定时器 | A `mjagent2-nightly-deploy.timer`（03:30 Asia/Shanghai，源 `scripts/deploy/`） |
| 部署日志 | A `logs/nightly-deploy.log`；B 后端日志 `logs/backend.log` |
| 网络调优 | 两台 `/etc/sysctl.d/99-mjagent2-net.conf`（BBR、`tcp_slow_start_after_idle=0`） |

## 发布代码到 B

- **手动**：A 上 `scripts/deploy_to_b.sh`——rsync 工作区（不含 data/projects/.env/logs）
  到 B，装依赖，重启 `mjagent2-backend`，经 18230 探活。在 A 上重启本机 :8230
  **不会**改变域名上的东西。
- **每日凌晨**：A 上 `scripts/deploy/nightly_deploy_to_b.sh`——`git fetch origin main`，
  rev 没变就退出；变了就把对象推到 B 的仓库并 `reset --hard`、在 A 上从干净导出
  `npm run build`、把 dist 推过去、装依赖、重启、探活；40s 不健康自动回滚到上一版。
  B 上 `git -C /root/MJAgent2 log -1` 与 `DEPLOYED_REV` 就是线上版本。
  为什么拉代码在 A：B 出站 github.com HTTPS 30s 超时，且 CentOS 8 的 yum 只有
  node 10/12/14，vite 5 要 node≥18。
- B 上的 `data/`、`projects/`、`.env` 是生产真源；A 的同名目录自切换起是过期副本。

## 恢复机制

- **B 重启**：`mjagent2-tunnel` 与 `mjagent2-backend` 都是 `enabled`，开机自起；隧道起来
  后 A 侧 18230 自动恢复。
- **A 重启**：nginx、sshd 自起；B 上的 autossh 每 5s 重试，A 一回来隧道即重建。
- **网络抖动**：ServerAlive 30s×3 判死，autossh 退出，systemd 5s 后重启；A 侧 sshd
  同样 30s×3 判死并释放 18230/2222，避免「端口被死会话占着」导致重连失败。
- **B 后端崩溃**：`Restart=always`，3s 拉起。
- 隧道断开期间域名返回中文 502 页（说明是隧道而不是站点没了）；故意不配 nginx `backup`
  回退到 A 本机——那是过期数据。

## 排障

1. 域名 502：A 上 `ss -tlnp | grep 18230`（没有 = 隧道断）→ B 上
   `systemctl status mjagent2-tunnel; journalctl -u mjagent2-tunnel -n 30`。
   18230 在但 502：B 上 `systemctl status mjagent2-backend; tail -50 logs/backend.log`。
2. A 上能否运维 B：`ssh mjb hostname`。不通 = 隧道断，只能到 B 的控制台/自己的入口处理。
3. 慢：A 上 `/var/log/nginx/mjagent2.diag.log` 的 `up_resp` 看后端（含隧道）耗时；
   A↔B 实测带宽约 1.5–2.4 MB/s，大文件下载受此限制。
4. 隧道被别的进程占了端口：A 上 `ss -tlnp | grep -E '18230|2222'` 找 PID；ExitOnForwardFailure
   会让 autossh 退出重试，直到端口空出来。
5. 换隧道密钥：A 上重新 `ssh-keygen` 到 `/root/mjagent2-deploy/b_tunnel_key`，更新
   `/var/lib/mjtunnel/.ssh/authorized_keys`，把私钥放到 B 的 `/root/.ssh/mjagent2_tunnel_ed25519`。
6. 重新引导 B（例如换机器）：A 上 `scripts/deploy/render_bootstrap.sh` 会生成带私钥的一次性
   脚本并打印 curl 命令；它需要 nginx 临时片段 `mjagent2-deploy-bootstrap.conf`
   （已删除，照 render 脚本注释临时加回，用完再删）。

## 2026-09-02 切换记录与验证结果

切换 08:20:55–08:22:47 PDT（23:20–23:22 北京时间）：停 A 后端 → 停 B 后端并清 WAL →
A 上 `sqlite3.backup` 一致快照 → 以 B 上旧副本为基准 delta 传输（97 万 KB 只发了 468KB）→
data/、projects/ 增量（0 文件差异）→ 代码再同步 → B 上 `integrity_check ok` → 起 B →
nginx 上游切到 18230 → A 后端以 `127.0.0.1` 重启。域名不可用窗口约 2 分钟。

走域名 `https://automanju.com` 的验证（`/root/mjagent2-deploy/verify_domain.sh`）：

| 项 | 结果 |
|---|---|
| 首页 + 主 bundle `index-CY2SzUdZ.js` | 200 / 200（与 A 上已发布产物一致） |
| 登录接口（错误密码） | 401 + 结构化错误体（不是 500） |
| 未登录 `/api/auth/me` | 401 |
| 带会话 `/api/system/jobs`、`/api/projects` | 200 |
| 媒体下载 `/media/...?mt=` | 200，`Cache-Control: private, max-age=31536000, immutable`；`Range: bytes=0-99` → 206 |
| 上传 multipart `/api/attachments/novel` | 200，返回 attachment_token |
| Agent 真实模型调用（B → HiAgent）+ SSE `/api/agent/turns/{id}/events` | 200 `text/event-stream`，ttfb 0.21s，23 个事件（thinking.delta×17、assistant.delta×3、turn.started/completed…）逐条到达 |
| WebSocket 握手透传（HTTP/1.1，Upgrade 头） | 直连 B 与走域名响应一致（uvicorn 对不存在的 WS 路由回 500），证明 nginx→隧道→B 原样透传；项目今天没有 WS 端点 |
| IP 入口 `https://43.153.78.247/` | 200 |
| B 侧确认 | `logs/backend.log` 记到这些请求；`mjagent2-backend`/`mjagent2-tunnel` 均 active |
| 编码/字体 | `ffmpeg -encoders` 有 libx264（实测编码成功）；`final_edit._font_path()` → wqy-zenhei |

**没做的**：真实登录（没有用户密码，只验了接口行为）；整集成片编码这类长任务没有跑，
只验证了 ffmpeg/字体前置条件与一次真实模型调用。

## 延迟：这套拓扑的固有代价

nginx `mjagent2.diag.log` 的 `up_resp`（后端耗时，含隧道）切换前后对比（同一批端点，200 响应）：

| 端点 | 切换前（A 本机） | 切换后（经隧道到 B，修 keep-alive 前） |
|---|---|---|
| `/api/projects/proj_*` | 38ms | 554ms |
| `/api/episodes/ep_*` | 69ms | 358ms |
| `/api/episodes/ep_*/screenplay/status` | 13ms | 684ms |
| API 总体中位 / p90 | 18 / 160ms | 498 / 979ms |

拆解：B↔A 裸 RTT 160ms（0 丢包）；B 本机处理 3–30ms；**经隧道复用连接每请求 172ms，
新建连接 700–1100ms**（开 SSH 通道 + B 侧 TCP 建连要多付 2–3 个跨境 RTT）。uvicorn 默认
5s 关空闲连接，页面轮询间隔一超过 5s，nginx 缓存的上游连接就全死了，每个请求都在付
新建连接的价。已改：B 上 `--timeout-keep-alive 330`，A 上上游 `keepalive_timeout 300s`
（nginx 先关，缓存里不会有已死连接）。改后走域名稳态 **~205ms/请求**（≈1 个 RTT + 处理）。

剩下的 ~160ms 是新加坡↔北京的物理往返，改不掉：用户在国内 → 新加坡 → 北京 → 新加坡 → 国内，
比原来多跨一次国际链路。要再降只能把入口挪到国内（B 开 443 或国内另起入口机，涉及备案），
这是用户拍板的事，不在本次范围。

## 2026-09-02 切换后事故：B 启动即整进程锁死（已修）

**现象**：用 `FORCE=1` 跑凌晨流水线重启 B（23:31 北京时间）后，B 一启动就打
`insert_error_log failed ... database is locked`，随后 worker 循环、watchdog、HTTP 全部卡死，
域名 502。域名临时切回 A 本机实例（23:35），B 停机排查。

**根因**（沙箱里用 B 的库复现，0.00s 必现）：`reconcile_stalled_video_jobs` →
`release_orphan_quarantined_versions` 只看「本镜没有 succeeded 版本」，漏了本镜已有
版本占着 `video_slot_active=1`（B 在线那 10 分钟里 worker 恢复了暂停的视频任务，为
`shot_1d91980603b8` 排了 v2）；放行 v1 的 UPDATE 撞 `uq_versions_active_video_shot`
抛 IntegrityError。真正致命的是第二层：抛错时任务局部的常驻连接上还开着写事务，调用方
记错误走独立连接（`timeout=0`）抢不到锁，事务永不回滚，整个进程所有写入者跟着死锁。

**修复**（commit `bea6e93`，A 主动重启 B 用它原来的冲突数据验证过，启动干净）：
放行判据改为与唯一索引同一谓词（有 succeeded 或占槽版本的镜不放行）；
`reconcile_stalled_video_jobs` 异常时 `conn.rollback()` 先于一切再抛。回归测试
`tests/test_quarantine_release_slot_guard.py` 修复前红（IntegrityError + 事务泄漏）、修复后绿。

**切回**：23:50 停 A → 以 A 的库为准再做一次 delta 同步（B 那 10 分钟的自动恢复产物作废）→
起 B → nginx 上游切回 18230 → 域名验证全绿 → A 以 127.0.0.1 重启。
这次经历也说明：**任何一次在 B 上的重启都是对启动恢复逻辑的真实测试**，凌晨流水线的
40s 健康检查 + 回滚是必要的。

## 备份：B 本地热备 + A 异地副本

- B：`mjagent2-backup.timer` 每天 03:17（B 本地时间）用 SQLite 在线备份 API 热备到
  `/var/backups/mjagent2/db/manju-<ts>.db.gz`（`scripts/backup_manju_db.py`：备份→
  integrity_check→gzip→原子换 `manju-latest.db.gz` 软链→按保留策略清理），日志
  `logs/backup_manju_db.log`。首次 2026-09-03 03:17 成功（928MB → 540MB，41s）。
- A：`mjagent2-backup-pull.timer` 每天 04:00 Asia/Shanghai 经隧道把 B 的备份镜像到
  `/var/backups/mjagent2/db-from-b/`（`scripts/deploy/pull_b_backups.sh`），只拉真实文件，
  最新一份 `gzip -t` 校验，本机保留最近 7 天 + 最新一份；日志 `logs/backup-pull.log`。
  B 整机丢失时从 A 这份恢复：`gunzip -c manju-<ts>.db.gz > data/manju.db`（已实测解压后
  `integrity_check ok`）。
- 恢复到 B 的顺序：停 `mjagent2-backend` → 删 `data/manju.db-wal/-shm` → 放入解压后的
  `manju.db` → 起服务（应用启动时自动切回 WAL）。

## 开发流程约定（迁移后）

- 改代码在 A：测试、ruff、`verify.py`、前端构建（node）都只在 A 上有；B 没有 node≥18。
- 让用户看到：`scripts/deploy_to_b.sh`；不动手也会在 03:30 由流水线按 origin/main 发布。
- **直接在 B 上热修必须当天在 A 提交推远端**，否则 03:30 的 `git reset --hard` 会抹掉。
- **回归/驱动脚本（`scripts/yyft_serial10.py` 等）只在 A 上跑**：它们硬编码
  `127.0.0.1:8230`，在 A 是调试实例，在 B 就是生产库。
- 排查线上问题到 B：`ssh mjb`，`logs/backend.log`、`journalctl -u mjagent2-backend`、
  `error_logs` 表（只读打开：`file:...?mode=ro`）。
