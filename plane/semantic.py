"""plane/semantic.py — 语义相似度的**组合层**：把 EmbeddingProvider 包成 `sim(a,b) -> float`。

================================================================================
为什么不改 `plane/memory.py`
================================================================================
Memory 的打分公式（`_score`）结构是好的，坏掉的是唯一一个输入：`sim` 用字符 bigram。
把 `memory.py` 改成直接依赖一个 HTTP adapter，会同时毁掉三件事：
① 关键路径（每轮召回）被一个网络调用污染；② `plane/*` 只能依赖 `contracts.py` 的
分层被破坏；③ bigram 作为"离线可复现"的对照面消失。

所以这里是**装饰器**而不是补丁：`sim(a, b)` 的形状与 `memory.text_similarity` 完全一致，
Memory 侧只需把「取 sim 的那一行」换成注入进来的 callable —— 公式、权重、Profile 加成、
预算裁剪、命中续命全部原样。

    # 接线（memory.py 不改）
    sim = SemanticSimilarity(provider)                  # provider=None 也安全
    score = 0.50 * sim.sim(query, rec.text) + 0.30 * rec.importance + 0.20 * recency

================================================================================
降级是**硬要求**，不是善意
================================================================================
记忆召回是每轮都要跑的关键路径。网关挂 / 超时 / 429 时：
- 抛异常 → 整轮对话挂掉（用户看到的是"AI 不回话"，不是"召回差一点"）；
- 静默返回 0.0 → 所有记忆分数掉到只剩下 importance+recency，排序退化成"重要的在前"，
  看起来正常、实际召回全错 —— 比崩更坏。

所以本模块的唯一立场：**provider 任何异常都翻译成 fallback 打分**，并计数
（`stats()["fallback_hits"]`）让"降级发生过"可观测。降级不是隐藏故障，是记账。

fallback 默认是一份**自带的 bigram 副本**（与 `plane/memory.text_similarity` 逐值等价，
有测试守着）：离线、零依赖、永远可用。它保证"网关再坏，召回也有个确定的基线"。

（`cosine` 从 `adapters/embeddings.py` 借用：它是纯函数，且那边的实现已经连同
"维度不一致必须炸"的裁决一起测过。adapter 不反向依赖 `plane/*`，所以没有循环。）
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Callable, Protocol, Sequence

from adapters.embeddings import cosine

__all__ = [
    "BigramFallback",
    "SemanticSimilarity",
    "bigram_similarity",
    "clamp01",
]

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


# ══════════════════════════════════════════════════════════════
# 兜底：与 plane/memory.text_similarity 逐值等价的 bigram Jaccard
# ══════════════════════════════════════════════════════════════
def _normalize(text: str) -> str:
    """小写 + 去空白/标点，保留 CJK 与字母数字（与 memory.normalize_text 同口径）。"""
    return _PUNCT_RE.sub("", (text or "").lower())


def _bigrams(text: str) -> set[str]:
    s = _normalize(text)
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard，输入是**字符串**（`memory.similarity` 收的是集合）。

    刻意复制一份而不是 import：① 分层——`plane/semantic.py` 不该在关键路径上
    再依赖 `plane/memory.py` 的内部函数；② 语义——这等价于"把旧实现钉成常量基线"，
    memory 侧将来换了公式也不会悄悄改变降级行为。一致性由测试守着。
    """
    x = _bigrams(a)
    y = _bigrams(b)
    if not x or not y:
        return 0.0
    inter = len(x & y)
    return inter / float(len(x | y)) if inter else 0.0


class BigramFallback:
    """把 bigram 包成可调用对象（`__call__` 与 `sim` 都可用），便于注入。"""

    name = "bigram-jaccard"

    def __call__(self, a: str, b: str) -> float:
        return bigram_similarity(a, b)

    def sim(self, a: str, b: str) -> float:
        return bigram_similarity(a, b)

    def stats(self) -> dict:
        return {"provider": self.name}


def clamp01(value: float) -> float:
    """裁剪到 [0, 1]。召回公式（0.5*sim + 0.3*imp + 0.2*recency）假设 sim∈[0,1]；
    负余弦对"该不该召回"没有增量信息（都该排最后），保留负值只会让公式权重失衡。"""
    if value != value:                      # NaN 防护：NaN 会让 sort 行为不确定
        return 0.0
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))


class _VectorSource(Protocol):
    """本模块只要求这一件事（`contracts.EmbeddingProvider` 的超集正好满足）。"""
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# ══════════════════════════════════════════════════════════════
# 主类
# ══════════════════════════════════════════════════════════════
class SemanticSimilarity:
    """`sim(a, b) -> float`，带 provider → fallback 的确定性降级。

    两层缓存不是重复：
    * adapter 的 `EmbeddingCache` 省的是**钱**（不发重复请求）；
    * 这里的 `_vec_cache` 省的是**往返**：`rank` 一次要给 N 条候选打分，
      没有本地向量表就得对每条候选单独 embed（N 次 HTTP），召回延迟直接爆。
    """

    def __init__(
        self,
        provider: _VectorSource | None = None,
        *,
        fallback: Callable[[str, str], float] | None = None,
        max_cache: int = 4096,
    ) -> None:
        self.provider = provider
        self._fallback: Callable[[str, str], float] = fallback or bigram_similarity
        self._vec_cache: "OrderedDict[str, list[float]]" = OrderedDict()
        self._max_cache = max(16, int(max_cache))

        self._embed_calls = 0
        self._fallback_hits = 0
        self._provider_errors = 0
        self._pairs = 0
        self.last_error: str = ""

    # ── 观测 ────────────────────────────────────────────────────
    @property
    def dim(self) -> int:
        return int(getattr(self.provider, "dim", 0) or 0) if self.provider else 0

    def stats(self) -> dict:
        return {
            "embed_calls": self._embed_calls,
            "fallback_hits": self._fallback_hits,
            "provider": (getattr(self.provider, "name", None) or "none")
            if self.provider is not None else "none",
            "dim": self.dim,
            "pairs": self._pairs,
            "provider_errors": self._provider_errors,
            "vec_cache_size": len(self._vec_cache),
            "last_error": self.last_error,
        }

    def prefetch(self, texts: Sequence[str]) -> int:
        """批量预热向量，返回本次新取到的条数（失败**静默**，不抛）。

        为什么需要它：`plane/memory.py` 的 `_score(rec, ...)` 是**逐条**调用的，
        单靠 `sim()` 就是"每条记忆一次 HTTP" —— 10 条记忆 = 10 次往返，
        而召回是每轮都要跑的关键路径。预热把 N 次合成 1 次（受 provider 的
        `batch_size` 约束），之后每条 `sim()` 都命中本地向量表。

        失败不计数 `fallback_hits`：预热是**优化**，真正的打分失败由随后每个
        `sim()` 自己记账 —— 否则一次预热失败会被记成 N 次降级信号。
        """
        items = [str(t) for t in (texts or ())]
        if not items or self.provider is None:
            return 0
        before = len(self._vec_cache)
        if self._vectors(items) is None:
            return 0
        return max(0, len(self._vec_cache) - before)

    # ── 核心 ────────────────────────────────────────────────────
    def sim(self, a: str, b: str) -> float:
        """语义相似度，范围 [0, 1]。provider 不可用 → fallback，**不抛给调用方**。"""
        self._pairs += 1
        if self.provider is None:
            self._fallback_hits += 1
            return clamp01(self._call_fallback(a, b))

        vecs = self._vectors([a, b])
        if vecs is None:
            self._fallback_hits += 1
            return clamp01(self._call_fallback(a, b))
        try:
            return clamp01(cosine(vecs[0], vecs[1]))
        except ValueError as exc:               # 维度串了 → 降级，别炸召回
            self._note_error(f"cosine: {exc}")
            self._fallback_hits += 1
            return clamp01(self._call_fallback(a, b))

    def rank(self, query: str, candidates: Sequence[str]) -> list[tuple[int, float]]:
        """按相似度降序返回 ``(原始下标, 分数)``。

        空候选 → ``[]``。provider 失败 → **整批**退到 fallback（一次降级算一次
        `fallback_hits`，因为它是"这一次召回"的信号，不是 N 个独立信号）。
        分数相同按下标升序，保证结果可复现。
        """
        cands = [str(c) for c in (candidates or ())]
        if not cands:
            return []

        vecs = self._vectors([query, *cands])
        scored: list[tuple[int, float]] = []
        if vecs is None:
            self._fallback_hits += 1
            for index, text in enumerate(cands):
                scored.append((index, clamp01(self._call_fallback(query, text))))
        else:
            query_vec = vecs[0]
            for index, vec in enumerate(vecs[1:]):
                try:
                    scored.append((index, clamp01(cosine(query_vec, vec))))
                except ValueError as exc:
                    self._note_error(f"cosine: {exc}")
                    scored.append((index, clamp01(self._call_fallback(query, cands[index]))))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored

    # ── 内部 ────────────────────────────────────────────────────
    def _call_fallback(self, a: str, b: str) -> float:
        try:
            return float(self._fallback(a, b))
        except Exception as exc:                # noqa: BLE001 — 兜底自己不能把召回炸掉
            self._note_error(f"fallback: {type(exc).__name__}: {exc}")
            return 0.0

    def _note_error(self, message: str) -> None:
        self._provider_errors += 1
        self.last_error = str(message)[:300]

    def _vectors(self, texts: Sequence[str]) -> list[list[float]] | None:
        """取向量；失败（异常 / 形状不符）一律返回 None，由调用方降级。

        "形状不符"也要当失败：provider 少返一条时，按位置配对会把 A 的向量安到 B 上，
        得到一条**看起来正常**的错误分数 —— 召回层面这是静默数据损坏。
        """
        if self.provider is None:
            return None
        wanted: list[str] = []
        seen: set[str] = set()
        for text in texts:
            if text not in seen:
                seen.add(text)
                if text not in self._vec_cache:
                    wanted.append(text)

        if wanted:
            self._embed_calls += 1
            try:
                fresh = self.provider.embed(wanted)
            except Exception as exc:            # noqa: BLE001 — 网关任何失败都走降级
                self._note_error(f"{type(exc).__name__}: {exc}")
                return None
            if not isinstance(fresh, (list, tuple)) or len(fresh) != len(wanted):
                got = len(fresh) if isinstance(fresh, (list, tuple)) else type(fresh).__name__
                self._note_error(f"embed 返回条数不符：要 {len(wanted)} 得 {got}")
                return None
            for text, vec in zip(wanted, fresh):
                try:
                    self._put(text, [float(x) for x in vec])
                except (TypeError, ValueError) as exc:
                    self._note_error(f"向量元素非数值：{exc}")
                    return None

        try:
            return [list(self._vec_cache[t]) for t in texts]
        except KeyError as exc:                 # 理论上不会发生：上面全 put 过
            self._note_error(f"内部错误：缺少向量 {exc}")
            return None

    def _put(self, text: str, vec: list[float]) -> None:
        self._vec_cache[text] = vec
        self._vec_cache.move_to_end(text)
        while len(self._vec_cache) > self._max_cache:
            self._vec_cache.popitem(last=False)

    def clear(self) -> None:
        self._vec_cache.clear()
