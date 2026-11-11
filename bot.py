"""大肥鱼启动入口：python bot.py

使用 NoneBot2 + OneBot v11 反向 WebSocket 连接 NapCat / LLOneBot 等协议端。
"""

from __future__ import annotations

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)

# 加载大肥鱼插件（本仓库自带，无需 pip 安装）
nonebot.load_plugin("dafeiyu")

if __name__ == "__main__":
    nonebot.run()
