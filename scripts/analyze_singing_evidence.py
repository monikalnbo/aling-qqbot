#!/usr/bin/env python3
"""对 evidence/singing_mode/ 里的音频做「唱 vs 念」量化（离线，可重复跑）。

做法：用容器里的 ffmpeg 把音频解成 16k 单声道 PCM → numpy 自相关估基频
（带峰值选择避免倍频、5 帧中值滤波）→ 统计：
  * 稳定拖长音段：连续 ≥250ms 且段内音高变化 < 0.6 半音（唱歌的典型特征）
  * 音高跨度：95% - 5% 分位，半音
  * 中位 F0、音高标准差、滑音/换音次数

判据（唱）：时长 ≥ 20 秒、稳定长音段 ≥ 5、音高跨度 ≥ 12 半音。
念的语音：长音段通常 0~2，跨度 8~12 半音以内。

用法：
    python3 -I scripts/analyze_singing_evidence.py [目录]     默认 evidence/singing_mode
输出：evidence/singing_mode_analysis.log（同时打印）
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_LOG = os.path.join(ROOT, "evidence", "singing_mode_analysis.log")
SR = 16000


def load_pcm(path: str) -> np.ndarray:
    with open(path, "rb") as fh:
        data = fh.read()
    r = subprocess.run(
        ["docker", "exec", "-i", "astrbot", "ffmpeg", "-v", "quiet", "-i", "-",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        input=data, capture_output=True,
    )
    raw = r.stdout[: len(r.stdout) // 2 * 2]
    return np.frombuffer(raw, np.int16).astype(np.float64) / 32768.0


def f0_track(x: np.ndarray, win=0.05, hop=0.01, fmin=90, fmax=700):
    W, H = int(win * SR), int(hop * SR)
    out = []
    for i in range(0, len(x) - W, H):
        fr = x[i : i + W]
        if np.sqrt((fr**2).mean()) < 0.008:
            out.append(0.0)
            continue
        fr = fr - fr.mean()
        fr = fr * np.hanning(len(fr))
        ac = np.correlate(fr, fr, "full")[W - 1 :]
        ac = ac / ac[0] if ac[0] > 0 else ac
        lo, hi = int(SR / fmax), int(SR / fmin)
        seg = ac[lo:hi]
        if not len(seg) or seg.max() < 0.35:
            out.append(0.0)
            continue
        pk = [
            k
            for k in range(1, len(seg) - 1)
            if seg[k] > seg[k - 1] and seg[k] >= seg[k + 1] and seg[k] >= 0.85 * seg.max()
        ]
        k = (pk[0] if pk else int(np.argmax(seg))) + lo
        out.append(SR / k)
    f = np.array(out)
    out = f.copy()
    for i in range(2, len(f) - 2):
        blk = f[i - 2 : i + 3]
        v = blk[blk > 0]
        out[i] = np.median(v) if len(v) >= 2 else 0
    return out


def stats(path: str) -> dict:
    x = load_pcm(path)
    f = f0_track(x)
    v = f[f > 0]
    base = {
        "file": os.path.relpath(path, ROOT),
        "dur": round(len(x) / SR, 2),
        "voiced_s": round(len(v) * 0.01, 2),
    }
    if len(v) < 10:
        base.update(sustain=0, sustain_s=0.0, prange=0.0, std=0.0, f0=0)
        return base
    st = 12 * np.log2(v / 55.0)
    sustained, sustain_frames, run, run_last = 0, 0, 0, None
    for s in st:
        if run and run_last is not None and abs(s - run_last) < 0.6:
            run += 1
        else:
            if run * 0.01 >= 0.25:
                sustained += 1
                sustain_frames += run
            run = 1
        run_last = s
    if run * 0.01 >= 0.25:
        sustained += 1
        sustain_frames += run
    base.update(
        sustain=sustained,
        sustain_s=round(sustain_frames * 0.01, 2),
        prange=round(float(np.percentile(st, 95) - np.percentile(st, 5)), 1),
        std=round(float(st.std()), 1),
        f0=round(float(np.median(v))),
    )
    return base


def main() -> int:
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "evidence", "singing_mode")
    files = sorted(glob.glob(os.path.join(d, "*.wav")) + glob.glob(os.path.join(d, "*.mp3")))
    if not files:
        print("没有找到音频：", d)
        return 2
    lines = [
        "唱歌模式音频分析（ffmpeg 解码 + 自相关基频，唱 > 长音段/音高跨度 明显更高）",
        f"时间：{time.strftime('%F %T')}",
        "",
        f"{'文件':52} {'时长s':>6} {'中位F0':>6} {'音高跨度':>8} {'起伏std':>7} {'长音段':>5} {'长音秒':>6} {'判定':>4}",
    ]
    for p in files:
        s = stats(p)
        verdict = "唱" if (s["dur"] >= 20 and s["sustain"] >= 5 and s["prange"] >= 12) else "念/说话"
        line = (
            f"{s['file']:52} {s['dur']:>6} {s['f0']:>6} {s['prange']:>8} {s['std']:>7} "
            f"{s['sustain']:>5} {s['sustain_s']:>6} {verdict:>4}"
        )
        print(line, flush=True)
        lines.append(line)
    os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
    with open(OUT_LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("-> " + OUT_LOG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
