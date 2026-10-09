"""请求 / 响应数据结构。

`message` 字段与上游 OCRServer 及前端约定保持一致，兼容两种形态：
- 纯文本字符串；
- Anthropic 风格的内容块数组（image + text）。
P0 只新增可选字段（`attachments` / `modelConfig`），不改既有字段语义。
"""
from __future__ import annotations

from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field


class ImageSource(BaseModel):
    """图片内容块中的 source。"""

    type: str = "base64"
    media_type: Optional[str] = None
    data: str = ""
    #: 落库前剥离像素后留下的原始字节数（由 OCRServer 侧写入历史时附加）
    bytes: Optional[int] = None


class ContentBlock(BaseModel):
    """内容块；P0 只用到 text / image。"""

    type: str
    text: Optional[str] = None
    source: Optional[ImageSource] = None


Message = Union[str, List[ContentBlock]]


class Attachment(BaseModel):
    """引入对话上下文的附件（把组件源码带进 prompt）。"""

    type: str = "component"  # component
    componentId: Optional[str] = None
    version: Optional[int] = None
    #: 允许直接粘贴源码（广场复制场景）；有 componentId 时以服务端读取的源码为准
    code: Optional[str] = None
    scope: str = "turn"  # turn | sticky


class ModelConfigIn(BaseModel):
    """用户自带模型（P4 落地；P0 接受字段但不支持，收到即明确报错，绝不静默忽略）。"""

    provider: str = "openai-compatible"
    baseUrl: str
    modelName: str
    apiKey: str
    timeoutSeconds: Optional[float] = None
    extraBody: Optional[dict[str, Any]] = None


class ChatRequest(BaseModel):
    """POST /api/chat 请求体（与上游契约一致，仅新增可选字段）。"""

    conversationId: Optional[str] = None
    message: Message
    system: Optional[str] = None
    model: Optional[str] = None
    #: 上游鉴权后转发的用户标识（users.id 的十进制字符串）
    userId: Optional[str] = None
    #: 新增：把组件源码引入上下文（P2 使用；P0 已解析并记录，暂不参与生成）
    attachments: Optional[List[Attachment]] = None
    #: 新增：用户自带模型（P4）
    modelConfig: Optional[ModelConfigIn] = None
    modelProfileId: Optional[str] = None


class IngestRequest(BaseModel):
    """知识库写入：纯文本列表或带元数据的文档列表。"""

    texts: Optional[List[str]] = None
    documents: Optional[List[dict]] = Field(default=None, description="每条含 text/source")
    collection: Optional[str] = None


class SearchRequest(BaseModel):
    """知识库检索。"""

    query: str
    top_k: int = 4
    collection: Optional[str] = None


class CreateConversationRequest(BaseModel):
    """创建会话（P0 起该能力归 OCRServer，本服务只保留请求模型以免破坏导入）。"""

    title: str = ""
    userId: Optional[str] = None
