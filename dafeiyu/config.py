"""大肥鱼插件配置。

所有可调项都通过 NoneBot 的环境变量读取（前缀 ``DAFEIYU_``），
也可以写在项目根目录的 ``.env`` / ``.env.prod`` 里。
API Key 只从环境变量 / 配置文件读取，代码中不保存任何密钥。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field


class DafeiyuConfig(BaseModel):
    """插件配置（环境变量前缀 DAFEIYU_）。"""

    # --- DeepSeek ---
    deepseek_api_key: str = Field(
        default="",
        description="DeepSeek 官方 API Key，建议写在 .env 里，不要提交到仓库",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        description="DeepSeek API 地址（兼容 OpenAI Chat Completions）",
    )
    deepseek_model: str = Field(default="deepseek-chat", description="对话模型")
    request_timeout: float = Field(default=90.0, description="单次请求超时（秒）")
    temperature: float = Field(default=0.8, description="回复随机度 0~2")
    max_tokens: int = Field(default=800, description="单次回复最大 token 数")

    # --- 人设 ---
    nickname: list[str] = Field(
        default_factory=lambda: ["大肥鱼"], description="机器人昵称，会写进人设提示词"
    )
    persona_file: str = Field(
        default="config/persona.txt",
        description="人设提示词文件路径；文件存在时优先于内置默认人设",
    )
    persona_inline: str = Field(
        default="", description="直接内联人设提示词（优先级最高，留空则用文件/默认）"
    )

    # --- 触发 ---
    group_trigger_keywords: list[str] = Field(
        default_factory=lambda: ["大肥鱼"],
        description="群聊里消息以这些词开头时也视为呼叫机器人（@ 一直有效）",
    )
    reset_keywords: list[str] = Field(
        default_factory=lambda: ["重置", "清空记忆", "忘记刚才", "重新开始"],
        description="清空当前会话记忆的关键词",
    )
    help_keywords: list[str] = Field(
        default_factory=lambda: ["帮助", "help", "用法"],
        description="查看用法的关键词",
    )

    # --- 记忆 ---
    max_history_turns: int = Field(
        default=10, ge=1, le=100, description="每个会话保留最近多少轮问答（1 轮=1问+1答）"
    )
    history_ttl_minutes: int = Field(
        default=180, ge=1, description="会话记忆闲置多久后过期（分钟）"
    )
    max_sessions: int = Field(
        default=2000, ge=10, description="内存中最多保留多少个会话，超出后淘汰最久未用的"
    )

    # --- 其它 ---
    max_reply_chars: int = Field(
        default=1500, ge=100, description="单条回复最大字符数，超出截断"
    )
    show_typing: bool = Field(
        default=True, description="调用模型前是否给 QQ 发“正在输入”状态"
    )


def _read_env_sources() -> dict[str, str]:
    """合并 .env / .env.<环境> 与进程环境变量（进程环境优先）。"""
    from pathlib import Path

    merged: dict[str, str] = {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - nonebot 自带 python-dotenv
        dotenv_values = None

    if dotenv_values is not None:
        env_name = os.getenv("ENVIRONMENT", "prod")
        for filename in (".env", f".env.{env_name}"):
            path = Path(filename)
            if path.is_file():
                merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update(os.environ)
    return merged


_PREFIX = "DAFEIYU_"


def get_config() -> DafeiyuConfig:
    """按 DAFEIYU_ 前缀读取环境变量 / .env，并做类型转换。"""
    import json

    raw = _read_env_sources()
    values: dict = {}
    for name, field in DafeiyuConfig.model_fields.items():
        env_key = f"{_PREFIX}{name.upper()}"
        if env_key not in raw:
            continue
        value = raw[env_key].strip()
        if value == "":
            continue
        values[name] = value

    # 列表类配置用 JSON 写法：DAFEIYU_NICKNAME=["大肥鱼"]
    try:
        parsed = dict(values)
        for name, value in values.items():
            if isinstance(value, str) and value[:1] in "[{":
                parsed[name] = json.loads(value)
        return DafeiyuConfig(**parsed)
    except Exception as exc:  # 配置写错时给出明确提示
        raise RuntimeError(f"读取 DAFEIYU_ 配置失败: {exc}") from exc


@lru_cache(maxsize=1)
def get_config_cached() -> DafeiyuConfig:
    return get_config()
