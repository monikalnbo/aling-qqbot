#!/usr/bin/env python3
"""旋律贴合度度量：把「目标旋律」和「合成结果」都转成音符序列后比对。

用途：哪家唱歌服务/本地引擎接进来，都用同一把尺子验收。
输入是音频（wav/mp3 都行，内部用 ffmpeg 转 16k 单声道），输出两份笔记谱 +
贴合度指标：

  * 音符序列：稳定段（≥120ms、音高变化 <1 半音）合并成一个音符，记 起点/时长/音高
  * 贴合度：把两段各自按时间归一化到 24 格，逐格比中位音高（相对各自的调中心），
    统计 ±1 半音内的比例、平均绝对误差（半音）
  * 判定：±1 半音比例 ≥0.7 且平均误差 ≤0.8 → 认为旋律贴合

用法：
    python3 -I scripts/melody_match.py 目标音频 合成音频 [输出json]
不给参数时，跑 evidence/sing_melody_probe/ 里的默认对照组。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SR = 16000
PROBE = os.path.join(ROOT, "evidence", "sing_melody_probe")


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
        fr = x[i:i + W]
        if np.sqrt((fr ** 2).mean()) < 0.008:
            out.append(0.0)
            continue
        fr = (fr - fr.mean()) * np.hanning(len(fr))
        ac = np.correlate(fr, fr, "full")[W - 1:]
        ac = ac / ac[0] if ac[0] > 0 else ac
        lo, hi = int(SR / fmax), int(SR / fmin)
        seg = ac[lo:hi]
        if not len(seg) or seg.max() < 0.35:
            out.append(0.0)
            continue
        pk = [k for k in range(1, len(seg) - 1)
              if seg[k] > seg[k - 1] and seg[k] >= seg[k + 1] and seg[k] >= 0.85 * seg.max()]
        k = (pk[0] if pk else int(np.argmax(seg))) + lo
        out.append(SR / k)
    f = np.array(out)
    med = f.copy()
    for i in range(2, len(f) - 2):
        blk = f[i - 2:i + 3]
        v = blk[blk > 0]
        med[i] = np.median(v) if len(v) >= 2 else 0
    return med


def semis(f: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        s = 12 * np.log2(np.maximum(f, 1e-6) / 55.0)
    s[f <= 0] = np.nan
    return s


def notes(path: str, hop_s: float = 0.01, min_len_s: float = 0.12) -> dict:
    s = semis(f0_track(load_pcm(path)))
    # 按 1 半音量化，聚成连续同音段
    q = np.where(np.isfinite(s), np.round(s), np.nan)
    segs = []
    start = None
    cur = None
    for i, v in enumerate(q):
        if not np.isfinite(v):
            if start is not None:
                segs.append((start, i, cur))
                start, cur = None, None
            continue
        if start is None:
            start, cur = i, v
        elif abs(v - cur) <= 1:
            cur = (cur + v) / 2
        else:
            segs.append((start, i, cur))
            start, cur = i, v
    if start is not None:
        segs.append((start, len(q), cur))
    out = []
    for a, b, p in segs:
        dur = (b - a) * hop_s
        if dur < min_len_s:
            continue
        out.append({"onset": round(a * hop_s, 2), "dur": round(dur, 2), "midi": int(round(p + 12))})
    # 合并相邻同音
    merged = []
    for n in out:
        if merged and merged[-1]["midi"] == n["midi"] and \
                abs(merged[-1]["onset"] + merged[-1]["dur"] - n["onset"]) < 0.02:
            merged[-1]["dur"] = round(merged[-1]["dur"] + n["dur"], 2)
        else:
            merged.append(n)
    return {"dur": round(len(q) * hop_s, 2), "notes": merged}


def grid(path: str, n: int = 24) -> np.ndarray:
    s = semis(f0_track(load_pcm(path)))
    idx = np.linspace(0, len(s), n + 1).astype(int)
    g = []
    for i in range(n):
        blk = s[idx[i]:idx[i + 1]]
        v = blk[np.isfinite(blk)]
        g.append(float(np.median(v)) if len(v) else np.nan)
    return np.array(g)


def compare(target: str, cand: str, n: int = 24) -> dict:
    a, b = grid(target, n), grid(cand, n)
    both = np.isfinite(a) & np.isfinite(b)
    if both.sum() < n * 0.3:
        return {"target": target, "cand": cand, "error": "有声帧太少"}
    a2, b2 = a[both], b[both]
    # 去掉各自的调性偏移（只看旋律走向，不看整体高低）
    a2 = a2 - np.median(a2)
    b2 = b2 - np.median(b2)
    # 估音高时会有整八度误判，允许整体平移若干个八度后取最好的一组
    best, best_k = None, 0
    for k in (-24, -12, 0, 12, 24):
        d = np.abs(a2 - (b2 + k))
        if best is None or d.mean() < best.mean():
            best, best_k = d, k
    d = best
    return {
        "target": os.path.basename(target),
        "cand": os.path.basename(cand),
        "cells": int(both.sum()),
        "octave_shift": int(best_k // 12),
        "within_1st": round(float((d <= 1).mean()), 2),
        "mae_st": round(float(d.mean()), 2),
        "verdict": "贴合" if (d <= 1).mean() >= 0.7 and d.mean() <= 0.8 else "不贴合",
    }


def main() -> int:
    if len(sys.argv) >= 3:
        t, c = sys.argv[1], sys.argv[2]
        res = compare(t, c)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        if len(sys.argv) > 3:
            with open(sys.argv[3], "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
        return 0

    pairs = [
        ("参考旋律 vs 按参考旋律生成", "D_ref_source.mp3", "D_ref_audio.mp3"),
        ("同请求重复两次", "A_control_1.mp3", "A_control_2.mp3"),
        ("同样歌词点名两只老虎 vs 生日快乐", "C_prompt_twotiger.mp3", "C_prompt_birthday.mp3"),
        ("同歌词 默认 vs 点名两只老虎", "A_control_1.mp3", "C_prompt_twotiger.mp3"),
    ]
    lines = ["旋律贴合度比对（±1 半音内比例 ≥0.7 且平均误差 ≤0.8 半音判为贴合）", ""]
    for name, x, y in pairs:
        px, py = os.path.join(PROBE, x), os.path.join(PROBE, y)
        if not (os.path.exists(px) and os.path.exists(py)):
            lines.append(f"{name}: 缺文件")
            continue
        r = compare(px, py)
        lines.append(f"{name}: 格数 {r.get('cells')}  八度平移 {r.get('octave_shift')}  "
                     f"±1半音 {r.get('within_1st')}  平均误差 {r.get('mae_st')} 半音  → {r.get('verdict')}")
    n = notes(os.path.join(PROBE, "D_ref_source.mp3"))
    lines.append("")
    lines.append(f"参考旋律提取到的音符数：{len(n['notes'])}（前 12 个：{n['notes'][:12]}）")
    txt = "\n".join(lines)
    print(txt)
    out = os.path.join(ROOT, "evidence", "melody_match.log")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\nsaved", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
