#!/usr/bin/env bash
# 监控「我欲封天」前 5 集串行剧本生成。
# 仅监控 + 在前一集 ready 后启动下一集；不做删除。任何一集 failed 立即停并报告。
# 用法: yyft_serial_monitor.sh <start_ep_index 1-5>
set -uo pipefail

BASE="http://127.0.0.1:8230"
S="MhbGdiOPLmPQEKgwK8AVfh9pm929uGl1DTcazYVjkn8"
LOG="/Users/bytedance/Desktop/漫剧Agent2.0/scripts/yyft_serial.log"

# ep_index -> episode_id
EIDS=( "" "ep_711b29204aa9" "ep_7630457a7928" "ep_12568eb5945b" "ep_c001d835c465" "ep_4857fc597e7c" )

log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

status_of(){
  local eid="$1"
  curl -s -m 15 -H "X-Manju-Session: $S" "$BASE/api/episodes/$eid/screenplay/status" \
   | python3 -c "import sys,json
try:
  d=json.load(sys.stdin)
except Exception:
  print('PARSE_ERR||||'); raise SystemExit
p=d.get('screenplay_production') or {}
print('%s|%s|%s|%s|%s' % (d.get('screenplay_status'), d.get('active'), (d.get('screenplay_error') or '').replace('\n',' ')[:120], p.get('phase'), p.get('progress')))"
}

start_gen(){
  local eid="$1"; local idem="$2"
  curl -s -m 30 -X POST -H "X-Manju-Session: $S" -H "Content-Type: application/json" \
    -d "{\"idempotency_key\":\"$idem\"}" "$BASE/api/episodes/$eid/screenplay" \
   | python3 -c "import sys,json
try:
  d=json.load(sys.stdin)
except Exception as e:
  print('START_ERR', e); raise SystemExit
print('start:', d.get('status'), d.get('run_id'), d.get('mode'), (d.get('summary') or '')[:60], d.get('detail') or d.get('message') or '')"
}

START_IDX="${1:-1}"
log "=== SERIAL MONITOR START from EP$START_IDX ==="

for idx in $(seq "$START_IDX" 5); do
  eid="${EIDS[$idx]}"
  # 若该集尚未启动（EP1 已在外部启动），非首个循环项则启动
  cur=$(status_of "$eid"); st="${cur%%|*}"
  if [ "$idx" -ne "$START_IDX" ] || [ "$st" = "pending" ]; then
    if [ "$st" = "pending" ]; then
      log "EP$idx starting generation..."
      start_gen "$eid" "gen-yyft-ep$idx-$(date +%s)" | tee -a "$LOG"
      sleep 3
    fi
  fi

  # 轮询直至 ready / failed
  waited=0
  while true; do
    line=$(status_of "$eid")
    st="${line%%|*}"; rest="${line#*|}"; active="${rest%%|*}"
    err=$(echo "$line" | cut -d'|' -f3); phase=$(echo "$line" | cut -d'|' -f4); prog=$(echo "$line" | cut -d'|' -f5)
    log "EP$idx [$st] active=$active phase=$phase prog=$prog ${err:+err=$err}"
    case "$st" in
      ready)
        log "EP$idx READY ✅"
        break ;;
      failed)
        log "EP$idx FAILED ❌ err=$err"
        log "=== ABORT: EP$idx failed, stopping serial run ==="
        exit 2 ;;
      repairing)
        # 一次性生成目标下，暂停待修=整体失败：立即中止，避免空轮询到超时
        log "EP$idx REPAIRING (paused) ❌ err=$err"
        log "=== ABORT: EP$idx entered repairing (one-shot failure), stopping serial run ==="
        exit 2 ;;
      pending)
        # 不应发生（已启动）；等待一轮
        : ;;
    esac
    sleep 20
    waited=$((waited+20))
    if [ "$waited" -gt 2400 ]; then
      log "EP$idx TIMEOUT after ${waited}s (status=$st) ❌"
      exit 3
    fi
  done
done

log "=== ALL EP1-5 READY ✅ SERIAL COMPLETE ==="
exit 0
