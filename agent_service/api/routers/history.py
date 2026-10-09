"""会话历史路由。

C2：会话域归 OCRServer，本服务**只读**。
- 列表 / 详情：读 MySQL（只读账号），返回结构与旧实现一致；
- 创建 / 删除：返回 501（由 OCRServer 负责），避免跨域写坏账。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...core.errors import NotFound, SessionDomainReadOnly
from ...repositories.sessions import session_repo
from ..deps import resolve_user_id

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
async def list_history(
    request: Request,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    userId: str = Query(default=""),
):
    user_id = await resolve_user_id(request, fallback_user_id=userId)
    return await session_repo.list_conversations(user_id, page, pageSize)


@router.get("/{conversation_id}")
async def get_history(
    conversation_id: str, request: Request, userId: str = Query(default="")
):
    user_id = await resolve_user_id(request, fallback_user_id=userId)
    detail = await session_repo.load_conversation(conversation_id, user_id)
    if detail is None:
        raise NotFound("对话不存在")
    return detail


@router.post("")
async def create_history():
    """C2：创建会话由 OCRServer 负责；这里直接 501，不必先鉴权（不触碰任何数据）。"""
    raise SessionDomainReadOnly("创建会话由 OCRServer 负责，本服务只读会话域")


@router.delete("/{conversation_id}")
async def delete_history(conversation_id: str):
    """C2：删除会话由 OCRServer 负责；这里直接 501。"""
    raise SessionDomainReadOnly()
