"""验证 PostgreSQL 检查点恢复；仅创建并清理独立测试会话，不调用模型 API。"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager

from langchain_core.messages import AIMessageChunk, HumanMessage
from langsmith import tracing_context

from ..agent.llm.factory import ModelSpec
from ..agent.runtime import AgentRuntime, thread_id_for
from ..core.config import settings
from ..core.errors import ConversationBusy
from ..repositories.checkpoints import CheckpointStore, open_checkpoint_store


class ProbeModel:
    def __init__(self) -> None:
        self.prompts = []

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        self.prompts.append(messages)
        yield AIMessageChunk(content="本地验证回复", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})


class ProbeCache:
    def __init__(self, model: ProbeModel) -> None:
        self.model = model

    @contextmanager
    def acquire(self, spec):
        yield self.model


async def verify() -> dict:
    settings.validate()
    if not settings.POSTGRES_DSN:
        raise ValueError("未配置 POSTGRES_DSN")
    conversation_id = "storage-probe-" + str(uuid.uuid4())
    thread_id = thread_id_for("0", conversation_id)
    marker = "恢复验证标记-" + str(uuid.uuid4())
    model = ProbeModel()
    cache = ProbeCache(model)
    first_spec = ModelSpec("deepseek", "probe-a", "probe-secret-not-persisted", "https://example.com")
    second_spec = ModelSpec("deepseek", "probe-b", "probe-secret-not-persisted", "https://example.com")
    try:
        async with open_checkpoint_store(settings) as storage:
            runtime = AgentRuntime(storage=storage)
            _ = [event async for event in runtime.stream([HumanMessage(content=marker)], spec=first_spec, cache=cache, user_id="0", conversation_id=conversation_id)]
        # 关闭连接池并重建执行图后，再次读取同一个会话。
        async with open_checkpoint_store(settings) as storage:
            runtime = AgentRuntime(storage=storage)
            try:
                _ = [event async for event in runtime.stream([HumanMessage(content="继续会话")], spec=second_spec, cache=cache, user_id="0", conversation_id=conversation_id)]
                state = await runtime.graph.aget_state({"configurable": {"thread_id": thread_id}})
                assert marker in str(model.prompts[1]), "重建后的执行图未恢复历史"
                assert state.values["model_selection"]["model"] == "probe-b", "模型切换未生效"
                assert "probe-secret-not-persisted" not in str(state.values), "路由密钥进入了状态"
                async with storage.lease(thread_id):
                    competing = CheckpointStore(storage.pool)
                    try:
                        async with competing.lease(thread_id):
                            raise AssertionError("另一个运行时意外获取了同一会话锁")
                    except ConversationBusy:
                        pass
                async with storage.pool.connection() as conn:
                    row = await (await conn.execute("SELECT count(*) AS count FROM checkpoints WHERE thread_id = %s", (thread_id,))).fetchone()
                    if settings.POSTGRES_PRUNE_CHECKPOINTS:
                        assert row["count"] == 1, "旧检查点未清理"
                    roles = await (await conn.execute("SELECT rolname, has_schema_privilege(rolname, %s, 'USAGE') AS exposed FROM pg_roles WHERE rolname IN ('anon', 'authenticated')", (settings.POSTGRES_SCHEMA,))).fetchall()
                    assert not any(role["exposed"] for role in roles), "私有 schema 意外开放给 Data API 角色"
                    blobs = await (await conn.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))).fetchall()
                    assert not any(b"probe-secret-not-persisted" in bytes(row["blob"] or b"") for row in blobs), "路由密钥进入了检查点序列化数据"
                return {"ok": True, "connection": True, "restartRecovery": True, "modelSwitch": True, "privateSchema": True, "distributedLock": True, "credentialIsolation": True, "checkpointCompaction": settings.POSTGRES_PRUNE_CHECKPOINTS}
            finally:
                await runtime.delete_session("0", conversation_id)
    except BaseException:
        # 首轮已提交而后续验证失败时，仍尝试清理本次专属会话。
        try:
            async with open_checkpoint_store(settings) as storage:
                await storage.saver.adelete_thread(thread_id)
                await storage.messages.delete_session(thread_id, "0", conversation_id)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        with tracing_context(enabled=False):
            print(json.dumps(asyncio.run(verify()), ensure_ascii=False))
    except Exception as exc:
        # 数据库连接异常可能含连接信息，仅输出异常类型。
        print(json.dumps({"ok": False, "errorType": type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(1) from None
