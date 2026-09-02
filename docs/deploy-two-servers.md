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
