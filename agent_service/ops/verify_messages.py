"""用本地模拟模型联调 Node、FastAPI、MySQL 与真实 PostgreSQL。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from pathlib import Path
from unittest.mock import AsyncMock, patch

import uvicorn
from langchain_core.messages import AIMessageChunk, HumanMessage
from langsmith import tracing_context

from .. import main
from ..agent import runtime as runtime_module
from ..agent.llm import cache as cache_module
from ..core.config import settings


class VerificationModel:
    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        last = messages[-1]
        if isinstance(last, HumanMessage) and (match := re.search(r"MEMORY_KEY=(probe_[a-f0-9]+)", str(last.content))):
            yield AIMessageChunk(content="", tool_call_chunks=[{"name": "remember_preference", "args": json.dumps({"key": match[1], "value": "使用简短中文"}), "id": "remember-preference", "index": 0}])
        else:
            prefix = "偏好已恢复。" if "使用简短中文" in str(messages[0].content) else ""
            yield AIMessageChunk(content=prefix + "本地联调回复", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})


async def verify() -> dict:
    node_root = Path(__file__).resolve().parents[3] / "OCRServer"
    if not (node_root / "scripts/verify_agent_messages.js").exists():
        raise FileNotFoundError("请先应用 OCRServer 配套代码")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    patches = [
        patch.object(settings, "INTERNAL_AUTH_MODE", "enforce"),
        patch.object(settings, "INTERNAL_API_TOKEN", "temporary-integration-shared-secret"),
        patch.object(settings, "AVAILABLE_MODELS", ["fixture-a", "fixture-b"]),
        patch.object(runtime_module, "build_tools", return_value=[]),
        patch.object(cache_module, "build_chat_model", side_effect=lambda _: VerificationModel()),
        patch.object(main.vector_store, "aping", new=AsyncMock(return_value={"ok": True, "collections": []})),
        patch.object(main.vector_store, "asearch", new=AsyncMock(return_value=[])),
    ]
    for item in patches:
        item.start()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(45):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("临时服务未启动")
                await asyncio.sleep(0.1)
        env = {
            **os.environ, "AGENT_SERVICE_ENABLED": "true", "AGENT_SERVICE_URL": f"http://127.0.0.1:{port}",
            "INTERNAL_API_TOKEN": "temporary-integration-shared-secret",
            "MIMO_KEY": "fixture-not-used", "MIMO_PREFIX": "https://example.com/v1",
        }
        process = await asyncio.create_subprocess_exec("node", "scripts/verify_agent_messages.js", cwd=node_root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        lines = stdout.decode().strip().splitlines()
        if process.returncode:
            # Node 验证器只输出安全的断言信息，不输出凭据或数据库正文。
            raise RuntimeError(stderr.decode().strip()[-1200:])
        return json.loads(lines[-1])
    finally:
        server.should_exit = True
        await task
        for item in reversed(patches):
            item.stop()


if __name__ == "__main__":
    try:
        with tracing_context(enabled=False):
            print(json.dumps(asyncio.run(verify()), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "errorType": type(exc).__name__, "detail": str(exc) if isinstance(exc, RuntimeError) else None}, ensure_ascii=False))
        raise SystemExit(1) from None
