"""plane/vector_backends.py — 向量库后端：三种实现，运行时可配。

**为什么要三个实现**（这是"可选可配置"的具体含义）：

| 后端 | 场景 | 依赖 | 特点 |
|---|---|---|---|
| `memory` | 单测 / 本地冒烟 | 无 | 暴力扫描，零依赖，确定性 |
| `qdrant` | 单机部署 / 边缘 | `qdrant-client` | 本地模式不用起服务，ANN + filter 下推 |
| `milvus` | 生产集群 | `pymilvus` | 水平扩展，需 Milvus 服务 |

三者实现同一个 `contracts.VectorBackend` 协议，**换库不改业务代码**。

统一约定（三个实现都遵守，测试逐条验证）：
1. payload 里 `tenant/user_id/team/project` **摊平到顶层** —— filter 只作用在顶层字段
2. filter 是 dict：`{"tenant": "acme", "user_id": "u1", "team": "", "project": ""}`，
   语义为「tenant 必等 AND (user_id 相等 OR (team 且 project 都相等))」
3. 跨租户查询在**库内**完成，不取回来再过滤（后者会让别人数据进进程内存）
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Iterable, Sequence

from contracts import VectorBackendInfo

__all__ = [
    "VectorBackendError",
    "MemoryVectorBackend",
    "QdrantVectorBackend",
    "MilvusVectorBackend",
    "make_vector_backend",
    "available_backends",
    "configure_vector_backend",
]

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, FieldCondition, Filter, MatchValue, PointIdsList, PointStruct,
        VectorParams,
    )
    _QDRANT = True
except Exception:                                              # noqa: BLE001
    _QDRANT = False

try:
    from pymilvus import DataType, MilvusClient
    _MILVUS = True
except Exception:                                              # noqa: BLE001
    _MILVUS = False


class VectorBackendError(RuntimeError):
    """后端操作失败（不可用、维度不一致、查询失败）。"""


# ══════════════════════════════════════════════════════════════
# filter 归一化（三个后端共用同一套语义）
# ══════════════════════════════════════════════════════════════
def _matches(payload: dict, flt: dict | None) -> bool:
    """内存后端的 filter 语义实现（也是另外两个后端的**语义基准**）。"""
    if not flt:
        return True
    tenant = flt.get("tenant")
    if tenant is not None and payload.get("tenant") != tenant:
        return False
    user_id = flt.get("user_id") or ""
    team = flt.get("team") or ""
    project = flt.get("project") or ""
    if user_id and payload.get("user_id") == user_id:
        return True
    if team and project and payload.get("team") == team and payload.get("project") == project:
        return True
    if not user_id and not (team and project):
        # 只有 tenant 的查询：能看到该租户下所有（管理/审计视角）
        return True
    return False


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else 0.0


# ══════════════════════════════════════════════════════════════
# ① 内存后端（零依赖，确定性强，用作语义基准与降级路径）
# ══════════════════════════════════════════════════════════════
class MemoryVectorBackend:
    """暴力扫描。小数据量够用，且**是另外两个后端的语义参照**。"""

    name = "memory"

    def __init__(self, *, dim: int = 0) -> None:
        self._dim = int(dim)
        self._points: dict[str, tuple[list[float], dict]] = {}

    def upsert(self, point_id: str, vector: Sequence[float], payload: dict) -> None:
        vec = list(vector)
        if not vec:
            raise VectorBackendError("向量为空")
        if self._dim and len(vec) != self._dim:
            raise VectorBackendError(
                f"维度不一致: 期望 {self._dim}，实际 {len(vec)}")
        if not self._dim:
            self._dim = len(vec)
        self._points[point_id] = (vec, dict(payload))

    def delete(self, point_id: str) -> bool:
        return self._points.pop(point_id, None) is not None

    def search(self, vector: Sequence[float], flt: dict | None,
               limit: int) -> list[tuple[str, float, dict]]:
        rows = []
        for pid, (vec, payload) in self._points.items():
            if not _matches(payload, flt):
                continue
            rows.append((pid, _cosine(vector, vec), payload))
        rows.sort(key=lambda r: (-r[1], r[0]))          # 确定性：同分按 id
        return rows[:max(0, int(limit))]

    def scroll(self, flt: dict | None, limit: int = 256) -> list[tuple[str, dict]]:
        rows = [(pid, payload) for pid, (_, payload) in self._points.items()
                if _matches(payload, flt)]
        rows.sort(key=lambda r: r[0])
        return rows[:max(0, int(limit))]

    def count(self) -> int:
        return len(self._points)

    def close(self) -> None:
        self._points.clear()

    def info(self) -> VectorBackendInfo:
        return VectorBackendInfo(
            kind="memory", available=True, shared=False, ann=False,
            dim=self._dim, location=":memory:",
            reason="进程内暴力扫描（零依赖；多副本不共享）")


# ══════════════════════════════════════════════════════════════
# ② Qdrant 后端（本地模式或远程服务）
# ══════════════════════════════════════════════════════════════
class QdrantVectorBackend:
    name = "qdrant"

    def __init__(self, *, location: str = ":memory:", url: str = "",
                 api_key: str = "", collection: str = "vectors",
                 dim: int = 0) -> None:
        if not _QDRANT:
            raise VectorBackendError("qdrant-client 未安装（pip install qdrant-client）")
        self.collection = collection
        self._dim = int(dim)
        if url:
            self._client = QdrantClient(url=url, api_key=api_key or None)
            self._location = url
        else:
            self._client = QdrantClient(location=location or ":memory:")
            self._location = location or ":memory:"
        self._ready = False
        if self._dim:
            self._ensure_collection()

    def _ensure_collection(self) -> None:
        if self._ready:
            return
        names = {c.name for c in self._client.get_collections().collections}
        if self.collection not in names:
            if not self._dim:
                raise VectorBackendError("首次创建集合需要 dim")
            self._client.create_collection(
                self.collection,
                vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE))
        self._ready = True

    @staticmethod
    def _to_filter(flt: dict | None):
        if not flt:
            return None
        must = []
        if flt.get("tenant") is not None:
            must.append(FieldCondition(key="tenant",
                                       match=MatchValue(value=flt["tenant"])))
        user_id = flt.get("user_id") or ""
        team = flt.get("team") or ""
        project = flt.get("project") or ""
        if user_id and not (team and project):
            must.append(FieldCondition(key="user_id", match=MatchValue(value=user_id)))
            return Filter(must=must)
        if user_id and team and project:
            return Filter(must=must, should=[
                Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]),
                Filter(must=[FieldCondition(key="team", match=MatchValue(value=team)),
                             FieldCondition(key="project",
                                            match=MatchValue(value=project))]),
            ])
        if team and project:
            must += [FieldCondition(key="team", match=MatchValue(value=team)),
                     FieldCondition(key="project", match=MatchValue(value=project))]
        return Filter(must=must) if must else None

    def upsert(self, point_id: str, vector: Sequence[float], payload: dict) -> None:
        vec = list(vector)
        if not self._dim:
            self._dim = len(vec)
        self._ensure_collection()
        if len(vec) != self._dim:
            raise VectorBackendError(
                f"维度不一致: 期望 {self._dim}，实际 {len(vec)}")
        try:
            self._client.upsert(self.collection, points=[
                PointStruct(id=_stable_id(point_id), vector=vec,
                            payload={**payload, "_pid": point_id})])
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"qdrant upsert 失败: {exc}") from exc

    def delete(self, point_id: str) -> bool:
        try:
            self._client.delete(self.collection, points_selector=PointIdsList(
                points=[_stable_id(point_id)]))
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def search(self, vector: Sequence[float], flt: dict | None,
               limit: int) -> list[tuple[str, float, dict]]:
        if not self._ready:
            return []
        try:
            resp = self._client.query_points(
                self.collection, query=list(vector), limit=max(1, int(limit)),
                query_filter=self._to_filter(flt), with_payload=True)
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"qdrant query 失败: {exc}") from exc
        out = []
        for p in resp.points:
            pl = dict(p.payload or {})
            pid = pl.pop("_pid", str(p.id))
            out.append((pid, float(p.score or 0.0), pl))
        return out

    def scroll(self, flt: dict | None, limit: int = 256) -> list[tuple[str, dict]]:
        if not self._ready:
            return []
        out: list[tuple[str, dict]] = []
        offset = None
        f = self._to_filter(flt)
        while len(out) < limit:
            points, offset = self._client.scroll(
                self.collection, scroll_filter=f, limit=min(256, limit - len(out)),
                offset=offset, with_payload=True, with_vectors=False)
            for p in points:
                pl = dict(p.payload or {})
                out.append((pl.pop("_pid", str(p.id)), pl))
            if offset is None:
                break
        return out

    def count(self) -> int:
        if not self._ready:
            return 0
        try:
            return int(self._client.count(self.collection).count)
        except Exception:                                      # noqa: BLE001
            return 0

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                                      # noqa: BLE001
            pass

    def info(self) -> VectorBackendInfo:
        return VectorBackendInfo(
            kind="qdrant", available=True, shared=self._location != ":memory:",
            ann=True, dim=self._dim, location=self._location,
            reason="本地模式无需起服务" if self._location == ":memory:"
            else "远程 qdrant")


# ══════════════════════════════════════════════════════════════
# ③ Milvus 后端（生产集群）
# ══════════════════════════════════════════════════════════════
class MilvusVectorBackend:
    name = "milvus"

    def __init__(self, *, uri: str = "http://127.0.0.1:19530",
                 token: str = "", collection: str = "vectors",
                 dim: int = 0) -> None:
        if not _MILVUS:
            raise VectorBackendError("pymilvus 未安装（pip install pymilvus）")
        if not dim:
            raise VectorBackendError("milvus 后端必须显式给 dim（建集合需要）")
        self.collection = collection
        self._dim = int(dim)
        self._uri = uri
        local = uri.endswith(".db")
        if local:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(uri)) or ".", exist_ok=True)
        try:
            # 本地文件（milvus-lite）不吃 token；远程服务才传
            self._client = (MilvusClient(uri=uri) if local
                            else MilvusClient(uri=uri, token=token or None))
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"milvus 连接失败: {exc}") from exc
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        try:
            if self._client.has_collection(self.collection):
                # 集合已存在时也要确保是 loaded 状态（可能由别的进程建过、
                # 或集群重启后未被重新 load）。
                try:
                    state = self._client.get_load_state(self.collection)
                    loaded = "Loaded" in str(state) or "loaded" in str(state)
                except Exception:                              # noqa: BLE001
                    loaded = False
                if not loaded:
                    self._client.load_collection(self.collection)
                return
            schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
            for f in ("tenant", "user_id", "team", "project"):
                schema.add_field(f, DataType.VARCHAR, max_length=128)
            self._client.create_collection(self.collection, schema=schema)
            # pymilvus 3.x 要求 IndexParams 对象（字符串/字典会被 ParamError 拒掉）
            ip = self._client.prepare_index_params()
            ip.add_index(field_name="vector", index_type="AUTOINDEX",
                         metric_type="COSINE")
            self._client.create_index(self.collection, ip)
            # **集群模式下建完索引必须 load，否则 search 报 collection not loaded**。
            # milvus-lite 会自动 load，所以这条只在真集群上暴露 —— 正是"本地验证
            # 通过、上线就炸"的典型形态。
            self._client.load_collection(self.collection)
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"milvus 建集合失败: {exc}") from exc

    @staticmethod
    def _to_expr(flt: dict | None) -> str:
        if not flt:
            return ""
        parts = []
        if flt.get("tenant") is not None:
            parts.append(f'tenant == "{flt["tenant"]}"')
        user_id = flt.get("user_id") or ""
        team = flt.get("team") or ""
        project = flt.get("project") or ""
        if user_id and not (team and project):
            parts.append(f'user_id == "{user_id}"')
        elif user_id and team and project:
            parts.append(f'(user_id == "{user_id}" or '
                         f'(team == "{team}" and project == "{project}"))')
        elif team and project:
            parts.append(f'team == "{team}" and project == "{project}"')
        return " and ".join(parts)

    def upsert(self, point_id: str, vector: Sequence[float], payload: dict) -> None:
        vec = list(vector)
        if len(vec) != self._dim:
            raise VectorBackendError(
                f"维度不一致: 期望 {self._dim}，实际 {len(vec)}")
        row = {"id": point_id, "vector": vec}
        for f in ("tenant", "user_id", "team", "project"):
            row[f] = str(payload.get(f, ""))
        row["_payload"] = json.dumps(payload, ensure_ascii=False)
        try:
            self._client.upsert(self.collection, data=[row])
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"milvus upsert 失败: {exc}") from exc

    def delete(self, point_id: str) -> bool:
        try:
            self._client.delete(self.collection, ids=[point_id])
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def search(self, vector: Sequence[float], flt: dict | None,
               limit: int) -> list[tuple[str, float, dict]]:
        try:
            rows = self._client.search(
                self.collection, data=[list(vector)], limit=max(1, int(limit)),
                filter=self._to_expr(flt),
                output_fields=["tenant", "user_id", "team", "project", "_payload"])
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"milvus search 失败: {exc}") from exc
        out = []
        for hit in (rows[0] if rows else []):
            ent = hit.get("entity") or {}
            try:
                payload = json.loads(ent.get("_payload") or "{}")
            except Exception:                                  # noqa: BLE001
                payload = {}
            out.append((str(hit.get("id")), float(hit.get("distance", 0.0)), payload))
        return out

    def scroll(self, flt: dict | None, limit: int = 256) -> list[tuple[str, dict]]:
        expr = self._to_expr(flt) or ""
        try:
            rows = self._client.query(
                self.collection, filter=expr, limit=max(1, int(limit)),
                output_fields=["_payload"])
        except Exception as exc:                               # noqa: BLE001
            raise VectorBackendError(f"milvus query 失败: {exc}") from exc
        out = []
        for r in rows or []:
            try:
                payload = json.loads(r.get("_payload") or "{}")
            except Exception:                                  # noqa: BLE001
                payload = {}
            out.append((str(r.get("id")), payload))
        return out

    def flush(self) -> None:
        """把内存里的写入刷到存储段。

        **Milvus 是最终一致的**：`upsert` 返回成功不代表马上能被 `search` 看到。
        单机 lite 几乎瞬时可见，真集群要等 flush / 段回收 —— 所以：
          * 测试与演示要显式 `flush()` 后再断言
          * 生产不要依赖"写完立刻查到"，跨副本更不可靠
        """
        try:
            self._client.flush(self.collection)
        except Exception:                                      # noqa: BLE001
            pass

    def count(self) -> int:
        """**活行数**（不含 tombstone）。

        坑：`get_collection_stats()["row_count"]` 统计的是"插入过的实体数"，
        Milvus 的 delete 是软删（打 tombstone），被删的行要等 compaction 才真正消失。
        于是 `upsert` 同一主键两次 → stats 变 4，而实际只有 3 条。
        正确口径是 `query(count(*))` —— 它数的是**当前可见**的行。

        这个差异 milvus-lite 上几乎看不出来（清得快），真集群上必现。
        """
        self.flush()
        try:
            rows = self._client.query(self.collection, filter="",
                                      output_fields=["count(*)"])
            if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
                for key in ("count(*)", "count"):
                    if key in rows[0]:
                        return int(rows[0][key])
        except Exception:                                      # noqa: BLE001
            pass
        try:                                   # 退路：数 id（大集合上很慢，但准）
            rows = self._client.query(self.collection, filter="", limit=16384,
                                      output_fields=["id"])
            return len(rows or [])
        except Exception:                                      # noqa: BLE001
            return 0

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                                      # noqa: BLE001
            pass

    def info(self) -> VectorBackendInfo:
        local = self._uri.endswith(".db")
        return VectorBackendInfo(
            kind="milvus", available=True, shared=not local, ann=True,
            dim=self._dim, location=self._uri,
            reason=("milvus-lite 本地文件（进程内，单机验证用）" if local
                    else "生产集群后端"))


# ══════════════════════════════════════════════════════════════
# 工厂 + 探测
# ══════════════════════════════════════════════════════════════
def _stable_id(point_id: str) -> int:
    import hashlib
    return int(hashlib.sha256(point_id.encode()).hexdigest()[:15], 16)


_CTORS = {
    "memory": MemoryVectorBackend,
    "qdrant": QdrantVectorBackend,
    "milvus": MilvusVectorBackend,
}


def available_backends() -> dict[str, dict]:
    """每种后端是否可用 + 原因。**不做"假装支持"**。"""
    out: dict[str, dict] = {}
    out["memory"] = {"available": True, "reason": "零依赖"}
    out["qdrant"] = {"available": _QDRANT,
                     "reason": "ok" if _QDRANT else "qdrant-client 未安装"}
    out["milvus"] = {"available": _MILVUS,
                     "reason": "ok" if _MILVUS else "pymilvus 未安装"}
    return out


def make_vector_backend(kind: str = "auto", **kw) -> Any:
    """按名字造后端。`auto` 按 qdrant → milvus → memory 的顺序挑第一个**造得出来**的。

    `auto` 的语义是"挑一个能用"，不是"挑一个装了库"：milvus 建集合必须给
    `dim`，没给就必然在构造时抛错。早先的写法只看 `available()`（库装了就算），
    于是**默认配置在装了 pymilvus 的机器上直接失败**（`VECTOR_BACKEND` 未设 →
    auto → 挑中 milvus → 抛"必须显式给 dim" → 后端为 None），而没装 pymilvus 的
    机器一切正常 —— 默认行为由"机器上装了什么"决定，正是本项目反复踩的那类坑。
    现在缺 `dim` 时跳过 milvus。

    返回的对象一定有 `info()`；不可用时会**抛错并说明原因**，
    而不是静默退化成内存后端（那会让"配置了 milvus 实际跑内存"无人知晓）。
    """
    k = (kind or "auto").strip().lower()
    if k == "auto":
        avail = available_backends()
        has_dim = bool(kw.get("dim"))
        k = "memory"
        for cand in ("qdrant", "milvus"):
            if not avail[cand]["available"]:
                continue
            if cand == "milvus" and not has_dim:
                continue                     # 没维度的 milvus 造不出来，别选它
            k = cand
            break
    ctor = _CTORS.get(k)
    if ctor is None:
        raise VectorBackendError(
            f"未知后端 {kind!r}（可选 {'/'.join(_CTORS)} 或 auto）")
    try:
        return ctor(**kw)
    except VectorBackendError:
        raise
    except Exception as exc:                                   # noqa: BLE001
        raise VectorBackendError(f"{k} 后端初始化失败: {exc}") from exc


def configure_vector_backend(env: dict | None = None) -> dict:
    """从环境变量读配置并造后端。返回 `{"backend": obj|None, "info":..., "reason":...}`。

    支持的环境变量::

        VECTOR_BACKEND   = memory | qdrant | milvus | auto   （默认 auto）
        QDRANT_URL / QDRANT_LOCATION / QDRANT_API_KEY / QDRANT_COLLECTION
        MILVUS_URI / MILVUS_TOKEN / MILVUS_COLLECTION
        VECTOR_DIM       = 向量维度（milvus 必需；qdrant 首次建集合需要）
    """
    import os
    e = dict(os.environ if env is None else env)
    kind = e.get("VECTOR_BACKEND", "auto")
    dim = int(e.get("VECTOR_DIM") or 0)
    try:
        if kind == "qdrant":
            obj = make_vector_backend(
                "qdrant", url=e.get("QDRANT_URL", ""),
                api_key=e.get("QDRANT_API_KEY", ""),
                location=e.get("QDRANT_LOCATION", ":memory:"),
                collection=e.get("QDRANT_COLLECTION", "vectors"), dim=dim)
        elif kind == "milvus":
            obj = make_vector_backend(
                "milvus", uri=e.get("MILVUS_URI", "http://127.0.0.1:19530"),
                token=e.get("MILVUS_TOKEN", ""),
                collection=e.get("MILVUS_COLLECTION", "vectors"), dim=dim)
        elif kind in ("memory", "auto", ""):
            obj = make_vector_backend(kind or "auto", dim=dim)
        else:
            # **未知名字必须报错，不能静默退化成内存**
            # （"配置了 milvus 实际跑内存"是本项目反复踩的坑）
            raise VectorBackendError(
                f"未知 VECTOR_BACKEND={kind!r}（可选 memory/qdrant/milvus/auto）")
        return {"backend": obj, "info": obj.info(), "reason": "ok"}
    except Exception as exc:                                   # noqa: BLE001
        return {"backend": None, "info": None,
                "reason": f"{type(exc).__name__}: {exc}"}