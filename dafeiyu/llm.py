"""DeepSeek 对话客户端（OpenAI Chat Completions 兼容接口）。

只依赖 httpx，方便测试时注入假 client / mock 传输层。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败（网络、鉴权、限流等）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 90.0,
        temperature: float = 0.9,
        max_tokens: int = 800,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._transport = transport
        self.max_retries = max_retries

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def build_messages(system_prompt: str, history: list[dict[str, str]], user_text: str):
        """拼装发给模型的 messages：人设 + 历史 + 本轮输入。"""
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        return messages

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        """调用 chat/completions，返回纯文本回复。"""
        if not self.api_key:
            raise DeepSeekError("未配置 DeepSeek API Key（DAFEIYU_DEEPSEEK_API_KEY）")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    transport=self._transport,
                    headers=self._headers(),
                ) as client:
                    resp = await client.post(self.endpoint, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        content = data["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover
                        raise DeepSeekError(f"DeepSeek 返回格式异常: {data!r}") from exc
                    return content.strip()
                if resp.status_code in (401, 403):
                    raise DeepSeekError(
                        f"DeepSeek 鉴权失败（HTTP {resp.status_code}），请检查 API Key", status=resp.status_code
                    )
                if resp.status_code == 402:
                    raise DeepSeekError("DeepSeek 账户余额不足（HTTP 402）", status=402)
                body = resp.text[:300]
                last_error = DeepSeekError(
                    f"DeepSeek 请求失败（HTTP {resp.status_code}）: {body}", status=resp.status_code
                )
            except DeepSeekError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
                last_error = DeepSeekError(f"连接 DeepSeek 失败: {exc}")
            if attempt < self.max_retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def close(self) -> None:  # 预留：当前每次请求独立 client
        return None
