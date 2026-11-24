"""adapters/embeddings.py — 语义向量 adapter：把「字符 bigram 假语义」换成真 embedding。

================================================================================
为什么非换不可（这是召回质量的物理上限，不是优化项）
================================================================================
`plane/memory.py` 的相似度是字符 bigram Jaccard。它是零依赖、可手算的好近似，
但它的**天花板就是字面**：同一件事的两种说法（字面几乎不重合）实测

    bigram Jaccard ≈ 0.07        ← 字面看不到它们的关系
    text-embedding-v4 余弦 0.81  ← 语义看到了

后果不是"分数低一点"，而是**用户换个说法问，那条内容就等于不存在**。一个「看起来有
语义检索、实际只是字面匹配」的系统比老老实实的关键字检索更危险：它让人相信语义存在。

（具体的业务语料对比在 `scripts/embedding_bench.py`；本目录是中性 Runtime 能力，
按 `tests/test_architecture.py` 的边界，示例一律用抽象文本。）

本模块只做一件事：把 `POST {base_url}/embeddings` 包成 `contracts.EmbeddingProvider`。
它**不认识 Memory**，也不知道分数怎么用 —— 打分公式归 Memory，向量归这里。

================================================================================
设计约束（照抄 `adapters/openai_compat.py` 的立场）
================================================================================
1. **零第三方依赖**：只用 ``urllib.request``。adapter 的价值全在「屏蔽协议差异」。
2. **批量 + 缓存**：embedding 按条计费，重复文本重复计费是纯浪费。所以
   ① `batch_size` 分批（网关对单次条数有上限）；② `EmbeddingCache` 做 LRU 记忆。
   命中缓存的文本**不进请求体**。
3. **失败分类先于重试**：401/403/400 重试是纯流量浪费（同一 body 一定再错一次）；
   只有 429/5xx/网络抖动才退避。见 :data:`RETRYABLE_STATUS` / :func:`classify_status`。
4. **顺序即契约**：返回向量必须与输入**一一对应**。OpenAI 兼容服务在批量时**可能
   乱序返回**，靠 ``data[i].index`` 排序，不靠到达顺序 —— 否则"文本 A 拿到文本 B
   的向量"，是静默的数据损坏，比报错恶劣得多。
5. **维度漂移必须炸**：模型换了 / 服务端换了维度时，旧缓存里的向量与新向量混用会得到
   无意义的余弦。所以维度不一致 → :class:`EmbeddingError`（提示清缓存），不静默截断。
6. **api_key 永不出现在任何异常/日志里**：所有对外消息过 :meth:`_scrub`。
7. **传输接缝可注入**：`opener` 与 `sleeper` 都是构造参数。测试用确定性 fake，
   **绝不联网**（本机可能挂系统代理，真实 socket 的失败形态不可移植）。

================================================================================
兜底
================================================================================
`HashingEmbeddings` 是零依赖的**确定性哈希向量**：不是语义，但形状正确、可测、
跨进程可复现（用 sha256 而不是内置 ``hash()``，后者受 ``PYTHONHASHSEED`` 影响）。
它的用途是让上层链路（缓存 / 批次 / rank / 降级）在没有网关时也能跑通并被测试。
**不要**用它在生产里假装有语义检索 —— 那就是本模块开头批判的那个错误。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any, Callable, Sequence

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "EMBEDDING_FATAL_STATUS",
    "EMBEDDING_RETRYABLE_STATUS",
    "EmbeddingCache",
    "EmbeddingError",
    "HashingEmbeddings",
    "OpenAICompatEmbeddings",
    "classify_status",
    "cosine",
    "from_env",
    "estimate_tokens",
]

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════
DEFAULT_BASE_URL = "https://www.babelark.ai/v1"
#: 实测：同义句余弦 0.81，1024 维；另一档 `qwen3.7-text-embedding-flash` 是 0.64。
DEFAULT_MODEL = "text-embedding-v4"

#: 值得退避重试的状态码：限流 / 网关抖动 / 上游过载 / 请求超时。
EMBEDDING_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 529})
#: 确定性失败：鉴权错、参数错。换 key/换模型可能有用，但**不重试同一请求**。
EMBEDDING_FATAL_STATUS: frozenset[int] = frozenset({400, 401, 403})

#: 退避：base * 2**i，封顶 8s（一次抖动不该把每轮都要跑的召回拖到 deadline 之后）。
RETRY_BASE_S: float = 0.5
RETRY_CAP_S: float = 8.0

#: 服务端错误体只取前 N 个字符进异常消息（防止 HTML 错误页灌爆日志）。
ERROR_BODY_CHARS = 400

#: 默认单价（USD / 1M tokens）。只用于 `stats()["cost_estimate"]` 的**量级**估算，
#: 不参与预算记账 —— 记账归 Loop，避免双记账（与 openai_compat 同一立场）。
DEFAULT_PRICE_PER_M_TOKENS = 0.02

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{3,}")
_KEYLIKE_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9._\-]{4,}")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class EmbeddingError(RuntimeError):
    """向量调用失败。**消息已脱敏**，``kind`` 给上层分流（降级 / 上报 / 重修缓存）。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        status: int = 0,
        model: str = "",
        retryable: bool = False,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = int(status)
        self.model = model
        self.retryable = bool(retryable)
        self.attempts = int(attempts)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status, "model": self.model,
                "retryable": self.retryable, "attempts": self.attempts,
                "message": str(self)}


class _Attempt(Exception):
    """内部单次尝试失败（不对外暴露，由 :meth:`OpenAICompatEmbeddings.embed` 翻译）。"""

    def __init__(self, kind: str, detail: str, *, status: int = 0,
                 retryable: bool = False) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = int(status)
        self.retryable = bool(retryable)


def classify_status(status: int) -> str:
    """HTTP 状态 → 失败类别。类别决定「要不要重试」，不决定文案。"""
    if status in (401, 403):
        return "auth"
    if status == 400:
        return "bad_request"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    if status in EMBEDDING_RETRYABLE_STATUS:
        return "transient"
    return "client"


def estimate_tokens(text: str) -> int:
    """确定性 token 估算（与 ``builder.estimate_tokens`` 同口径）。

    只用于 ``cost_estimate`` 的量级，不追求与真实 tokenizer 精确对齐：
    CJK 1 char ≈ 1 token，其余 3.5 char ≈ 1 token。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    other = len(text) - cjk
    return max(1, int(cjk + other / 3.5))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ══════════════════════════════════════════════════════════════
# 余弦
# ══════════════════════════════════════════════════════════════
_BATCH_TOO_BIG_HINTS = ("batch size", "batch_size", "too large", "too many",
                         "exceed", "maximum", "limit", "size is invalid")


def _looks_like_batch_too_big(exc: BaseException) -> bool:
    """400/413 且文本里在讲"批次/大小/上限" → 判定为"批次太大"。

    只在明确像的时候才拆批；其他 400（参数错、模型不存在）照常抛。
    """
    msg = str(exc).lower()
    return ("400" in msg or "413" in msg) and any(h in msg for h in _BATCH_TOO_BIG_HINTS)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（裸值，范围 [-1, 1]，不做截断）。

    * 任一向量为零向量 → 0.0（无方向可比，不是"都不相关"的错误，是定义）；
    * 长度不一致 → ``ValueError``：这是**编程错误**（缓存串了维度），静默补齐/
      截断会把它变成"分数有点怪"，那是最难查的一类 bug。

    ``plane/semantic.py`` 会在此基础上截断到 [0,1]（召回公式假设 sim∈[0,1]）。
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        raise ValueError(f"cosine 维度不一致：{len(a)} vs {len(b)}（缓存可能串了模型）")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


# ══════════════════════════════════════════════════════════════
# 缓存
# ══════════════════════════════════════════════════════════════
class EmbeddingCache:
    """LRU 缓存：**相同文本不重复计费**。

    为什么是 LRU 而不是 dict：进程长跑时记忆文本会持续增长，无界 dict 就是内存泄漏。
    为什么键是**原文**而不是归一化文本：缓存的价值是"同一请求不发第二次"，
    而网关按收到的原文计费；归一化会把「A」和「A 」合并成一个请求，省不了钱还
    可能改变模型的向量（尾部空白的处理因模型而异）。
    """

    def __init__(self, max_size: int = 2048) -> None:
        self.max_size = max(0, int(max_size))
        self._store: "OrderedDict[str, tuple[float, ...]]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, text: str) -> list[float] | None:
        """取值并计入 hits/misses。"""
        vec = self._store.get(text)
        if vec is None:
            self.misses += 1
            return None
        self._store.move_to_end(text)          # 命中即刷新新鲜度
        self.hits += 1
        return list(vec)

    def peek(self, text: str) -> list[float] | None:
        """取值但**不计数**：adapter 内部回填顺序时用，避免把一次请求算成 N 次命中。"""
        vec = self._store.get(text)
        if vec is None:
            return None
        self._store.move_to_end(text)
        return list(vec)

    def put(self, text: str, vec: Sequence[float]) -> None:
        if self.max_size <= 0:
            return
        self._store[text] = tuple(float(x) for x in vec)
        self._store.move_to_end(text)
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)     # 淘汰最久未用
            self.evictions += 1

    def clear(self) -> None:
        """模型换维度 / 换供应商后必须清（否则新旧向量混用毫无意义）。"""
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, text: object) -> bool:
        return text in self._store

    def to_dict(self) -> dict:
        return {"size": len(self._store), "max_size": self.max_size,
                "hits": self.hits, "misses": self.misses,
                "evictions": self.evictions}


# ══════════════════════════════════════════════════════════════
# 主 adapter
# ══════════════════════════════════════════════════════════════
class OpenAICompatEmbeddings:
    """OpenAI 兼容 ``POST /embeddings``（OpenAI / DashScope 兼容模式 / vLLM / TEI 都吃这套）。

    实现 ``contracts.EmbeddingProvider``（``name`` / ``dim`` / ``embed``）。

    用法::

        prov = OpenAICompatEmbeddings("https://www.babelark.ai/v1",
                                      "text-embedding-v4", key)
        vecs = prov.embed(["第一种说法", "第二种说法"])        # 2 条 → 1 次请求
        assert prov.dim == 1024
    """

    name = "openai-compat-embeddings"
    #: 首次调用后确定（或构造时传入）。0 = 未知。
    dim = 0

    RETRY_BASE_S = RETRY_BASE_S
    RETRY_CAP_S = RETRY_CAP_S

    def __init__(
        self,
        base_url: str,
        model: str = DEFAULT_MODEL,
        api_key: str = "",
        *,
        timeout_ms: int = 20000,
        max_retries: int = 2,
        batch_size: int = 10,
        cache: EmbeddingCache | None = None,
        dim: int = 0,
        price_per_m_tokens: float = DEFAULT_PRICE_PER_M_TOKENS,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        base = (base_url or "").strip()
        if not base:
            raise ValueError("base_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        if not (model or "").strip():
            raise ValueError("model 不能为空")

        self.base_url = base
        self.endpoint = (
            base if base.rstrip("/").endswith("/embeddings")
            else base.rstrip("/") + "/embeddings"
        )
        self.model = model.strip()
        self.api_key = (api_key or "").strip()
        self.timeout_ms = max(1, int(timeout_ms))
        self.max_retries = max(0, int(max_retries))
        #: 单批上限。**可按网关能力自适应下调**（见 _embed_batch_adaptive）：
        #: 实测 babelark 限制 10，超了直接 400，而 400 不在可重试集合里。
        self.batch_size = max(1, int(batch_size))
        self._batch_splits = 0
        self.cache = cache if cache is not None else EmbeddingCache()
        self.dim = int(dim) if dim and int(dim) > 0 else 0
        self.price_per_m_tokens = float(price_per_m_tokens)
        self.sleeper = sleeper
        #: 传输接缝：默认 urllib.request.urlopen。测试注入确定性 fake（**不联网**）。
        self._open: Callable[..., Any] = opener or urllib.request.urlopen

        # ── 观测/计量（只读快照，不改 budget）────────────────
        self.last_status: int = 0
        self.last_texts: list[str] = []          # 最近一次真正发往网关的文本
        self.history: list[dict] = []            # 每次重试 / 失败的流水

        self._calls = 0            # embed() 调用次数
        self._texts = 0            # 累计提交的文本条数（含缓存命中）
        self._sent_texts = 0       # 累计真正上行的文本条数（计费口径）
        self._requests = 0         # HTTP 请求数
        self._retries = 0
        self._errors = 0
        self._cached = 0
        self._tokens_est = 0

    # ── 对外入口 ────────────────────────────────────────────────
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量取向量。返回顺序与 ``texts`` **一一对应**。

        * 空输入 → ``[]``，**不发请求**（网关对空 input 会 400，纯浪费）；
        * 全部命中缓存 → 也不发请求；
        * 同一批里重复的文本只上行一次（去重后按首次出现位置回填）。
        """
        if isinstance(texts, str):
            raise TypeError("embed 需要 Sequence[str]；单个字符串请传 [text]")
        seq = [str(t) for t in (texts or ())]
        self._calls += 1
        self._texts += len(seq)
        if not seq:
            return []

        # ① 先查缓存（同一批内的重复文本也只上行一次）
        resolved: dict[str, list[float]] = {}
        todo: list[str] = []
        seen: set[str] = set()
        cache_hits = 0
        for text in seq:
            if text in seen:                    # 批内重复：一次请求就够
                continue
            seen.add(text)
            vec = self.cache.get(text)          # get 计 hits/misses（观测口径）
            if vec is None:
                todo.append(text)
            else:
                resolved[text] = vec
                cache_hits += 1
        self._cached += cache_hits

        blanks = [t for t in todo if not t.strip()]
        todo = [t for t in todo if t.strip()]

        # ② 分批请求（**自适应拆批**：网关对单批条数/字符数的限制各不相同，
        #    而超限返回的是 400 —— 不可重试的错误码。实测 babelark 单批上限 10，
        #    默认 32 会整批 400 然后静默降级。所以遇到"批次太大"就减半重试，直到 1。）
        for start in range(0, len(todo), self.batch_size):
            batch = todo[start:start + self.batch_size]
            vectors = self._embed_batch_adaptive(batch)
            for text, vec in zip(batch, vectors):
                self.cache.put(text, vec)
                resolved[text] = vec

        # ③ 空白文本没有向量可言：给零向量（余弦 0），不浪费一次请求
        #    dim 未知说明还没成功调过任何一次真实请求 —— 那时零向量无从构造。
        if blanks:
            if self.dim <= 0:
                raise EmbeddingError(
                    "dim 未知：只有空白文本无法确定向量维度（先成功调用一次非空文本）",
                    kind="empty_input", model=self.model)
            for text in blanks:
                self.cache.put(text, [0.0] * self.dim)
                resolved[text] = [0.0] * self.dim

        # ④ 按原文逐条回填，保证「返回顺序 == 输入顺序」
        out: list[list[float]] = []
        for text in seq:
            vec = resolved.get(text)
            if vec is None:
                vec = self.cache.peek(text)     # 批内重复的第二次出现走这里
            if vec is None:                     # 逻辑上不可能：上面每一条都 put 过
                raise EmbeddingError(
                    f"内部错误：{text!r} 缺少向量（缓存被外部清空？）",
                    kind="internal", model=self.model)
            out.append(vec)
        return out

    # ── 单批请求（含重试）──────────────────────────────────────
    def _embed_batch_adaptive(self, batch: list[str]) -> list[list[float]]:
        """按需拆批：批次太大（400/413 且提示 size/token/batch）→ 减半重试。"""
        try:
            return self._embed_batch(batch)
        except EmbeddingError as exc:
            if len(batch) <= 1 or not _looks_like_batch_too_big(exc):
                raise
            mid = max(1, len(batch) // 2)
            self._batch_splits += 1
            if mid < self.batch_size:
                self.batch_size = mid          # 记住学到的上限，后续批次直接变小
            return (self._embed_batch_adaptive(batch[:mid])
                    + self._embed_batch_adaptive(batch[mid:]))

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        last: _Attempt | None = None
        for attempt in range(self.max_retries + 1):
            try:
                doc = self._post(batch)
            except _Attempt as exc:
                last = exc
                will_retry = exc.retryable and attempt < self.max_retries
                self.history.append({
                    "event": "retry" if will_retry else (
                        "exhausted" if exc.retryable else "fatal"),
                    "attempt": attempt,
                    "status": exc.status,
                    "kind": exc.kind,
                    "error": self._scrub(exc.detail)[:300],
                })
                if not will_retry:
                    raise self._fail(
                        f"embeddings request rejected ({exc.kind}) by {self.model}: "
                        f"{exc.detail}",
                        kind=exc.kind, status=exc.status,
                        retryable=False, attempts=attempt + 1,
                    ) from None
                delay = min(self.RETRY_BASE_S * (2 ** attempt), self.RETRY_CAP_S)
                self.sleeper(delay)
                self._retries += 1
                continue
            return self._parse(doc, batch)

        assert last is not None                     # pragma: no cover — 循环必有出口
        raise self._fail(                           # pragma: no cover
            f"embeddings exhausted retries: {last.kind}({last.status}): {last.detail}",
            kind=last.kind, status=last.status, retryable=False,
            attempts=self.max_retries + 1)

    def _parse(self, doc: dict, batch: Sequence[str]) -> list[list[float]]:
        """按 ``data[i].index`` 还原输入顺序，并校验维度。"""
        items = doc.get("data")
        if not isinstance(items, list) or not items:
            err = doc.get("error")
            detail = json.dumps(err, ensure_ascii=False)[:ERROR_BODY_CHARS] if err else ""
            raise self._fail(
                f"protocol error: no data in embeddings response"
                f"{': ' + detail if detail else ''}",
                kind="protocol", status=self.last_status,
            )
        if len(items) != len(batch):
            raise self._fail(
                f"protocol error: 请求 {len(batch)} 条，返回 {len(items)} 条"
                f"（顺序/条数无法一一对应）",
                kind="protocol", status=self.last_status,
            )

        ordered: list[tuple[int, list[float]]] = []
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                raise self._fail(f"protocol error: data[{position}] 不是对象",
                                 kind="protocol", status=self.last_status)
            raw_vec = item.get("embedding")
            if not isinstance(raw_vec, (list, tuple)) or not raw_vec:
                raise self._fail(
                    f"protocol error: data[{position}].embedding 不是非空数组",
                    kind="protocol", status=self.last_status)
            try:
                vec = [float(x) for x in raw_vec]
            except (TypeError, ValueError):
                raise self._fail(
                    f"protocol error: data[{position}].embedding 含非数值元素",
                    kind="protocol", status=self.last_status) from None
            index = item.get("index", position)
            if not isinstance(index, int) or isinstance(index, bool):
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    raise self._fail(
                        f"protocol error: data[{position}].index 非整数（{index!r}）",
                        kind="protocol", status=self.last_status) from None
            ordered.append((index, vec))

        ordered.sort(key=lambda pair: pair[0])
        expected = list(range(len(batch)))
        got = [idx for idx, _ in ordered]
        if got != expected:
            raise self._fail(
                f"protocol error: index 序列异常 {got}（期望 {expected}）",
                kind="protocol", status=self.last_status)

        vectors = [vec for _, vec in ordered]
        dims = {len(vec) for vec in vectors}
        if len(dims) != 1:
            raise self._fail(
                f"维度不一致：同批返回 {sorted(dims)} 种维度（缓存必须清）",
                kind="dim_mismatch", status=self.last_status)
        got_dim = dims.pop()
        if self.dim and got_dim != self.dim:
            raise self._fail(
                f"维度不一致：provider 声明 dim={self.dim}，服务端返回 {got_dim}"
                f"（模型换了？缓存必须清）",
                kind="dim_mismatch", status=self.last_status)
        self.dim = got_dim

        for text in batch:
            self._tokens_est += estimate_tokens(text)
        self._sent_texts += len(batch)
        return vectors

    # ── 传输 ────────────────────────────────────────────────────
    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "agent-runtime-embeddings/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, batch: Sequence[str]) -> dict:
        body: dict = {"model": self.model, "input": list(batch),
                      "encoding_format": "float"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=data, method="POST", headers=self._headers())
        timeout_s = max(0.05, self.timeout_ms / 1000.0)
        self.last_texts = list(batch)
        self._requests += 1

        try:
            with self._open(req, timeout=timeout_s) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                self.last_status = status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            self.last_status = int(exc.code)
            raise _Attempt(
                classify_status(exc.code),
                f"HTTP {exc.code} {self._error_detail(exc)}".strip(),
                status=exc.code,
                retryable=exc.code in EMBEDDING_RETRYABLE_STATUS,
            ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _Attempt("network", f"{type(exc).__name__}: {_as_text(reason)}",
                           retryable=True) from None
        except Exception as exc:                    # noqa: BLE001 — 传输层兜底
            raise _Attempt("transport", f"{type(exc).__name__}: {_as_text(exc)}",
                           retryable=True) from None

        # 有的网关不走 HTTPError，直接回 4xx/5xx 带错误体（fake 与部分自建代理都这样），
        # 所以状态码要判两次：异常路径 + 返回路径。
        if status >= 400:
            detail = _as_text(raw)[:ERROR_BODY_CHARS].strip()
            raise _Attempt(
                classify_status(status), f"HTTP {status} {detail}".strip(),
                status=status, retryable=status in EMBEDDING_RETRYABLE_STATUS)

        try:
            doc = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise _Attempt("protocol", "response body is not valid JSON",
                           status=status, retryable=True) from None
        if not isinstance(doc, dict):
            raise _Attempt("protocol",
                           f"response JSON root is {type(doc).__name__}, require object",
                           status=status, retryable=False)
        return doc

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read()
        except Exception:                           # noqa: BLE001
            raw = b""
        finally:
            try:
                exc.close()                         # 不 close 会漏文件描述符
            except Exception:                       # noqa: BLE001
                pass
        return _as_text(raw)[:ERROR_BODY_CHARS].strip()

    # ── 脱敏 / 错误 ─────────────────────────────────────────────
    def _scrub(self, text: Any) -> str:
        out = _as_text(text)
        if self.api_key:
            out = out.replace(self.api_key, "***")
        out = _BEARER_RE.sub("Bearer ***", out)
        out = _KEYLIKE_RE.sub("***", out)
        return out

    def _fail(self, message: str, *, kind: str = "unknown", status: int = 0,
              retryable: bool = False, attempts: int = 0) -> EmbeddingError:
        self._errors += 1
        return EmbeddingError(self._scrub(message), kind=kind, status=status,
                              model=self.model, retryable=retryable,
                              attempts=attempts)

    # ── 观测 ────────────────────────────────────────────────────
    def stats(self) -> dict:
        tokens = self._tokens_est
        return {
            "provider": self.name,
            "model": self.model,
            "dim": self.dim,
            "calls": self._calls,
            "texts": self._texts,
            "sent_texts": self._sent_texts,
            "cached": self._cached,
            "requests": self._requests,
            "retries": self._retries,
            "errors": self._errors,
            "tokens_est": tokens,
            "cost_estimate": round(tokens / 1_000_000.0 * self.price_per_m_tokens, 6),
            "cache": self.cache.to_dict(),
            "history": list(self.history),
        }


# ══════════════════════════════════════════════════════════════
# 零依赖兜底：确定性哈希向量
# ══════════════════════════════════════════════════════════════
class HashingEmbeddings:
    """确定性哈希向量（signed hashing trick）。**不是语义**，但可测、可复现。

    用途：没有网关时把上层链路（缓存 / 批量 / rank / 降级 / bench 脚本）跑通。
    它对"同义改写"没有任何理解力 —— 所以它不能替代真 embedding，只能替代"没有"。

    为什么是 sha256 而不是内置 ``hash()``：字符串哈希受 ``PYTHONHASHSEED`` 影响，
    跨进程不可复现，那样"确定性"就是假的（与 ``plane/memory.py`` 的 ``fact_key``
    同一个理由）。
    """

    name = "hashing"
    dim = 256

    def __init__(self, dim: int = 256, *, ngram: int = 2,
                 whole_weight: float = 1.0, gram_weight: float = 0.5) -> None:
        self.dim = max(1, int(dim))
        self.ngram = max(1, int(ngram))
        self.whole_weight = float(whole_weight)
        self.gram_weight = float(gram_weight)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, str):
            raise TypeError("embed 需要 Sequence[str]；单个字符串请传 [text]")
        return [self._one(str(t)) for t in (texts or ())]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        s = _PUNCT_RE.sub("", (text or "").lower())
        if not s:
            return vec
        # 整句一个 token（抓"完全同名"）+ 滑窗 ngram（抓局部重合）
        self._add(vec, s, self.whole_weight)
        if len(s) > self.ngram:
            for i in range(len(s) - self.ngram + 1):
                self._add(vec, s[i:i + self.ngram], self.gram_weight)
        norm = sum(x * x for x in vec) ** 0.5
        if norm <= 0.0:
            return vec
        return [x / norm for x in vec]           # L2 归一化：余弦就直接是点积

    def _add(self, vec: list[float], token: str, weight: float) -> None:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[slot] += sign * weight

    def stats(self) -> dict:
        return {"provider": self.name, "dim": self.dim, "ngram": self.ngram}


# ══════════════════════════════════════════════════════════════
# 工厂：按环境变量装配（缺 key 返回 None，调用方据此跳过真实用例）
# ══════════════════════════════════════════════════════════════
def from_env(prefix: str = "BABEL") -> OpenAICompatEmbeddings | None:
    """读 ``{PREFIX}_KEY`` / ``{PREFIX}_API_KEY`` / ``{PREFIX}_BASE`` /
    ``{PREFIX}_EMBED_MODEL``。

    * **api_key 必填**：这里与 ``openai_compat.from_env`` 不同 —— 向量接口没有
      "本地不校验"的常见场景，缺 key 就是缺能力，返回 ``None`` 让调用方 skip，
      而不是发一个注定 401 的请求。
    * ``BASE`` / ``BASE_URL`` 都可，缺省用 :data:`DEFAULT_BASE_URL`。
    * 可选：``{PREFIX}_EMBED_TIMEOUT_MS`` / ``{PREFIX}_EMBED_BATCH``。
    * 任何配置不可用 → 返回 ``None``，**不抛错**（真实冒烟应当 skip 而不是红）。
    """
    head = (prefix or "BABEL").strip().upper()

    def read(name: str) -> str:
        return (os.environ.get(f"{head}_{name}") or "").strip()

    api_key = read("KEY") or read("API_KEY")
    if not api_key:
        return None
    base_url = read("BASE") or read("BASE_URL") or DEFAULT_BASE_URL
    model = read("EMBED_MODEL") or DEFAULT_MODEL

    kwargs: dict = {}
    raw_timeout = read("EMBED_TIMEOUT_MS")
    if raw_timeout.isdigit():
        kwargs["timeout_ms"] = int(raw_timeout)
    raw_batch = read("EMBED_BATCH")
    if raw_batch.isdigit():
        kwargs["batch_size"] = int(raw_batch)

    try:
        return OpenAICompatEmbeddings(base_url, model, api_key, **kwargs)
    except (TypeError, ValueError):
        return None
