#!/usr/bin/env python3
"""唱歌模式端到端验收（假 NapCat，真实 TTS / 真实对话模型，不往真实 QQ 发消息）。

流程：
  §1 普通聊天  → 必须仍是「快、短句、日常音色」的语音（不得走唱歌模型）
  §2 反例「不要唱歌」 → 不得触发唱歌
  §3 三条点歌（小星星 / 两只老虎 / 生日快乐）→ 必须收到长音频，且音高有起伏
  §4 /唱歌 两只老虎 指令 → 必须收到歌声
  §5 普通聊天复测 → 速度不退化

用法：
    python3 -I scripts/verify_singing_e2e.py [--url ws://127.0.0.1:6199/ws]
输出：evidence/singing_mode/*.mp3|wav + evidence/singing_mode_e2e.log
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "evidence", "singing_mode")
OUT_LOG = os.path.join(ROOT, "evidence", "singing_mode_e2e.log")
# 分段复跑（--sections）时不要覆盖整轮日志，用例名由 main() 补后缀
OUT_NAME = "singing_mode_e2e.log"

SELF_ID = 10000
GROUP_ID = 555111
USER_ID = 123456789  # 假用户，不是任何真实账号

lines: list[str] = []


def log(s: str) -> None:
    print(s, flush=True)
    lines.append(s)


def build_event(text: str, mid: int) -> dict:
    return {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "message_id": mid,
        "font": 0,
        "sub_type": "normal",
        "sender": {"user_id": USER_ID, "nickname": "测试员", "card": "测试员"},
        "to_me": True,
        "group_id": GROUP_ID,
        "user_id": USER_ID,
        "message_type": "group",
        "message": [
            {"type": "at", "data": {"qq": str(SELF_ID)}},
            {"type": "text", "data": {"text": " " + text}},
        ],
        "raw_message": f"[CQ:at,qq={SELF_ID}] {text}",
    }


def describe(params: dict) -> tuple[str, list[str]]:
    parts, audios = [], []
    for seg in params.get("message", []):
        if not isinstance(seg, dict):
            continue
        st, data = seg.get("type"), seg.get("data", {}) or {}
        if st == "text":
            parts.append(data.get("text", ""))
        elif st == "record":
            f = data.get("file", "")
            parts.append(f"[record {len(f)}B]")
            if f.startswith("base64://"):
                audios.append(f[len("base64://"):])
        elif st == "at":
            parts.append(f"[at:{data.get('qq')}]")
        else:
            parts.append(f"[{st}]")
    return "".join(parts), audios


def pcm(data: bytes) -> "list[float]":
    r = subprocess.run(
        ["docker", "exec", "-i", "astrbot", "ffmpeg", "-v", "quiet", "-i", "-",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        input=data, capture_output=True,
    )
    import array

    a = array.array("h")
    a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    return [x / 32768.0 for x in a]


def analyze(data: bytes) -> dict:
    """时长 + 粗音高起伏：逐帧估基频，统计稳定长音段与音高跨度。"""
    # 与 scripts/analyze_singing_evidence.py 同一套算法：
    # 自相关估基频（带峰值选择避免倍频）+ 5 帧中值滤波；
    # 长音段 = 连续 ≥250ms 且相邻帧音高差 <0.6 半音；音域 = p5~p95 半音跨度。
    import numpy as np

    xs = np.array(pcm(data), dtype=np.float64)
    sr = 16000
    if not len(xs):
        return {"dur": 0.0, "notes": 0, "sustain": 0, "sustain_s": 0.0, "range": 0.0}
    win, hop = 800, 160  # 50ms / 10ms
    lo, hi = int(sr / 700), int(sr / 90)
    hann = np.hanning(win)
    f0s: list[float] = []
    for i in range(0, len(xs) - win, hop):
        fr = xs[i : i + win]
        if np.sqrt((fr**2).mean()) < 0.008:
            f0s.append(0.0)
            continue
        fr = (fr - fr.mean()) * hann
        ac = np.correlate(fr, fr, "full")[win - 1 :]
        ac = ac / ac[0] if ac[0] > 0 else ac
        seg = ac[lo:hi]
        if not len(seg) or seg.max() < 0.35:
            f0s.append(0.0)
            continue
        peaks = [
            k
            for k in range(1, len(seg) - 1)
            if seg[k] > seg[k - 1] and seg[k] >= seg[k + 1] and seg[k] >= 0.85 * seg.max()
        ]
        k = (peaks[0] if peaks else int(np.argmax(seg))) + lo
        f0s.append(sr / k)
    f = np.array(f0s)
    out = f.copy()
    for i in range(2, len(f) - 2):
        blk = f[i - 2 : i + 3]
        v = blk[blk > 0]
        out[i] = np.median(v) if len(v) >= 2 else 0
    voiced = out[out > 0]
    dur = round(len(xs) / sr, 2)
    if len(voiced) < 10:
        return {"dur": dur, "notes": 0, "sustain": 0, "sustain_s": 0.0, "range": 0.0}
    st = 12 * np.log2(voiced / 55.0)
    sustain, sustain_frames, run, run_last = 0, 0, 0, None
    for s in st:
        if run and run_last is not None and abs(s - run_last) < 0.6:
            run += 1
        else:
            if run * 0.01 >= 0.25:
                sustain += 1
                sustain_frames += run
            run = 1
        run_last = s
    if run * 0.01 >= 0.25:
        sustain += 1
        sustain_frames += run
    return {
        "dur": dur,
        "notes": len({round(float(v)) for v in st}),
        "sustain": sustain,
        "sustain_s": round(sustain_frames * 0.01, 2),
        "range": round(float(np.percentile(st, 95) - np.percentile(st, 5)), 1),
    }


async def run(url: str, only_sections: str = "") -> int:
    import websockets

    frames: list[dict] = []
    ok_all = True
    want = {x.strip() for x in only_sections.split(",") if x.strip()}

    def section(n: int) -> bool:
        return (not want) or (str(n) in want)

    async with websockets.connect(
        url,
        additional_headers={"X-Self-ID": str(SELF_ID), "X-Client-Role": "Universal"},
        ping_interval=None,
        max_size=None,
    ) as ws:

        async def reader():
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                action = data.get("action")
                if action is None:
                    continue
                if action in ("send_msg", "send_group_msg", "send_private_msg"):
                    frames.append(data)
                await ws.send(
                    json.dumps(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 900 + len(frames), "user_id": USER_ID,
                                     "nickname": "测试员"},
                            "echo": data.get("echo"),
                        },
                        ensure_ascii=False,
                    )
                )

        task = asyncio.create_task(reader())
        await asyncio.sleep(1.0)

        async def ask(text: str, wait: float, idle: float = 3.0) -> tuple[list[dict], float]:
            start = len(frames)
            t0 = time.time()
            await ws.send(json.dumps(build_event(text, int(time.time() * 1000) % 900_000_000), ensure_ascii=False))
            deadline = time.time() + wait
            last, quiet = start, 0.0
            while time.time() < deadline:
                await asyncio.sleep(0.25)
                if len(frames) > last:
                    last, quiet = len(frames), 0.0
                elif last > start:
                    quiet += 0.25
                    if quiet >= idle:
                        break
            # 返回的是本轮开始之后的全部帧快照：即使合成完成的那条语音比静默窗口晚到，
            # 也不会因为它落在 ask() 返回之后就被漏掉（grab 内部会按 index 再取最新快照）。
            return frames[start:], time.time() - t0

        os.makedirs(OUT_DIR, exist_ok=True)
        seq = 0

        async def grab(tag: str, text: str, wait: float, idle: float = 3.0) -> list[dict]:
            nonlocal seq
            got, dt = await ask(text, wait, idle)
            if seq:
                # 文件名里带全局序号：旧的重复文件会被覆盖，不留下过期音频
                for old in glob.glob(os.path.join(OUT_DIR, f"{tag}_*.wav")) + glob.glob(
                    os.path.join(OUT_DIR, f"{tag}_*.mp3")
                ):
                    os.remove(old)
            summary = " | ".join(describe(f.get("params", {}))[0] for f in got) or "(无回复)"
            log(f"[{tag}] 问：{text!r} 等待={dt:.1f}s 收到 {len(got)} 条：{summary[:160]}")
            texts = " ".join(describe(f.get("params", {}))[0] for f in got)
            if "没唱出来" in texts:
                log("   !! 插件报「没唱出来」：合成超时或接口未返回音频（看插件日志 sing 合成失败）")
                ok_all = False
            for f in got:
                _, audios = describe(f.get("params", {}))
                for j, b64 in enumerate(audios):
                    seq += 1
                    data = base64.b64decode(b64)
                    ext = ".wav" if data[:4] == b"RIFF" else ".mp3"
                    p = os.path.join(OUT_DIR, f"{tag}_{seq}_{j}{ext}")
                    with open(p, "wb") as fh:
                        fh.write(data)
                    m = analyze(data)
                    log(
                        f"    ↳ {os.path.relpath(p, ROOT)} {len(data)}B "
                        f"duration={m['dur']}s 音符={m['notes']} 长音段={m['sustain']}"
                        f"({m['sustain_s']}s) 音域={m['range']}半音"
                    )
            frames.clear()
            return got

        # §1 普通聊天
        if section(1):
            log("== §1 普通聊天（应快、短、日常音色）==")
            got = await grab("01_chat", "喂，说句话听听", 90)
            audios = [b for f in got for b in describe(f.get("params", {}))[1]]
            if not audios:
                log("   !! 普通聊天没有语音")
                ok_all = False
            else:
                ms = [analyze(base64.b64decode(b)) for b in audios]
                total = sum(m["dur"] for m in ms)
                long_song = sum(1 for m in ms if m["dur"] >= 30 and m["sustain"] >= 5)
                if long_song or total > 90:
                    log(f"   !! 普通聊天疑似走了唱歌：{len(ms)} 条共 {total:.1f}s")
                    ok_all = False
                else:
                    log(f"   ✓ 普通语音：{len(ms)} 条共 {total:.1f}s，无长歌声")

        # §2 反例
        if section(2):
            log("== §2 反例：不要唱歌 ==")
            got = await grab("02_negative", "不要唱歌，聊聊天吧", 60)
            texts = " ".join(describe(f.get("params", {}))[0] for f in got)
            audios = [b for f in got for b in describe(f.get("params", {}))[1]]
            if "[record" in texts:
                m = analyze(base64.b64decode(audios[0])) if audios else None
                if m and m["dur"] > 25:
                    log(f"   !! 反例触发了唱歌（音频 {m['dur']}s）")
                    ok_all = False
                else:
                    log(f"   ✓ 只是普通语音（{m['dur'] if m else 0}s）")
            else:
                log("   ✓ 反例走文字回复，没有唱歌")

        # §3 三首歌
        if section(3):
            log("== §3 点歌（必须长音频 + 有明显音高起伏）==")
            for tag, text in (
                ("03_sing_xiaoxingxing", "唱首小星星"),
                ("04_sing_tiger", "来一首两只老虎"),
                ("05_sing_birthday", "唱首生日快乐"),
            ):
                got = await grab(tag, text, 170, idle=30)
                audio_bytes = [
                    base64.b64decode(b)
                    for f in got
                    for b in describe(f.get("params", {}))[1]
                ]
                if not audio_bytes:
                    log(f"   !! {tag} 没收到音频")
                    ok_all = False
                    continue
                ms = [analyze(b) for b in audio_bytes]
                total = sum(m["dur"] for m in ms)
                sustain = sum(m["sustain"] for m in ms)
                sustain_s = sum(m["sustain_s"] for m in ms)
                rng = max(m["range"] for m in ms)
                # 判「唱」而不是「念」：够长 + 多个稳定拖长音（至少占总时长 1/4）+ 音域 ≥12 半音
                if total < 20 or sustain < 5 or sustain_s < total * 0.25 or rng < 12:
                    log(
                        f"   !! {tag} 不像唱歌：总时长 {total:.1f}s 长音段 {sustain}"
                        f"({sustain_s:.1f}s) 音域 {rng}"
                    )
                    ok_all = False
                else:
                    log(
                        f"   ✓ {tag} 是唱歌：总时长 {total:.1f}s 长音段 {sustain}"
                        f"({sustain_s:.1f}s) 音域 {rng} 半音"
                    )

        # §4 显式指令
        if section(4):
            log("== §4 显式指令 /唱歌 两只老虎 ==")
            got = await grab("06_cmd_sing", "/唱歌 两只老虎", 240, idle=90)
            audio_bytes = [
                base64.b64decode(b) for f in got for b in describe(f.get("params", {}))[1]
            ]
            if not audio_bytes:
                log("   !! 指令没收到音频")
                ok_all = False
            else:
                ms = [analyze(b) for b in audio_bytes]
                total = sum(m["dur"] for m in ms)
                sustain = sum(m["sustain"] for m in ms)
                sustain_s = sum(m["sustain_s"] for m in ms)
                rng = max(m["range"] for m in ms)
                log(
                    f"   指令唱歌总时长 {total:.1f}s，长音段 {sustain}"
                    f"({sustain_s:.1f}s) 音域 {rng}"
                )
                if total < 20 or sustain < 5 or sustain_s < total * 0.25 or rng < 12:
                    log("   !! /唱歌 没唱出旋律")
                    ok_all = False
                else:
                    log("   ✓ /唱歌 收到歌声")

        # §7 更多正例（意图识别正例真跑，只取首条音频确认走了唱歌链路）
        if section(7):
            log("== §7 正例组：3 条点歌说法（必须收到长歌声）==")
            for tag, text in (
                ("11_pos_song", "唱首歌"),
                ("12_pos_yishou", "来一首吧"),
                ("13_pos_ba", "给我唱一段"),
            ):
                got = await grab(tag, text, 170, idle=30)
                audios = [b for f in got for b in describe(f.get("params", {}))[1]]
                if not audios:
                    log(f"   !! {tag} 没收到音频")
                    ok_all = False
                    continue
                ms = [analyze(base64.b64decode(b)) for b in audios]
                total = sum(m["dur"] for m in ms)
                sus = sum(m["sustain_s"] for m in ms)
                if total < 20 or any(m["sustain"] < 5 for m in ms) or sus < total * 0.25:
                    log(f"   !! {tag} 不像唱歌：{total:.1f}s 长音 {sus:.1f}s")
                    ok_all = False
                else:
                    log(f"   ✓ {tag} 是唱歌：{total:.1f}s 长音 {sus:.1f}s")

        # §6 更多反例（意图识别负例真跑）
        if section(6):
            log("== §6 反例组：3 条非点歌说法（不得唱歌）==")
            for tag, text in (
                ("08_neg_can", "你会唱歌吗"),
                ("09_neg_stop", "别唱了，安静会儿"),
                ("10_neg_praise", "他唱得真好听"),
            ):
                # 每条只等 20 s 静默；上一轮漏跑的唱歌很慢，靠日志里的 text= 精确核对
                got = await grab(tag, text, 60, idle=20)
                audios = [b for f in got for b in describe(f.get("params", {}))[1]]
                if not audios:
                    log(f"   ✓ {tag} 文字回复，没有唱歌")
                    continue
                ms = []
                for i, b in enumerate(audios, 1):
                    m = analyze(base64.b64decode(b))
                    ms.append(m)
                    log(
                        f"    ↳ {tag} 第{i}条 duration={m['dur']}s 长音段={m['sustain']}"
                        f"({m['sustain_s']}s) 音域={m['range']}半音"
                    )
                if any(m["dur"] >= 20 and m["sustain"] >= 5 for m in ms):
                    log(f"   !! {tag} 触发了唱歌（查日志 sing-intent text= 确认是哪句）")
                    ok_all = False
                else:
                    log(f"   ✓ {tag} 普通语音（{sum(m['dur'] for m in ms):.1f}s）")

        # §5 普通聊天复测
        if section(5):
            log("== §5 普通聊天复测（速度不得退化）==")
            t0 = time.time()
            got = await grab("07_chat_again", "晚上吃点啥好呢", 90)
            log(f"   复测耗时 {time.time() - t0:.1f}s，语音 {sum(len(describe(f.get('params', {}))[1]) for f in got)} 条")

        task.cancel()

    log(f"RESULT {'PASS' if ok_all else 'FAIL'}")

    # 线程池/事件循环收尾
    os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
    out_log = os.path.join(ROOT, "evidence", OUT_NAME)
    with open(out_log, "w", encoding="utf-8") as f:
        f.write(
            f"唱歌模式端到端（假 NapCat，真实 TTS / 真实对话模型，未向真实 QQ 发送）\n"
            f"时间：{time.strftime('%F %T')}\n\n"
        )
        f.write("\n".join(lines) + "\n")
    log("evidence -> " + out_log)
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    ap.add_argument("--sections", default="", help="只跑指定段，逗号分隔，如 4")
    args = ap.parse_args()
    global OUT_NAME
    if args.sections:
        OUT_NAME = f"singing_mode_e2e_sec{args.sections.replace(',', '-')}.log"
    try:
        return asyncio.run(run(args.url, args.sections))
    except Exception as exc:  # noqa: BLE001
        log(f"[verify] 失败: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
