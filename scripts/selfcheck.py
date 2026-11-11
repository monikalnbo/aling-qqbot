"""配置自检 / 真实 DeepSeek 连通性检查。

用法：
    python scripts/selfcheck.py          # 只检查配置（不联网）
    python scripts/selfcheck.py --call   # 用 .env 里的 Key 真调一次 DeepSeek
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 插件包导入时会注册 NoneBot 生命周期钩子，这里先做一次轻量初始化
import nonebot  # noqa: E402

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")

from dafeiyu.config import get_config  # noqa: E402
from dafeiyu.core import load_persona  # noqa: E402
from dafeiyu.llm import DeepSeekClient, DeepSeekError  # noqa: E402


def mask(key: str) -> str:
    if not key:
        return "(未配置)"
    return key[:6] + "*" * max(0, len(key) - 10) + key[-4:] if len(key) > 10 else "***"


def main() -> int:
    conf = get_config()
    print("=== 大肥鱼配置自检 ===")
    print(f"DeepSeek Key : {mask(conf.deepseek_api_key)}")
    print(f"Base URL     : {conf.deepseek_base_url}")
    print(f"模型          : {conf.deepseek_model}")
    print(f"人设文件      : {conf.persona_file}")
    print(f"人设预览      : {load_persona(conf, ROOT)[:80]}...")
    print(f"群聊触发词    : {conf.group_trigger_keywords}")
    print(f"记忆轮数/过期 : {conf.max_history_turns} 轮 / {conf.history_ttl_minutes} 分钟")

    problems = []
    if not conf.deepseek_api_key:
        problems.append("未填写 DAFEIYU_DEEPSEEK_API_KEY（把 .env.example 复制成 .env 再填）")
    if conf.deepseek_api_key and not conf.deepseek_api_key.startswith("sk-"):
        problems.append("Key 不是以 sk- 开头，请确认是从 platform.deepseek.com 复制的")

    if problems:
        print("\n⚠️ 待处理：")
        for p in problems:
            print("  -", p)
        if not ("--call" in sys.argv and conf.deepseek_api_key):
            return 1

    if "--call" in sys.argv:
        print("\n>>> 真实调用 DeepSeek ...")
        client = DeepSeekClient(
            conf.deepseek_api_key,
            base_url=conf.deepseek_base_url,
            model=conf.deepseek_model,
            timeout=conf.request_timeout,
        )
        messages = DeepSeekClient.build_messages(
            load_persona(conf, ROOT), [], "你好，用一句话介绍一下你自己"
        )
        try:
            reply = asyncio.run(client.chat(messages))
        except DeepSeekError as exc:
            print("❌ 调用失败：", exc)
            return 2
        print("✅ DeepSeek 回复：", reply)
        return 0

    print("\n✅ 配置检查通过（如需验证 Key，运行 python scripts/selfcheck.py --call）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
