#!/usr/bin/env bash
# 把入口机 A（开发机）上的代码与已发布的前端产物推到计算服务器 B，并重启 B 上的后端。
#
# 走运维隧道：B 主动打到 A 的反向隧道把 B:22 映射到 A 的 127.0.0.1:2222，
# A 的 ~/.ssh/config 里叫 `mjb`。B 才是用户看到的那个后端——在 A 上重启本机 :8230
# 不会改变域名上的任何东西，改完后端代码要让用户看到就跑这个脚本。
#
# 不同步（B 上的是生产真源，别覆盖）：data/、projects/、.env、logs/；
# 也不同步 .venv、node_modules、.git、dist-staging、缓存目录。
#
# 用法：scripts/deploy_to_b.sh [--no-restart] [--dry-run]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
B="${MJ_B_SSH:-mjb}"
RESTART=1; DRY=""
for a in "$@"; do case "$a" in --no-restart) RESTART=0;; --dry-run) DRY="--dry-run";; *) echo "未知参数 $a" >&2; exit 1;; esac; done

ssh -o ConnectTimeout=10 "$B" true 2>/dev/null || { echo "连不上 $B（127.0.0.1:2222）——B 的反向隧道没起来。B 上看：systemctl status mjagent2-tunnel" >&2; exit 2; }

echo "== rsync 代码与前端产物 -> $B:/root/MJAgent2 =="
rsync -az --delete $DRY \
  --exclude '/data/' --exclude '/projects/' --exclude '/.env' --exclude '/logs/' \
  --exclude '/.venv/' --exclude 'node_modules/' --exclude '/.git/' \
  --exclude '/frontend/dist-staging/' --exclude '/frontend/dist.superseded-*/' \
  --exclude '__pycache__/' --exclude '/.pytest_cache/' --exclude '/.ruff_cache/' \
  --exclude '/.claude/' --exclude '/.uploads/' --exclude '/_*' \
  "$ROOT/" "$B:/root/MJAgent2/"
[ -n "$DRY" ] && { echo "dry-run 结束"; exit 0; }

echo "== 依赖对齐（requirements.txt）=="
ssh "$B" '.venv/bin/pip install -q -r requirements.txt'

if [ "$RESTART" = 1 ]; then
  echo "== 重启 B 后端 =="
  ssh "$B" 'systemctl restart mjagent2-backend && sleep 2 && systemctl is-active mjagent2-backend'
  for _ in $(seq 1 40); do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:18230/ || true)
    [ "$code" = 200 ] && { echo "B 后端经隧道可达：http://127.0.0.1:18230/ -> 200"; exit 0; }
    sleep 1
  done
  echo "B 后端 40s 内未就绪，看 B 上 logs/backend.log" >&2; exit 3
fi
