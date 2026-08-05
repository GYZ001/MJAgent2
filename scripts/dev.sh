#!/usr/bin/env bash
# 启动 / 停止 / 查看 漫剧 Agent 2.0 前后端开发服务。
#
# 服务以独立会话（start_new_session）后台常驻：父进程/终端退出后由 init 接管，
# 不会随终端关闭或父进程被杀而退出——只有手动 `scripts/dev.sh stop` 或 kill 端口进程才会停。
#
#   后端  uvicorn  http://127.0.0.1:8230  （默认稳定模式；MJ_BACKEND_RELOAD=1 开启热重载）
#   前端  vite     http://127.0.0.1:5230  （/api、/media 反代到后端）
#
# 用法：scripts/dev.sh [start|stop|status|restart]   （缺省 start）
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
backend_args = [
    "./.venv/bin/uvicorn", "app.main:app",
    "--host", "127.0.0.1", "--port", "8230",
    "--timeout-keep-alive", "1",
    "--timeout-graceful-shutdown", "3",
]
backend_reload = be_env.get("MJ_BACKEND_RELOAD", "").strip().lower() in {
    "1", "true", "yes", "on",
}
if backend_reload:
    backend_args.extend([
        "--reload", "--reload-dir", "app",
        "--reload-exclude", "__pycache__",
        "--reload-exclude", "*.pyc",
        "--reload-delay", "2",
    ])
b = subprocess.Popen(
    backend_args,
    cwd=root, stdin=dn, stdout=be, stderr=be, env=be_env, start_new_session=True)
f = subprocess.Popen(
    ["npm", "run", "dev", "--", "--host", "127.0.0.1"],
    cwd=os.path.join(root, "frontend"), stdin=dn, stdout=fe, stderr=fe, start_new_session=True)
print(f"backend pid={b.pid}  frontend pid={f.pid}")
print("backend mode=" + ("reload" if backend_reload else "stable"))
PY
  echo "已启动：后端 http://127.0.0.1:8230   前端 http://127.0.0.1:5230"
  echo "日志：${BACKEND_LOG} / ${FRONTEND_LOG}"
  echo "停止：scripts/dev.sh stop"
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  status)  do_status ;;
  restart) do_stop; sleep 1; do_start ;;
  *) echo "用法：scripts/dev.sh [start|stop|status|restart]"; exit 1 ;;
esac
