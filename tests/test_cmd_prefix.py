"""`/唱歌 歌名` 参数解析单元测试（不加载 AstrBot，只测纯函数）。

跑法：python3 -m pytest tests/test_cmd_prefix.py -q
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_aling_voice" / "main.py"


def _load_helper():
    """从 main.py 里只取出 _WAKE_PREFIXES / _strip_cmd_prefix，避免 import 整个 AstrBot。"""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    ns: dict = {}
    picked = [
        node
        for node in tree.body
        if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_WAKE_PREFIXES")
        or (isinstance(node, ast.FunctionDef) and node.name == "_strip_cmd_prefix")
    ]
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(MAIN), "exec"), ns)
    return ns["_strip_cmd_prefix"]


strip = _load_helper()

CASES = [
    # 群聊 @ 机器人时 message_str 里可能仍带着唤醒前缀
    ("/唱歌 两只老虎", "两只老虎"),
    ("唱歌 两只老虎", "两只老虎"),
    ("／唱歌 两只老虎", "两只老虎"),
    ("!唱歌 两只老虎", "两只老虎"),
    ("！唱歌 两只老虎", "两只老虎"),
    # 无参数 / 只有标点
    ("/唱歌", ""),
    ("唱歌", ""),
    ("/唱歌：", ""),
    # 歌名里带书名号或普通前后缀
    ("/唱歌 《小星星》", "《小星星》"),
    ("/唱歌：生日快乐", "生日快乐"),
    ("/唱歌，来一首茉莉花", "来一首茉莉花"),
        # 歌名本身以「唱」开头时，不能把指令名再切一次
    ("/唱歌 唱歌的孩子", "唱歌的孩子"),
    ("/唱歌 唱一首歌", "唱一首歌"),
    # 开关词不能被吃掉
    ("/唱歌 关闭", "关闭"),
    ("/唱歌 开启", "开启"),
    # 说话里出现「唱歌」二字不能被当指令前缀误删
    ("/唱歌 教我唱歌", "教我唱歌"),
]


def test_cmd_sing_arg_parsing():
    bad = [(raw, want, strip(raw, "唱歌")) for raw, want in CASES if strip(raw, "唱歌") != want]
    assert not bad, bad


if __name__ == "__main__":
    for raw, want in CASES:
        print(f"{raw!r:28} -> {strip(raw, '唱歌')!r}  {'OK' if strip(raw, '唱歌') == want else 'FAIL want ' + repr(want)}")
    sys.exit(0)
