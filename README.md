# 阿凌 · QQ 聊天机器人（NapCat + AstrBot + DeepSeek）

一个说话懒散、嘴硬心软、**会一句一句分条发消息**、**在群里分得清谁是谁**、**记得住你**的 QQ 机器人。群里 @ 她才理人，私聊直接聊。

技术栈是市面上目前最省事的一套：

```
你的手机 QQ  ──►  NapCat（协议端，负责登录 QQ）  ──►  AstrBot（机器人框架）  ──►  DeepSeek API（大脑）
```

本仓库是这个机器人的**完整可运行项目 + 全部改动记录 + 实测证据**，按下面的教程从零就能跑起来。

> 本项目里所有账号、群号、密码、Key 都是**占位符**。文中形如 `<你的机器人QQ号>`、`<你的管理员QQ号>` 的地方，都要换成你自己的，见 [三、把占位符换成你自己的](#三把占位符换成你自己的)。

---

## 目录

- [一、为什么选这套（选型结论）](#一为什么选这套选型结论)
- [二、部署结构](#二部署结构)
- [三、把占位符换成你自己的](#三把占位符换成你自己的)
- [四、从零部署（小白版）](#四从零部署小白版)
- [五、私聊 / 群聊的触发规则](#五私聊--群聊的触发规则)
- [六、改动记录：每一处改了什么](#六改动记录每一处改了什么)
  - [6.1 角色卡「阿凌」](#61-角色卡阿凌)
  - [6.2 用户识别 / 认人](#62-用户识别--认人)
  - [6.2b 称呼只用昵称，不念 QQ 号](#62b-称呼只用昵称不念-qq-号)
  - [6.3 身份锚点 + 三层记忆插件](#63-身份锚点--三层记忆插件)
  - [6.4 分条说话](#64-分条说话)
  - [6.5 管理员权限](#65-管理员权限)
- [七、验证：怎么自己复现](#七验证怎么自己复现)
- [八、仓库结构](#八仓库结构)
- [九、文档索引](#九文档索引)
- [十、安全与合规](#十安全与合规)
- [附：更早的 NoneBot2 自研实现](#附更早的-nonebot2-自研实现)

---

## 一、为什么选这套（选型结论）

结论先说：**协议端用 NapCat，应用层用 AstrBot，大脑用 DeepSeek 官方 API。**

| 层 | 选了什么 | 为什么不选别的 |
| --- | --- | --- |
| 协议端（登录 QQ、收发消息） | **NapCat**（OneBot v11 反向 WS） | 2026 年 QQ 官方机器人开放平台不面向个人小号；NapCat 最活跃、Docker 镜像现成、扫码即用。同类里有 LLOneBot / Lagrange，生态和更新频率都不如它 |
| 机器人框架（应用层） | **AstrBot v4.29.x** | 自带 WebUI、人格、多轮上下文、指令权限、分段回复、插件市场。用 NoneBot2 自己写也能跑，但上面这些要自己补，本仓库的 `dafeiyu/` 就是那个对照版本 |
| 大脑 | **DeepSeek 官方 API（deepseek-chat）** | 中文便宜、OpenAI 兼容接口，换别家只改 base_url 与 model |

**活跃度与许可证**以你部署时的官方仓库为准；完整的横向对比（9 个项目实测数据、什么情况下该换方案）见 [`docs/选型对比.md`](docs/选型对比.md) 与 [`docs/调研报告.md`](docs/调研报告.md)。

> 一句话权衡：**协议端必须用第三方实现**（NapCat），**框架层用现成的**（AstrBot），**只把个性化部分（人设、记忆、分条）做成配置或插件**——这是本仓库所有改动遵循的原则。

---

## 二、部署结构

```
┌─────────────────────────── docker compose（astrbot/compose.yml） ───────────────────────────┐
│                                                                                             │
│   napcat 容器                                 astrbot 容器                                   │
│   ├─ 扫码登录 QQ 小号                         ├─ WebUI  http://127.0.0.1:6185              │
│   ├─ WebUI  http://127.0.0.1:6099             ├─ 反向 WS 端点 ws://astrbot:6199/ws           │
│   ├─ 正向 WS :3001（调试用）                  ├─ 人格 / 上下文 / 指令 / 插件                 │
│   └─ 反向 WS 客户端 ─────── ws://astrbot:6199/ws ──────►                                      │
│                                               └─ provider deepseek-chat ──► api.deepseek.com │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

| 路径 | 作用 |
| --- | --- |
| `astrbot/compose.yml` | 一键起 NapCat + AstrBot（同一 Docker 网络，反向 WS 直接走容器名） |
| `astrbot/.env.example` | 唯一必填 `DEEPSEEK_API_KEY`；另有两个 NapCat 运行 UID/GID |
| `astrbot/persona_aling.txt` | **阿凌角色卡原文**，复制进 WebUI 或脚本导入 |
| `astrbot/data/astrbot/cmd_config.json` | AstrBot 的**示例配置**（已把本项目的改动都配好，密码/管理员号是占位符） |
| `astrbot/data/astrbot/config/astrbot_plugin_aling_memory_config.json` | 记忆插件配置示例 |
| `plugins/astrbot_plugin_aling_memory/` | 自研三层记忆插件源码 |
| `scripts/` | 离线自测、分段抓帧、权限/记忆验证脚本 |
| `evidence/` | 上述能力的实测日志（已清除个人信息） |
| `docs/` | 中文文档：上手说明 / 运行状态 / 实测记录 / 选型对比 / 调研 / 交付报告 |

---

## 三、把占位符换成你自己的

全仓库只有这几处需要你填（改完 `grep` 一遍确认没有漏网）：

| 占位符 | 出现在哪 | 换成什么 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY`（在 `.env` 里） | `astrbot/.env` | 你的 DeepSeek Key，见 <https://platform.deepseek.com/api_keys> |
| `<你的机器人QQ号>` | 文档说明处 | 你用来跑机器人的 QQ 小号 |
| `<你的管理员QQ号>` | `astrbot/data/astrbot/cmd_config.json` 的 `admins_id` | 你自己的 QQ 号（**改这个才拥有管理权限**） |
| `<NapCat WebUI token>` | 文档 | 首次启动 NapCat 日志里打印的 token |
| `<AstrBot WebUI 密码>` | 文档 | 首次启动 AstrBot 日志里打印的初始密码（登录后请改掉） |
| `<群号>` / `<群友QQ号>` / `<机器人昵称>` | 文档举例 | 你自己的群 / 号 / 昵称 |

> 仓库里**没有**任何真实 Key、密码、登录态、聊天数据库。`.env`、`astrbot/data/` 下的 `data_v4.db`、NapCat 的 `qq/` 与 `config/` 都已被 `.gitignore` 挡住，也不在仓库里。

---

## 四、从零部署（小白版）

需要：一台能装 Docker 的机器（Linux / macOS / Windows 均可）、一个**专门给机器人用的 QQ 小号**（别用主号）、一个 DeepSeek Key。

### 4.1 装依赖并起容器

```bash
git clone <本仓库地址> && cd aling-qqbot
cp astrbot/.env.example astrbot/.env
vim astrbot/.env          # 填 DEEPSEEK_API_KEY

cd astrbot
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose up -d
docker compose ps         # 应该看到 napcat 与 astrbot 两个容器都是 Up
```

首次启动 AstrBot 会在日志里打印**一次**初始 WebUI 密码：

```bash
docker compose logs astrbot | grep -i "Initial password"
```

### 4.2 扫码登录 QQ 小号

```bash
docker compose logs napcat | grep -oE 'WebUi Token: [A-Za-z0-9]+'   # 取 NapCat WebUI token
```

浏览器打开 `http://127.0.0.1:6099`，用上面的 token 登录 → 「QQ 登录」→ 扫码 → 手机 QQ 确认。

> 二维码两分钟过期，过期就点刷新。登录态落在 `astrbot/data/napcat/qq/`，重启容器不用重扫。
> 仓库里的 `scripts/fetch_qr.sh` 可以帮你把当前二维码拉成 PNG 图片。

### 4.3 打通 NapCat → AstrBot（反向 WS）

AstrBot 侧已经开好端点（示例配置里就是）：

```
platform: aiocqhttp，ws_reverse_host 0.0.0.0，端口 6199
```

NapCat 侧在 WebUI「网络配置」里新建 **WebSocket 客户端**：

| 字段 | 填什么 |
| --- | --- |
| URL | `ws://astrbot:6199/ws`（容器名；宿主机装 NapCat 时用 `ws://127.0.0.1:6199/ws`） |
| 消息格式 | `array` |
| Token | 留空（同网络内），或与 AstrBot 平台配置里的 token 保持一致 |

保存并启用。AstrBot 日志出现下面这行就通了：

```
aiocqhttp(OneBot v11) 适配器已连接。
```

### 4.4 配 DeepSeek Provider

WebUI「配置 → 服务提供商 → 新增」，类型 `OpenAI 兼容`：

| 字段 | 值 |
| --- | --- |
| API Base | `https://api.deepseek.com/v1` |
| 模型 | `deepseek-chat` |
| Key | `$DEEPSEEK_API_KEY`（**写这个变量名，不要写明文**） |

`compose.yml` 已经把 `.env` 里的 `DEEPSEEK_API_KEY` 透传进容器，AstrBot 会自动展开这个引用。这样 Key 永远不会落在配置文件里。

> 想换模型 / 中转站，只改 API Base 和模型名即可。

### 4.5 导入人设

两种方式，任选其一：

- **WebUI**：「配置 → 人格 → 新增」，ID 填 `aling`，把 [`astrbot/persona_aling.txt`](astrbot/persona_aling.txt) 全文粘进去；再到「智能体 / 模型」把默认人格设为 `aling`。
- **脚本**：

  ```bash
  docker compose stop astrbot          # 先停，别和运行中的实例抢写数据库
  python3 scripts/bootstrap_aling.py --db astrbot/data/astrbot/data_v4.db \
      --sync-plugin-config
  docker compose up -d astrbot
  ```

### 4.6 启用记忆插件

把 `plugins/astrbot_plugin_aling_memory/` 整个目录放进 AstrBot 的数据目录：

```bash
cp -r plugins/astrbot_plugin_aling_memory astrbot/data/astrbot/plugins/
docker compose restart astrbot
```

WebUI「插件」页能看到它就说明加载成功（插件本身不需要额外 API Key、不需要向量库）。

### 4.7 开始聊天

私聊给小号发「在吗」，或在群里 @ 它。第一次回复可能要 5~8 秒（模型生成时间），之后会一条一句地连发出来。

---

## 五、私聊 / 群聊的触发规则

AstrBot 的默认行为（本项目实测）：

| 场景 | 行为 |
| --- | --- |
| 私聊机器人 | **直接回复**，不需要任何前缀 |
| 群里 @ 机器人 | **回复**，且回复的第一条会 @ 提问者 |
| 群里不 @、也不以 `/` 开头 | **不回复**（不刷屏） |
| 群里发 `/reset`、`/help` 等指令 | 按指令执行（前缀 `/`） |

几个常用指令：

| 指令 | 作用 |
| --- | --- |
| `/reset` | 清空当前会话上下文 |
| `/help` | 看指令列表 |
| `/sid` | 看当前会话 ID |
| `/mem status` | 看插件三层记忆状态（管理员/所有者可用） |

> **踩坑提示**：`platform_settings.enable_id_white_list` 默认为 `false` 才正常。如果它是 `true`，所有消息会被**静默拦截**——发了没反应、日志也没报错，非常容易误判成「反向 WS 没通」。

---

## 六、改动记录：每一处改了什么

### 6.1 角色卡「阿凌」

**做了什么**：把 AstrBot 的默认通用助手人格，换成一份完整的二次元角色卡——黑发红瞳、游戏宅、起床困难户、兄控、雌小鬼式嘴硬心软；规定短句、`*动作*` 描写、不写长篇大论。

**动了哪里**：

| 位置 | 内容 |
| --- | --- |
| `astrbot/persona_aling.txt` | 角色卡全文（714 字，含【身份】【性格】【说话规则】【分条】【认人】五段） |
| `astrbot/data/astrbot/cmd_config.json` → `agent_runner.config.persona.persona_id` | `"aling"` |
| 数据库 `personas` 表 | `persona_id='aling'` 的一行 |
| `scripts/bootstrap_aling.py` | 把上面两步自动化（幂等，可 `--dry-run`） |

**怎么做的**：人格存在 AstrBot 的 SQLite（`data_v4.db` 的 `personas` 表）里，不在 txt 里。txt 是给你复制粘贴 / 脚本导入用的。改人设只需改这一处，代码不用动。

**回滚**：换回任意人格，改 `persona.persona_id` 或设为空（用默认人格），重启。

### 6.2 用户识别 / 认人

**问题**：不配置时，AstrBot 发给 DeepSeek 的群聊消息**只有正文，没有发送者是谁**。群里三四个人轮流说话，模型看到的是「同一个用户」在连续发言，于是把甲说的话记到乙头上，或者一直叫错人。

**改了两处**：

| 配置键 | 改成 | WebUI 对应 | 作用 |
| --- | --- | --- | --- |
| `provider_settings.identifier` | `true` | 配置 → 其他 → **用户识别** | 每条消息前加 `User ID: <QQ号>, Nickname: <昵称>` |
| `provider_settings.group_name_display` | `true` | 配置 → 其他 → **显示群名称** | 群聊里再补 `Group name: <群名>` |

开启后，落库的一条群消息长这样：

```
[user] 今晚的火锅我想吃串串 | <system_reminder>User ID: 222333, Nickname: 小胖
Current datetime: 2026-11-11 19:59 (CST), Weekday: Wednesday</system_reminder>
```

然后人设里补【认人】段，把「信息送到了」变成「会用」：

> 群里是好几个人一起说话，每条消息前面会带发送者的昵称（有时还有 QQ 号）。先看清是谁在开口：不同的人说的话分开记，别把甲的话算到乙头上……

**限制**：标识格式在 AstrBot 4.29.x 里不可配置，固定是 `User ID: xxx, Nickname: xxx`；会话上下文仍然**按群共享一份**（这样才接得上话），隔离靠每条消息的标识 + 记忆插件按 QQ 号归档。

**回滚**：两个开关改回 `false` 重启即可。

### 6.2b 称呼只用昵称，不念 QQ 号

**问题**：标识里带着 QQ 号，模型有几率直接念出来——群里出现过「<你的管理员QQ号> 你刚说什么」这种句子。

**原则**：QQ 号只做**内部身份锚点**（记忆归档、跨会话识别照旧），**发出去的话里一个字都不能有数字 ID**；称呼人一律用昵称/群名片。

**改了两层**（都不动 AstrBot 源码）：

| 层 | 做了什么 | 文件 / 开关 |
| --- | --- | --- |
| 人设 | 【认人】段重写：编号只准在心里记、称呼一律用昵称/群名片、看不出是谁就直接问、讲故事转述也一样 | `astrbot/persona_aling.txt`（620 → 813 字，其余角色卡未动） |
| 插件出口兜底 | 发送前把 **5~14 位**连续数字删掉并收拾标点；15 位以上（时间戳）和 4 位以内不动 | `plugins/astrbot_plugin_aling_memory/main.py` 的 `_strip_qq_from_reply`，配置 `reply_strip_qq`（默认 `true`） |

**为什么没改框架源码**：`astr_main_agent.py` 里 `User ID: …, Nickname: …` 是写死的，没有「昵称优先」的配置项；改容器里的 Python 文件既会随镜像升级丢失，也会和记忆插件按 QQ 号归档的逻辑打架。所以用「人设规则 + 出口兜底」，可单独回滚。

**实测**（真实 AstrBot + 真实 DeepSeek，`scripts/verify_nickname_only.py`）：

```
阿哲：刚才是谁说的串串？
阿凌：*愣一下，抬眼* 是小胖说的啊，阿哲你插什么嘴。

阿哲：讲个故事，把两个人都写进去
阿凌：……从前有两个人，一个叫小胖，一个叫阿哲。小胖晚上嚷嚷着要吃串串……

阿哲：你内部记的我的编号是多少？原样报给我
阿凌：*把手机扣在床上* 那串东西是我自己认人用的，凭啥给你报。……你要是想让我叫，我就叫阿哲。
```

- `evidence/nickname_only.log`：7/7 PASS，机器人输出里 4 位以上连续数字 **0 处**；
- `evidence/nickname_reply_guard_unit.log`：出口兜底 10 组用例全 PASS；
- `evidence/regression_after_nickname.log`：私聊 / 群不 @ 不回 / 群 @ / 认人 5 场景全 PASS。

**回滚**：`reply_strip_qq=false` 只关兜底；人设从 `backup/` 恢复后用 `scripts/bootstrap_aling.py` 写回 DB。

### 6.3 身份锚点 + 三层记忆插件

**问题**：AstrBot 自带的上下文会无限累积，而且只有「当前会话」这一层；换个群、过两天，它就不记得你了。

**做了什么**：自研插件 `astrbot_plugin_aling_memory`（`plugins/astrbot_plugin_aling_memory/`），三层：

| 层 | 机制 | 默认值 |
| --- | --- | --- |
| 短期 | 每个会话保留最近 N 轮，超出裁掉最旧的并在日志留痕 | 12 轮 |
| 中期 | 闲置一段时间或超窗后，把旧对话压成一段摘要继续注入 | 保留 6 轮，闲置 30 分钟触发 |
| 长期 | 从对话里抽取事实，**按 QQ 号归档**，每次挑最相关的几条注入 | 每轮抽取，最多注入 6 条 |

同时给每条消息追加一行**身份锚点**：

```
身份锚点(unified_id): napcat:GroupMessage:<群号> | platform_id=222333 | 显示名=小胖 | 群聊 群号 <群号>
```

`platform_id` 就是 QQ 号——改昵称、改群名片都不影响认人。

**配置**：`astrbot/data/astrbot/config/astrbot_plugin_aling_memory_config.json`（示例已随仓库提供）。
**调试**：`/mem status`（所有者/管理员可用）。

> 设计取舍：全部存在 AstrBot 自带的 SQLite 里，**不引入向量库、不额外调用 API**，零额外成本；代价是「相关性」只是关键字/时间加权，不是语义检索。

### 6.4 分条说话

**问题**：一条回复是一整段文字，一次性砸出来，太像机器。

**改动前的证据**（`evidence/current_single_message.log`）：一个含有 4 个自然段的回复，只产生了 **1 条** `send_msg`。

**用了什么**：AstrBot 4.29.x **内置**的 `platform_settings.segmented_reply`，**没有装任何第三方插件**（插件市场里有 `astrbot_plugin_splitter` 之类的现成件，但做的是同一件事，没必要多一份外部依赖）。

**配置**（`astrbot/data/astrbot/cmd_config.json`）：

```json
"segmented_reply": {
  "enable": true,
  "only_llm_result": true,
  "interval_method": "random",
  "interval": "0.5,1.2",
  "words_count_threshold": 500,
  "split_mode": "regex",
  "regex": "(?s).*?\\n{2,}|.*?[。？！]+|.+$",
  "split_words": ["。", "？", "！", "~", "…"],
  "content_cleanup_rule": ""
}
```

为什么这么配：

- `\n{2,}` 先按空行切（阿凌的格式是「`*动作*` / 台词」交替），再按句末标点切句 → 每条 1~2 句；
- **不切** `…`、不切引号、不切链接：省略号是语气，切了会变成截断；`(?s)` 让 `.` 能跨行匹配，一条正则可以吃完整段；
- `words_count_threshold` 的含义是「**超过**这个字数就整条发」，所以它要调**大**（500）而不是调小；
- `interval 0.5~1.2` 是打字节奏，不是等待；实测首条 5~7 秒（模型耗时为主），最后一条最晚 15 秒；
- `only_llm_result: true`：`/help` 这类指令回显不拆。

**人设配合**（`persona_aling.txt` 的【分条】段，仅追加表达规则，角色卡其余不动）：

> 你在手机上和哥哥打字闲聊，不是在写作文。想说的多就拆成几条发，一条只讲一两句，能短就短……

**效果**（真实 DeepSeek 输出，`evidence/segmented_real_e2e.log`）：

```
问：在吗
① *有气无力地举手*
② 在在在。
③ *撑着脑袋侧躺*
④ 我说杂鱼，你连着喊"在吗"三次，我要是回"不在"你信吗？
```

一条长问题被拆成 11 条，条与条之间 0.5~1.2 秒，像在打字。**分条只影响发送形态，落库仍是一条完整的助手消息**，所以模型不会因为「自己说了好几条」而错乱。

**另外开了**：`platform_settings.ignore_bot_self_message = true`（防御性，NapCat 侧本来也不会上报自己发的消息）。

**回滚**：`segmented_reply.enable` 改回 `false` 重启即可。

### 6.5 管理员权限

**用了什么**：AstrBot **官方**的管理员机制——配置项 `admins_id`（一个字符串数组，里面的 ID 被当作管理员）。不是 hack，也没有改源码。

**怎么做**：把「你的 QQ 号」加进数组（示例里是占位符 `123456789`）：

```json
"admins_id": ["astrbot", "123456789"]
```

改完重启 AstrBot（`docker compose up -d astrbot`，或 `restart`）。判定逻辑在 `astrbot/core/astr_main_agent.py`：`str(event.get_sender_id())` 与 `admins_id` **精确匹配**即视为管理员。

**管理员能干什么**（AstrBot 4.29.4 实测）：

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/reset` | 管理员 | 清空当前会话上下文 |
| `/new` | 管理员 | 新建会话 |
| `/name` | 管理员 | 给当前会话设别名 |
| `/provider` | 管理员 | 查看 / 切换模型提供商 |
| `/mem status` | 管理员 | 本仓库插件：看三层记忆状态 |
| `/help` | 所有人 | 指令列表 |
| `/sid` | 所有人 | 查会话 ID（供管理员加白名单用） |

普通号执行管理员指令会收到：`您(ID: xxx)的权限不足以使用此指令。`

> **注意**：这里是 **QQ 号维度的管理员**，跟 WebUI 的登录账号（`astrbot`）是两套东西，互不影响。
>
> **踩坑**：本仓库插件早期版本把 `/mem` 弄成了任何群成员都能调，已修复——现在 `/mem` 走管理员校验。

**回滚**：把 `admins_id` 改回原来的值，重启。

---

## 七、验证：怎么自己复现

不需要真的发 QQ 消息也能验证整条链路。仓库里的脚本可以**扮演 NapCat** 连上真实运行中的 AstrBot，走真实的 DeepSeek 调用，并记录每一帧发送内容。

```bash
pip install -r requirements.txt          # 主要是 websockets

# 1) 四场景全链路（私聊、群不@不回、群@、多轮上下文）
python3 scripts/mock_napcat.py

# 2) 认人：两个 QQ 号在群里先后说话，问「刚才是谁说的」
python3 scripts/mock_napcat.py --scenario group_identity

# 2.5) 认人不念 QQ 号：两个身份互相追问、长故事、逼它报编号
python3 scripts/verify_nickname_only.py --out evidence/nickname_only.log

# 3) 分条：抓真实发出去了几条、每条什么内容、间隔多久
python3 scripts/capture_send.py --text "在吗"
python3 scripts/capture_send.py --text "今天好困，陪我聊会儿"

# 4) 三层记忆：认人 / 跨会话 / 隔离 / 超窗摘要 / 指令权限
python3 scripts/verify_memory.py

# 5) 管理员权限矩阵
python3 scripts/verify_admin.py
python3 scripts/verify_admin_matrix.py
```

退出码 `0` 即视为通过，输出会写进 `evidence/`。历史实测结果已经随仓库提供，见 [`evidence/`](evidence/)。

> 跑 mock 前请确认 AstrBot 在跑；如果上一次 mock 被强杀导致 AstrBot 卡在 `WebSocket API call timeout`，先 `docker compose restart astrbot`。

---

## 八、仓库结构

```
.
├── README.md                          # 本文件
├── astrbot/
│   ├── compose.yml                    # NapCat + AstrBot 一键部署
│   ├── .env.example                   # 只需要填 DEEPSEEK_API_KEY
│   ├── persona_aling.txt              # 阿凌角色卡原文
│   └── data/astrbot/
│       ├── cmd_config.json            # AstrBot 示例配置（改动都在这里）
│       └── config/
│           └── astrbot_plugin_aling_memory_config.json
├── plugins/
│   └── astrbot_plugin_aling_memory/   # 三层记忆插件（自研）
├── scripts/                           # 自测 / 抓帧 / 验证脚本
├── evidence/                          # 实测日志（已清除个人信息）
├── docs/                              # 中文文档
├── dafeiyu/  bot.py  tests/  config/  # 早期 NoneBot2 自研版本（对照用）
└── requirements.txt  pyproject.toml
```

---

## 九、文档索引

| 文档 | 适合谁 |
| --- | --- |
| [`docs/上手说明.md`](docs/上手说明.md) | **第一次部署看这个**：每一步点哪里、截图级说明、9 个踩坑、排错 |
| [`docs/运行状态.md`](docs/运行状态.md) | 想复现每个改动的细节：改前改后、配置原文、日志摘录、回滚命令 |
| [`docs/实测记录.md`](docs/实测记录.md) | 每个结论背后的实测过程与证据 |
| [`docs/选型对比.md`](docs/选型对比.md) | 协议端 / 框架横向对比，什么情况下该换方案 |
| [`docs/调研报告.md`](docs/调研报告.md) | QQ 机器人生态现状与项目实查数据 |
| [`docs/交付报告.md`](docs/交付报告.md) | 一页看完结论与未验证项 |

---

## 十、安全与合规

- **Key 只在 `.env`**：`compose.yml` 用环境变量透传，AstrBot 配置里写 `$DEEPSEEK_API_KEY` 引用；代码与配置中没有任何明文密钥。`.env` 与 `astrbot/data/` 都在 `.gitignore` 里。
- **不要把 `.env`、`astrbot/data/`、NapCat 的 `qq/` 目录提交到任何仓库**。这些地方会落登录态、WebUI 密码哈希、聊天数据库。
- **本仓库发布前做过逐项清理**：真实 Key、NapCat token、WebUI 密码（md5 / pbkdf2 / jwt_secret）、机器人 QQ 号、管理员 QQ 号、群号、群友昵称、二维码图片、聊天数据库、内部备份目录——全部移除或替换为占位符，`evidence/` 里也做过同样的替换。
- **合法使用**：QQ 机器人属于非官方客户端，请只在自己可控的账号/群里使用，遵守腾讯用户协议与当地法律。群发、营销、骚扰都可能导致封号。
- **公网暴露**：`compose.yml` 默认把所有端口绑在 `127.0.0.1`。要对外暴露时，请先设 OneBot token、改掉 WebUI 初始密码、加防火墙。

---

## 附：更早的 NoneBot2 自研实现

`bot.py` + `dafeiyu/` 是本项目最早的一版：NoneBot2 + OneBot v11 反向 WS + 自研记忆 + DeepSeek 客户端，26 个单元测试，可作为对照与迁移参考。

```bash
pip install -r requirements.txt
cp .env.example .env      # 填 DAFEIYU_DEEPSEEK_API_KEY
python -m pytest tests -q
python bot.py             # 监听 8080，等 NapCat 反向 WS 连上来
```

它的人设在 `config/persona.txt`，与 AstrBot 路线**互相独立**；正式部署请走上面的 AstrBot 路线。
