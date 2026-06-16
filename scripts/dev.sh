#!/usr/bin/env bash
# 启动 / 停止 / 查看 漫剧 Agent 2.0 前后端开发服务。
#
# 服务以独立会话（start_new_session）后台常驻：父进程/终端退出后由 init 接管，
# 不会随终端关闭或父进程被杀而退出——只有手动 `scripts/dev.sh stop` 或 kill 端口进程才会停。
#
#   后端  uvicorn  http://127.0.0.1:8230  （--reload，改后端代码自动热重载）
#   前端  vite     http://127.0.0.1:5230  （/api、/media 反代到后端）
#
# 用法：scripts/dev.sh [start|stop|status|restart]   （缺省 start）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_LOG="/tmp/manju2_backend.log"
FRONTEND_LOG="/tmp/manju2_frontend.log"

listeners() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true; }

stop_one() {
  local port="$1" name="$2" pids
  pids=$(listeners "$port")
  if [ -n "$pids" ]; then
    kill $pids 2>/dev/null || true
    echo "已停止 ${name}（:${port}，pid ${pids//$'\n'/ }）"
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
  for port in 8230 5230; do
    pids=$(listeners "$port"); [ -n "$pids" ] && { kill $pids 2>/dev/null || true; }
  done
  sleep 1
  # 用 Python 的 start_new_session 彻底脱离当前会话与进程组
  "$ROOT/.venv/bin/python" - "$ROOT" "$BACKEND_LOG" "$FRONTEND_LOG" <<'PY'
import os, subprocess, sys
root, be_log, fe_log = sys.argv[1], sys.argv[2], sys.argv[3]
dn = open(os.devnull, "rb")
be = open(be_log, "ab"); fe = open(fe_log, "ab")
b = subprocess.Popen(
    ["./.venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8230", "--reload"],
    cwd=root, stdin=dn, stdout=be, stderr=be, start_new_session=True)
f = subprocess.Popen(
    ["npm", "run", "dev", "--", "--host", "127.0.0.1"],
    cwd=os.path.join(root, "frontend"), stdin=dn, stdout=fe, stderr=fe, start_new_session=True)
print(f"backend pid={b.pid}  frontend pid={f.pid}")
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
