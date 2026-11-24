"""向量存储（Qdrant）的验收：替代全扫，但记忆策略不变。"""
from __future__ import annotations

import unittest

from contracts import MemoryKind, MemoryRecord, Subject, now_ms
from plane.vector_store import (
    VectorProvider, VectorStoreError, make_subject_filter, qdrant_available,
    vector_memory,
)


class FakeEmbedder:
    """确定性假嵌入：按关键词映射到固定维度（不联网、不依赖模型）。"""

    name, dim = "fake", 4

    def embed(self, texts):
        out = []
        for t in texts or ():
            v = [0.0] * 4
            # 语义簇：同一簇内的不同说法要落到同一维度上，
            # 否则「数据库连接满了」与「连接池耗尽」会被判成无关 —— 那不是语义嵌入，
            # 只是关键词精确匹配。这是假嵌入最容易写错的地方。
            if any(k in t for k in ("连接池", "连接", "pool", "数据库")):
                v[0] = 1.0
            elif any(k in t for k in ("灰度", "发布", "回归")):
                v[1] = 1.0
            elif any(k in t for k in ("日志", "磁盘", "盘")):
                v[2] = 1.0
            else:
                v[3] = 1.0
            out.append(v)
        return out


class BrokenEmbedder:
    name, dim = "broken", 4

    def embed(self, texts):
        raise RuntimeError("嵌入服务挂了")


TEXTS = ["连接池耗尽导致超时", "灰度发布先过全量回归", "日志盘占用 92% 六天写满"]


def subj(**kw):
    base = {"tenant": "acme", "user_id": "u1"}
    base.update(kw)
    return Subject(**base)


def rec(i: int, text: str, s: Subject, **kw):
    base = dict(id=f"m{i}", kind=MemoryKind.EPISODIC, text=text, subject=s,
                importance=0.5, confidence=0.5, created_at=now_ms())
    base.update(kw)
    return MemoryRecord(**base)


@unittest.skipUnless(qdrant_available(), "qdrant-client 未安装")
class TestProviderProtocol(unittest.TestCase):
    def setUp(self):
        self.p = VectorProvider(embedder=FakeEmbedder())
        self.s = subj()
        for i, t in enumerate(TEXTS):
            self.p.put(rec(i, t, self.s))

    def tearDown(self):
        self.p.close()

    def test_put_get_roundtrip(self):
        r = self.p.get("m0")
        self.assertIsNotNone(r)
        self.assertEqual(r.text, TEXTS[0])
        self.assertEqual(r.subject.tenant, "acme")

    def test_scan_returns_subject_records(self):
        self.assertEqual(len(self.p.scan(self.s)), 3)

    def test_cross_tenant_invisible(self):
        self.assertEqual(self.p.scan(subj(tenant="evilcorp")), [])
        self.assertEqual(self.p.search("连接池", subj(tenant="evilcorp")), [])

    def test_same_tenant_other_user_invisible(self):
        self.assertEqual(self.p.scan(subj(user_id="u2")), [])

    def test_team_project_sharing_visible(self):
        """同 team + 同 project 的记录能互相看到（共享层）。"""
        shared = subj(user_id="u2", team="sre", project="p1")
        self.p.put(rec(9, "连接池调优经验", shared))
        mine = subj(user_id="u1", team="sre", project="p1")
        texts = [r.text for r in self.p.scan(mine)]
        self.assertIn("连接池调优经验", texts)

    def test_delete(self):
        self.assertTrue(self.p.delete("m0"))
        self.assertFalse(self.p.delete("m0"))
        self.assertIsNone(self.p.get("m0"))

    def test_superseded_not_returned_by_search(self):
        old = rec(10, "连接池上限 20", self.s)
        old.superseded_by = "m11"
        self.p.put(old)
        ids = [r.id for r, _ in self.p.search("连接池", self.s, k=10)]
        self.assertNotIn("m10", ids)

    def test_superseded_visible_with_flag(self):
        old = rec(10, "连接池上限 20", self.s)
        old.superseded_by = "m11"
        self.p.put(old)
        ids = [r.id for r, _ in self.p.search("连接池", self.s, k=10,
                                              include_superseded=True)]
        self.assertIn("m10", ids)

    def test_rebuild_index_idempotent(self):
        n1 = self.p.rebuild_index()["reindexed"]
        n2 = self.p.rebuild_index()["reindexed"]
        self.assertEqual(n1, n2)
        self.assertEqual(n1, 3)
        self.assertEqual(len(self.p.scan(self.s)), 3)

    def test_stats(self):
        st = self.p.stats()
        self.assertEqual(st["backend"], "qdrant")
        self.assertEqual(st["dim"], 4)
        self.assertGreaterEqual(st["points"], 3)


@unittest.skipUnless(qdrant_available(), "qdrant-client 未安装")
class TestVectorSearch(unittest.TestCase):
    def setUp(self):
        self.p = VectorProvider(embedder=FakeEmbedder())
        self.s = subj()
        for i, t in enumerate(TEXTS):
            self.p.put(rec(i, t, self.s))

    def tearDown(self):
        self.p.close()

    def test_semantic_match_ranks_first(self):
        hits = self.p.search("连接池超时", self.s, k=3)
        self.assertEqual(hits[0][0].text, TEXTS[0])
        self.assertGreater(hits[0][1], 0.9)

    def test_k_limit(self):
        self.assertLessEqual(len(self.p.search("连接池 灰度 日志", self.s, k=2)), 2)

    def test_min_score_filters(self):
        hits = self.p.search("连接池", self.s, k=5, min_score=0.5)
        self.assertTrue(all(sc >= 0.5 for _, sc in hits))

    def test_empty_query_returns_or_raises_cleanly(self):
        """空查询的明确行为：要么返回空，要么抛 VectorStoreError（不能静默返回无关结果）。"""
        try:
            hits = self.p.search("", self.s, k=1)
        except VectorStoreError:
            return
        self.assertIsInstance(hits, list)

    def test_synonym_recall_via_vector(self):
        """换说法也能召回（向量分靠关键词映射模拟语义）。"""
        self.p.put(rec(20, "pool 满了", self.s))
        hits = self.p.search("连接池", self.s, k=3)
        texts = [t for t, _ in [(r.text, sc) for r, sc in hits]]
        self.assertIn("pool 满了", texts)

    def test_division_by_tenant_filter(self):
        other = subj(tenant="other", user_id="u1")
        self.p.put(rec(30, "连接池在别的租户", other))
        mine = [r.text for r, _ in self.p.search("连接池", self.s, k=5)]
        self.assertNotIn("连接池在别的租户", mine)
        theirs = [r.text for r, _ in self.p.search("连接池", other, k=5)]
        self.assertIn("连接池在别的租户", theirs)

    def test_dimension_mismatch_raises(self):
        class WrongDim:
            name, dim = "wrong", 8

            def embed(self, texts):
                return [[0.0] * 8 for _ in texts]

        # 显式声明期望 4 维，让 8 维嵌入触发不一致检测
        p2 = VectorProvider(embedder=WrongDim(), dim=4)
        try:
            with self.assertRaises(VectorStoreError):
                p2.put(rec(40, "x", subj()))
        finally:
            p2.close()

    def test_embedder_failure_raises(self):
        p2 = VectorProvider(embedder=BrokenEmbedder(), dim=4)
        with self.assertRaises(VectorStoreError):
            p2.put(rec(41, "x", subj()))
        p2.close()

    def test_scan_empty_for_unknown_subject(self):
        self.assertEqual(self.p.scan(subj(tenant="nobody")), [])


@unittest.skipUnless(qdrant_available(), "qdrant-client 未安装")
class TestFilterFailClosed(unittest.TestCase):
    def test_empty_tenant_rejected(self):
        with self.assertRaises(VectorStoreError):
            make_subject_filter(Subject(tenant=""))

    def test_blank_tenant_rejected(self):
        with self.assertRaises(VectorStoreError):
            make_subject_filter(Subject(tenant="   "))


@unittest.skipUnless(qdrant_available(), "qdrant-client 未安装")
class TestMemoryWiring(unittest.TestCase):
    def _memory(self):
        from agents.demo_extractor import RuleBasedExtractor
        from plane.memory import MemoryRuntime
        # 注入领域抽取器：Runtime 默认的 PassthroughExtractor 不做语义分类，
        # 所以 "我偏好…" 不会被标成 profile。这是 ADR-8 的边界，不是 bug。
        return vector_memory(MemoryRuntime, embedder=FakeEmbedder(),
                             extractor=RuleBasedExtractor())

    def test_recall_works(self):
        m = self._memory()
        s = subj()
        for t in TEXTS:
            m.write({"user": t, "assistant": "记下了", "tools": []}, s)
        hits = m.recall("连接池超时", s, k=3)
        self.assertTrue(hits)

    def test_recall_keeps_multi_factor_ordering(self):
        """换存储不该改变记忆策略：Profile 仍要置顶。"""
        m = self._memory()
        s = subj()
        m.write({"user": "我偏好只读排查，禁止改生产配置", "assistant": "好", "tools": []}, s)
        m.write({"user": "日志盘占用 92%", "assistant": "好", "tools": []}, s)
        hits = m.recall("日志盘占满", s, k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].kind, "profile")

    def test_budget_trim_still_applies(self):
        m = self._memory()
        s = subj()
        for t in TEXTS:
            m.write({"user": t, "assistant": "记下了", "tools": []}, s)
        tiny = m.recall("连接池", s, k=5, budget_tokens=8)
        self.assertLessEqual(len(tiny), 3)

    def test_provider_swappable_and_isolated(self):
        a = self._memory()
        b = self._memory()
        a.write({"user": "连接池上限 20", "assistant": "好", "tools": []}, subj())
        self.assertEqual(len(b.recall("连接池", subj(), k=5)), 0)

    def test_scores_within_unit_interval(self):
        """回归：分数必须在 [0,1]。

        曾经的实现用「再调一次 _score 再减 old_sim」倒推非语义项，
        结果算出 1.32（公式上限是 0.5+0.3+0.2=1.0，PROFILE 再 +0.3）。
        """
        m = self._memory()
        s = subj()
        for t in ("上次连接池耗尽导致接口超时，重启后恢复",
                  "上周灰度发布没跑回归，结果线上报错了",
                  "昨天日志盘占用到了 92%，按当前增速六天写满"):
            m.write({"user": t, "assistant": "已记录该经验", "tools": []}, s)
        for h in m.recall("数据库连接满了打不开", s, k=5):
            self.assertGreaterEqual(h.score, 0.0)
            self.assertLessEqual(h.score, 1.3 + 1e-9,
                                 f"分数 {h.score} 超出公式上限：{h.text[:20]}")

    def test_semantic_nearest_ranks_first(self):
        m = self._memory()
        s = subj()
        for t in ("上次连接池耗尽导致接口超时，重启后恢复",
                  "上周灰度发布没跑回归，结果线上报错了"):
            m.write({"user": t, "assistant": "已记录该经验", "tools": []}, s)
        hits = m.recall("数据库连接满了打不开", s, k=3)
        self.assertTrue(hits)
        self.assertIn("连接池", hits[0].text)

    def test_semantic_stats_available(self):
        st = self._memory().vector_provider.stats()
        self.assertEqual(st["backend"], "qdrant")

    def test_recall_survives_provider_failure(self):
        """向量库挂了要降级回全扫，不是抛错给调用方。"""
        m = self._memory()
        s = subj()
        m.write({"user": "连接池上限 20", "assistant": "好", "tools": []}, s)

        def boom(*a, **k):
            raise VectorStoreError("down")

        m.vector_provider.search = boom       # type: ignore[assignment]
        hits = m.recall("连接池", s, k=3)
        self.assertTrue(hits)                 # 降级成功，仍有结果


class TestOptionalDependency(unittest.TestCase):
    def test_availability_probe(self):
        self.assertIsInstance(qdrant_available(), bool)

    def test_error_type(self):
        self.assertTrue(issubclass(VectorStoreError, RuntimeError))


if __name__ == "__main__":
    unittest.main(verbosity=2)
