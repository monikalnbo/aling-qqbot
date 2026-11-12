# 阿凌 · QQ 聊天机器人

一个黑发红瞳、起床困难、嘴硬心软的雌小鬼兄控少女。私聊直接聊天，群里 @ 她才会理你，回复由 **DeepSeek 官方 API（deepseek-chat）** 生成，带多轮上下文记忆，人设提示词可随时改。

## 📌 本次更新（2026-11-12）：默认语音 + 一句一条 + 换音色

这一版把阿凌从「文字回复」升级成「开口说话」，而且三件事都做成默认、不用教：

| 改动 | 怎么用 |
| --- | --- |
| **默认就是语音** | 不发任何口令，机器人直接发语音；重启也不掉（状态存在插件自己的 `state.json`）。想临时看文字：管理员发「**文字模式**」，想切回来发「**语音模式**」，随时发「**语音状态**」查当前模式。 |
| **一句一条语音** | 复用 AstrBot 原有的分段回复规则拆句，**每一句各自合成一条语音**，按原顺序连发，条间沿用 0.5~1.2 秒随机间隔。`*动作描写*`、`（叹气）` 这类不念：整段只有动作就不发，动作+台词同段只念台词。 |
| **换音色一条命令** | 默认音色 `longanxiaoxin`（龙安小昕，22 岁活泼元气）。想换：`python3 scripts/set_voice.py longanhuan_v3.6`，脚本会改配置并自动重启；不带参数则列出全部 12 个可选音色。想听小样：`evidence/voice_pick/*.mp3`（同一句话、同样音量 100）。 |

实现要点：插件 `plugins/astrbot_plugin_aling_voice/` 在消息发出前把每段文字换成 QQ 语音组件，AstrBot 后端本来就会把多个语音逐条发送并按配置随机间隔，所以顺序和节奏直接复用原链路，没有另造规则。合成失败、超时或超长时会**退回发文字**，不会吞消息。

细节与实测：`docs/运行状态.md` §4.11、`docs/音色清单.md`、`docs/交付报告.md` §九/§十。

---

## ⚠️ 先看这里：技术栈已冻结

交付主线是 **NapCat + AstrBot + DeepSeek**，完整步骤见 **[`docs/上手说明.md`](docs/上手说明.md)**：

```
QQ 小号 ──► NapCat（协议端）──► AstrBot（机器人框架）──► DeepSeek API（大脑）
```

本仓库对应目录：

| 路径 | 作用 |
| --- | --- |
| `astrbot/compose.yml` | 一键起 NapCat + AstrBot 两个容器 |
| `astrbot/.env.example` | 只需填 `DEEPSEEK_API_KEY` |
| `astrbot/persona_aling.txt` | 当前人格「阿凌」，复制到 AstrBot WebUI 的「人格」里（旧「大肥鱼」人格备份在 `backup/`） |
| `plugins/astrbot_plugin_aling_voice/` | 语音回复插件 **1.2.1**：把文字回复换成 QQ 语音（**默认开启、重启保持**，按 AstrBot 原有分段规则一句一条发出；管理员可用「文字模式」临时切回文字；走百炼 SpeechSynthesizer，默认音色 longanxiaoxin（龙安小昕）、volume=100，改音色用 `scripts/set_voice.py`，见 `docs/运行状态.md` §4.11 与 `docs/音色清单.md`） |
| `plugins/astrbot_plugin_aling_memory/` | 记忆插件 **1.2.0**：短期 12 轮 + 6000 字封顶、中期摘要、长期事实、群聊存档与按需回看；每轮上下文比 1.1.0 少 42%~55%（见 `docs/运行状态.md` §4.9） |
| `docs/上手说明.md` | **中文完整上手说明**（扫码、对接、配 Provider、排错） |
| `docs/实测记录.md` 第 0 节 | 这条链路的实测证据与踩坑 |
| `docs/交付报告.md` | 结论、证据摘要、未完成项（一页看完） |
| `docs/运行状态.md` | **实机运行状态**：容器/端口/token/密码、二维码刷新命令、待办与限制 |
| `scripts/qr_portal_server.py` | **扫码登录实时看板**：浏览器开着页面，二维码自动换新、登录成功变大字提示（见 `docs/上手说明.md` §12.7） |

> 栈已经在跑（`astrbot/` 下 `docker compose ps` 可见 `napcat`、`astrbot`），扫码和换 Key 的具体操作见 [`docs/运行状态.md`](docs/运行状态.md)。

> **API Key 只从 `.env` 或环境变量读取**（AstrBot 侧写 `$DEEPSEEK_API_KEY` 引用），代码和配置里都没有明文密钥，`.env` / `.astrbot/` / `astrbot/data/` 均已被 `.gitignore` 忽略。

下面第二部分是**更早的 NoneBot2 自研实现**（`bot.py` + `dafeiyu/` 插件，26 个单元测试），作为对照版本保留、仍可独立运行；如果你要按冻结路线走，直接看 [`docs/上手说明.md`](docs/上手说明.md) 即可。

---

## 附：NoneBot2 自研版本

- 框架：**NoneBot2** + `nonebot-adapter-onebot`（OneBot v11）
- 协议端：**NapCatQQ**（反向 WebSocket 连进来）
- 大脑：**DeepSeek**（OpenAI 兼容接口）
- 选型依据、各框架对比见 [`docs/选型对比.md`](docs/选型对比.md)，调研过程见 [`docs/调研报告.md`](docs/调研报告.md)，实测记录见 [`docs/实测记录.md`](docs/实测记录.md)

---

## 一、整体结构

```
QQ 客户端（你的手机/电脑）
        │  NTQQ 协议
        ▼
NapCatQQ（协议端，扫码登录 QQ，开一个反向 WS 服务）
        │  ws://127.0.0.1:8080/onebot/v11/ws  （OneBot v11）
        ▼
本项目 bot.py ──► dafeiyu 插件 ──► 记忆 MemoryStore
                                      │
                                      └──► DeepSeek API（deepseek-chat）
```

目录说明：

| 路径 | 作用 |
| --- | --- |
| `bot.py` | 启动入口：初始化 NoneBot、注册 OneBot 适配器、加载插件 |
| `dafeiyu/__init__.py` | 插件入口，收消息、判断是否该回、发消息 |
| `dafeiyu/core.py` | 回复逻辑（私聊/群聊判定、@ 处理、重置/帮助、人设拼装） |
| `dafeiyu/llm.py` | DeepSeek 客户端（httpx、重试、中文报错） |
| `dafeiyu/memory.py` | 多轮记忆（TTL 过期 + LRU 上限，按用户隔离） |
| `dafeiyu/config.py` | 读取 `DAFEIYU_*` 配置 |
| `dafeiyu/app.py` | 把上面几块组装起来 |
| `config/persona.txt` | 人设提示词，**改这个文件就换性格** |
| `tests/` | 26 个单元测试 |
| `scripts/selfcheck.py` | 配置自检 / 真调 DeepSeek 验证 |
| `scripts/e2e_mock.py` | 不连 QQ 的全链路自测（假 NapCat） |

---

## 二、装依赖

需要 **Python 3.10 或更高**。

```bash
pip install -r requirements.txt
```

> 建议先建虚拟环境（可选）：
>
> ```bash
> python -m venv .venv
> source .venv/bin/activate     # Windows：.venv\Scripts\activate
> pip install -r requirements.txt
> ```

## 三、填 DeepSeek Key（唯一必填项）

1. 打开 <https://platform.deepseek.com/api_keys>，创建一个 Key（形如 `sk-xxxxxxxx`）。
2. 复制示例配置：

   ```bash
   cp .env.example .env
   ```

3. 编辑 `.env`，把 Key 填到这一行：

   ```dotenv
   DAFEIYU_DEEPSEEK_API_KEY=sk-你的key
   ```

4. 自检（不联网，只看配置）：

   ```bash
   python scripts/selfcheck.py
   ```

   看到 `✅ 配置检查通过` 即可。想顺手验证 Key 真的能用：

   ```bash
   python scripts/selfcheck.py --call
   ```

   > 也可以用环境变量代替 `.env`：`export DAFEIYU_DEEPSEEK_API_KEY=sk-xxx`（Windows PowerShell 用 `$env:DAFEIYU_DEEPSEEK_API_KEY="sk-xxx"`）。环境变量优先于 `.env`。

## 四、装 NapCat 并扫码登录 QQ

这一步是**用你自己的 QQ 号登录协议端**，必须在自己的机器上做（我这边无法代扫）。

1. 按官方文档安装 NapCatQQ：<https://napneko.github.io/>（Windows 一般下载 `NapCat.Shell.zip`，Linux 可用一键脚本或 Docker，以官网当前说明为准）。
2. 启动 NapCat，它会拉起 NTQQ。打开它的 WebUI：<http://127.0.0.1:6099>（首次启动会在终端打印 token，用 token 登录）。
3. 在 WebUI 的「QQ 登录」页选择**扫码登录**，用手机 QQ 扫码确认。
4. 打开 WebUI 的「网络配置」→ 新建 **WebSocket 客户端（反向 WS）**，填写：
   - URL：`ws://127.0.0.1:8080/onebot/v11/ws`
   - 消息格式：`array`（也兼容 string）
   - Token：与 `.env` 里的 `ONEBOT_ACCESS_TOKEN` 保持一致；都留空也可以（本机使用）
5. 保存并**启用**这个连接。NapCat 侧显示已连接，就说明协议端就绪了。

> 注意：本项目监听的是 **8080** 端口，必须先把 `bot.py` 跑起来，NapCat 才能连上。

## 五、启动

```bash
python bot.py
```

正常情况下你会看到 NoneBot 启动日志，以及 `OneBot V11` 适配器已加载。然后在 QQ 里测试：

- **私聊**：直接给机器人 QQ 发消息，例如「你好呀」。
- **群聊**：`@大肥鱼 在吗`，或者消息以「大肥鱼」开头，例如「大肥鱼今天吃啥」。
- **群里没 @ 也没喊名字**：不会插话，避免刷屏。
- **重置记忆**：发送「重置」「清空记忆」「忘记刚才」「重新开始」任一。
- **看用法**：发送「帮助」或「help」。

## 六、配置项（都在 `.env`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `DAFEIYU_DEEPSEEK_API_KEY` | 空 | **必填**，DeepSeek Key |
| `DAFEIYU_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 用中转站时改这里（需兼容 `/chat/completions`） |
| `DAFEIYU_DEEPSEEK_MODEL` | `deepseek-chat` | 想用推理模型可改 `deepseek-reasoner` |
| `DAFEIYU_TEMPERATURE` | `0.8` | 0~2，越大越放飞 |
| `DAFEIYU_MAX_TOKENS` | `800` | 单次回复上限 |
| `DAFEIYU_PERSONA_FILE` | `config/persona.txt` | 人设文件路径 |
| `DAFEIYU_PERSONA_INLINE` | 空 | 填了就直接用它当人设（优先级最高） |
| `DAFEIYU_NICKNAME` | `["大肥鱼"]` | 昵称，写进人设 |
| `DAFEIYU_GROUP_TRIGGER_KEYWORDS` | `["大肥鱼"]` | 群聊里以这些词开头就算呼叫 |
| `DAFEIYU_MAX_HISTORY_TURNS` | `10` | 每个会话保留最近多少轮 |
| `DAFEIYU_HISTORY_TTL_MINUTES` | `180` | 多久没说话就忘掉（分钟） |
| `DAFEIYU_MAX_REPLY_CHARS` | `1500` | 回复超长会截断 |
| `DAFEIYU_SHOW_TYPING` | `true` | 调用模型前发「正在输入」 |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | NoneBot 监听地址，需与 NapCat 的 URL 对应 |
| `ONEBOT_ACCESS_TOKEN` | 空 | 与 NapCat 侧的 Token 一致；对公网开放时务必设置 |

列表类配置用 JSON 写法，例如多个触发词：

```dotenv
DAFEIYU_GROUP_TRIGGER_KEYWORDS=["大肥鱼","肥鱼"]
```

换人设：直接改 `config/persona.txt`（身份、性格、说话风格、边界都在里面，文件有注释），改完重启即可，不用动代码。

## 七、不装 QQ 也能验证

```bash
# 26 个单元测试（记忆、LLM 客户端、回复逻辑、插件行为）
python -m pytest tests -q

# 全链路自测：真实启动 NoneBot + 假 NapCat 通过真实 WebSocket 连上来
# 覆盖：私聊回复、群聊不@不回、群聊@回复、群聊触发词、多轮上下文
python scripts/e2e_mock.py

# 用 .env 里的真 Key 跑一遍全链路
python scripts/e2e_mock.py --real
```

## 八、常见问题

**启动报「未配置 DAFEIYU_DEEPSEEK_API_KEY」**
`.env` 没建或 Key 没填。确认文件在**项目根目录**（与 `bot.py` 同级），且变量名是 `DAFEIYU_DEEPSEEK_API_KEY`。

**NapCat 显示连不上 8080**
先确认 `python bot.py` 正在运行；再确认 URL 是 `ws://127.0.0.1:8080/onebot/v11/ws`（路径不能少）；如果机器人跑在 Docker/另一台机器，把 `127.0.0.1` 换成对应主机 IP，并保证 `HOST=0.0.0.0`。

**NapCat 连上了，但发消息没反应**
看 `bot.py` 控制台日志。群聊必须是 @ 或触发词开头；检查 `.env` 里 `DAFEIYU_GROUP_TRIGGER_KEYWORDS` 是否被改坏（JSON 格式）。

**回复「鱼这边连不上大脑了…」**
说明 DeepSeek 请求失败，错误原因会写在括号里：Key 无效（401）、余额不足、网络超时、Base URL 写错等，按提示改 `.env` 后重启。

**端口 8080 被占用**
改 `.env` 里的 `PORT`，同时把 NapCat 的 WebSocket URL 改成新端口。

**群聊里 @ 了但还是不回**
确认 @ 的是机器人本人的 QQ（不是群名片/别人）；OneBot 事件里的 `to_me` 依赖协议端正确上报。

**记忆什么时候丢**
进程重启会清空；闲置超过 `DAFEIYU_HISTORY_TTL_MINUTES` 也会清空；群里每个用户独立记忆，互不串台。

## 九、安全与合规

- `.env`、`.env.prod` 等均已加入 `.gitignore`，**不要把 Key 提交到仓库**；泄露后立刻去平台吊销重建。
- 机器人建议只在自己可控的账号/群里使用，遵守腾讯 QQ 的用户协议与相关法律法规；频繁群发、营销、骚扰可能被封号。
- 想开放到公网时，请设置 `ONEBOT_ACCESS_TOKEN` 并配置防火墙，不要让 8080 裸奔。
