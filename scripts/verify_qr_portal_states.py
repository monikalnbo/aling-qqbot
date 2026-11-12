#!/usr/bin/env python3
"""验证看板四种状态的判定与提示文案（不打桩真实扫码，用假状态覆盖全部分支）。

覆盖：等待扫码 / 已扫码待确认 / 换码中（无码、到期）/ 登录成功 / 接口异常。
证据输出：evidence/qr_portal_states.log
"""

import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "evidence", "qr_portal_states.log")

spec = importlib.util.spec_from_file_location("qrportal", os.path.join(ROOT, "scripts", "qr_portal_server.py"))
mod = importlib.util.module_from_spec(spec)
os.environ.setdefault("QRPORTAL_TOKEN", "test-token")
spec.loader.exec_module(mod)

URL_A = "https://txz.qq.com/p?k=AAAA&f=1600001615"
URL_B = "https://txz.qq.com/p?k=BBBB&f=1600001615"

fails = []


def case(name, fake, expect_phase, expect_login=False, expect_label=None):
    mod.napcat_api = lambda path: fake
    snap = mod.status_snapshot()
    ok = snap["phase"] == expect_phase and snap["is_login"] == expect_login
    if expect_label and snap["label"] != expect_label:
        ok = False
    if not ok:
        fails.append(name)
    print(json.dumps({
        "case": name, "ok": ok, "phase": snap["phase"], "label": snap["label"],
        "is_login": snap["is_login"], "accepted": snap["accepted"],
        "age": snap["age"], "remain": snap["remain"], "error": snap["error"],
    }, ensure_ascii=False))
    return snap


def fake(is_login=False, accepted=False, url=URL_A, uin=""):
    return {"code": 0, "data": {"isLogin": is_login, "qrLoginAccepted": accepted,
                                "qrcodeurl": url, "uin": uin}, "message": "success"}


lines = []
print("== 1. 待扫码（首次见到这张码）")
s = case("waiting", fake(), "waiting", False, "等待扫码")
lines.append(s)

print("== 2. 同一张码过了一段时间（倒计时在走）")
mod._seen[URL_A] = time.time() - 45
s = case("waiting_age45", fake(), "waiting", False)
lines.append(s)
assert 70 <= s["remain"] <= 80, s

print("== 3. 码到期（应显示换码中）")
mod._seen[URL_A] = time.time() - 130
s = case("expired", fake(), "refreshing", False, "这张码已到期，正在换新的…")
lines.append(s)

print("== 4. 换了新码（URL 变了，倒计时重置）")
s = case("new_code", fake(url=URL_B), "waiting", False, "等待扫码")
lines.append(s)
assert s["remain"] >= 118, s

print("== 5. 已扫码待确认")
s = case("pending_confirm", fake(accepted=True, url=URL_B), "pending_confirm", False,
         "已扫码，请在手机上点「确认登录」")
lines.append(s)

print("== 6. 登录成功")
s = case("logged_in", fake(is_login=True, accepted=False, url="", uin="10001"), "logged_in", True,
         "已登录成功，可以关掉这个页面了")
lines.append(s)

print("== 7. 无码（NapCat 正在换码）")
s = case("no_qr", fake(url=""), "refreshing", False, "换码中…马上出新的")
lines.append(s)

print("== 8. 接口异常（页面应显示可重试而不是崩）")


def boom(path):
    raise RuntimeError("connection refused")


mod.napcat_api = boom
s = case("api_error", None, "unknown", False, "暂时读不到登录状态，页面会自动重试")
assert s["error"], s
lines.append(s)

print()
print(f"RESULT {'PASS' if not fails else 'FAIL ' + ','.join(fails)}  cases={8} failed={len(fails)}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("看板状态判定验证（用假状态覆盖分支，未动真实链路）\n")
    f.write(f"时间：{time.strftime('%F %T')}\n\n")
    for s in lines:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")
    f.write(f"\nRESULT {'PASS' if not fails else 'FAIL'} cases=8 failed={len(fails)}\n")
print(f"evidence -> {OUT}")
sys.exit(0 if not fails else 1)
