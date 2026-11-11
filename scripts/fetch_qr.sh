#!/usr/bin/env bash
# 拉取 NapCat 当前的 QQ 登录二维码，保存为 evidence/qq_login_qr.png（覆盖旧图）。
#
# QQ 登录二维码会过期（实测约 2 分钟），过期后重新执行本脚本即可。
#
# 用法：
#   ./scripts/fetch_qr.sh                 # 自动从容器日志里找 token
#   NAPCAT_TOKEN=xxxxxxxx ./scripts/fetch_qr.sh
#   NAPCAT_URL=http://1.2.3.4:6099 ./scripts/fetch_qr.sh
#   ./scripts/fetch_qr.sh --refresh       # 先让 NapCat 刷新一张新码再拉
#
# 依赖：curl、sha256sum、python3（qrcode 库，缺了会自动装到 /tmp/pyqr）
#
# 退出码：0 成功（已生成图片，或机器人已登录无需扫码）；1 失败。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/evidence/qq_login_qr.png"
URL="${NAPCAT_URL:-http://127.0.0.1:6099}"
CONTAINER="${NAPCAT_CONTAINER:-napcat}"
REFRESH=0
[[ "${1:-}" == "--refresh" ]] && REFRESH=1

say() { printf '[fetch_qr] %s\n' "$*"; }
die() { printf '[fetch_qr] 失败：%s\n' "$*" >&2; exit 1; }

# ---------- 1. token ----------
TOKEN="${NAPCAT_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  TOKEN="$(docker logs "$CONTAINER" 2>&1 | grep -oE 'WebUi Token: [A-Za-z0-9]+' | tail -1 | awk '{print $3}' || true)"
fi
[[ -n "$TOKEN" ]] || die "拿不到 NapCat WebUI token。容器 '$CONTAINER' 是否在运行？或用 NAPCAT_TOKEN= 指定。"
say "NapCat WebUI token: $TOKEN"

# ---------- 2. 登录 WebUI 拿 Credential ----------
# NapCat WebUI 的 /api/auth/login 接收 sha256(token + ".napcat")
HASH="$(printf '%s' "${TOKEN}.napcat" | sha256sum | cut -d' ' -f1)"
LOGIN="$(curl -sS -m 15 -X POST "${URL}/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"hash\":\"${HASH}\"}" || true)"
CRED="$(printf '%s' "$LOGIN" | python3 -c 'import sys,json;
try:
    d=json.load(sys.stdin)
    print((d.get("data") or {}).get("Credential",""))
except Exception:
    print("")')"
[[ -n "$CRED" ]] || die "WebUI 鉴权失败：${LOGIN}"

api() { # api <path> [body]
  curl -sS -m 15 -X POST "${URL}/api/${1}" \
    -H "Authorization: Bearer ${CRED}" \
    -H 'Content-Type: application/json' \
    -d "${2:-{}}"
}

# ---------- 3. 已登录就别给二维码了 ----------
STATUS="$(api QQLogin/CheckLoginStatus)"
if python3 - "$STATUS" <<'PY'
import json,sys
try:
    d=json.loads(sys.argv[1]).get("data") or {}
except Exception:
    sys.exit(1)          # 解析不了 → 按“未登录”继续走
if d.get("isLogin"):
    print(f"[fetch_qr] 机器人已登录：uin={d.get('uin','?')}，无需扫码。")
    sys.exit(0)
sys.exit(1)
PY
then
  exit 0
fi

# ---------- 4. 取二维码 ----------
if [[ $REFRESH -eq 1 ]]; then
  say "请求 NapCat 刷新二维码…"
  api QQLogin/RefreshQRcode >/dev/null || true
  sleep 2
fi

QR_JSON="$(api QQLogin/GetQQLoginQrcode)"
QR_URL="$(printf '%s' "$QR_JSON" | python3 -c 'import sys,json;
try:
    print((json.load(sys.stdin).get("data") or {}).get("qrcode",""))
except Exception:
    print("")')"
[[ -n "$QR_URL" ]] || die "没有拿到二维码 URL：${QR_JSON}"

say "二维码内容：${QR_URL}"

# ---------- 5. 生成 PNG ----------
mkdir -p "$(dirname "$OUT")"
python3 - "$QR_URL" "$OUT" <<'PY'
import sys, importlib.util, subprocess, os
url, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, "/tmp/pyqr")          # 之前脚本装过的本地目录
if importlib.util.find_spec("qrcode") is None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "--break-system-packages", "--target", "/tmp/pyqr", "qrcode"], check=True)
    sys.path.insert(0, "/tmp/pyqr")
import qrcode
qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
qr.add_data(url); qr.make(fit=True)
qr.make_image(fill_color="black", back_color="white").convert("RGB").save(out)
print(f"[fetch_qr] 已写入 {out}")
PY

# ---------- 6. 自检：必须是有效非空 PNG ----------
[[ -s "$OUT" ]] || die "输出文件为空"
python3 - "$OUT" <<'PY'
import sys
from PIL import Image
p = sys.argv[1]
im = Image.open(p); im.verify()
im = Image.open(p)
assert im.format == "PNG" and im.size[0] > 50 and im.size[1] > 50, f"尺寸异常 {im.size}"
print(f"[fetch_qr] 校验通过：PNG {im.size[0]}x{im.size[1]}，{__import__('os').path.getsize(p)} 字节")
PY

say "把图片发给手机 QQ 扫（或直接在 WebUI 6099 扫码）。过期了就重新执行本脚本。"
say "生成时间：$(date '+%Y-%m-%d %H:%M:%S')"
