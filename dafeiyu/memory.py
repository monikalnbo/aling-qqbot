"""会话记忆：按会话（私聊用户 / 群内用户）保存多轮上下文。

纯内存实现，进程重启后清空，够 QQ 群聊场景使用；
接口保持简单，后续想换 Redis / SQLite 只需替换本文件。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class Session:
    """一个会话的上下文。"""

    key: str
    messages: list[dict[str, str]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    def append(self, role: str, content: str, *, max_turns: int) -> None:
        self.messages.append({"role": role, "content": content})
        # 只保留最近 max_turns 轮（user + assistant 各一条）
        limit = max_turns * 2
        if len(self.messages) > limit:
            self.messages = self.messages[-limit:]
        self.updated_at = time.monotonic()

    def history(self) -> list[dict[str, str]]:
        """返回可直接拼进请求的历史消息副本。"""
        return [dict(m) for m in self.messages]


class MemoryStore:
    """带 TTL 与容量上限的会话存储。"""

    def __init__(
        self,
        *,
        max_sessions: int = 2000,
        ttl_seconds: float = 3 * 3600,
        max_turns: int = 10,
        clock=time.monotonic,
    ) -> None:
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self._clock = clock
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()

    # ---- 会话 key ----
    @staticmethod
    def private_key(user_id: str) -> str:
        return f"private:{user_id}"

    @staticmethod
    def group_key(group_id: str, user_id: str) -> str:
        return f"group:{group_id}:user:{user_id}"

    # ---- 读写 ----
    def get(self, key: str) -> Session:
        """取得会话（不存在则创建）；TTL 只由真实消息刷新。"""
        self._evict_expired()
        session = self._sessions.get(key)
        if session is None:
            session = Session(key=key, updated_at=self._clock())
            self._sessions[key] = session
        else:
            self._sessions.move_to_end(key)
        return session

    def history(self, key: str) -> list[dict[str, str]]:
        """只读：没有历史时返回空列表，不会新建会话。"""
        self._evict_expired()
        session = self._sessions.get(key)
        return session.history() if session else []

    def append(self, key: str, role: str, content: str) -> Session:
        session = self.get(key)
        session.append(role, content, max_turns=self.max_turns)
        session.updated_at = self._clock()
        self._sessions.move_to_end(key)
        return session

    def reset(self, key: str) -> bool:
        """清空指定会话，返回之前是否存在。"""
        self._sessions.pop(key, None)
        return True

    def clear(self) -> None:
        self._sessions.clear()

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._sessions)

    # ---- 内部 ----
    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.updated_at > self.ttl_seconds
        ]
        for key in expired:
            self._sessions.pop(key, None)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
