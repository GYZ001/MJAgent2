#!/usr/bin/env bash
# 每轮：有改动则自动提交 → fetch → 远程领先则 rebase → 本地领先则 push。
# 吃进远程提交后重启前后端。分叉冲突会 abort rebase，不强制覆盖。
#
# 用法：scripts/sync_remote.sh [--dry-run]
# 环境变量：
#   MJ_SYNC_REPO     仓库路径（默认本脚本上级目录）
#   MJ_SYNC_REMOTE   远程名（默认 origin）
#   MJ_SYNC_BRANCH   分支（默认当前分支，须为该远程的跟踪分支）
#   MJ_SYNC_LOG      日志文件（默认 <repo>/logs/sync-remote.log）
set -euo pipefail

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
elif [ -n "${1:-}" ]; then
  echo "用法：scripts/sync_remote.sh [--dry-run]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${MJ_SYNC_REPO:-$ROOT}"
REMOTE="${MJ_SYNC_REMOTE:-origin}"
LOG="${MJ_SYNC_LOG:-$REPO/logs/sync-remote.log}"
LOCK="${REPO}/logs/sync-remote.lock"

log() {
  local line
  line="$(printf '[%s] %s' "$(date '+%Y-%m-%d %H:%M:%S')" "$*")"
  printf '%s\n' "$line"
  mkdir -p "$(dirname "$LOG")"
  printf '%s\n' "$line" >> "$LOG"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN: $*"
    return 0
  fi
  "$@"
}

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")"
exec 9>"$LOCK"
if ! flock -n 9; then
  log "SKIP: 上一轮同步仍在运行"
  exit 0
fi

export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=Never
export GH_PROMPT_DISABLED=1

if ! cd "$REPO"; then
  log "ERROR: 无法进入仓库 $REPO"
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "ERROR: 不是 Git 仓库: $REPO"
  exit 1
fi

git_dir="$(git rev-parse --git-dir)"
for state_path in \
  "$git_dir/MERGE_HEAD" \
  "$git_dir/CHERRY_PICK_HEAD" \
  "$git_dir/REVERT_HEAD" \
  "$git_dir/BISECT_LOG" \
  "$git_dir/rebase-apply" \
  "$git_dir/rebase-merge" \
  "$git_dir/index.lock"; do
  if [ -e "$state_path" ]; then
    log "SKIP: Git 操作进行中: $state_path"
    exit 0
  fi
done

BRANCH="${MJ_SYNC_BRANCH:-$(git symbolic-ref --quiet --short HEAD || true)}"
if [ -z "$BRANCH" ]; then
  log "SKIP: 当前不在命名分支上"
  exit 0
fi

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  log "ERROR: 远程不存在: $REMOTE"
  exit 1
fi

auto_commit() {
  local dirty author_name author_email commit_message staged
  dirty="$(git status --porcelain)"
  if [ -z "$dirty" ]; then
    log "工作区干净，无需提交"
    return 0
  fi
  log "发现未提交改动，准备自动提交"
  if [ "$DRY_RUN" -eq 1 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && log "DRY-RUN: $line"
    done <<< "$dirty"
    return 0
  fi
  git add -A
  if git diff --cached --quiet --exit-code; then
    log "暂存后无有效改动（可能均被 ignore）"
    return 0
  fi
  staged="$(git diff --cached --name-only | wc -l | tr -d ' ')"
  author_name="$(git log -1 --format='%an' 2>/dev/null || true)"
  author_email="$(git log -1 --format='%ae' 2>/dev/null || true)"
  author_name="${author_name:-MJAgent2 Auto Commit}"
  author_email="${author_email:-mjagent2-auto@localhost}"
  commit_message="chore: auto commit $(date '+%Y-%m-%d %H:%M:%S')"
  git \
    -c user.name="$author_name" \
    -c user.email="$author_email" \
    commit -m "$commit_message"
  log "已提交 ${staged} 个文件: $commit_message"
}

read_ahead() {
  local counts
  counts="$(git rev-list --left-right --count "HEAD...$remote_ref")"
  local_ahead="${counts%%$'\t'*}"
  remote_ahead="${counts#*$'\t'}"
  local_head="$(git rev-parse --short HEAD)"
  remote_head="$(git rev-parse --short "$remote_ref")"
}

auto_commit

if ! git \
  -c http.lowSpeedLimit=1 \
  -c http.lowSpeedTime=60 \
  fetch --prune "$REMOTE" "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"; then
  log "ERROR: fetch 失败，下次重试"
  exit 1
fi

remote_ref="refs/remotes/$REMOTE/$BRANCH"
if ! git rev-parse --verify "$remote_ref" >/dev/null 2>&1; then
  log "ERROR: 远程分支不存在: $REMOTE/$BRANCH"
  exit 1
fi

read_ahead
log "状态: branch=$BRANCH local=$local_head ahead=$local_ahead remote=$remote_head behind=$remote_ahead dry_run=$DRY_RUN"

pulled_remote=0
if [ "$remote_ahead" -gt 0 ]; then
  log "远程领先 ${remote_ahead} 个提交，准备 rebase"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN: git rebase $remote_ref"
  elif git rebase "$remote_ref"; then
    pulled_remote=1
    log "rebase 完成"
  else
    git rebase --abort >/dev/null 2>&1 || true
    log "ERROR: rebase 冲突已中止，需人工处理"
    exit 1
  fi
  read_ahead
  log "rebase 后: local=$local_head ahead=$local_ahead remote=$remote_head behind=$remote_ahead"
fi

if [ "$local_ahead" -gt 0 ]; then
  log "本地领先 ${local_ahead} 个提交，准备 push"
  run git \
    -c http.lowSpeedLimit=1 \
    -c http.lowSpeedTime=60 \
    push "$REMOTE" "HEAD:refs/heads/$BRANCH"
  log "OK: 已推送到 $REMOTE/$BRANCH"
fi

if [ "$pulled_remote" -eq 1 ]; then
  log "准备重启前后端"
  run "$REPO/scripts/dev.sh" restart
  log "OK: 已吃进远程提交并重启前后端"
  exit 0
fi

if [ "$local_ahead" -eq 0 ]; then
  log "OK: 已与 $REMOTE/$BRANCH 同步，无需操作"
fi
exit 0
