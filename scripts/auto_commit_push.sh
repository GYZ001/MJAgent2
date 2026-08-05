#!/usr/bin/env bash

set -u
umask 077

REPO="/Users/lnuyasha/Desktop/MJAgent2"
REMOTE="origin"
BRANCH="main"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

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
  pending_commits=$(git rev-list --count "$REMOTE/$BRANCH..HEAD" 2>/dev/null || printf '1')
  if [ "$pending_commits" -eq 0 ]; then
    log "No changes to commit or push"
    exit 0
  fi
  log "No new changes; retrying $pending_commits pending commit(s)"
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

if ! git \
  -c http.lowSpeedLimit=1 \
  -c http.lowSpeedTime=60 \
  push "$REMOTE" "$BRANCH:$BRANCH"; then
  log "ERROR: push failed; the next run will retry"
  exit 1
fi

log "Push completed: $REMOTE/$BRANCH"
