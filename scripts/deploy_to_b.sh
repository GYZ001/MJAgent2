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
# 本脚本 rsync 的是**工作区**，不是某个提交。工作区脏的时候跑它，会把别人没写完的
# 代码静默发到生产——2026-09-12 实测踩到边上：仓库里同时有另一拨未提交的在途改动
# （组织/角色 + 模型库，`app/orgs` 还没建完、全仓测试是红的），只差没人手滑跑这个
# 脚本。所以脏树默认拒绝执行，要覆盖得显式写 --allow-dirty。与前端发布闸门
# （scripts/publish_frontend.py 拦「后端启动时间早于最新 app/**/*.py」）同一族做法：
# 把一个人人顺手跑的动作带的副作用变响。
#
# 用法：scripts/deploy_to_b.sh [--no-restart] [--dry-run] [--allow-dirty]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
B="${MJ_B_SSH:-mjb}"
RESTART=1; DRY=""; ALLOW_DIRTY=0
for a in "$@"; do case "$a" in --no-restart) RESTART=0;; --dry-run) DRY="--dry-run";; --allow-dirty) ALLOW_DIRTY=1;; *) echo "未知参数 $a" >&2; exit 1;; esac; done

# 排在连通性检查之前：这一条不需要网络，而且先报「连不上 B」会把真正该看的问题挡住。
if [ "$ALLOW_DIRTY" != 1 ]; then
  DIRTY="$(git -C "$ROOT" status --porcelain)"
  if [ -n "$DIRTY" ]; then
    echo "工作区不干净，拒绝发布——本脚本同步的是工作区而不是某个提交，脏树会把未完成的改动一起推上生产：" >&2
    echo "$DIRTY" | head -30 >&2
    echo "先提交/移走这些改动；确认它们都该上线再加 --allow-dirty。只想发已提交的代码就等走 git 的定时部署（scripts/deploy/nightly_deploy_to_b.sh）。" >&2
    exit 4
  fi
fi

ssh -o ConnectTimeout=10 "$B" true 2>/dev/null || { echo "连不上 $B（127.0.0.1:2222）——B 的反向隧道没起来。B 上看：systemctl status mjagent2-tunnel" >&2; exit 2; }

echo "== rsync 代码与前端产物 -> $B:/root/MJAgent2 =="
rsync -az --delete $DRY \
  --exclude '/data/' --exclude '/projects/' --exclude '/.env' --exclude '/logs/' \
  --exclude '/.venv/' --exclude 'node_modules/' --exclude '/.git' \
  --exclude '/frontend/dist-staging/' --exclude '/frontend/dist.superseded-*/' \
  --exclude '__pycache__/' --exclude '/.pytest_cache/' --exclude '/.ruff_cache/' \
  --exclude '/.claude/' --exclude '/.uploads/' --exclude '/_*' \
  "$ROOT/" "$B:/root/MJAgent2/"
[ -n "$DRY" ] && { echo "dry-run 结束"; exit 0; }

echo "== 依赖对齐（requirements.txt）=="
ssh "$B" 'cd /root/MJAgent2 && .venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt'

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
