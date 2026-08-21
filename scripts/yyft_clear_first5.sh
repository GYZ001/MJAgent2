#!/usr/bin/env bash
# 清空「我欲封天」前 5 集当前剧本状态（两步批准：先取 approval_token，再带 token 确认）。
# 幂等：已是 pending 的集直接跳过删除。用户已授权本次批量清空。
set -uo pipefail

BASE="http://127.0.0.1:8230"
S="MhbGdiOPLmPQEKgwK8AVfh9pm929uGl1DTcazYVjkn8"
LOG="/Users/bytedance/Desktop/漫剧Agent2.0/scripts/yyft_serial.log"

EIDS=( "" "ep_711b29204aa9" "ep_7630457a7928" "ep_12568eb5945b" "ep_c001d835c465" "ep_4857fc597e7c" )

log(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

delete_ep(){
  local eid="$1"
  # step 1: request approval
  local resp token
  resp=$(curl -s -m 30 -X DELETE -H "X-Manju-Session: $S" -H "Content-Type: application/json" \
    -d '{}' "$BASE/api/episodes/$eid/screenplay")
  token=$(echo "$resp" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(''); raise SystemExit
print(d.get('approval_token') or '')")
  if [ -z "$token" ]; then
    # maybe already deleted / ok without approval
    local ok
    ok=$(echo "$resp" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('PARSE_ERR'); raise SystemExit
print(d.get('status') or d.get('ok'))")
    echo "no-approval-needed resp: $ok"
    return 0
  fi
  # step 2: confirm with token
  curl -s -m 30 -X DELETE -H "X-Manju-Session: $S" -H "Content-Type: application/json" \
    -H "x-manju-approval-token: $token" -d '{}' "$BASE/api/episodes/$eid/screenplay" \
   | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception as e: print('CONFIRM_PARSE_ERR', e); raise SystemExit
print('confirm:', d.get('ok'), d.get('status'), (d.get('summary') or '')[:60])"
}

log "=== CLEAR FIRST 5 EPISODES START ==="
for idx in 1 2 3 4 5; do
  eid="${EIDS[$idx]}"
  st=$(curl -s -m 15 -H "X-Manju-Session: $S" "$BASE/api/episodes/$eid/screenplay/status" \
       | python3 -c "import sys,json
try: print((json.load(sys.stdin).get('screenplay_status')) or 'ERR')
except Exception: print('ERR')")
  log "EP$idx ($eid) current status=$st"
  log "EP$idx deleting screenplay..."
  delete_ep "$eid" | tee -a "$LOG"
  # verify
  post=$(curl -s -m 15 -H "X-Manju-Session: $S" "$BASE/api/episodes/$eid/screenplay/status" \
       | python3 -c "import sys,json
try:
  d=json.load(sys.stdin); print('%s|active=%s' % (d.get('screenplay_status'), d.get('active')))
except Exception: print('ERR')")
  log "EP$idx post-delete status=$post"
done
log "=== CLEAR FIRST 5 EPISODES DONE ==="
