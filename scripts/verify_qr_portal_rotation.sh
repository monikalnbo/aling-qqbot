#!/usr/bin/env bash
# 实测看板自动换码：每 10s 拉一次 state 和 qr.png，记录 URL/图片哈希/状态变化。
# 用法：bash scripts/verify_qr_portal_rotation.sh [秒数]  → 证据 evidence/qr_portal_rotation.log
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOK="$(cat "$ROOT/.qqqr_token")"
DUR="${1:-200}"
OUT="$ROOT/evidence/qr_portal_rotation.log"
URL="${QRPORTAL_URL:-https://your-domain.example/qqqr-${TOK}}"
: >"$OUT"
end=$(( $(date +%s) + DUR ))
prev=""
while (( $(date +%s) < end )); do
  ts="$(date '+%F %T')"
  st="$(curl -sS -m 10 "${URL}/state?t=$(date +%s%N)" || echo '{}')"
  url="$(printf '%s' "$st" | python3 -c 'import sys,json;print((json.load(sys.stdin) or {}).get("qr_url",""))' 2>/dev/null)"
  ph="$(printf '%s' "$st" | python3 -c 'import sys,json;d=json.load(sys.stdin) or {};print(d.get("phase","")+"|"+str(d.get("age"))+"|"+str(d.get("remain")))' 2>/dev/null)"
  h="$(curl -sS -m 10 "${URL}/qr.png?t=$(date +%s%N)" -o /tmp/_rp_qr.png 2>/dev/null && md5sum /tmp/_rp_qr.png | cut -d' ' -f1)"
  flag=""
  [[ -n "$prev" && "$url" != "$prev" ]] && flag="QR_CHANGED"
  [[ "$url" != "$prev" ]] && echo "[$ts] ${flag:-first} phase=${ph} md5=${h} url=${url}" >>"$OUT"
  prev="$url"
  sleep 10
done
echo "[$(date '+%F %T')] END" >>"$OUT"
