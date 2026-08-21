#!/usr/bin/env bash
# 启动 / 停止 / 查看 漫剧 Agent 2.0 前后端开发服务。
#
# 服务以独立会话（start_new_session）后台常驻：父进程/终端退出后由 init 接管，
# 不会随终端关闭或父进程被杀而退出——只有手动 `scripts/dev.sh stop` 或 kill 端口进程才会停。
#
#   后端  uvicorn  http://0.0.0.0:8230   （同时服务 frontend/dist 构建产物 + /api + /media，全程 gzip）
#   前端  vite     http://0.0.0.0:5230   （改代码用的热重载入口；/api、/media 反代到后端）
#
# 日常使用请走 :8230（构建产物，首屏与切标签比 dev 快一个数量级）；
# :5230 只在改前端代码需要热重载时用——dev 模式不压缩、按需现编译，公网访问必然慢。
#
#   MJ_BACKEND_HOST=127.0.0.1  收回后端为仅本机
#   MJ_FRONTEND_HOST=127.0.0.1 收回 vite 为仅本机
#   MJ_SKIP_BUILD=1            跳过构建产物的陈旧检查（不重新 npm run build）
#
# 用法：scripts/dev.sh [start|stop|status|restart|build]   （缺省 start）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_LOG="/tmp/manju2_backend.log"
FRONTEND_LOG="/tmp/manju2_frontend.log"

listeners() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true; }

stop_one() {
  local port="$1" name="$2" pids current_pgid pgid groups="" alive=""
  pids=$(listeners "$port")
  if [ -n "$pids" ]; then
    current_pgid=$(ps -o pgid= -p $$ | tr -d ' ')
    for pid in $pids; do
      pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
      if [ -n "$pgid" ] && [ "$pgid" != "$current_pgid" ]; then
        case " $groups " in *" $pgid "*) ;; *) groups="$groups $pgid" ;; esac
      fi
    done
    for pgid in $groups; do kill -TERM -- "-$pgid" 2>/dev/null || true; done
    for _ in {1..50}; do
      alive=""
      for pgid in $groups; do
        kill -0 -- "-$pgid" 2>/dev/null && alive="$alive $pgid"
      done
      [ -z "$alive" ] && break
      sleep 0.1
    done
    if [ -n "$alive" ]; then
      for pgid in $alive; do kill -KILL -- "-$pgid" 2>/dev/null || true; done
      echo "已强制结束 ${name} 的未退出请求（:${port}，pgid${alive}）"
    else
      echo "已停止 ${name}（:${port}，pgid${groups}）"
    fi
  else
    echo "${name}（:${port}）未在运行"
  fi
}

DIST="$ROOT/frontend/dist"

dist_stale() {
  [ -f "$DIST/index.html" ] || return 0
  # 任一前端源文件比产物新即视为陈旧（tsc -b 增量，无改动时本函数直接短路）
  [ -n "$(find "$ROOT/frontend/src" "$ROOT/frontend/index.html" "$ROOT/frontend/vite.config.ts" \
            -newer "$DIST/index.html" -print -quit 2>/dev/null)" ]
}

do_build() {
  echo "构建前端产物（tsc -b && vite build，约 30s）..."
  if (cd "$ROOT/frontend" && npm run build); then
    echo "构建完成：$DIST"
  else
    echo "[警告] 前端构建失败，:8230 将继续服务上一次的产物；请修掉 TS 错误后重跑 scripts/dev.sh build" >&2
    return 1
  fi
}

ensure_dist() {
  if [ -n "${MJ_SKIP_BUILD:-}" ]; then
    echo "已跳过构建检查（MJ_SKIP_BUILD）"
    return 0
  fi
  if dist_stale; then
    do_build || true
  else
    echo "构建产物已是最新，跳过构建"
  fi
}

do_stop() {
  stop_one 8230 后端
  stop_one 5230 前端
}

do_status() {
  local b f
  b=$(listeners 8230); f=$(listeners 5230)
  echo "后端 :8230 -> ${b:-（停）}"
  echo "前端 :5230 -> ${f:-（停）}"
}

do_start() {
  # 构建产物必须在后端起来之前就位：main.py 在导入期判断 dist 是否存在并挂载。
  ensure_dist
  # 仅释放本项目端口，绝不影响其它项目（如 :5173 的另一套前端）
  [ -n "$(listeners 8230)" ] && stop_one 8230 后端
  [ -n "$(listeners 5230)" ] && stop_one 5230 前端
  sleep 1
  # 用 Python 的 start_new_session 彻底脱离当前会话与进程组
  "$ROOT/.venv/bin/python" - "$ROOT" "$BACKEND_LOG" "$FRONTEND_LOG" <<'PY'
import os, subprocess, sys
root, be_log, fe_log = sys.argv[1], sys.argv[2], sys.argv[3]
dn = open(os.devnull, "rb")
be = open(be_log, "ab"); fe = open(fe_log, "ab")
# Codex Desktop/LaunchAgent 等图形进程的 PATH 通常不含 Homebrew。仅为后端补上
# 已实际安装 ffmpeg 的常见目录，避免机器已安装但 shutil.which("ffmpeg") 仍误判缺失。
be_env = os.environ.copy()
path_entries = [item for item in be_env.get("PATH", "").split(os.pathsep) if item]
media_bin_dirs = [
    item for item in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")
    if os.path.isfile(os.path.join(item, "ffmpeg")) and item not in path_entries
]
be_env["PATH"] = os.pathsep.join([*media_bin_dirs, *path_entries])
# 图形终端或自动化宿主可能注入仅对宿主进程有效的本地代理。后台服务默认直连，
# 避免代理退出后所有 provider 调用在构造 HTTP 客户端时直接失败。
inherit_proxy = be_env.get("MJ_BACKEND_INHERIT_PROXY", "").strip().lower() in {
    "1", "true", "yes", "on",
}
if not inherit_proxy:
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        be_env.pop(key, None)
backend_reload = be_env.get("MJ_BACKEND_RELOAD", "").strip().lower() in {
    "1", "true", "yes", "on",
}
# 后端自带会话闸门（X-Manju-Session + Origin 同源），与 vite 反代暴露的面一致；
# 绑 0.0.0.0 才能让外部直接访问构建产物，绕开 dev 服务器的未压缩按需编译。
backend_host = be_env.get("MJ_BACKEND_HOST", "0.0.0.0").strip() or "0.0.0.0"
if backend_reload:
    backend_args = [
        "./.venv/bin/uvicorn", "app.main:app",
        "--host", backend_host, "--port", "8230",
        "--reload", "--reload-dir", "app",
    ]
    backend_mode = "reload"
else:
    backend_args = [
        "./.venv/bin/uvicorn", "app.main:app",
        "--host", backend_host, "--port", "8230",
        "--timeout-graceful-shutdown", "30",
    ]
    backend_mode = "stable"
frontend_host = be_env.get("MJ_FRONTEND_HOST", "0.0.0.0").strip() or "0.0.0.0"
b = subprocess.Popen(
    backend_args,
    cwd=root, stdin=dn, stdout=be, stderr=be, env=be_env, start_new_session=True)
f = subprocess.Popen(
    ["npm", "run", "dev", "--", "--host", frontend_host],
    cwd=os.path.join(root, "frontend"), stdin=dn, stdout=fe, stderr=fe, start_new_session=True)
print(f"backend pid={b.pid}  frontend pid={f.pid}")
print("backend mode=" + backend_mode)
print("backend host=" + backend_host)
print("frontend host=" + frontend_host)
PY
  echo "已启动："
  echo "  日常使用（构建产物，快） http://<本机IP或域名>:8230"
  echo "  改前端代码（热重载，慢） http://<本机IP或域名>:5230"
  echo "日志：${BACKEND_LOG} / ${FRONTEND_LOG}"
  echo "停止：scripts/dev.sh stop"
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  status)  do_status ;;
  restart) do_stop; sleep 1; do_start ;;
  build)   do_build ;;
  *) echo "用法：scripts/dev.sh [start|stop|status|restart|build]"; exit 1 ;;
esac
