"""最小场景：A 说一句事实，换会话再问，检查抽取与注入。"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import pathlib
import sys
import time

spec = importlib.util.spec_from_file_location(
    "vm", pathlib.Path(__file__).with_name("verify_memory.py")
)
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


async def main(url: str) -> int:
    import websockets

    headers = {"X-Self-ID": str(vm.SELF_ID), "X-Client-Role": "Universal"}
    async with websockets.connect(url, additional_headers=headers, max_size=2**22) as ws:
        cli = vm.Client(ws)
        task = asyncio.create_task(cli.reader())
        await ws.send(
            '{"time": %d, "self_id": %d, "post_type": "meta_event", '
            '"meta_event_type": "lifecycle", "sub_type": "connect"}'
            % (int(time.time()), vm.SELF_ID)
        )
        await asyncio.sleep(1.2)
        g1 = 770001 + int(time.time()) % 500
        g2 = g1 + 1
        r1 = await cli.say(
            vm.at_event(
                "记住，我家的猫叫年糕，是只橘猫",
                message_id=vm.fresh_id(1),
                group_id=g1,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        print("[1] 小胖说完 ->", r1, flush=True)
        await asyncio.sleep(4)
        r2 = await cli.say(
            vm.at_event(
                "我家的猫叫什么名字？",
                message_id=vm.fresh_id(2),
                group_id=g2,
                user_id=222333,
                nickname="小胖",
                card="小胖",
            )
        )
        print("[2] 新会话再问 ->", r2, flush=True)
        print(f"groups={g1},{g2}", flush=True)
        task.cancel()
    return 0 if r2 and "年糕" in r2 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url)))
