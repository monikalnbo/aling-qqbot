#!/usr/bin/env bash
# 保持登录二维码「同一个文件路径」始终是最新可扫的那张。
#
# - 输出图片固定为 evidence/qq_login_qr.png
# - 旁边 evidence/qq_login_qr.meta 记录当前这张码的生成时间与内容
# - 每 8 秒查一次：NapCat 自己约每 2 分钟换一张码，换码后 8 秒内图片就会更新
# - 登录成功后写 LOGIN_OK 并退出（不再改动图片）
#
# 用法：bash scripts/qq_qr_watch.sh [--max-seconds N]

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${ROOT}/evidence/qq_login_qr.png"
META="${ROOT}/evidence/qq_login_qr.meta"
MAX=0
[[ "${1:-}" == "--max-seconds" ]] && MAX="${2:-0}"

START=$(date +%s)
last=""
first=1
while :; do
  if [[ "$MAX" != 0 ]] && (( $(date +%s) - START > MAX )); then
    echo "$(date '+%F %T') WATCH_TIMEOUT 监控到时退出，图片停留在最后一张" >>"$META"
    exit 0
  fi

  args=()
  (( first )) && args=(--refresh)     # 第一次先主动换一张新码，保证扫码时是新鲜的
  first=0
  out="$(bash "${ROOT}/scripts/fetch_qr.sh" ${args[@]+"${args[@]}"} 2>&1)"

  ts="$(date '+%F %T')"
  if grep -q "机器人已登录" <<<"$out"; then
    { echo "$ts LOGIN_OK"; grep -oE 'uin=[^，, ]+' <<<"$out"; } >>"$META"
    exit 0
  fi

  url="$(sed -n 's/^\[fetch_qr\] 二维码内容：//p' <<<"$out" | tail -1)"
  if [[ -n "$url" && "$url" != "$last" ]]; then
    last="$url"
    {
      echo "生成时间：$ts"
      echo "二维码内容：$url"
      echo "说明：扫码后请在手机上确认；本文件会自动换成最新一张"
    } >"$META"
  fi
  sleep 8
done
