#!/usr/bin/env bash
# 入口机 A 每天把计算服务器 B 的数据库热备拉一份到本机：B 的备份与数据在同一块盘上，
# B 整机没了备份就跟着没了；A 上这份是异地副本。走运维隧道 `mjb`（B → A 的反向隧道）。
#
# B 侧：mjagent2-backup.timer 每天 03:17（B 本地时间，Asia/Shanghai）热备到
#       /var/backups/mjagent2/db/manju-<ts>.db.gz，并维护 manju-latest.db.gz 软链。
# A 侧：本脚本 04:00 Asia/Shanghai 拉取（mjagent2-backup-pull.timer），只增不删地镜像，
#       本机保留最近 7 天 + 最新一份（A 盘小，2 核 60G）。
# 安装：cp scripts/deploy/mjagent2-backup-pull.{service,timer} /etc/systemd/system/
#       systemctl daemon-reload && systemctl enable --now mjagent2-backup-pull.timer
set -uo pipefail
B="${MJ_B_SSH:-mjb}"
SRC=/var/backups/mjagent2/db
DST=/var/backups/mjagent2/db-from-b
LOG=/root/MJAgent2/logs/backup-pull.log
KEEP_DAYS=7
mkdir -p "$DST" "$(dirname "$LOG")"
log() { echo "[$(date '+%F %T %Z')] $*" | tee -a "$LOG"; }

ssh -o ConnectTimeout=15 "$B" true 2>/dev/null || { log "连不上 $B（隧道断），本次跳过"; exit 2; }
if ! ssh "$B" "test -d $SRC"; then log "B 上还没有 $SRC（备份定时器尚未首次运行），本次跳过"; exit 0; fi

if ! rsync -a --partial --include='manju-*.db.gz' --include='manju-latest.db.gz' --exclude='*' \
     "$B:$SRC/" "$DST/" 2>>"$LOG"; then
  log "rsync 失败"; exit 1
fi
# 只看真实文件：manju-latest.db.gz 是软链，ls -t 会把它排最前，du/gzip 都会被它带偏
latest="$(find "$DST" -maxdepth 1 -type f -name 'manju-2*.db.gz' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
[ -n "$latest" ] || { log "拉取后本机没有任何 manju-<ts>.db.gz 真实文件"; exit 1; }
if ! gzip -t "$latest" 2>>"$LOG"; then log "最新备份 gzip 校验失败：$latest"; exit 1; fi

# 保留：最近 KEEP_DAYS 天 + 最新一份（find 的结果里排除 latest 本身）
find "$DST" -maxdepth 1 -name 'manju-2*.db.gz' -type f -mtime +"$KEEP_DAYS" ! -newer "$latest" ! -samefile "$latest" -print -delete \
  | sed 's/^/  已清理: /' | tee -a "$LOG"
count="$(find "$DST" -maxdepth 1 -type f -name 'manju-2*.db.gz' | wc -l)"
log "拉取完成：最新 $(basename "$latest") $(du -h "$latest" | cut -f1)，本机共 $count 份，目录 $(du -sh "$DST" | cut -f1)"
