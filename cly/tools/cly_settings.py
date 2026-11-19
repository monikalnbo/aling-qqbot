"""cly 独立配置槽 · 唯一写入点（给自助设置页和运维命令共用）

为什么要有这个文件
------------------
cly 这台机器人是「托管」给别人的：**只能用他自己的 key**。
所以它的 DeepSeek key / 模型、语音 key / 接口地址 / 模型，全部存在这里，
和第一台大肥鱼的任何配置都不共享、不读取、不回退。

5 个槽（初始全空）
------------------
    deepseek_key    他自己的 DeepSeek Key（sk- 开头）
    deepseek_model  他自己选的 DeepSeek 模型（如 deepseek-chat）
    voice_key       他自己的语音（百炼 SpeechSynthesizer）Key
    voice_base_url  他自己的语音接口地址（可带 /api/v1，可不带）
    voice_model     他自己的语音模型

没填 DeepSeek Key 时，cly 绝不调用任何付费接口：guard 插件在 AstrBot 调模型之前
就把这一轮拦下来，回一句友好提示（见 cly_data/plugins/astrbot_plugin_cly_guard）。

文件
----
    cly/data/cli_settings/secret.json   明文密钥，权限 600，不进 git
    cly/data/astrbot/cmd_config.json    由本文件同步：provider.key 写成明文、
                                        provider.model 跟随 deepseek_model
    cly/data/astrbot/config/astrbot_plugin_aling_voice_config.json
                                        语音那 3 个槽写进这里（插件读它）

apply 之后只重启 astrbot-cly 这一个容器；第一台 astrbot / napcat 一概不碰。

命令行
------
    python3 cly/tools/cly_settings.py status
    python3 cly/tools/cly_settings.py set --deepseek-key sk-xxx --deepseek-model deepseek-chat
    python3 cly/tools/cly_settings.py set --voice-key sk-yyy --voice-base-url https://host \
        --voice-model qwen-audio-3.0-tts-flash --voice-name longanxiaoxin
    python3 cly/tools/cly_settings.py clear --fields voice_key
    python3 cly/tools/cly_settings.py apply          # 只写配置文件，不重启
    python3 cly/tools/cly_settings.py restart        # 只重启 astrbot-cly
    python3 cly/tools/cly_settings.py reveal-tail deepseek_key
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 工作区根目录
# CLY_DATA_DIR 只给离线验证用（指向测试容器的 data 目录），线上不设这个变量。
CLY_DATA = Path(os.environ.get("CLY_DATA_DIR") or (ROOT / "cly" / "data"))
SECRET_FILE = CLY_DATA / "cli_settings" / "secret.json"
VOICE_STATUS_FILE = CLY_DATA / "cli_settings" / "voice_status.json"
ASTROBOT_DIR = CLY_DATA / "astrbot"
CMD_CONFIG = ASTROBOT_DIR / "cmd_config.json"
VOICE_CONFIG = ASTROBOT_DIR / "config" / "astrbot_plugin_aling_voice_config.json"
VOICE_PLUGIN_DEFAULTS = (
    CLY_DATA / "plugins" / "astrbot_plugin_aling_voice" / "main.py"
)

CONTAINER = "astrbot-cly"

# 占位 key：没填 key 时写进 AstrBot 的 provider 配置，避免它因为「没凭据」直接
# 加载失败、把整条 LLM 流程干脆绕过。它一定调不通任何接口，且守卫插件认得它，
# 会在发请求之前就把这一轮拦下来换成友好提示。
PLACEHOLDER_KEY = "sk-cly-not-configured-placeholder"

# 5 个槽的元信息：页面文案也从这里取，避免两处写不一致
FIELDS: dict[str, dict] = {
    "deepseek_key": {
        "label": "DeepSeek Key",
        "placeholder": "sk-...",
        "secret": True,
        "tip": "他自己在 DeepSeek 官网申请的 Key（platform.deepseek.com → API keys）。"
               "没填之前机器人不会回话，只回一句「还没配置」的提示，也不会花任何钱。",
    },
    "deepseek_model": {
        "label": "DeepSeek 模型",
        "placeholder": "deepseek-chat",
        "secret": False,
        "tip": "填深寻接口里的模型名，常用的就是 deepseek-chat（便宜够用）。"
               "留空默认用 deepseek-chat。",
    },
    "voice_key": {
        "label": "语音 Key",
        "placeholder": "sk-...",
        "secret": True,
        "tip": "阿里云百炼（DashScope）的 API Key，用来把文字读成语音。"
               "留空就只是纯文字聊天，不会去调语音接口、也不会报错。",
    },
    "voice_base_url": {
        "label": "语音接口地址",
        "placeholder": "https://你的语音服务域名",
        "secret": False,
        "tip": "语音服务的主机地址，例如 https://maas.qianwenaiapi.com "
               "或 https://dashscope.aliyuncs.com。带不带 /api/v1 都行，程序会自己拼。",
    },
    "voice_model": {
        "label": "语音模型",
        "placeholder": "qwen-audio-3.0-tts-flash",
        "secret": False,
        "tip": "语音合成的模型名。不填默认 qwen-audio-3.0-tts-flash（快、便宜）。",
    },
}

# 语音插件里跟着一起写的非密钥项（音色等）。不放进 5 个槽，但换 key 时保留原值。
VOICE_EXTRA_DEFAULTS = {
    "enable_on_start": True,
    "voice": "longanxiaoxin",
    "volume": 100,
    "sample_rate": 24000,
    "max_chars": 120,
    "max_concurrency": 3,
    "dual_output": False,
    "timeout": 60,
    "sing_enabled": True,
    "sing_auto": True,
    "sing_model": "qwen-audio-3.1-tts-next",
    "sing_voice": "",
    "sing_max_chars": 40,
    "sing_max_segments": 2,
    "sing_timeout": 150,
    "sing_extra_words": [],
    "sing_extra_regex": "",
    "lyrics_sources": ["qq", "lrclib"],
    "lyrics_timeout": 6,
    "lyrics_allow_model_fallback": True,
    "lyrics_allow_singer_pick": True,
}

EMPTY: dict[str, str] = {k: "" for k in FIELDS}

VOICE_DEFAULT_BASE = "https://maas.qianwenaiapi.com"
VOICE_DEFAULT_MODEL = "qwen-audio-3.0-tts-flash"

# 守卫插件 / 语音插件共用的「key 是否真能用」判定用的占位集合（与 guard 插件一致）
PLACEHOLDERS = {
    "",
    PLACEHOLDER_KEY,
    "sk-xxx",
    "sk-...",
    "sk-xxxx",
    "你的key",
    "填入你的key",
    "none",
    "null",
}


# --------------------------------------------------------------------- 读写
def _atomic_write_json(path: Path, data: dict, *, bom: bool = False, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_secret() -> dict[str, str]:
    """读配置槽。文件不存在 = 全空（初始状态就是这样）。"""
    out = dict(EMPTY)
    try:
        raw = SECRET_FILE.read_bytes()
    except FileNotFoundError:
        return out
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return out
    for k in FIELDS:
        v = data.get(k)
        if isinstance(v, str):
            out[k] = v.strip()
    return out


def save_secret(values: dict[str, str]) -> dict[str, str]:
    clean = {k: str(values.get(k, "") or "").strip() for k in FIELDS}
    payload = {
        "_comment": "cly 托管配置槽 · 明文密钥，权限 600，禁止提交到 git / 禁止贴到聊天里",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **clean,
    }
    old_umask = os.umask(0o077)
    try:
        _atomic_write_json(SECRET_FILE, payload, mode=0o600)
    finally:
        os.umask(old_umask)
    os.chmod(SECRET_FILE, 0o600)
    return clean


def _write_voice_status(status: dict) -> None:
    """语音能否用的结论，单独存；页面只读这个，不读 key。"""
    payload = {
        "_comment": "语音 key 实测结果（不含 key 本体）",
        "voice_ok": bool(status.get("voice_ok")),
        "checked_at": status.get("voice_checked_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "msg": status.get("voice_msg") or "",
    }
    _atomic_write_json(VOICE_STATUS_FILE, payload, mode=0o600)


def load_voice_status() -> dict:
    try:
        data = json.loads(VOICE_STATUS_FILE.read_text(encoding="utf-8-sig"))
        return {
            "voice_ok": bool(data.get("voice_ok")),
            "checked_at": str(data.get("checked_at") or ""),
            "msg": str(data.get("msg") or ""),
        }
    except Exception:
        return {"voice_ok": False, "checked_at": "", "msg": "还没测过"}


def mask(value: str) -> str:
    """只露最后 4 位，其余星号。空值返回空串。"""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 4:
        return "*" * len(v)
    return "*" * max(4, len(v) - 4) + v[-4:]


def tail(value: str) -> str:
    v = (value or "").strip()
    return v[-4:] if v else ""


def check_key_shape(key: str) -> tuple[bool, str]:
    """只看格式，不联网：空 / 占位 / 明显不是 key → 不通过。"""
    k = (key or "").strip()
    if not k:
        return False, "未填写"
    if k == PLACEHOLDER_KEY or k.lower() in PLACEHOLDERS:
        return False, "还是占位示例，请填真的 key"
    if not k.startswith("sk-"):
        return False, "格式不对（应以 sk- 开头）"
    if len(k) < 20:
        return False, "太短了，不像完整的 key"
    return True, "格式正常"


VOICE_PROBE_TEXT = "你好"


def probe_voice(key: str, base_url: str, model: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """真的发一次最小合成请求，确认这把语音 key 能用。

    没填 key 时**根本不发请求**，直接返回失败——保证「没填就不会调接口」。
    返回 (可用, 说明)。401/403 视为 key 无效；其它网络错误视为「暂时验不了」。
    """
    ok, why = check_key_shape(key)
    if not ok:
        return False, why
    base = (base_url or VOICE_DEFAULT_BASE).strip().rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    body = {
        "model": (model or VOICE_DEFAULT_MODEL).strip(),
        "input": {"text": VOICE_PROBE_TEXT, "voice": "longanxiaoxin", "format": "mp3"},
    }
    req = urllib.request.Request(
        base + "/api/v1/services/audio/tts/SpeechSynthesizer",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key.strip(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:160].decode("utf-8", "replace")
        if exc.code in (401, 403):
            return False, f"key 被拒（HTTP {exc.code}）：{detail}"
        return False, f"接口报错 HTTP {exc.code}：{detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"暂时连不上语音接口：{exc}"
    if obj.get("code") and obj.get("message"):
        return False, f"接口拒绝（{obj.get('code')}）：{obj.get('message')}"
    if not ((obj.get("output") or {}).get("audio") or {}).get("url"):
        return False, "接口没返回音频"
    return True, "语音 key 可用"


# --------------------------------------------------------------------- 写进 AstrBot
def _load_voice_defaults() -> dict:
    cfg = dict(VOICE_EXTRA_DEFAULTS)
    cfg["api_base"] = ""
    cfg["model"] = ""
    cfg["api_key"] = ""
    return cfg


def _read_voice_config() -> dict:
    base = _load_voice_defaults()
    try:
        cur = json.loads(VOICE_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return base
    if isinstance(cur, dict):
        for k, v in cur.items():
            if k in ("api_key", "api_base", "model") or k in VOICE_EXTRA_DEFAULTS:
                base[k] = v
    return base


def apply(values: dict[str, str] | None = None) -> dict:
    """把 5 个槽落到 cly 真正会读的文件里。只动 cly，不碰第一台。"""
    values = values if values is not None else load_secret()
    dk = values.get("deepseek_key", "").strip()
    dm = values.get("deepseek_model", "").strip() or "deepseek-chat"
    vk = values.get("voice_key", "").strip()
    vb = values.get("voice_base_url", "").strip()
    vm = values.get("voice_model", "").strip() or "qwen-audio-3.0-tts-flash"

    # ---- 1. cmd_config.json：provider / provider_sources / agent_runner ----
    raw = CMD_CONFIG.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    conf = json.loads(raw.decode("utf-8-sig"))

    # 注意：不动 conf["platform"]（QQ 适配器配置在这里），只改模型相关的三处。
    providers = conf.get("provider") or []
    if not isinstance(providers, list):
        providers = []
    srcs = conf.get("provider_sources") or []
    if not isinstance(srcs, list):
        srcs = []

    # provider 只留一个：id=deepseek-chat，key 写明文（空就是空，绝不回落环境变量）
    # 注意 enable 保持 true：provider 被禁用时 AstrBot 会在进钩子之前就打回
    # 「未找到任何可用的对话模型」，那是报错刷屏。保持启用 + 空 key，
    # 才能真正走到 guard 插件的 on_llm_request，由它回一句人话并拦住付费调用。
    provider = providers[0] if providers else {}
    provider.update(
        {
            "id": "deepseek-chat",
            "provider_source_id": "deepseek-chat_source",
            "enable": True,
            "model": dm,
            "api_base": "https://api.deepseek.com/v1",
            "key": [dk or PLACEHOLDER_KEY],
        }
    )
    conf["provider"] = [provider]

    src = srcs[0] if srcs else {}
    src.update(
        {
            "id": "deepseek-chat_source",
            "provider": "deepseek",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "key": [dk or PLACEHOLDER_KEY],
            "api_base": "https://api.deepseek.com/v1",
        }
    )
    conf["provider_sources"] = [src]

    runner = conf.setdefault("agent_runner", {})
    runner.setdefault("runner_type", "local")
    model_cfg = runner.setdefault("config", {}).setdefault("model", {})
    model_cfg["provider_id"] = "deepseek-chat"
    model_cfg.setdefault("fallback_provider_ids", [])
    model_cfg.setdefault("request_max_retries", 5)

    _atomic_write_json(CMD_CONFIG, conf, bom=had_bom, mode=0o600)

    # ---- 2. 语音插件配置：3 个槽 + 其余保留 ----
    vcfg = _read_voice_config()
    # 语音 key 没填 / 格式就不对：清空，插件因此根本不会去合成（见 guard_voice）
    vok = check_key_shape(vk)[0]
    vcfg["api_key"] = vk if vok else ""
    vcfg["api_base"] = vb or VOICE_DEFAULT_BASE
    vcfg["model"] = vm
    _atomic_write_json(VOICE_CONFIG, vcfg, mode=0o600)

    # ---- 3. 语音可用性标记：真的能用才写 true ----
    # 设置页保存时已经探测过；命令行 set/apply 时这里兜底再探一次（没填就不发请求）。
    status: dict = {}
    if vok and os.environ.get("CLY_NO_VOICE_PROBE") == "1":
        status = {
            "voice_ok": False,
            "voice_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "voice_msg": "离线验证：未发起真实请求",
        }
    elif vok:
        usable, why = probe_voice(vk, vcfg["api_base"], vm)
        status = {
            "voice_ok": usable,
            "voice_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "voice_msg": why,
        }
    else:
        status = {
            "voice_ok": False,
            "voice_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "voice_msg": check_key_shape(vk)[1],
        }
    _write_voice_status(status)

    # 插件目录下的示例配置（跟着一起更新，方便对照；不含密钥字样以外的真实值）
    example = CLY_DATA / "plugins" / "astrbot_plugin_aling_voice" / "config.example.json"
    if example.parent.is_dir():
        ex = _read_voice_config()
        ex["api_key"] = ""
        _atomic_write_json(example, ex, mode=0o644)

    return {
        "deepseek_key": bool(dk),
        "deepseek_model": dm,
        "deepseek_provider_enable": bool(dk),
        "voice_key": bool(vk),
        "voice_base_url": vcfg["api_base"],
        "voice_model": vm,
        "voice_ok": status["voice_ok"],
        "voice_msg": status["voice_msg"],
    }


# --------------------------------------------------------------------- 容器操作
def _uid_gid() -> tuple[int, int]:
    try:
        out = subprocess.run(
            ["docker", "inspect", CONTAINER, "--format", "{{.Config.User}}"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
        if out and ":" in out:
            u, g = out.split(":", 1)
            return int(u), int(g)
    except Exception:
        pass
    return 0, 0


def fix_perms() -> None:
    """把密钥文件交给容器里的运行用户，其他人可读权限一律去掉。"""
    uid, gid = _uid_gid()
    for p in (SECRET_FILE, CMD_CONFIG, VOICE_CONFIG):
        if p.exists():
            try:
                os.chown(p, uid, gid)
            except PermissionError:
                pass
            os.chmod(p, 0o600)
    d = SECRET_FILE.parent
    if d.exists():
        try:
            os.chown(d, uid, gid)
        except PermissionError:
            pass
        os.chmod(d, 0o700)


def restart_container() -> tuple[int, str]:
    r = subprocess.run(
        ["docker", "restart", CONTAINER],
        capture_output=True, text=True, timeout=120, check=False,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


def wait_ready(timeout: float = 60.0) -> bool:
    """等容器真正起来（AstrBot 会打印 WebUI ready）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "inspect", CONTAINER, "--format", "{{.State.Status}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if r.stdout.strip() == "running":
            logs = subprocess.run(
                ["docker", "logs", "--tail", "80", CONTAINER],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout
            if "AstrBot started" in logs or "WebUI is ready" in logs:
                return True
        time.sleep(1.5)
    return False


# --------------------------------------------------------------------- CLI
def cmd_status(_: argparse.Namespace) -> int:
    vals = load_secret()
    out = {}
    for k, meta in FIELDS.items():
        v = vals.get(k, "")
        out[k] = {
            "label": meta["label"],
            "filled": bool(v),
            "shown": mask(v) if meta["secret"] else v,
        }
    out["_voice_check"] = load_voice_status()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    vals = load_secret()
    changed = []
    mapping = {
        "deepseek_key": args.deepseek_key,
        "deepseek_model": args.deepseek_model,
        "voice_key": args.voice_key,
        "voice_base_url": args.voice_base_url,
        "voice_model": args.voice_model,
    }
    for k, v in mapping.items():
        if v is None:
            continue
        vals[k] = v.strip()
        changed.append(k)
    save_secret(vals)
    print(json.dumps(apply(vals) | {"changed": changed}, ensure_ascii=False))
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    vals = load_secret()
    for f in args.fields:
        if f not in FIELDS:
            print(f"未知槽位：{f}", file=sys.stderr)
            return 2
        vals[f] = ""
    save_secret(vals)
    print(json.dumps(apply(vals), ensure_ascii=False))
    return 0


def cmd_apply(_: argparse.Namespace) -> int:
    print(json.dumps(apply(), ensure_ascii=False))
    return 0


def cmd_restart(_: argparse.Namespace) -> int:
    rc, msg = restart_container()
    ok = rc == 0 and wait_ready()
    print(json.dumps({"rc": rc, "ok": ok, "msg": msg[-400:], "ready": ok}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_reveal_tail(args: argparse.Namespace) -> int:
    vals = load_secret()
    print(tail(vals.get(args.field, "")))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="cly 托管配置槽")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("apply").set_defaults(func=cmd_apply)
    sub.add_parser("restart").set_defaults(func=cmd_restart)

    p = sub.add_parser("set")
    p.add_argument("--deepseek-key")
    p.add_argument("--deepseek-model")
    p.add_argument("--voice-key")
    p.add_argument("--voice-base-url")
    p.add_argument("--voice-model")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("clear")
    p.add_argument("--fields", nargs="+", required=True)
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("reveal-tail")
    p.add_argument("field", choices=list(FIELDS))
    p.set_defaults(func=cmd_reveal_tail)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
