"""本地假 DeepSeek（OpenAI 兼容）服务，用于离线验证 AstrBot → 大模型 这一段。

只实现 ``POST /v1/chat/completions``，支持普通 JSON 与流式 SSE 两种响应，
并把收到的请求（是否带 system 人设、历史条数）打印出来，方便核对人格是否生效。

用法：
    python scripts/mock_openai.py            # 监听 0.0.0.0:8899
    python scripts/mock_openai.py 9000       # 指定端口
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
CALLS: list[dict] = []


def _as_text(content) -> str:
    """OpenAI 的 content 可能是字符串，也可能是 [{type:text,text:...}] 列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text") or "")
                elif "text" in part:
                    parts.append(str(part["text"]))
        return "".join(parts)
    return "" if content is None else str(content)


def _reply_text(messages: list[dict]) -> str:
    system = next((m for m in messages if m.get("role") == "system"), None)
    user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    persona_hit = "大肥鱼" in _as_text((system or {}).get("content"))
    history = len([m for m in messages if m.get("role") in ("user", "assistant")]) - 1
    text = _as_text((user or {}).get("content"))
    text = text.replace("\n", " ")[:40]
    return (
        f"我是大肥鱼～收到「{text}」（人设注入={'是' if persona_hit else '否'}，"
        f"历史={history}条，消息={len(messages)}条）"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        CALLS.append({"model": body.get("model"), "messages": messages})
        print(
            f"[mock-openai] 第 {len(CALLS)} 次调用 model={body.get('model')} "
            f"messages={len(messages)} stream={bool(body.get('stream'))}",
            flush=True,
        )
        reply = _reply_text(messages)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            for piece in (reply[i : i + 8] for i in range(0, len(reply), 8)):
                chunk = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model", "deepseek-chat"),
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            done = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "deepseek-chat"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "deepseek-chat"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        # AstrBot 可能探测 /models
        if self.path.rstrip("/").endswith("/models"):
            data = json.dumps(
                {"object": "list", "data": [{"id": "deepseek-chat", "object": "model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    print(f"[mock-openai] listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
