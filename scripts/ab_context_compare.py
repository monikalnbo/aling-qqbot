"""上下文裁剪 A/B 对照：同一份历史，分别喂给旧版(1.1.0)和新版(1.2.0)插件，
量最终真正发给模型的上下文体积。

不连网、不调模型：provider 置空，只比较窗口裁剪结果。

用法：python3 scripts/ab_context_compare.py --out evidence/ctx_ab.log
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_plugin(pkg_dir: Path, mod_name: str):
    """按包结构加载插件 main.py（它用了相对导入 .store）。"""
    pkg = types.ModuleType(mod_name)
    pkg.__path__ = [str(pkg_dir)]
    sys.modules[mod_name] = pkg
    for sub in ("store",):
        spec = importlib.util.spec_from_file_location(
            f"{mod_name}.{sub}", pkg_dir / f"{sub}.py"
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"{mod_name}.{sub}"] = m
        spec.loader.exec_module(m)
    spec = importlib.util.spec_from_file_location(
        f"{mod_name}.main", pkg_dir / "main.py"
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"{mod_name}.main"] = m
    spec.loader.exec_module(m)
    return m


class FakeEvent:
    unified_msg_origin = "napcat:GroupMessage:990001"
    message_str = "在吗"

    def get_sender_id(self):
        return "222333"

    def get_sender_name(self):
        return "小胖"

    def get_group_id(self):
        return "990001"

    def get_message_type(self):
        return None


class FakeReq:
    def __init__(self, contexts):
        self.contexts = contexts
        self.system_prompt = "S" * 1500
        self.extra_user_content_parts = []
        self.prompt = "在吗"


class FakeCtx:
    async def get_using_provider_async(self, umo):
        return None


def build_history(turns: int, user_len: int, bot_len: int) -> list[dict]:
    ctx: list[dict] = [{"role": "system", "content": "S" * 1500}]
    for i in range(turns):
        ctx.append({"role": "user", "content": f"第{i}轮：" + "啊" * user_len})
        ctx.append({"role": "assistant", "content": "*动作*\n" + "嗯" * bot_len})
    return ctx


def size_of(req) -> int:
    return sum(len(str(m.get("content", ""))) for m in req.contexts)


async def run_one(mod, cfg: dict, history, db_dir: Path) -> tuple[int, int, int]:
    plugin = object.__new__(mod.AlingMemoryPlugin)
    plugin.cfg = dict(mod.DEFAULT_CONFIG)
    plugin.cfg.update(cfg)
    plugin.store = mod.MemoryStore(db_dir / "t.db")
    plugin._inflight = set()
    plugin._tasks = set()
    plugin._last_umo = ""
    plugin.context = FakeCtx()
    req = FakeReq([dict(m) for m in history])
    await plugin.on_llm_request(FakeEvent(), req)  # type: ignore[arg-type]
    plugin.store.close()
    return size_of(req), len(req.contexts), sum(
        len(getattr(p, "text", "")) for p in req.extra_user_content_parts
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="backup/ctx_trim_20261111_231043")
    ap.add_argument("--new", default="plugins/astrbot_plugin_aling_memory")
    ap.add_argument("--out", default="evidence/ctx_ab.log")
    args = ap.parse_args()

    old_dir = (ROOT / args.old).resolve()
    new_dir = (ROOT / args.new).resolve() if not os.path.isabs(args.new) else Path(args.new)
    old = load_plugin(old_dir, "oldplug")
    new = load_plugin(new_dir, "newplug")

    cases = [
        ("20 轮 × 短消息(20字)", 20, 20, 40),
        ("20 轮 × 中消息(200字)", 20, 200, 300),
        ("20 轮 × 长消息(1200字)", 20, 1200, 1500),
        ("40 轮 × 长消息(1200字)", 40, 1200, 1500),
        ("60 轮 × 混合长短", 60, 600, 900),
        ("200 轮 × 中消息(200字)", 200, 200, 300),
    ]
    lines = ["# 上下文裁剪 A/B：旧版(1.1.0) vs 新版(1.2.0)", ""]
    lines.append(f"{'场景':<24}{'旧版字符':>10}{'新版字符':>10}{'降幅':>9}{'旧条数':>8}{'新条数':>8}")
    with tempfile.TemporaryDirectory() as td:
        for name, turns, ulen, blen in cases:
            hist = build_history(turns, ulen, blen)
            ob, oc, _ = await run_one(old, {}, hist, Path(td) / "o")
            nb, nc, _ = await run_one(new, {}, hist, Path(td) / "n")
            ratio = (1 - nb / ob) * 100 if ob else 0
            lines.append(f"{name:<24}{ob:>10,}{nb:>10,}{ratio:>8.0f}%{oc:>8}{nc:>8}")
    out = "\n".join(lines) + "\n"
    print(out)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    (ROOT / args.out).write_text(out, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
