"""plane/vector_store.py — 用真实向量库（Qdrant）做记忆检索。

**为什么换**：`InMemoryProvider` + 逐条算相似度是 O(记录数) 全扫。
记录到几千条时，每轮召回都要把全部文本算一遍相似度 —— 这是真实的性能墙，
而且没有 ANN 索引、没有 payload 过滤下推。

Qdrant 本地模式（`:memory:` 或磁盘目录）**不需要起服务**，`pip install qdrant-client` 即可，
所以它满足"该用的组件要用"又不引入运维负担。

**跨租户隔离**（INV-12）靠 payload filter 在库内完成，不是取回来再过滤 ——
后者会让别人的数据进入进程内存，且分页/limit 语义会被污染。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Sequence

from contracts import Chunk, MemoryKind, MemoryRecord, Subject

__all__ = [
    "VectorStoreError", "VectorProvider", "vector_memory", "qdrant_available",
    "make_subject_filter",
]

try:                                                           # 可选依赖
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, FieldCondition, Filter, MatchValue, PointIdsList, PointStruct,
        VectorParams,
    )
    _QDRANT = True
except Exception:                                              # noqa: BLE001
    _QDRANT = False
    QdrantClient = None                                        # type: ignore


class VectorStoreError(RuntimeError):
    """向量库操作失败（嵌入失败、维度不一致、库不可用）。"""


def qdrant_available() -> bool:
    return _QDRANT


# ══════════════════════════════════════════════════════════════
# payload filter：跨租户隔离的物理形态
# ══════════════════════════════════════════════════════════════
def make_subject_filter(subject: Subject) -> Any:
    """把 Subject 转成 Qdrant filter。

    隔离规则（与 `InMemoryProvider._visible` 同语义）：
    * `tenant` **必须**相等（多租户强隔离的底线）
    * `user_id` 相等，**或** `team` 且 `project` 都相等（团队共享层）
    空 subject 直接抛错 —— 无主查询不该放行任何数据（fail-closed）。
    """
    if not _QDRANT:
        raise VectorStoreError("qdrant-client 未安装")
    if not subject or not (subject.tenant or "").strip():
        raise VectorStoreError("subject.tenant 为空：拒绝无主语查询（INV-12）")

    must = [FieldCondition(key="tenant", match=MatchValue(value=subject.tenant))]
    own = [FieldCondition(key="user_id", match=MatchValue(value=subject.user_id or ""))]
    shared: list[Any] = []
    if subject.team:
        shared.append(
            FieldCondition(key="team", match=MatchValue(value=subject.team)))
    if subject.project:
        shared.append(
            FieldCondition(key="project", match=MatchValue(value=subject.project)))

    if subject.user_id and not (subject.team or subject.project):
        return Filter(must=must + own)
    if subject.user_id and len(shared) == 2:
        # 自己 OR (同 team 且同 project)
        from qdrant_client.models import Filter as F
        return Filter(must=must, should=[
            F(must=own),
            F(must=shared),
        ])
    # 没有 user_id、只有 team/project：只用共享层
    return Filter(must=must + shared) if shared else Filter(must=must + own)


def subject_filter_dict(subject: Subject) -> dict:
    """Subject → **dict** filter（后端 SPI 的统一语义）。

    三个后端（memory/qdrant/milvus）都吃这个 dict，各自翻译成自己的查询语法。
    早期这里直接返回 qdrant 的 `Filter` 对象，等于把 qdrant 语义泄漏进了 Provider ——
    换 milvus 时立刻炸（`'Filter' object has no attribute 'get'`）。
    """
    if not subject or not (subject.tenant or "").strip():
        raise VectorStoreError("subject.tenant 为空：拒绝无主语查询（INV-12）")
    return {
        "tenant": subject.tenant,
        "user_id": subject.user_id or "",
        "team": subject.team or "",
        "project": subject.project or "",
    }


class VectorProvider:
    """实现 `plane.memory.Provider` 协议的向量库后端。"""

    def __init__(self, *, embedder: Any, location: str = ":memory:",
                 collection: str = "memory", dim: int | None = None,
                 backend: Any = None, backend_kind: str = "qdrant",
                 **backend_kw: Any) -> None:
        """`backend` 走 SPI（memory / qdrant / milvus），`backend_kind="auto"` 自动挑。

        **换库不改业务代码**：`vector_memory(..., backend_kind="milvus")` 即可，
        调用方（MemoryRuntime / Loop）完全无感。
        """
        if embedder is None or not callable(getattr(embedder, "embed", None)):
            raise VectorStoreError("embedder 必须提供 embed(texts) -> list[list[float]]")
        self.embedder = embedder
        self.collection = collection
        self._dim = int(dim) if dim else 0
        self._records: dict[str, MemoryRecord] = {}

        if backend is not None:
            self.backend = backend
        else:
            from plane.vector_backends import make_vector_backend
            # **按后端类型过滤参数**：memory 不吃 collection/location，
            # milvus 不吃 location。混投会 TypeError，而错误信息完全看不出
            # 是"参数给错了后端"（早期就是这么坑的）。
            accepted = {
                "memory": ("dim",),
                "qdrant": ("dim", "collection", "location", "url", "api_key"),
                "milvus": ("dim", "collection", "uri", "token"),
            }
            kind = backend_kind if backend_kind != "auto" else "qdrant"
            allowed = accepted.get(kind, ())
            kw = {"dim": self._dim}
            if "collection" in allowed:
                kw["collection"] = collection
            if "location" in allowed:
                kw["location"] = location
            kw.update({k: v for k, v in backend_kw.items() if k in allowed})
            try:
                self.backend = make_vector_backend(backend_kind, **kw)
            except Exception as exc:                           # noqa: BLE001
                raise VectorStoreError(
                    f"向量后端 {backend_kind!r} 初始化失败: {exc}") from exc
        if not self._dim:
            probe = self.embedder.embed(["dim probe"])
            if not probe or not probe[0]:
                raise VectorStoreError("embedder 返回空向量，无法确定维度")
            self._dim = len(probe[0])

    # ── 内部 ────────────────────────────────────────────────
    def _ensure_collection(self) -> None:
        """后端自己负责建集合；这里只做一次维度探测。"""
        if not self._dim:
            probe = self.embedder.embed(["dim probe"])
            if not probe or not probe[0]:
                raise VectorStoreError("embedder 返回空向量，无法确定维度")
            self._dim = len(probe[0])

    def _embed_one(self, text: str) -> list[float]:
        try:
            vecs = self.embedder.embed([text])
        except Exception as exc:                               # noqa: BLE001
            raise VectorStoreError(f"嵌入失败: {type(exc).__name__}: {exc}") from exc
        if not vecs or not vecs[0]:
            raise VectorStoreError("embedder 返回空向量")
        vec = list(vecs[0])
        if self._dim and len(vec) != self._dim:
            raise VectorStoreError(
                f"向量维度不一致: 期望 {self._dim}，实际 {len(vec)}（换模型要重建索引）")
        return vec

    @staticmethod
    def _point_id(rec_id: str) -> int:
        """Qdrant 只收 int 或 UUID 作为 id；字符串用稳定哈希映射。"""
        import hashlib
        return int(hashlib.sha256(rec_id.encode()).hexdigest()[:15], 16)

    # ── Provider 协议 ───────────────────────────────────────
    @staticmethod
    def _payload(rec: MemoryRecord) -> dict:
        """**subject 字段必须摊平到顶层**。

        Qdrant 的 filter 只能作用在顶层 key 上；把 tenant 嵌在 `subject.tenant` 里
        会导致 filter 静默不匹配（实测：写入成功、`scan` 返回 0 条）。
        这是本项目反复踩的同一类坑 —— 配置看起来对，实际不生效。
        """
        payload = rec.to_dict()
        subj = payload.pop("subject", None) or {}
        payload["tenant"] = subj.get("tenant", "")
        payload["user_id"] = subj.get("user_id", "")
        payload["team"] = subj.get("team", "")
        payload["project"] = subj.get("project", "")
        payload["subject"] = subj          # 原始形状也留着（便于还原）
        payload["_rec_id"] = rec.id
        payload["_alive"] = rec.is_alive()
        return payload

    def put(self, rec: MemoryRecord) -> None:
        vec = self._embed_one(rec.text)
        payload = self._payload(rec)
        try:
            self.backend.upsert(rec.id, vec, payload)
        except VectorStoreError:
            raise
        except Exception as exc:                               # noqa: BLE001
            raise VectorStoreError(f"upsert 失败: {exc}") from exc
        self._records[rec.id] = rec

    def get(self, rec_id: str) -> MemoryRecord | None:
        return self._records.get(rec_id)

    def scan(self, subject: Subject) -> list[MemoryRecord]:
        """返回该主体可见的全部记录（含已覆写，供审计）。

        隔离靠 filter 下推，不靠 Python 侧过滤 —— 后者会让别人的数据进内存。
        """
        flt = subject_filter_dict(subject)
        out: list[MemoryRecord] = []
        for rid, _payload in self.backend.scroll(flt, 10000):
            rec = self._records.get(rid)
            if rec is not None:
                out.append(rec)
        return out

    def delete(self, rec_id: str) -> bool:
        rec = self._records.pop(rec_id, None)
        if rec is None:
            return False
        try:
            self.backend.delete(rec_id)
        except Exception:                                      # noqa: BLE001
            pass
        return True

    # ── 向量检索（本类的价值）──────────────────────────────
    def search(self, query: str, subject: Subject, k: int = 5,
               *, min_score: float = 0.0,
               include_superseded: bool = False) -> list[tuple[MemoryRecord, float]]:
        flt = subject_filter_dict(subject)
        vec = self._embed_one(query)
        try:
            rows = self.backend.search(vec, flt, max(1, int(k)) * 4)
        except VectorStoreError:
            raise
        except Exception as exc:                               # noqa: BLE001
            raise VectorStoreError(f"query 失败: {exc}") from exc

        out: list[tuple[MemoryRecord, float]] = []
        for rid, score, _payload in rows:
            rec = self._records.get(rid)
            if rec is None:
                continue
            if not include_superseded and not rec.is_alive():
                continue
            if score < min_score:
                continue
            out.append((rec, float(score)))
            if len(out) >= k:
                break
        return out

    def rebuild_index(self) -> dict:
        """从内存记录重建（Qdrant 侧没有"重建"概念，这里是显式重放）。"""
        n = 0
        for rec in list(self._records.values()):
            vec = self._embed_one(rec.text)
            payload = self._payload(rec)
            self.backend.upsert(rec.id, vec, payload)
            n += 1
        return {"reindexed": n, "dim": self._dim}

    def stats(self) -> dict:
        try:
            cnt = self.backend.count()
        except Exception:                                      # noqa: BLE001
            cnt = -1
        info = self.backend.info()
        return {"backend": info.kind, "collection": self.collection,
                "dim": self._dim, "points": cnt, "records": len(self._records),
                "shared": info.shared, "ann": info.ann, "location": info.location}

    def close(self) -> None:
        try:
            self.backend.close()
        except Exception:                                      # noqa: BLE001
            pass


# ══════════════════════════════════════════════════════════════
# 接线：让 MemoryRuntime 用向量检索，但**保留多因子公式**
# ══════════════════════════════════════════════════════════════
#: `vector_memory` 自己的参数（其余全部透传给 MemoryRuntime）
_VECTOR_KEYS = ("location", "collection", "dim", "backend", "backend_kind",
                "url", "api_key", "uri", "token")


def vector_memory(memory_cls, *, embedder: Any, **kw):
    """返回一个「召回走 ANN、排序仍走多因子」的 MemoryRuntime。

    只把**语义相似度那一项**换成向量库给的余弦分；重要度、时效、Profile 加成、
    预算裁剪、命中续命全部沿用原实现 —— 换存储不该改变记忆策略。

    `backend_kind` 支持 `memory` / `qdrant` / `milvus` / `auto`。
    `location/collection/dim/url/...` 归向量后端，其余 keyword（如 `extractor`）
    原样透传给 MemoryRuntime —— 两类参数不能混投（早期就是混投导致 TypeError）。
    """
    vector_kw = {k: kw.pop(k) for k in list(kw) if k in _VECTOR_KEYS}
    provider = VectorProvider(embedder=embedder, **vector_kw)
    inst = memory_cls(provider=provider, **kw)
    inst.vector_provider = provider

    original_rank = memory_cls._rank

    def _rank(self, query, subject, k, budget_tokens, activate):
        if not (query or "").strip():
            return original_rank(self, query, subject, k, budget_tokens, activate)
        try:
            hits = self.vector_provider.search(query, subject,
                                               k=max(k * 3, k, 8))
        except Exception:                                      # noqa: BLE001
            return original_rank(self, query, subject, k, budget_tokens, activate)
        if not hits:
            return original_rank(self, query, subject, k, budget_tokens, activate)

        # 用向量分替换语义项，其余项（重要度/时效/Profile 加成）仍由原实现算。
        #
        # **不能**用「再调一次 _score 再减 old_sim」来倒推 —— 那样 base 可能取到
        # 别的记录（用 text 当 query 召回的第一条不一定是自己），extra 就错了。
        # 实测后果：分数算出 1.32 > 1.0（公式上限），排序被污染。
        import plane.memory as mem
        now = self.now() if callable(getattr(self, "now", None)) else mem.now_ms()
        rows: list[Any] = []
        for rec, vsim in hits:
            sem = mem.W_SIMILARITY * max(0.0, float(vsim))
            # 非语义项 = 原公式扣掉它的 sim 项（其余项与 sim 无关，直接算）
            if rec.kind is MemoryKind.PROFILE:
                recency = 1.0
            else:
                ttl = rec.ttl_days
                if ttl is None:
                    rule = mem._rule_for(rec.kind)
                    ttl = (rule.ttl_days if rule and rule.ttl_days else 90.0)
                anchor = rec.last_hit_at or rec.created_at or now
                age_days = max(0.0, (now - anchor) / mem.MS_PER_DAY)
                recency = math.exp(-age_days / max(ttl, 1e-9))
            extra = mem.W_IMPORTANCE * rec.importance + mem.W_RECENCY * recency
            if rec.kind is MemoryKind.PROFILE:
                extra += mem.PROFILE_BONUS
            rows.append((rec, sem + extra))
        rows.sort(key=lambda pair: (0 if pair[0].kind is MemoryKind.PROFILE else 1,
                                    -pair[1], pair[0].id))
        out: list[Chunk] = []
        used = 0
        for rec, score in rows:
            if used + len(rec.text) > budget_tokens and out:
                break
            used += len(rec.text)
            if activate:
                rec.hits += 1
                rec.last_hit_at = now
            out.append(Chunk(doc_id=rec.id, text=rec.text, score=score,
                             source="memory", kind=rec.kind.value))
            if len(out) >= k:
                break
        return out

    inst._rank = _rank.__get__(inst, type(inst))
    return inst