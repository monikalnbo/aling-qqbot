"""单元验证插件出口兜底：`_strip_qq_from_reply` 把念出来的 QQ 号删掉，不误伤正常文本。

直接 import 插件类（不启动 AstrBot），构造 LLMResponse 调私有方法。
在 AstrBot 容器里跑（容器有 astrbot 依赖）：

    docker exec astrbot python3 /AstrBot/data/plugins/.../verify_reply_guard.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/AstrBot/data/plugins")
sys.path.insert(0, "/AstrBot")

from astrbot.api.provider import LLMResponse  # noqa: E402
from astrbot_plugin_aling_memory.main import AlingMemoryPlugin  # noqa: E402

CASES = [
    # (输入, 期望输出)
    ("<你的管理员QQ号> 你刚说什么", "你刚说什么"),
    ("<你的管理员QQ号>你刚说什么", "你刚说什么"),
    ("小胖（222333）说想吃串串", "小胖说想吃串串"),
    ("阿哲，333444，别再问了", "阿哲，，别再问了"),
    ("222333 你刚说什么", "你刚说什么"),
    ("杂口杂口。。。", "杂口杂口。。。"),                       # 无数字，原样
    ("现在是 2026-11-11 22:20:38，该睡了。", None),             # 日期/时间不许动
    ("我 12 点就睡了，明天 8 点叫你。", None),                  # 短数字不许动
    ("她说 12345 遍都想吃串串", "她说 遍都想吃串串"),            # 5 位 QQ 型数字删除
    ("*翻了个身* 小胖，222333。", "*翻了个身* 小胖，。"),          # 标点保留
]


def main() -> int:
    import inspect

    plugin = object.__new__(AlingMemoryPlugin)
    plugin.cfg = {"reply_strip_qq": True}
    fn = plugin._strip_qq_from_reply  # noqa: SLF001
    params = list(inspect.signature(fn).parameters)
    print(f"# _strip_qq_from_reply({', '.join(params)})")
    ok_all = True
    for src, want in CASES:
        resp = LLMResponse(role="assistant", completion_text=src)
        fn(resp, None) if "event" in params else fn(resp)
        got = resp.completion_text
        if want is None:
            ok = got == src
            label = "不变"
        else:
            ok = got == want
            label = f"期望 {want!r}"
        ok_all = ok_all and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {src!r} -> {got!r}  ({label})")
    print("ALL PASS" if ok_all else "HAS FAILURES")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
