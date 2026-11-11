"""动手能力组装层：把配置、记忆、DeepSeek 客户端接成 ReplyEngine。

插件与测试共用同一个工厂，保证“测什么就跑什么”。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DafeiyuConfig, get_config, get_config_cached
from .core import ReplyEngine, load_persona
from .llm import DeepSeekClient
from .memory import MemoryStore


@dataclass
class DafeiyuApp:
    config: DafeiyuConfig
    store: MemoryStore
    client: DeepSeekClient
    engine: ReplyEngine


def create_app(
    config: DafeiyuConfig | None = None,
    *,
    client: DeepSeekClient | None = None,
    store: MemoryStore | None = None,
    persona: str | None = None,
) -> DafeiyuApp:
    conf = config or get_config_cached()
    store = store or MemoryStore(
        max_sessions=conf.max_sessions,
        ttl_seconds=conf.history_ttl_minutes * 60,
        max_turns=conf.max_history_turns,
    )
    client = client or DeepSeekClient(
        conf.deepseek_api_key,
        base_url=conf.deepseek_base_url,
        model=conf.deepseek_model,
        timeout=conf.request_timeout,
        temperature=conf.temperature,
        max_tokens=conf.max_tokens,
    )
    engine = ReplyEngine(
        conf, store, client, persona=persona if persona is not None else load_persona(conf)
    )
    return DafeiyuApp(config=conf, store=store, client=client, engine=engine)
