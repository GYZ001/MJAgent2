#!/usr/bin/env bash
# 安装 / 卸载 MJAgent2 每 5 分钟 git 同步的 systemd timer。
# 用法：scripts/install_sync_timer.sh [install|uninstall|status]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT/scripts/systemd"
UNIT_DIR="/etc/systemd/system"
NAME="mjagent2-sync"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root：请用 sudo 运行" >&2
    exit 1
  fi
}

do_install() {
  need_root
  chmod +x "$ROOT/scripts/sync_remote.sh"
  install -m 644 "$SRC_DIR/${NAME}.service" "$UNIT_DIR/${NAME}.service"
  install -m 644 "$SRC_DIR/${NAME}.timer" "$UNIT_DIR/${NAME}.timer"
  systemctl daemon-reload
  systemctl enable --now "${NAME}.timer"
  systemctl status "${NAME}.timer" --no-pager
  echo "已启用：每 5 分钟执行 ${NAME}.service -> $ROOT/scripts/sync_remote.sh"
}

do_uninstall() {
  need_root
  systemctl disable --now "${NAME}.timer" 2>/dev/null || true
  rm -f "$UNIT_DIR/${NAME}.service" "$UNIT_DIR/${NAME}.timer"
  systemctl daemon-reload
  echo "已卸载 ${NAME}.timer"
}

do_status() {
  systemctl status "${NAME}.timer" --no-pager || true
  echo
  systemctl list-timers "${NAME}.timer" --no-pager || true
  echo
  echo "最近日志："
  journalctl -u "${NAME}.service" -n 30 --no-pager || true
}

case "${1:-install}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  status)    do_status ;;
  *) echo "用法：scripts/install_sync_timer.sh [install|uninstall|status]"; exit 1 ;;
esac
