"""按 MySQL 切换脚本生成的清单清理旧测试会话，不清理用户级偏好。"""
import asyncio
import json
import sys
from pathlib import Path

from ..agent.runtime import AgentRuntime, thread_id_for
from ..core.config import settings
from ..repositories.checkpoints import open_checkpoint_store


async def discard(path: Path) -> dict:
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError("清单格式不正确")
    async with open_checkpoint_store(settings) as storage:
        runtime = AgentRuntime(storage=storage)
        for entry in entries:
            user_id, conversation_id = str(entry["userId"]), entry["conversationId"]
            await runtime.delete_session(user_id, conversation_id)
            # 本地测试数据整体重置，旧业务元数据已删除，不再保留测试墓碑。
            async with storage.pool.connection() as conn:
                await conn.execute("DELETE FROM session_heads WHERE thread_id=%s AND user_id=%s", (thread_id_for(user_id, conversation_id), user_id))
    return {"ok": True, "discardedTestSessions": len(entries)}


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(discard(Path(sys.argv[1])))))
    except Exception as exc:
        print(json.dumps({"ok": False, "errorType": type(exc).__name__}))
        raise SystemExit(1) from None
