"""Milvus 向量库封装（本地 standalone / 生产 Zilliz Cloud 双环境）。

相对旧实现的修正（均由实测驱动，见 DESIGN §4.4）：
1. **认证**：补上 `MILVUS_USER` / `MILVUS_PASSWORD`（旧代码从未读取，compose 注入的值被静默忽略）。
2. **建库策略分环境**：standalone 才建库；cloud 只校验库名（serverless 通常只有一个 `db_xxx`）。
3. **装载状态解析**：不再用 `str(state)` 子串匹配（`"{'state': <LoadState: Loaded>}"` 恒含 LOADED，会误判）。
4. **空结果假阴性**：刚装载后的短窗口内，检索为空会重试若干次，避免把「没就绪」当成「库里没有」而静默降级。
5. **取消逐条 flush**：实测不 flush 也可立即检索；云端按 CU 计费，逐条 flush 纯浪费。改为显式 `flush()`。
6. 超时与退避重试（云端需要）。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, List, Optional

from pymilvus import DataType, MilvusClient

from ..core.config import settings
from ..core.errors import VectorStoreError
from ..core.logging import get_logger
from . import embeddings

logger = get_logger("milvus")

try:  # 枚举比较最可靠；不同版本路径若变化则回落到字符串判断
    from pymilvus.client.types import LoadState  # type: ignore
except Exception:  # pragma: no cover
    LoadState = None  # type: ignore

#: 刚装载后的「预热窗口」：此窗口内检索为空时重试
_WARMUP_SECONDS = 60.0
_EMPTY_RETRY_TIMES = 3
_EMPTY_RETRY_INTERVAL = 0.25


class VectorStore:
    """Milvus 客户端封装（同步核心 + 异步包装）。"""

    def __init__(self) -> None:
        self._client: Optional[MilvusClient] = None
        self._ready: dict[str, bool] = {}
        #: 集合名 -> 装载完成的单调时刻
        self._loaded_at: dict[str, float] = {}
        self.scalar_index_supported: Optional[bool] = None

    # ── 客户端 ──
    def _get_client(self) -> MilvusClient:
        if self._client is None:
            logger.info("Milvus 客户端连接开始：mode=%s timeout=%ss", settings.milvus_mode_resolved, settings.MILVUS_TIMEOUT)
            kwargs: dict[str, Any] = {
                "uri": settings.MILVUS_URI,
                "timeout": settings.MILVUS_TIMEOUT,
            }
            # 显式用户名/密码可用于自托管集群；Token 仅用于 cloud 模式。
            # standalone 不携带从基础环境文件继承的云端 Token。
            if settings.MILVUS_USER and settings.MILVUS_PASSWORD:
                kwargs["user"] = settings.MILVUS_USER
                kwargs["password"] = settings.MILVUS_PASSWORD
            elif settings.milvus_mode_resolved == "cloud" and settings.MILVUS_TOKEN:
                kwargs["token"] = settings.MILVUS_TOKEN
            self._client = MilvusClient(**kwargs)
            logger.info(
                "Milvus 客户端已创建：mode=%s uri=%s",
                settings.milvus_mode_resolved,
                settings.MILVUS_URI,
            )
        return self._client

    def _ensure_database(self, client: MilvusClient) -> None:
        db_name = settings.MILVUS_DB
        if settings.milvus_mode_resolved == "cloud":
            # 托管集群：不建库，只校验（现有集群是 serverless，只有一个 db_xxx）
            if db_name and db_name not in ("", "default"):
                existing = client.list_databases()
                if db_name not in existing:
                    raise VectorStoreError(
                        f"云端 Milvus 不存在数据库 {db_name!r}（现有：{', '.join(existing) or '无'}）。"
                        "serverless 集群通常只有一个形如 db_xxx 的库：请把 MILVUS_DB 留空"
                        "（用 Token 默认库）或填既有库名。",
                        detail=f"MILVUS_DB={db_name}",
                    )
                client.use_database(db_name=db_name)
            return

        # 自托管 standalone：按需建库
        if db_name and db_name != "default":
            existing = client.list_databases()
            if db_name not in existing:
                client.create_database(db_name=db_name)
                logger.info("已创建 Milvus 数据库 %s", db_name)
            client.use_database(db_name=db_name)

    # ── 集合 ──
    def _wait_loaded(self, client: MilvusClient, name: str) -> bool:
        """解析真实装载状态（最多等约 4 秒）。"""
        for _ in range(20):
            try:
                state = client.get_load_state(collection_name=name)
            except Exception as exc:
                logger.warning("查询 %s 装载状态失败：%s", name, exc)
                return False
            value = state.get("state") if isinstance(state, dict) else state
            if LoadState is not None and value == LoadState.Loaded:
                return True
            if str(value).upper().endswith("LOADED"):
                return True
            time.sleep(0.2)
        return False

    def _create_component_collection(self, client: MilvusClient, name: str) -> None:
        """组件检索集合：schema 模式，带可过滤标量（云端已实测支持 INVERTED）。"""
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=settings.EMBED_DIM)
        schema.add_field("kind", DataType.VARCHAR, max_length=16)
        schema.add_field("category", DataType.VARCHAR, max_length=32)
        schema.add_field("visibility", DataType.VARCHAR, max_length=16)
        schema.add_field("status", DataType.VARCHAR, max_length=16)
        schema.add_field("owner_user_id", DataType.VARCHAR, max_length=64)
        schema.add_field("semantic_text", DataType.VARCHAR, max_length=2048)

        def _index_params(with_scalar: bool):
            params = client.prepare_index_params()
            params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
            if with_scalar:
                params.add_index(field_name="category", index_type="INVERTED")
            return params

        try:
            client.create_collection(
                collection_name=name, schema=schema, index_params=_index_params(True)
            )
            self.scalar_index_supported = True
        except Exception as exc:
            # 能力探测降级：标量索引不被支持时，去掉它重建（过滤仍可用，只是慢）
            logger.warning("标量索引 INVERTED 不可用，降级为无标量索引：%s", exc)
            self.scalar_index_supported = False
            client.create_collection(
                collection_name=name, schema=schema, index_params=_index_params(False)
            )

    def ensure_ready(self, collection: Optional[str] = None) -> None:
        """惰性初始化：库 + 集合 + 装载。幂等。

        把连接/建库/建集合/装载的失败统一包成 `VectorStoreError`（→ HTTP 503），
        否则客户端只会看到一个含义不明的 500。
        """
        name = collection or settings.MILVUS_COLLECTION
        if self._ready.get(name):
            return
        started = time.monotonic()
        logger.info("Milvus 初始化检查开始：db=%s collection=%s", settings.MILVUS_DB or "默认库", name)
        try:
            self._ensure_ready_inner(collection)
        except VectorStoreError as exc:
            logger.error("Milvus 初始化检查失败：collection=%s type=%s elapsed=%.2fs", name, type(exc).__name__, time.monotonic() - started)
            raise
        except Exception as exc:
            logger.error("Milvus 初始化检查失败：collection=%s type=%s elapsed=%.2fs", name, type(exc).__name__, time.monotonic() - started)
            raise VectorStoreError(
                "向量库初始化失败（请确认 Milvus 是否可用、MILVUS_URI/MODE/DB 是否正确）",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        logger.info("Milvus 初始化流程完成：collection=%s elapsed=%.2fs", name, time.monotonic() - started)

    def _ensure_ready_inner(self, collection: Optional[str] = None) -> None:
        name = collection or settings.MILVUS_COLLECTION
        if self._ready.get(name):
            return

        client = self._get_client()
        self._ensure_database(client)
        logger.info("Milvus 数据库检查通过：db=%s", settings.MILVUS_DB or "默认库")

        if not client.has_collection(collection_name=name):
            if name == settings.MILVUS_COMPONENT_COLLECTION:
                self._create_component_collection(client, name)
            else:
                client.create_collection(
                    collection_name=name,
                    dimension=settings.EMBED_DIM,
                    metric_type="COSINE",
                    id_type="string",
                    max_length=64,
                )
            logger.info("已创建 Milvus 集合 %s（dim=%d）", name, settings.EMBED_DIM)
        else:
            logger.info("Milvus 集合已存在：collection=%s", name)

        logger.info("Milvus 集合装载检查开始：collection=%s", name)
        try:
            client.load_collection(collection_name=name)
        except Exception as exc:  # 已装载时可能抛错，不致命
            logger.debug("load_collection(%s) 返回：%s", name, exc)

        if not self._wait_loaded(client, name):
            logger.warning("集合 %s 未在预期时间内变为 Loaded，首次检索将重试", name)
        else:
            logger.info("Milvus 集合装载检查通过：collection=%s", name)

        self._ready[name] = True
        self._loaded_at[name] = time.monotonic()

    # ── 写入 ──
    def upsert_texts(
        self,
        texts: List[str],
        sources: Optional[List[str]] = None,
        collection: Optional[str] = None,
        flush: bool = False,
    ) -> int:
        """向量化并写入一批文本。默认**不 flush**（实测不影响可检索性）。"""
        if not texts:
            return 0
        name = collection or settings.MILVUS_COLLECTION
        self.ensure_ready(name)
        client = self._get_client()

        vectors = embeddings.embed_documents(texts)
        data = [
            {
                "id": str(uuid.uuid4()),
                "vector": vectors[i],
                "text": text,
                "source": sources[i] if sources and i < len(sources) else "",
            }
            for i, text in enumerate(texts)
        ]

        def _do() -> None:
            client.upsert(collection_name=name, data=data)
            if flush:
                client.flush(collection_name=name)

        self._with_retry(_do, f"写入集合 {name}")
        return len(data)

    def flush(self, collection: Optional[str] = None) -> None:
        """显式落盘（批量导入收尾或进程退出前调用）。"""
        client = self._get_client()
        client.flush(collection_name=collection or settings.MILVUS_COLLECTION)

    # ── 检索 ──
    def search(
        self, query: str, top_k: Optional[int] = None, collection: Optional[str] = None
    ) -> List[dict]:
        """相似度检索，返回 `{text, source, score}`。"""
        name = collection or settings.MILVUS_COLLECTION
        self.ensure_ready(name)
        client = self._get_client()
        limit = top_k or settings.RAG_TOP_K
        vector = embeddings.embed_query(query)

        def _search():
            return client.search(
                collection_name=name,
                data=[vector],
                limit=limit,
                output_fields=["text", "source"],
            )

        results = self._with_retry(_search, f"检索集合 {name}")

        def _hits(raw) -> List[dict]:
            out = []
            for hit in (raw[0] if raw else []):
                entity = hit.get("entity", {}) or {}
                out.append(
                    {
                        "text": entity.get("text", ""),
                        "source": entity.get("source", ""),
                        "score": float(hit.get("distance", 0.0)),
                    }
                )
            return out

        hits = _hits(results)

        # 预热窗口内的空结果可能是「尚未就绪」的假阴性 → 有限重试
        if not hits and time.monotonic() - self._loaded_at.get(name, 0.0) < _WARMUP_SECONDS:
            for _ in range(_EMPTY_RETRY_TIMES):
                time.sleep(_EMPTY_RETRY_INTERVAL)
                hits = _hits(self._with_retry(_search, f"检索集合 {name}（预热重试）"))
                if hits:
                    logger.info("集合 %s 预热后检索到 %d 条（空结果重试生效）", name, len(hits))
                    break

        return hits

    def stats(self, collection: Optional[str] = None) -> dict:
        name = collection or settings.MILVUS_COLLECTION
        self.ensure_ready(name)
        return self._get_client().get_collection_stats(collection_name=name)

    def ping(self) -> dict:
        """真实探活：列出集合（不建集合，避免探活产生副作用）。"""
        client = self._get_client()
        collections = client.list_collections()
        return {
            "mode": settings.milvus_mode_resolved,
            "uri": settings.MILVUS_URI,
            "collections": collections,
            "ok": True,
        }

    # ── 工具 ──
    def _with_retry(self, fn, what: str):
        last: Optional[BaseException] = None
        for attempt in range(settings.MILVUS_MAX_RETRIES + 1):
            try:
                return fn()
            except Exception as exc:
                last = exc
                if attempt < settings.MILVUS_MAX_RETRIES:
                    time.sleep(0.3 * (attempt + 1))
        raise VectorStoreError(f"{what}失败", detail=f"{type(last).__name__}: {last}")

    # ── 异步包装（避免同步网络调用阻塞事件循环）──
    async def aupsert_texts(self, texts, sources=None, collection=None, flush=False) -> int:
        return await asyncio.to_thread(self.upsert_texts, texts, sources, collection, flush)

    async def asearch(self, query, top_k=None, collection=None) -> List[dict]:
        return await asyncio.to_thread(self.search, query, top_k, collection)

    async def astats(self, collection=None) -> dict:
        return await asyncio.to_thread(self.stats, collection)

    async def aping(self) -> dict:
        return await asyncio.to_thread(self.ping)

    async def aensure_ready(self, collection=None) -> None:
        return await asyncio.to_thread(self.ensure_ready, collection)


vector_store = VectorStore()
