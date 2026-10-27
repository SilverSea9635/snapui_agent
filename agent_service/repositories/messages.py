"""业务消息与会话事件；所有写入通过同一仓储维护事务一致性。"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, HumanMessage
from psycopg.types.json import Jsonb

from ..core.errors import BadRequest, ConversationBusy, Gone, NotFound


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_heads (
    thread_id text PRIMARY KEY,
    user_id text NOT NULL,
    conversation_id text NOT NULL,
    last_seq bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    deleted_at timestamptz,
    UNIQUE (user_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY,
    thread_id text NOT NULL REFERENCES session_heads(thread_id),
    turn_id uuid NOT NULL,
    seq bigint NOT NULL CHECK (seq > 0),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content jsonb NOT NULL,
    model_content text,
    model text,
    usage jsonb,
    status text NOT NULL CHECK (status IN ('streaming', 'completed', 'cancelled', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, seq),
    UNIQUE (thread_id, turn_id, role)
);
CREATE TABLE IF NOT EXISTS session_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id text NOT NULL REFERENCES session_heads(thread_id),
    turn_id uuid NOT NULL,
    event_type text NOT NULL,
    event_key text NOT NULL DEFAULT '',
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, turn_id, event_type, event_key)
);
CREATE INDEX IF NOT EXISTS session_events_thread_order_idx ON session_events(thread_id, id);
ALTER TABLE session_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_events ENABLE ROW LEVEL SECURITY;
"""


def safe_content(value: Any) -> str | list:
    """只接收正文或稳定附件引用，不保存像素、签名地址或任意扩展字段。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise BadRequest("原始消息必须是文字或内容块数组")
    blocks = []
    for block in value:
        if hasattr(block, "model_dump"):
            block = block.model_dump()
        if not isinstance(block, dict):
            raise BadRequest("原始消息内容块格式不正确")
        if block.get("type") == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif block.get("type") == "image" and block.get("attachmentId"):
            try:
                attachment_id = str(UUID(str(block["attachmentId"])))
            except (ValueError, TypeError, AttributeError):
                raise BadRequest("附件标识格式不正确") from None
            source = block.get("source") or {}
            if not isinstance(source, dict):
                raise BadRequest("附件元数据格式不正确")
            blocks.append({
                "type": "image", "attachmentId": attachment_id,
                "source": {"type": "attachment", "media_type": source.get("media_type"), "bytes": source.get("bytes")},
                **{key: block[key] for key in ("fileName", "width", "height") if key in block},
            })
        else:
            raise BadRequest("图片消息必须使用已上传的附件引用")
    return blocks


def view(row: dict) -> dict:
    return {
        "id": str(row["id"]), "role": row["role"], "content": row["content"],
        "createdAt": row["created_at"].isoformat(), "seq": row["seq"],
        "status": row["status"], "model": row["model"], "usage": row["usage"],
    }


class MessageRepository:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def begin_turn(self, thread_id: str, user_id: str, conversation_id: str, turn_id: str, assistant_id: str, content: Any, model_content: str, model: str) -> dict | None:
        content = safe_content(content)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("INSERT INTO session_heads(thread_id,user_id,conversation_id) VALUES (%s,%s,%s) ON CONFLICT (thread_id) DO NOTHING", (thread_id, user_id, conversation_id))
            head = await (await conn.execute("SELECT * FROM session_heads WHERE thread_id=%s AND user_id=%s AND conversation_id=%s FOR UPDATE", (thread_id, user_id, conversation_id))).fetchone()
            if not head or head["deleted_at"]:
                raise Gone()
            prior = await (await conn.execute("SELECT * FROM messages WHERE thread_id=%s AND turn_id=%s ORDER BY seq", (thread_id, turn_id))).fetchall()
            if prior:
                if len(prior) != 2 or prior[0]["content"] != content or str(prior[1]["id"]) != assistant_id:
                    raise BadRequest("消息标识已用于其他请求")
                if prior[1]["status"] == "completed":
                    return view(prior[1])
                raise ConversationBusy("该请求已经提交，请读取历史或使用新的消息标识继续")
            # 已拿到跨进程会话锁，其他 streaming 行属于异常退出遗留，不能一直显示生成中。
            await conn.execute("UPDATE messages SET status='cancelled',updated_at=now() WHERE thread_id=%s AND status='streaming'", (thread_id,))
            seq = head["last_seq"]
            await conn.execute("UPDATE session_heads SET last_seq=last_seq+2 WHERE thread_id=%s", (thread_id,))
            await conn.execute("INSERT INTO messages(id,thread_id,turn_id,seq,role,content,model_content,model,status) VALUES (%s,%s,%s,%s,'user',%s,%s,%s,'completed')", (turn_id, thread_id, turn_id, seq+1, Jsonb(content), model_content, model))
            await conn.execute("INSERT INTO messages(id,thread_id,turn_id,seq,role,content,model,status) VALUES (%s,%s,%s,%s,'assistant',%s,%s,'streaming')", (assistant_id, thread_id, turn_id, seq+2, Jsonb(""), model))
            await self._event(conn, thread_id, turn_id, "user/message", {"id": turn_id, "content": content})
        return None

    async def update_partial(self, thread_id: str, assistant_id: str, text: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE messages SET content=%s,updated_at=now() WHERE thread_id=%s AND id=%s AND status='streaming'", (Jsonb(text), thread_id, assistant_id))

    async def finish_turn(self, thread_id: str, turn_id: str, assistant_id: str, text: str, usage: dict, status: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE messages SET content=%s,usage=%s,status=%s,updated_at=now() WHERE thread_id=%s AND id=%s AND status='streaming'", (Jsonb(text), Jsonb(usage), status, thread_id, assistant_id))
            await self._event(conn, thread_id, turn_id, "assistant/" + status, {"id": assistant_id, "content": text, "usage": usage})

    @staticmethod
    async def _event(conn, thread_id: str, turn_id: str, event_type: str, payload: dict, key: str = "") -> None:
        await conn.execute("INSERT INTO session_events(thread_id,turn_id,event_type,event_key,payload) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (thread_id, turn_id, event_type, key, Jsonb(payload)))

    async def append_event(self, thread_id: str, turn_id: str, event_type: str, payload: dict, key: str = "") -> None:
        async with self.pool.connection() as conn:
            await self._event(conn, thread_id, turn_id, event_type, payload, key)

    async def list_messages(self, thread_id: str, user_id: str, *, limit: int = 100, before: int | None = None) -> dict:
        async with self.pool.connection() as conn:
            head = await (await conn.execute("SELECT deleted_at FROM session_heads WHERE thread_id=%s AND user_id=%s", (thread_id, user_id))).fetchone()
            if head and head["deleted_at"]:
                raise Gone()
            rows = await (await conn.execute("SELECT * FROM messages WHERE thread_id=%s AND (%s::bigint IS NULL OR seq<%s) ORDER BY seq DESC LIMIT %s", (thread_id, before, before, limit+1))).fetchall() if head else []
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        return {"messages": [view(row) for row in rows], "nextCursor": rows[0]["seq"] if has_more else None}

    async def model_history(self, thread_id: str, exclude_turn_id: str, limit: int) -> list:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute("SELECT role,content,model_content,id FROM messages WHERE thread_id=%s AND turn_id<>%s AND status<>'streaming' ORDER BY seq DESC LIMIT %s", (thread_id, exclude_turn_id, limit))).fetchall()
        result = []
        for row in reversed(rows):
            text = row["model_content"] if row["role"] == "user" else row["content"]
            if isinstance(text, str) and text:
                cls = HumanMessage if row["role"] == "user" else AIMessage
                result.append(cls(content=text, id=str(row["id"])))
        return result

    async def get_message(self, thread_id: str, user_id: str, message_id: str) -> dict:
        async with self.pool.connection() as conn:
            head = await (await conn.execute("SELECT deleted_at FROM session_heads WHERE thread_id=%s AND user_id=%s", (thread_id, user_id))).fetchone()
            if head and head["deleted_at"]:
                raise Gone()
            row = await (await conn.execute("SELECT * FROM messages WHERE thread_id=%s AND id=%s", (thread_id, message_id))).fetchone() if head else None
        if row is None:
            raise NotFound("消息不存在")
        return view(row)

    async def delete_session(self, thread_id: str, user_id: str, conversation_id: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("INSERT INTO session_heads(thread_id,user_id,conversation_id,deleted_at) VALUES (%s,%s,%s,now()) ON CONFLICT (thread_id) DO UPDATE SET deleted_at=now()", (thread_id, user_id, conversation_id))
            await conn.execute("DELETE FROM session_events WHERE thread_id=%s", (thread_id,))
            await conn.execute("DELETE FROM messages WHERE thread_id=%s", (thread_id,))
