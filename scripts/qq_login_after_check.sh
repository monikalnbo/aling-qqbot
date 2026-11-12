#!/usr/bin/env bash
# 扫码后置检查：等待登录成功 → 记录 uin → 确认 AstrBot 适配器已连接 → 自发一条私聊验证收发。
# 不向任何其他人/群发消息（只发给机器人自己的号），每 20s 查一次，最多 45 分钟。
#
# 输出：evidence/qq_relogin_after.log（追加）

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/evidence/qq_relogin_after.log"
URL="http://127.0.0.1:6099"
MAX=$((45 * 60))
START=$(date +%s)

log() { echo "[$(date '+%F %T')] $*" >>"$OUT"; }

TOKEN="$(docker logs napcat 2>&1 | grep -oE 'WebUi Token: [A-Za-z0-9]+' | tail -1 | awk '{print $3}')"
HASH="$(printf '%s' "${TOKEN}.napcat" | sha256sum | cut -d' ' -f1)"
CRED="$(curl -sS -m 10 -X POST "${URL}/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"hash\":\"${HASH}\"}" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("Credential",""))')"
[[ -n "$CRED" ]] || { log "FAIL 拿不到 NapCat WebUI 凭据"; exit 1; }

api() { curl -sS -m 15 -X POST "${URL}/api/$1" -H "Authorization: Bearer ${CRED}" \
  -H 'Content-Type: application/json' -d "${2:-{}}"; }

log "开始等待扫码（每 20s 检查一次）"
while :; do
  if (( $(date +%s) - START > MAX )); then log "TIMEOUT 45 分钟内未检测到登录"; exit 0; fi
  st="$(api QQLogin/CheckLoginStatus)"
  if python3 - "$st" <<'PY'
import json,sys
d=json.loads(sys.argv[1]).get("data") or {}
sys.exit(0 if d.get("isLogin") else 1)
PY
  then
    uin="$(printf '%s' "$st" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("uin",""))')"
    log "LOGIN_OK uin=${uin} status=${st}"
    sleep 5
    log "--- AstrBot 适配器状态（登录后日志） ---"
    docker logs astrbot --since 15m 2>&1 | grep -E "适配器已连接|适配器已断开|aiocqhttp" | tail -5 | sed 's/\x1b\[[0-9;]*m//g' >>"$OUT"

    # 自发一条私聊：只发给机器人自己的号，不打扰任何人
    SEND="$(api QQLogin/../.. 2>/dev/null || true)"
    SEND="$(curl -sS -m 20 -X POST "${URL}/api/OneBot/send_private_msg" \
      -H "Authorization: Bearer ${CRED}" -H 'Content-Type: application/json' \
      -d "{\"user_id\":${uin},\"message\":\"[登录检查] 自检消息，可忽略\"}" || true)"
    log "自检私聊返回：${SEND}"

    sleep 20
    log "--- 最近 AstrBot 事件（15 分钟内，含自发消息是否被收到） ---"
    docker logs astrbot --since 15m 2>&1 | grep -E "napcat\(aiocqhttp\)" | tail -5 | sed 's/\x1b\[[0-9;]*m//g' >>"$OUT"
    log "DONE 检查结束"
    exit 0
  fi
  sleep 20
done
