#!/usr/bin/env bash
# 每天凌晨在入口机 A 上跑：从 GitHub 拉 origin/main，与上次部署的 rev 不同才动手——
# 把提交对象推到计算服务器 B 的仓库并 reset 到该 rev、在 A 上从干净导出构建前端产物
# 推过去、装依赖、重启 B 的后端；健康检查不过就回滚到上一版。
#
# 为什么拉代码这一步在 A 而不是 B：B 出站到 github.com 的 HTTPS 不通（实测 30s 超时），
# 且 B（CentOS 8）的 yum 只有 node 10/12/14，vite 5 要 node>=18；A 两样都齐。
# B 上 `git -C /root/MJAgent2 log -1` 永远是当前线上的提交，排障先看它。
#
# 安装：cp scripts/deploy/mjagent2-nightly-deploy.{service,timer} /etc/systemd/system/
#       systemctl daemon-reload && systemctl enable --now mjagent2-nightly-deploy.timer
# 手动：FORCE=1 scripts/deploy/nightly_deploy_to_b.sh   （rev 没变也强制发一次）
set -euo pipefail
ROOT=/root/MJAgent2
D=/root/mjagent2-deploy
B="${MJ_B_SSH:-mjb}"
LOG="$ROOT/logs/nightly-deploy.log"
MARK="$D/last_deployed_rev"
REL="$D/releases"
mkdir -p "$REL" "$ROOT/logs"
exec 9>"$D/deploy.lock"
flock -n 9 || { echo "另一个部署正在进行，跳过"; exit 0; }
log() { echo "[$(date '+%F %T %Z')] $*" | tee -a "$LOG"; }

git -C "$ROOT" fetch -q origin main
NEW="$(git -C "$ROOT" rev-parse origin/main)"
OLD="$(cat "$MARK" 2>/dev/null || echo none)"
if [ "$NEW" = "$OLD" ] && [ "${FORCE:-0}" != 1 ]; then
  log "origin/main 无变化（$NEW），不动"
  exit 0
fi
log "开始部署 $OLD -> $NEW"
ssh -o ConnectTimeout=15 "$B" true 2>/dev/null || { log "连不上 $B（B 的反向隧道没起来），放弃"; exit 2; }

# 1) 前端产物：从干净导出构建（复用 A 的 node_modules），产物存 releases/<rev>/dist 供回滚
build_dist() {
  local rev="$1" work; work="$(mktemp -d /tmp/mj-release.XXXXXX)"
  git -C "$ROOT" archive --format=tar "$rev" | tar -x -C "$work"
  ln -s "$ROOT/frontend/node_modules" "$work/frontend/node_modules"
  if ! (cd "$work/frontend" && npm run build --silent) >>"$LOG" 2>&1; then rm -rf "$work"; return 1; fi
  [ -f "$work/frontend/dist-staging/index.html" ] || { rm -rf "$work"; return 1; }
  rm -rf "$REL/$rev"; mkdir -p "$REL/$rev"
  mv "$work/frontend/dist-staging" "$REL/$rev/dist"
  rm -rf "$work"
}
# 2) 把某个 rev 落到 B：推对象 → reset --hard → 同步 dist → 装依赖 → 重启
push_rev() {
  local rev="$1"
  git -C "$ROOT" push -q "$B:/root/MJAgent2" "$rev:refs/remotes/a/main"
  ssh "$B" "cd /root/MJAgent2 && git reset -q --hard $rev && git clean -qfd && echo $rev > DEPLOYED_REV"
  rsync -az --delete "$REL/$rev/dist/" "$B:/root/MJAgent2/frontend/dist/"
  ssh "$B" 'cd /root/MJAgent2 && .venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt && systemctl restart mjagent2-backend'
}
healthy() {
  local i code
  for i in $(seq 1 40); do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:18230/ || true)"
    [ "$code" = 200 ] && return 0
    sleep 1
  done
  return 1
}

build_dist "$NEW" || { log "前端构建失败（rev $NEW），本次不部署，B 保持 $OLD"; exit 1; }
push_rev "$NEW"
if healthy; then
  echo "$NEW" > "$MARK"
  log "部署完成 $NEW，B 后端经隧道健康（http://127.0.0.1:18230/ -> 200）"
  ls -1dt "$REL"/*/ 2>/dev/null | tail -n +4 | xargs -r rm -rf
  exit 0
fi
log "部署 $NEW 后 40s 内健康检查未通过"
if [ "$OLD" != none ] && [ -d "$REL/$OLD/dist" ]; then
  push_rev "$OLD" && healthy && { log "已回滚到 $OLD"; exit 3; }
  log "回滚到 $OLD 也失败——需要人工介入：ssh $B 'systemctl status mjagent2-backend; tail -50 /root/MJAgent2/logs/backend.log'"
fi
exit 3
