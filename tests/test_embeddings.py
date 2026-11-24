"""tests/test_embeddings.py — W26：语义向量 adapter + 相似度组合层。

**离线原则**：本文件不打开任何 socket。传输层通过 `opener=` 注入确定性 fake
（`FakeGateway`），所有"网关"行为都由脚本回放。真网关只在 `scripts/embedding_bench.py`
里出现（那里没有 ``BABEL_KEY`` 就 skip）。

验收核心是 :class:`TestSemanticBeatsLiteral`：同一批候选上，**生产里的** bigram
（`plane.memory.text_similarity`）把"字面像"的句子排第一，embedding 把"意思像"的
句子排第一。这是"召回质量"唯一可断言的定义。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.embeddings import (  # noqa: E402
    DEFAULT_MODEL,
    EmbeddingCache,
    EmbeddingError,
    HashingEmbeddings,
    OpenAICompatEmbeddings,
    classify_status,
    cosine,
    from_env,
)
from contracts import MemoryKind, MemoryRecord, Subject  # noqa: E402
from plane import memory as memory_plane  # noqa: E402
from plane.semantic import (  # noqa: E402
    SemanticSimilarity,
    bigram_similarity,
    clamp01,
)

API_KEY = "sk-w26-SECRET-must-not-leak-3f9a"
MODEL = "text-embedding-v4"


# ══════════════════════════════════════════════════════════════
# fake 传输层
# ══════════════════════════════════════════════════════════════
class _Resp:
    """urllib 响应的最小形状（context manager + status + read）。"""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):                     # noqa: ANN002 — contextlib 协议
        return False


def _vec(text: str, dim: int) -> list[float]:
    """确定性伪向量：同一文本永远同一条（用来断言"顺序对得上"）。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in digest[:dim]]


class FakeGateway:
    """回放式 fake 网关。``handler(call_no, body) -> (status, doc)``。

    默认行为：按 ``input`` 逐条造确定性向量；``shuffle=True`` 时**故意乱序返回**，
    用来验证 adapter 是靠 ``index`` 而不是到达顺序还原的。
    """

    def __init__(self, handler=None, *, dim: int = 8, shuffle: bool = False) -> None:
        self.handler = handler
        self.dim = dim
        self.shuffle = shuffle
        self.calls = 0
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.timeouts: list[float] = []

    # ── 传输入口 ────────────────────────────────────────────────
    def __call__(self, req, timeout=None):        # noqa: ANN001 — 模拟 urlopen
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls += 1
        self.bodies.append(body)
        self.headers.append(dict(req.headers))
        self.timeouts.append(float(timeout or 0))
        if self.handler is not None:
            status, doc = self.handler(self.calls, body)
        else:
            status, doc = 200, self.auto(body)
        payload = doc if isinstance(doc, bytes) else json.dumps(
            doc, ensure_ascii=False).encode("utf-8")
        return _Resp(status, payload)

    # ── 默认应答 ────────────────────────────────────────────────
    def auto(self, body: dict, *, dim: int | None = None) -> dict:
        use_dim = int(dim if dim is not None else self.dim)
        texts = body.get("input") or []
        data = [{"object": "embedding", "index": i, "embedding": _vec(t, use_dim)}
                for i, t in enumerate(texts)]
        if self.shuffle:
            data.reverse()
        return {"object": "list", "data": data, "model": body.get("model"),
                "usage": {"prompt_tokens": 3, "total_tokens": 3}}

    # ── 断言便利 ────────────────────────────────────────────────
    @property
    def sent(self) -> list[str]:
        """所有上行文本（按请求顺序摊平）。"""
        out: list[str] = []
        for body in self.bodies:
            out.extend(body.get("input") or [])
        return out


def make_provider(gateway: FakeGateway, **kw) -> OpenAICompatEmbeddings:
    kw.setdefault("api_key", API_KEY)
    kw.setdefault("max_retries", 2)
    return OpenAICompatEmbeddings(
        "https://gw.test/v1", MODEL, gateway and kw.pop("api_key", API_KEY),
        sleeper=kw.pop("sleeper", lambda _d: None), opener=gateway, **kw)


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(round(float(delay), 6))


# ══════════════════════════════════════════════════════════════
# fake EmbeddingProvider（给 SemanticSimilarity 用）
# ══════════════════════════════════════════════════════════════
class TableProvider:
    """按表查向量；记录调用次数与上行文本。可切换成"网关挂了"。"""

    name = "fake-table"
    dim = 2

    def __init__(self, table: dict[str, list[float]] | None = None, *,
                 dim: int = 2, fail: bool = False, short: bool = False,
                 raises: BaseException | None = None) -> None:
        self.table = dict(table or {})
        self.dim = dim
        self.fail = fail
        self.short = short
        self.raises = raises
        self.calls = 0
        self.texts: list[str] = []

    def embed(self, texts):
        self.calls += 1
        self.texts.extend(str(t) for t in texts)
        if self.raises is not None:
            raise self.raises
        if self.fail:
            raise EmbeddingError("fake gateway down: HTTP 503", kind="server", status=503)
        out = [list(self.table.get(str(t), [0.0] * self.dim)) for t in texts]
        return out[:-1] if self.short else out


# ══════════════════════════════════════════════════════════════
# 组合接线：**不改 memory.py** 把 sim 那一项换掉
# ══════════════════════════════════════════════════════════════
class SemanticMemory(memory_plane.MemoryRuntime):
    """只重写 `_score` 里的 **sim 一项**，其余（权重/Profile 加成/预算裁剪/续命）全继承。

    打分公式对 sim 是线性的，所以「减掉 bigram 项、加上语义项」在数学上等价于
    "把那个输入换掉"。这不是权宜：它让 `memory.py` 一行不动，也能被证明是零偏差的
    —— `sim` 降级成 bigram 副本时，两边分数必须逐值相等（见 test_46）。

    `_rank` 只做一件事：把 query 原文留下来（`_score` 只收 query 的 bigram 集合，
    拿不到原文；这是 memory.py 的既有签名，不改它）。
    """

    def __init__(self, *args, sim=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sim = sim if sim is not None else SemanticSimilarity(None)
        self._query = ""

    def _rank(self, query, subject, k, budget_tokens, activate):
        self._query = query or ""
        # 预热：把「每条记忆一次 HTTP」压成一次批量请求（关键路径上的账）
        try:
            candidates = [r.text for r in self.provider.scan(subject)]
        except Exception:                       # noqa: BLE001 — 预热失败不影响正确性
            candidates = []
        self.sim.prefetch([self._query, *candidates])
        return super()._rank(query, subject, k, budget_tokens, activate)

    def _score(self, rec, query_bg, t):
        base = super()._score(rec, query_bg, t)
        old = memory_plane.W_SIMILARITY * memory_plane.similarity(
            query_bg, memory_plane._bigrams(rec.text))
        new = memory_plane.W_SIMILARITY * self.sim.sim(self._query, rec.text)
        return base - old + new


def seed(texts, subject, now: float = 1_000_000.0):
    """把文本直接落库（跳过 Extractor：这里测的是重排，不是抽取）。"""
    provider = memory_plane.InMemoryProvider()
    for i, text in enumerate(texts):
        provider.put(MemoryRecord(
            id=f"mem_{i:02d}", kind=MemoryKind.EPISODIC, text=text, subject=subject,
            importance=0.5, confidence=0.5, created_at=now, last_hit_at=0.0, hits=0,
            version=1, superseded_by=None, ttl_days=60.0, source="seed", fact_key=""))
    return provider


# ══════════════════════════════════════════════════════════════
# ① adapter：协议解析 / 顺序 / 批次 / 缓存
# ══════════════════════════════════════════════════════════════
class TestEmbeddingProtocol(unittest.TestCase):
    def test_01_normal_response_parses_vectors(self):
        """fake 服务正常返回 → 向量正确解析，维度在首次调用后确定。"""
        gw = FakeGateway(dim=8)
        prov = make_provider(gw)
        self.assertEqual(prov.dim, 0)                     # 构造时未知
        out = prov.embed(["连接池耗尽", "数据库连接数打满", "召回质量"])
        self.assertEqual(len(out), 3)
        self.assertEqual(prov.dim, 8)                     # 首次调用后确定
        for text, vec in zip(["连接池耗尽", "数据库连接数打满", "召回质量"], out):
            self.assertEqual(vec, _vec(text, 8))
        self.assertEqual(gw.calls, 1)
        self.assertEqual(prov.stats()["dim"], 8)

    def test_02_index_order_not_arrival_order(self):
        """服务端乱序返回 → 仍按 ``index`` 还原，输入输出一一对应。"""
        gw = FakeGateway(dim=4, shuffle=True)
        prov = make_provider(gw)
        texts = ["甲", "乙", "丙", "丁"]
        out = prov.embed(texts)
        for text, vec in zip(texts, out):
            self.assertEqual(vec, _vec(text, 4), f"{text} 拿到了别人的向量")

    def test_03_batching_splits_requests(self):
        """输入 70 条 + batch_size=32 → 恰好 3 次请求（32/32/6）。"""
        gw = FakeGateway(dim=4)
        prov = make_provider(gw, batch_size=32)
        texts = [f"记忆条目-{i}" for i in range(70)]
        out = prov.embed(texts)
        self.assertEqual(len(out), 70)
        self.assertEqual(gw.calls, 3)
        self.assertEqual([len(b["input"]) for b in gw.bodies], [32, 32, 6])
        self.assertEqual(gw.sent, texts)                  # 上行顺序也没乱
        self.assertEqual(prov.stats()["sent_texts"], 70)

    def test_04_cache_hit_skips_request(self):
        """同文本二次调用不发请求，``cached`` 递增。"""
        gw = FakeGateway(dim=4)
        prov = make_provider(gw)
        first = prov.embed(["连接池耗尽", "数据库连接数打满"])
        self.assertEqual(gw.calls, 1)
        second = prov.embed(["连接池耗尽", "数据库连接数打满"])
        self.assertEqual(gw.calls, 1, "缓存命中却又发了请求 = 白付一次钱")
        self.assertEqual(first, second)
        self.assertEqual(prov.stats()["cached"], 2)
        self.assertEqual(prov.stats()["calls"], 2)
        self.assertEqual(prov.stats()["texts"], 4)

    def test_05_dedupes_within_one_call(self):
        """同一批里的重复文本只上行一次，但仍按位置逐条返回。"""
        gw = FakeGateway(dim=4)
        prov = make_provider(gw)
        out = prov.embed(["同一句", "同一句", "另一句"])
        self.assertEqual(gw.sent, ["同一句", "另一句"])
        self.assertEqual(out[0], out[1])
        self.assertEqual(len(out), 3)
        self.assertEqual(prov.stats()["sent_texts"], 2)

    def test_06_blank_text_gets_zero_vector_without_request(self):
        """空白文本 → 零向量（余弦 0），不占用一次网络请求。"""
        gw = FakeGateway(dim=3)
        prov = make_provider(gw)      # dim=3 → 取响应里的真实维度
        out = prov.embed(["有内容", "   ", ""])
        self.assertEqual(out[1], [0.0] * prov.dim)
        self.assertEqual(out[2], [0.0] * prov.dim)
        self.assertEqual(gw.sent, ["有内容"])
        self.assertEqual(gw.calls, 1)

    def test_07_vector_elements_are_floats(self):
        """输出向量元素都是 float（int 会让下游序列化/比较出现假相等）。"""
        gw = FakeGateway(dim=8)
        prov = make_provider(gw)
        for vec in prov.embed(["a", "b"]):
            self.assertTrue(all(isinstance(x, float) for x in vec), vec)
        for vec in HashingEmbeddings().embed(["a", "b"]):
            self.assertTrue(all(isinstance(x, float) for x in vec), vec)

    def test_08_declared_dim_matches_response(self):
        """构造时传入 dim → 服务端维度必须一致。"""
        gw = FakeGateway(dim=8)
        prov = make_provider(gw, dim=8)
        self.assertEqual(prov.dim, 8)
        self.assertEqual(len(prov.embed(["x"])[0]), 8)


# ══════════════════════════════════════════════════════════════
# ② 失败分类 / 重试 / 脱敏
# ══════════════════════════════════════════════════════════════
class TestRetryAndErrors(unittest.TestCase):
    def test_09_429_retries_then_succeeds(self):
        """429 → 指数退避重试后成功，sleeper 记录退避序列（0.5 → 1.0）。"""
        sleep = RecordingSleeper()
        state = {"n": 0}

        def handler(call_no, body):
            state["n"] += 1
            if state["n"] <= 2:
                return 429, {"error": {"message": "rate limited"}}
            return 200, gw.auto(body)

        gw = FakeGateway(handler, dim=4)
        prov = make_provider(gw, sleeper=sleep, max_retries=2)
        out = prov.embed(["限流后仍要成功"])
        self.assertEqual(len(out), 1)
        self.assertEqual(gw.calls, 3)
        self.assertEqual(sleep.delays, [0.5, 1.0])
        stats = prov.stats()
        self.assertEqual(stats["retries"], 2)
        self.assertEqual(stats["errors"], 0)

    def test_10_5xx_exhausted_raises_non_retryable(self):
        """5xx 重试耗尽 → EmbeddingError，且 marked 不可重试。"""
        def handler(call_no, body):
            return 503, {"error": {"message": "gateway down"}}

        gw = FakeGateway(handler)
        prov = make_provider(gw, max_retries=2)
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["炸"])
        self.assertEqual(ctx.exception.kind, "server")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(gw.calls, 3)                     # 1 次 + 2 次重试
        self.assertEqual(prov.stats()["errors"], 1)

    def test_11_401_does_not_retry(self):
        """401 → 不重试，一次就抛（同一 body 再发一次一定是再错一次）。"""
        gw = FakeGateway(lambda n, body: (401, {"error": {"message": "invalid api key"}}))
        sleep = RecordingSleeper()
        prov = make_provider(gw, sleeper=sleep, max_retries=3)
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["鉴权失败"])
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(gw.calls, 1)
        self.assertEqual(sleep.delays, [])

    def test_12_400_does_not_retry(self):
        gw = FakeGateway(lambda n, body: (400, {"error": {"message": "bad input"}}))
        prov = make_provider(gw, max_retries=3)
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["参数错"])
        self.assertEqual(ctx.exception.kind, "bad_request")
        self.assertEqual(gw.calls, 1)

    def test_13_network_error_retries(self):
        """网络抖动异常 → 按可重试处理（URLError 收到 reason 里）。"""
        import urllib.error

        class Boom:
            def __call__(self, req, timeout=None):
                raise urllib.error.URLError("connection refused")

        prov = OpenAICompatEmbeddings("https://gw.test/v1", MODEL, API_KEY,
                                      max_retries=1, sleeper=lambda _d: None,
                                      opener=Boom())
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["网络断"])
        self.assertEqual(ctx.exception.kind, "network")
        self.assertTrue(ctx.exception.retryable is False)   # 对外：已耗尽
        self.assertEqual(prov.stats()["requests"], 2)

    def test_14_dim_mismatch_raises(self):
        """维度不一致 → EmbeddingError（提示模型换了、缓存必须清）。"""
        gw = FakeGateway(dim=8)
        prov = make_provider(gw, dim=4)                    # 声明 4，服务端给 8
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["维度变了"])
        self.assertEqual(ctx.exception.kind, "dim_mismatch")
        self.assertIn("缓存", str(ctx.exception))

    def test_15_inconsistent_dims_within_response(self):
        """同一响应里混着两种维度（模型被换了）→ 也必须是错，不许静默按位置配。"""
        def handler(call_no, body):
            return 200, {"data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
                {"index": 1, "embedding": [0.1, 0.2]},
            ]}

        gw = FakeGateway(handler)
        prov = make_provider(gw)
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["甲", "乙"])
        self.assertEqual(ctx.exception.kind, "dim_mismatch")

    def test_16_count_mismatch_raises(self):
        """返回条数与请求条数不符 → 协议错（按位置配对会静默错位）。"""
        gw = FakeGateway(lambda n, body: (200, {"data": [
            {"index": 0, "embedding": [0.1, 0.2]}]}))
        prov = make_provider(gw)
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["甲", "乙"])
        self.assertEqual(ctx.exception.kind, "protocol")

    def test_17_empty_input_returns_empty_without_request(self):
        gw = FakeGateway()
        prov = make_provider(gw)
        self.assertEqual(prov.embed([]), [])
        self.assertEqual(gw.calls, 0)
        self.assertEqual(prov.stats()["calls"], 1)
        self.assertEqual(prov.stats()["texts"], 0)

    def test_18_api_key_never_leaks(self):
        """异常消息里不含 api_key —— 包括服务端把 key 回显在错误体里的情况。"""
        def handler(call_no, body):
            return 401, {"error": {"message": f"bad key {API_KEY}",
                                   "auth": f"Bearer {API_KEY}"}}

        gw = FakeGateway(handler)
        prov = make_provider(gw)
        with self.assertRaises(EmbeddingError) as ctx:
            prov.embed(["探 key"])
        msg = str(ctx.exception)
        self.assertNotIn(API_KEY, msg)
        self.assertNotIn("must-not-leak", msg)
        self.assertIn("***", msg)
        # 发出去的请求头里当然要有 key（那是协议），但异常里不能有
        self.assertIn("Authorization", gw.headers[0])

    def test_19_stats_shape(self):
        """stats() 至少提供约定的五个键，且能算量级成本。"""
        gw = FakeGateway(dim=4)
        prov = make_provider(gw)
        prov.embed(["连接池耗尽导致超时"])
        stats = prov.stats()
        for key in ("calls", "texts", "cached", "errors", "cost_estimate"):
            self.assertIn(key, stats)
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["texts"], 1)
        self.assertEqual(stats["cached"], 0)
        self.assertEqual(stats["errors"], 0)
        # 9 个中文 token * 0.02 USD/1M ≈ 1.8e-7 → 6 位小数下就是 0.0。
        # 所以断言落在 token 估算上，成本换算单独用放大 1e6 的单价验证。
        self.assertGreater(stats["tokens_est"], 0)
        self.assertGreaterEqual(stats["cost_estimate"], 0.0)
        loud = OpenAICompatEmbeddings("https://gw.test/v1", MODEL, API_KEY,
                                      price_per_m_tokens=1_000_000.0,
                                      sleeper=lambda _d: None, opener=FakeGateway(dim=4))
        loud.embed(["连接池耗尽导致超时"])
        self.assertAlmostEqual(loud.stats()["cost_estimate"],
                               loud.stats()["tokens_est"], places=6)

    def test_20_classify_status(self):
        self.assertEqual(classify_status(401), "auth")
        self.assertEqual(classify_status(400), "bad_request")
        self.assertEqual(classify_status(429), "rate_limit")
        self.assertEqual(classify_status(503), "server")
        self.assertEqual(classify_status(418), "client")


# ══════════════════════════════════════════════════════════════
# ③ 缓存 / 余弦 / 哈希兜底 / 工厂
# ══════════════════════════════════════════════════════════════
class TestCosinesAndCache(unittest.TestCase):
    def test_21_cosine_hand_computed(self):
        """手算对照（不是"跑通即对"）。"""
        self.assertAlmostEqual(cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0, places=12)
        self.assertAlmostEqual(cosine([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]), 0.0, places=12)
        self.assertAlmostEqual(cosine([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0]), -1.0, places=12)
        # (3*4 + 4*3) / (5 * 5) = 24/25
        self.assertAlmostEqual(cosine([3.0, 4.0], [4.0, 3.0]), 0.96, places=12)
        # (1*2 + 2*4) / (sqrt5 * sqrt20) = 10/10
        self.assertAlmostEqual(cosine([1.0, 2.0], [2.0, 4.0]), 1.0, places=12)
        # 零向量没有方向 → 0.0（不是异常，是定义）
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertEqual(cosine([], []), 0.0)

    def test_22_cosine_dim_mismatch_raises(self):
        with self.assertRaises(ValueError):
            cosine([1.0, 2.0], [1.0, 2.0, 3.0])

    def test_23_cache_lru_eviction(self):
        """max_size=2 插 3 个 → 最旧的被淘汰。"""
        cache = EmbeddingCache(max_size=2)
        cache.put("a", [1.0])
        cache.put("b", [2.0])
        self.assertEqual(cache.get("a"), [1.0])            # 触碰 a → b 变成最旧
        cache.put("c", [3.0])
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.get("b"), None, "LRU 应淘汰最久未用的 b")
        self.assertEqual(cache.get("a"), [1.0])
        self.assertEqual(cache.get("c"), [3.0])
        self.assertEqual(cache.evictions, 1)

    def test_24_cache_returns_copy(self):
        cache = EmbeddingCache(max_size=2)
        cache.put("a", [1.0, 2.0])
        got = cache.get("a")
        got[0] = 999.0                                      # 调用方手滑不该污染缓存
        self.assertEqual(cache.get("a"), [1.0, 2.0])
        cache.clear()
        self.assertEqual(cache.get("a"), None)

    def test_25_hashing_is_deterministic(self):
        """同文本两次 → 完全相同（跨进程也成立：sha256 而非内置 hash）。"""
        h = HashingEmbeddings()
        first = h.embed(["连接池耗尽导致超时"])
        second = HashingEmbeddings().embed(["连接池耗尽导致超时"])
        self.assertEqual(first, second)
        self.assertEqual(first[0], h.embed(["连接池耗尽导致超时"])[0])
        self.assertNotEqual(h.embed(["连接池耗尽"])[0], h.embed(["完全不同的一句话"])[0])

    def test_26_hashing_dim_fixed(self):
        h = HashingEmbeddings()
        self.assertEqual(h.dim, 256)
        for vec in h.embed(["短", "长一点的一句话", ""]):
            self.assertEqual(len(vec), 256)
        self.assertEqual(HashingEmbeddings(dim=64).dim, 64)
        self.assertEqual(len(HashingEmbeddings(dim=64).embed(["x"])[0]), 64)
        # 空文本 → 零向量（不是随机方向）
        self.assertEqual(h.embed([""])[0], [0.0] * 256)

    def test_27_hashing_normalized(self):
        """L2 归一化后，自己跟自己的余弦恰好是 1.0。"""
        h = HashingEmbeddings()
        vec = h.embed(["连接池耗尽导致超时"])[0]
        self.assertAlmostEqual(cosine(vec, vec), 1.0, places=9)

    def test_28_from_env_missing_key_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("W26TEST_KEY", "W26TEST_API_KEY", "W26TEST_BASE",
                         "W26TEST_BASE_URL", "W26TEST_EMBED_MODEL"):
                os.environ.pop(name, None)
            self.assertIsNone(from_env("W26TEST"))

    def test_29_from_env_with_key_builds_provider(self):
        env = {"W26TEST_KEY": API_KEY,
               "W26TEST_BASE": "https://gw.test/v1"}
        with mock.patch.dict(os.environ, env, clear=False):
            prov = from_env("W26TEST")
        self.assertIsNotNone(prov)
        self.assertEqual(prov.model, DEFAULT_MODEL)
        self.assertEqual(prov.endpoint, "https://gw.test/v1/embeddings")
        self.assertEqual(prov.api_key, API_KEY)


# ══════════════════════════════════════════════════════════════
# ④ SemanticSimilarity：走 provider / 降级 / rank
# ══════════════════════════════════════════════════════════════
class TestSemanticSimilarity(unittest.TestCase):
    def test_30_uses_provider(self):
        """真 provider 在链路上 → 走 provider，且一次 sim 只 embed 一次。"""
        prov = TableProvider({"甲": [1.0, 0.0], "乙": [0.0, 1.0]})
        sim = SemanticSimilarity(prov)
        self.assertAlmostEqual(sim.sim("甲", "乙"), 0.0, places=9)
        self.assertAlmostEqual(sim.sim("甲", "甲"), 1.0, places=9)
        self.assertEqual(prov.calls, 1, "两条文本应合成一次 embed 调用")
        self.assertEqual(prov.texts, ["甲", "乙"])     # 本地向量缓存：第二次不再上行
        stats = sim.stats()
        self.assertEqual(stats["embed_calls"], 1)
        self.assertEqual(stats["fallback_hits"], 0)
        self.assertEqual(stats["provider"], "fake-table")

    def test_31_provider_exception_falls_back(self):
        """provider 抛异常 → 降级到 fallback，**不抛给调用方**。"""
        prov = TableProvider(fail=True)
        sim = SemanticSimilarity(prov)
        a, b = "连接池耗尽导致超时", "连接池耗尽导致超时"
        self.assertAlmostEqual(sim.sim(a, b), 1.0, places=9)   # bigram 也认出一样
        self.assertEqual(sim.stats()["provider_errors"], 1)
        self.assertIn("503", sim.stats()["last_error"])

    def test_32_fallback_hits_counted(self):
        """降级计数正确（这是"降级发生过"的唯一观测面）。"""
        prov = TableProvider(fail=True)
        sim = SemanticSimilarity(prov)
        sim.sim("甲", "乙")
        sim.sim("丙", "丁")
        self.assertEqual(sim.stats()["fallback_hits"], 2)
        sim.rank("查询", ["候选一", "候选二"])
        self.assertEqual(sim.stats()["fallback_hits"], 3, "一次 rank 降级 = 一次信号")

    def test_33_no_provider_is_pure_fallback(self):
        sim = SemanticSimilarity(None)
        self.assertEqual(sim.sim("连接池耗尽", "连接池耗尽"), 1.0)
        self.assertEqual(sim.stats()["provider"], "none")
        self.assertEqual(sim.stats()["fallback_hits"], 1)
        self.assertEqual(sim.stats()["embed_calls"], 0)

    def test_34_rank_sorted_desc_with_original_index(self):
        """rank 按分数降序且返回原始下标；并列按下标升序（可复现）。"""
        prov = TableProvider({
            "Q": [1.0, 0.0],
            "贴题": [0.95, 0.05],
            "半贴题": [0.7, 0.7],
            "跑题": [0.0, 1.0],
            "并列-贴题": [0.95, 0.05],
        })
        sim = SemanticSimilarity(prov)
        ranked = sim.rank("Q", ["贴题", "跑题", "半贴题", "并列-贴题"])
        self.assertEqual([i for i, _ in ranked], [0, 3, 2, 1])
        scores = [s for _, s in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # 0 与 3 向量相同 → 分数并列，只靠「原始下标升序」决胜
        self.assertAlmostEqual(scores[0], scores[1], places=12)
        self.assertGreater(scores[1], scores[2])
        self.assertGreater(scores[2], scores[3])

    def test_35_rank_empty_candidates(self):
        sim = SemanticSimilarity(TableProvider())
        self.assertEqual(sim.rank("查询", []), [])
        self.assertEqual(sim.stats()["fallback_hits"], 0)

    def test_36_rank_degrades_as_a_whole_batch(self):
        prov = TableProvider(fail=True)
        sim = SemanticSimilarity(prov)
        ranked = sim.rank("连接池耗尽", ["连接池耗尽导致超时", "今天天气不错"])
        self.assertEqual(ranked[0][0], 0)
        self.assertEqual(prov.calls, 1)                  # 只在 rank 里试了一次
        self.assertEqual(sim.stats()["fallback_hits"], 1)

    def test_37_short_provider_response_degrades(self):
        """provider 少返一条 → 不许按位置硬配，整批降级。"""
        prov = TableProvider({"甲": [1.0, 0.0], "乙": [0.0, 1.0]}, short=True)
        sim = SemanticSimilarity(prov)
        value = sim.sim("甲", "乙")
        self.assertEqual(value, bigram_similarity("甲", "乙"))
        self.assertEqual(sim.stats()["fallback_hits"], 1)
        self.assertIn("条数", sim.stats()["last_error"])

    def test_38_clamps_to_unit_interval(self):
        """负余弦 / >1 的浮点噪声都裁到 [0,1]（召回公式假设 sim∈[0,1]）。"""
        prov = TableProvider({"甲": [1.0, 0.0], "反方向": [-1.0, 0.0]})
        sim = SemanticSimilarity(prov)
        self.assertEqual(sim.sim("甲", "反方向"), 0.0)
        self.assertEqual(clamp01(-0.4), 0.0)
        self.assertEqual(clamp01(1.0000001), 1.0)
        self.assertEqual(clamp01(float("nan")), 0.0)

    def test_39_bigram_copy_matches_production(self):
        """兜底副本必须与 `plane.memory.text_similarity` **逐值等价**。

        否则"降级"会悄悄改变召回行为，而没人能看出来 —— 那是最难查的一类回归。
        """
        pairs = [
            ("连接池耗尽导致超时", "数据库连接数打满"),
            ("连接池耗尽导致超时", "连接池耗尽导致超时"),
            ("召回质量", "召回质量很差"),
            ("苹果多少钱一斤", "苹果手机屏幕多少钱"),
            ("", "非空"),
            ("甲", "甲"),
        ]
        for a, b in pairs:
            self.assertAlmostEqual(bigram_similarity(a, b),
                                   memory_plane.text_similarity(a, b), places=12,
                                   msg=f"{a!r} vs {b!r}")

    def test_40_repeated_query_not_reembedded(self):
        """rank 之后再 sim 同一 query → 不再上行（本地向量表生效）。"""
        prov = TableProvider({"Q": [1.0, 0.0], "A": [0.9, 0.1], "B": [0.0, 1.0]})
        sim = SemanticSimilarity(prov)
        sim.rank("Q", ["A", "B"])
        self.assertEqual(prov.calls, 1)
        self.assertEqual(sorted(prov.texts), sorted(["Q", "A", "B"]))
        sim.sim("Q", "A")
        self.assertEqual(prov.calls, 1, "Q/A 都已在缓存里，不该再请求")


# ══════════════════════════════════════════════════════════════
# ⑤ 验收核心：语义 vs 字面
# ══════════════════════════════════════════════════════════════
class TestSemanticBeatsLiteral(unittest.TestCase):
    """**本任务的验收核心**：同一批候选，bigram 与 embedding 的排名必须不同，
    且 embedding 排出的第一名才是"意思对"的那条。

    这里的向量是**手工构造的 fixture**，语义轴照抄实测结论：text-embedding-v4 下
    「同义改写」余弦 ≈0.8，「字面像但意思不同」≈0.15。真实测量在
    `scripts/embedding_bench.py`（需要网关）；本测试证明的是**链路**：
    一旦向量是语义的，`sim → rank → 排序` 就会把对的那条顶上来，而 bigram 不会。
    """

    MEMORIES = [
        "红富士每斤八块钱",        # 0 ← 语义目标：与查询零字面重合
        "苹果手机屏幕报价",        # 1 ← 字面陷阱：共享「苹果」「多少钱」的骨架
        "iPhone 15 换屏价格",      # 2
        "服务器内存条报价",        # 3
        "今天食堂的西红柿炒蛋",    # 4
    ]
    QUERY = "苹果多少钱一斤"
    TARGET = 0
    LITERAL_DECOY = 1

    #: 两轴语义空间：[水果/生鲜, 电子设备]
    TABLE = {
        "苹果多少钱一斤": [1.00, 0.05],
        "红富士每斤八块钱": [0.98, 0.08],
        "苹果手机屏幕报价": [0.10, 0.95],
        "iPhone 15 换屏价格": [0.02, 0.99],
        "服务器内存条报价": [0.00, 0.20],
        "今天食堂的西红柿炒蛋": [0.55, 0.45],
    }

    def test_41_embedding_ranks_semantic_target_first(self):
        prov = TableProvider(self.TABLE, dim=2)
        semantic = SemanticSimilarity(prov)

        emb = semantic.rank(self.QUERY, self.MEMORIES)
        emb_order = [i for i, _ in emb]
        emb_scores = dict(emb)

        # ① 语义版第一名 = 意思对的那条（零字面重合）
        self.assertEqual(emb_order[0], self.TARGET,
                         f"embedding 版第一名应为 {self.TARGET}，实际 {emb_order}")
        # ② 字面陷阱没被顶上来
        self.assertGreater(emb_scores[self.TARGET], emb_scores[self.LITERAL_DECOY])

    def test_42_bigram_ranks_literal_decoy_first(self):
        """对照组：生产里的 bigram（`plane/memory.text_similarity`）把字面像的排第一。"""
        bigram = [(-memory_plane.text_similarity(self.QUERY, text), i)
                  for i, text in enumerate(self.MEMORIES)]
        bigram.sort()
        order = [i for _, i in bigram]
        self.assertEqual(order[0], self.LITERAL_DECOY,
                         f"bigram 版第一名应为字面陷阱 {self.LITERAL_DECOY}，实际 {order}")
        self.assertEqual(memory_plane.text_similarity(self.QUERY, self.MEMORIES[self.TARGET]),
                         0.0, "目标与查询本就不该有字面重合 —— 这正是问题所在")

    def test_43_semantic_strictly_better_than_literal(self):
        """把两边排名放一起比：目标记忆的名次必须**严格**变好，且跃到第一。"""
        prov = TableProvider(self.TABLE, dim=2)
        semantic = SemanticSimilarity(prov)
        emb_order = [i for i, _ in semantic.rank(self.QUERY, self.MEMORIES)]

        bigram_scored = sorted(
            ((memory_plane.text_similarity(self.QUERY, t), i)
             for i, t in enumerate(self.MEMORIES)),
            key=lambda p: (-p[0], p[1]))
        big_order = [i for _, i in bigram_scored]

        emb_rank_of_target = emb_order.index(self.TARGET)
        big_rank_of_target = big_order.index(self.TARGET)
        self.assertEqual(emb_rank_of_target, 0)
        self.assertGreater(big_rank_of_target, 0,
                           "字面版若不把目标排在后面，这组 fixture 就没证明力")
        self.assertLess(emb_rank_of_target, big_rank_of_target)

        # 反向对照：字面陷阱在语义版里必须掉下去
        self.assertGreater(emb_order.index(self.LITERAL_DECOY),
                           big_order.index(self.LITERAL_DECOY))

    def test_44_downstream_memory_formula_flips_winner(self):
        """接进 **memory 的真实打分公式**后，胜者从"字面陷阱"翻成"语义目标"。

        公式与 `plane/memory._score` 同形：
            0.50*sim + 0.30*importance + 0.20*recency   （importance/recency 取等值 0.5
            以消掉干扰项 —— 这里只比较 sim 这一项的贡献）
        它证明的不只是"分数变了"，而是"**召回结果变了**"：同一条记忆在 bigram 下
        排第 2、在语义下排第 1。用户换个说法提问，能不能想起来就是这一行的差别。
        """
        W_SIM, W_IMP, W_REC = 0.50, 0.30, 0.20
        importance = recency = 0.5
        fixed = W_IMP * importance + W_REC * recency          # 常量项

        semantic = SemanticSimilarity(TableProvider(self.TABLE, dim=2))
        rows = []
        for i, text in enumerate(self.MEMORIES):
            big = W_SIM * memory_plane.text_similarity(self.QUERY, text) + fixed
            emb = W_SIM * semantic.sim(self.QUERY, text) + fixed
            rows.append((i, big, emb))

        big_winner = max(rows, key=lambda r: r[1])[0]
        emb_winner = max(rows, key=lambda r: r[2])[0]
        self.assertEqual(big_winner, self.LITERAL_DECOY)
        self.assertEqual(emb_winner, self.TARGET)
        # 差距必须是量级上的，不是小数点后三位
        target_big = next(r[1] for r in rows if r[0] == self.TARGET)
        target_emb = next(r[2] for r in rows if r[0] == self.TARGET)
        self.assertGreater(target_emb - target_big, 0.4,
                           "语义增益过小说明 fixture 没抓到真实差距")

    def test_45_db_paraphrase_pair(self):
        """任务里那对真实案例（bigram≈0.07 vs 余弦 0.81）在同一链路下成立。"""
        pair = ("数据库连接数打满", "连接池耗尽导致超时")
        prov = TableProvider({
            pair[0]: [1.00, 0.05],
            pair[1]: [0.80, 0.20],       # 实测 0.81 的量级
        }, dim=2)
        semantic = SemanticSimilarity(prov)
        char_score = memory_plane.text_similarity(*pair)
        emb_score = semantic.sim(*pair)
        self.assertLess(char_score, 0.10)
        self.assertGreater(emb_score, 0.75)
        self.assertGreater(emb_score - char_score, 0.6)


    def test_49_prefetch_batches_and_is_idempotent(self):
        """prefetch 把 N 条合成 1 次；重复预热不再请求。"""
        prov = TableProvider({"甲": [1.0, 0.0], "乙": [0.0, 1.0], "丙": [1.0, 1.0]})
        sim = SemanticSimilarity(prov)
        self.assertEqual(sim.prefetch(["甲", "乙", "丙"]), 3)
        self.assertEqual(prov.calls, 1)
        self.assertEqual(sim.prefetch(["甲", "乙", "丙"]), 0)
        self.assertEqual(prov.calls, 1)
        sim.sim("甲", "乙")                      # 全部命中本地表
        self.assertEqual(prov.calls, 1)
        self.assertEqual(sim.stats()["fallback_hits"], 0)

    def test_50_prefetch_failure_is_silent_and_uncounted(self):
        """预热失败：不抛、不计 fallback_hits（那 N 次信号属于随后每个 sim）。"""
        sim = SemanticSimilarity(TableProvider(fail=True))
        self.assertEqual(sim.prefetch(["甲", "乙"]), 0)
        self.assertEqual(sim.stats()["fallback_hits"], 0)
        self.assertEqual(sim.stats()["provider_errors"], 1)
        sim.sim("甲", "乙")
        self.assertEqual(sim.stats()["fallback_hits"], 1)
        self.assertEqual(SemanticSimilarity(None).prefetch(["甲"]), 0)


# ══════════════════════════════════════════════════════════════
# ⑥ 端到端：真 recall() 上的召回反转（memory.py 一行未改）
# ══════════════════════════════════════════════════════════════
class TestRealRecallIntegration(unittest.TestCase):
    """把 `SemanticSimilarity` 接进**真的** `MemoryRuntime.recall()`。

    前面的用例证明的是 `rank()`；这里证明的是用户实际看到的东西：
    `recall(query, subject, k)` 返回的第一条 Chunk 是谁。
    """

    NOW = 1_000_000.0
    MEMORIES = TestSemanticBeatsLiteral.MEMORIES
    TABLE = TestSemanticBeatsLiteral.TABLE
    QUERY = TestSemanticBeatsLiteral.QUERY
    TARGET = TestSemanticBeatsLiteral.TARGET
    DECOY = TestSemanticBeatsLiteral.LITERAL_DECOY

    def _subject(self):
        return Subject(tenant="acme", user_id="u1")

    def test_46_degraded_wiring_is_score_identical(self):
        """降级态（provider=None）与原生 runtime 的召回**逐值相等**。

        这是接线的自证：如果分数有任何偏差，说明"换 sim"顺手改了别的东西。
        """
        subj = self._subject()
        plain = memory_plane.MemoryRuntime(provider=seed(self.MEMORIES, subj),
                                          now=lambda: self.NOW)
        wired = SemanticMemory(provider=seed(self.MEMORIES, subj),
                               sim=SemanticSimilarity(None), now=lambda: self.NOW)
        a = plain.recall(self.QUERY, subj, k=5, budget_tokens=4000)
        b = wired.recall(self.QUERY, subj, k=5, budget_tokens=4000)
        self.assertEqual([(c.doc_id, c.score) for c in a],
                         [(c.doc_id, c.score) for c in b])
        self.assertEqual(wired.sim.stats()["fallback_hits"], len(self.MEMORIES))

    def test_47_recall_finds_paraphrased_memory(self):
        """同一个 recall()：bigram 版召回字面陷阱，语义版召回换过说法的目标。"""
        subj = self._subject()
        plain = memory_plane.MemoryRuntime(provider=seed(self.MEMORIES, subj),
                                           now=lambda: self.NOW)

        provider = TableProvider(self.TABLE, dim=2)
        semantic = SemanticSimilarity(provider)
        wired = SemanticMemory(provider=seed(self.MEMORIES, subj),
                               sim=semantic, now=lambda: self.NOW)

        before = plain.recall(self.QUERY, subj, k=3, budget_tokens=4000)
        after = wired.recall(self.QUERY, subj, k=3, budget_tokens=4000)

        self.assertEqual(before[0].doc_id, f"mem_{self.DECOY:02d}",
                         f"bigram 版召回的第一条应是字面陷阱，实际 {before[0].text!r}")
        self.assertEqual(after[0].doc_id, f"mem_{self.TARGET:02d}",
                         f"语义版召回的第一条应是目标记忆，实际 {after[0].text!r}")
        # 召回结果确实变了，而不只是分数变了
        self.assertNotEqual(before[0].text, after[0].text)
        # 语义链路没有降级过：这是"真召回了"，不是"退化到字面"的巧合
        self.assertEqual(semantic.stats()["fallback_hits"], 0)
        self.assertEqual(semantic.stats()["provider_errors"], 0)
        # 5 条候选只 embed 了一次（本地向量表 + 批次合并）
        self.assertEqual(provider.calls, 1)

    def test_48_recall_survives_gateway_outage(self):
        """网关挂掉时 recall 仍然返回结果（降级），**不抛异常**。"""
        subj = self._subject()
        wired = SemanticMemory(provider=seed(self.MEMORIES, subj),
                               sim=SemanticSimilarity(TableProvider(fail=True)),
                               now=lambda: self.NOW)
        chunks = wired.recall(self.QUERY, subj, k=3, budget_tokens=4000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(c.text for c in chunks))
        self.assertGreater(wired.sim.stats()["fallback_hits"], 0)


class TestAdaptiveBatch(unittest.TestCase):
    """回归：网关批次上限比默认小时，必须**减半重试**而不是整批 400 降级。

    实测 babelark 限制 10 条/批，而 400 不可重试 —— 不拆批就会静默降级成 bigram，
    跑一次真实 bench 才发现（hit@1 从 4/5 掉到 1/5）。
    """

    def _adapter(self, limit: int, batch_size: int = 32):
        from adapters.embeddings import EmbeddingError, OpenAICompatEmbeddings
        calls: list[int] = []

        a = OpenAICompatEmbeddings("https://x/v1", api_key="k", batch_size=batch_size)
        a.dim = 4

        def fake_batch(batch):
            calls.append(len(batch))
            if len(batch) > limit:
                raise EmbeddingError(
                    f"HTTP 400 batch size is invalid, it should not be larger than {limit}")
            return [[float(len(t)), 1.0, 0.0, 0.0] for t in batch]

        a._embed_batch = fake_batch          # type: ignore[assignment]
        a._calls_seen = calls                # type: ignore[attr-defined]
        return a

    def test_splits_when_gateway_limit_smaller(self):
        a = self._adapter(limit=10)
        out = a.embed([f"t{i}" for i in range(25)])
        self.assertEqual(len(out), 25)
        self.assertTrue(a._batch_splits > 0, "必须触发拆批")

    def test_learns_smaller_batch_for_later_batches(self):
        a = self._adapter(limit=8)
        a.embed([f"t{i}" for i in range(40)])
        self.assertLessEqual(a.batch_size, 8, "拆批后应记住学到的上限")

    def test_single_item_never_splits_forever(self):
        a = self._adapter(limit=0)           # 永远 400：单条也要能报错退出，不死循环
        from adapters.embeddings import EmbeddingError
        with self.assertRaises(EmbeddingError):
            a.embed(["x"])

    def test_unrelated_400_not_treated_as_batch_problem(self):
        a = self._adapter(limit=10)
        from adapters.embeddings import EmbeddingError

        def boom(batch):
            raise EmbeddingError("HTTP 400 model not found")
        a._embed_batch = boom                # type: ignore[assignment]
        with self.assertRaises(EmbeddingError):
            a.embed(["x", "y"])

    def test_default_batch_size_is_gateway_safe(self):
        from adapters.embeddings import OpenAICompatEmbeddings
        self.assertLessEqual(OpenAICompatEmbeddings("https://x/v1").batch_size, 16)


if __name__ == "__main__":
    unittest.main()
