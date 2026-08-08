#!/usr/bin/env bash

set -u
umask 077

REPO="/Users/lnuyasha/Desktop/MJAgent2"
REMOTE="origin"
BRANCH="main"
LOCK_DIR="${HOME}/Library/Caches/MJAgent2/auto-commit-push.lock"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

mkdir -p "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
  case "$lock_pid" in
    ""|*[!0-9]*)
      lock_pid=""
      ;;
  esac

  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    log "SKIP: another auto-push run is active (PID $lock_pid)"
    exit 0
  fi

  rm -rf "$LOCK_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: could not acquire auto-push lock"
    exit 0
  fi
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT HUP INT TERM

if ! cd "$REPO"; then
  log "ERROR: repository is unavailable: $REPO"
  exit 1
fi

git_dir=$(git rev-parse --git-dir 2>/dev/null || true)
if [ -z "$git_dir" ]; then
  log "ERROR: not a Git repository: $REPO"
  exit 1
fi

current_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
if [ "$current_branch" != "$BRANCH" ]; then
  log "SKIP: current branch is '${current_branch:-detached HEAD}', expected '$BRANCH'"
  exit 0
fi

for state_path in \
  "$git_dir/MERGE_HEAD" \
  "$git_dir/CHERRY_PICK_HEAD" \
  "$git_dir/REVERT_HEAD" \
  "$git_dir/BISECT_LOG" \
  "$git_dir/rebase-apply" \
  "$git_dir/rebase-merge" \
  "$git_dir/index.lock"; do
  if [ -e "$state_path" ]; then
    log "SKIP: Git operation or index lock is active: $state_path"
    exit 0
  fi
done

export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=Never
export GH_PROMPT_DISABLED=1

if ! git add -A; then
  log "ERROR: failed to stage changes"
  exit 1
fi

if git diff --cached --quiet --exit-code; then
  log "No changes to commit"
else
  author_name=$(git log -1 --format='%an' 2>/dev/null || true)
  author_email=$(git log -1 --format='%ae' 2>/dev/null || true)
  author_name=${author_name:-MJAgent2 Auto Commit}
  author_email=${author_email:-mjagent2-auto@localhost}
  commit_message="chore: auto commit $(date '+%Y-%m-%d %H:%M:%S')"

  if ! git \
    -c user.name="$author_name" \
    -c user.email="$author_email" \
    commit -m "$commit_message"; then
    log "ERROR: failed to commit staged changes"
    exit 1
  fi
  log "Created commit: $commit_message"
fi

remote_ref="refs/remotes/$REMOTE/$BRANCH"
if ! git \
  -c http.lowSpeedLimit=1 \
  -c http.lowSpeedTime=60 \
  fetch "$REMOTE" "refs/heads/$BRANCH:$remote_ref"; then
  log "ERROR: fetch failed; local commits are preserved and the next run will retry"
  exit 1
fi

if ! git merge-base --is-ancestor "$remote_ref" HEAD; then
  log "Remote updates detected; rebasing local commits onto $REMOTE/$BRANCH"
  if ! git rebase "$remote_ref"; then
    git rebase --abort >/dev/null 2>&1 || true
    log "ERROR: rebase conflicted and was aborted; manual synchronization is required"
    exit 1
  fi
fi

local_head=$(git rev-parse HEAD)
remote_head=$(git rev-parse "$remote_ref")
if [ "$local_head" = "$remote_head" ]; then
  log "No local commits to push"
  exit 0
fi

if ! git \
  -c http.lowSpeedLimit=1 \
  -c http.lowSpeedTime=60 \
  push "$REMOTE" "HEAD:refs/heads/$BRANCH"; then
  log "ERROR: push failed; the next run will retry"
  exit 1
fi

log "Push completed: $REMOTE/$BRANCH"

if [ -n "$(git status --porcelain)" ]; then
  log "Changes arrived during this run and will be included in the next cycle"
fi
