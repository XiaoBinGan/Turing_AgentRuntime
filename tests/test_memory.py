"""tests/test_memory.py — Memory Plane 自带用例（W5）。

覆盖五阶段代谢链 + 五维评估 + Provider SPI 可替换性：
    ① write  低价值/代指过滤、Profile/Semantic/Episodic 分级、工具回包抽取、INV-12
    ② recall 分数降序、Profile 置顶、多租户强隔离、预算裁剪、命中续命
    ③ reflect 3 条同主题 Episodic → 1 条 Semantic
    ④ consolidate 只产出 confidence>=0.8 且 hits>=2 的候选（不写 Skill 库）
    ⑤ sweep  delete / archive / keep 三种落法 + INV-13 规则溯源
    ⑥ metrics 五维（precision / overhead / recency / conflict / profile）
    ⑦ Extractor SPI：Runtime 判形状 / Agent 判内容（本次边界重构的验收点）
    ⑧ 全链路 write → recall → reflect → consolidate → sweep

分级正则（Profile/Semantic/Episodic）已随重构搬到 `agents/demo_extractor.py`，
本文件的构造全部显式注入 `RuleBasedExtractor()` —— 那才是「领域策略」，
Runtime 自身只提供 `PassthroughExtractor` 兜底。

只依赖 plane/memory.py + contracts.py（不 import core/*，不碰别人的文件）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import (  # noqa: E402
    RETENTION_MATRIX,
    MemoryKind,
    MemoryRecord,
    Subject,
)
from agents.demo_extractor import RuleBasedExtractor  # noqa: E402
from plane.memory import (  # noqa: E402
    ARCHIVED_TOMBSTONE,
    InMemoryProvider,
    MemoryRuntime,
    PassthroughExtractor,
    fact_key,
    text_similarity,
)

MS_PER_DAY = 86_400_000.0
T0 = 1_700_000_000_000.0


# ── 构造 helper ────────────────────────────────────────────────
class FakeClock:
    """可控时钟：TTL/衰减必须可复现，测试不能靠 time.sleep 熬时间。"""

    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance_days(self, days: float) -> None:
        self.t += days * MS_PER_DAY


def subj(tenant: str = "acme", user: str = "u1",
         team: str = "team-core", project: str = "proj-alpha") -> Subject:
    return Subject(tenant=tenant, user_id=user, team=team, project=project)


def turn(user: str = "", assistant: str = "", tools: list | None = None,
         turn_id: str = "turn_1") -> dict:
    return {"user": user, "assistant": assistant, "tools": tools or [],
            "turn_id": turn_id}


def semantic_record(rt: MemoryRuntime, text: str, s: Subject, *,
                    importance: float = 0.6, confidence: float = 0.6,
                    version: int = 1, superseded_by: str | None = None,
                    created_at: float | None = None) -> MemoryRecord:
    """绕过 write，直接造一条受控记录（sweep/metrics 用例需要精确控制时间与分数）。"""
    rec = MemoryRecord(
        id=f"mem_fix_{abs(hash(text)) % 10 ** 8:08d}",
        kind=MemoryKind.SEMANTIC, text=text, subject=s,
        importance=importance, confidence=confidence,
        created_at=created_at if created_at is not None else rt.now(),
        last_hit_at=0.0, hits=0, version=version,
        superseded_by=superseded_by, ttl_days=180.0, source="fixture",
    )
    rt.provider.put(rec)
    return rec


# ══════════════════════════════════════════════════════════════
# ① Extraction
# ══════════════════════════════════════════════════════════════
class TestExtraction(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.rt = MemoryRuntime(self.provider, RuleBasedExtractor(), now=self.clock)
        self.s = subj()

    def test_low_value_turn_is_dropped(self):
        """寒暄 / 确认句不入库（Memory Swamp 第一现场）。"""
        for text in ("好的", "收到", "谢谢", "ok", "嗯", "了解"):
            self.assertEqual(self.rt.write(turn(user=text, assistant=text), self.s), [])
        self.assertEqual(len(self.provider), 0)

    def test_low_value_mixed_with_content_keeps_content(self):
        """寒暄只丢掉寒暄那一句，不牵连同一 turn 里的实质内容。"""
        created = self.rt.write(turn(user="好的，连接池上限是 20"), self.s)
        self.assertEqual(len(created), 1)
        self.assertIn("连接池上限", created[0].text)

    def test_anaphora_sentences_dropped(self):
        """代指且无法消解 → 丢弃；能消解（后面紧跟具体名词）→ 保留。"""
        self.assertEqual(self.rt.write(turn(user="它应该改一下"), self.s), [])
        self.assertEqual(self.rt.write(turn(user="这个改成 30 吧"), self.s), [])
        kept = self.rt.write(turn(user="这个限制必须一律遵守"), self.s)
        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0].kind, MemoryKind.PROFILE)

    def test_profile_extraction(self):
        created = self.rt.write(turn(user="我更喜欢用 tabs 而不是空格"), self.s)
        self.assertEqual(len(created), 1)
        rec = created[0]
        self.assertIs(rec.kind, MemoryKind.PROFILE)
        self.assertIsNone(rec.ttl_days)          # Profile 永久，免疫衰减
        self.assertAlmostEqual(rec.importance, 0.9, places=4)

    def test_semantic_extraction(self):
        created = self.rt.write(turn(user="连接池上限是 20"), self.s)
        self.assertEqual(len(created), 1)
        rec = created[0]
        self.assertIs(rec.kind, MemoryKind.SEMANTIC)
        self.assertEqual(rec.ttl_days, 180.0)
        self.assertAlmostEqual(rec.importance, 0.6, places=4)

    def test_episodic_extraction(self):
        created = self.rt.write(turn(user="上次重启后连接池报错了"), self.s)
        self.assertEqual(len(created), 1)
        rec = created[0]
        self.assertIs(rec.kind, MemoryKind.EPISODIC)
        self.assertEqual(rec.ttl_days, 60.0)
        self.assertAlmostEqual(rec.importance, 0.4, places=4)

    def test_embedded_anaphora_still_extracts_episodic(self):
        """「刚才」单独作时间副词时不该被代指过滤吃掉（模块 docstring A 的裁决）。"""
        created = self.rt.write(turn(user="刚才重启后服务直接失败了"), self.s)
        self.assertEqual(len(created), 1)
        self.assertIs(created[0].kind, MemoryKind.EPISODIC)

    def test_tool_payload_extracts_only_explicit_facts(self):
        """只抽明确事实（字段 → 人话），不整包入库，密钥字段不落库。"""
        created = self.rt.write(turn(
            assistant="查完了",
            tools=[{"tool": "db.info",
                    "payload": {"pool_limit": 20, "password": "hunter2"}}]), self.s)
        self.assertEqual(len(created), 1)
        rec = created[0]
        self.assertIn("连接池上限 20", rec.text)
        self.assertNotIn("hunter2", rec.text)
        self.assertNotIn("pool_limit", rec.text)
        self.assertTrue(rec.source.startswith("tool:db.info"))
        self.assertIs(rec.kind, MemoryKind.SEMANTIC)

    def test_missing_subject_raises_INV12(self):
        """INV-12：无 subject 拒绝入库。"""
        with self.assertRaises(ValueError):
            self.rt.write(turn(user="连接池上限是 20"), None)          # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.rt.write(turn(user="连接池上限是 20"), Subject(tenant=""))
        with self.assertRaises(ValueError):
            self.rt.recall("连接池", None)                              # type: ignore[arg-type]
        self.assertEqual(len(self.provider), 0)

    def test_every_record_has_subject_and_source(self):
        created = self.rt.write(turn(
            user="我必须一律用 4 空格缩进",
            tools=[{"tool": "cfg", "payload": {"shard_count": 3}}]), self.s)
        self.assertGreaterEqual(len(created), 2)
        for rec in created:
            self.assertEqual(rec.subject, self.s)
            self.assertTrue(rec.source)
            self.assertIsNotNone(rec.id)

    def test_duplicate_write_is_idempotent(self):
        a = self.rt.write(turn(user="连接池上限是 20"), self.s)
        b = self.rt.write(turn(user="连接池上限是 20", turn_id="turn_2"), self.s)
        self.assertEqual(len(a), 1)
        self.assertEqual(b, [])                  # 同一句话不重复入库


# ══════════════════════════════════════════════════════════════
# ② Recall & Rerank
# ══════════════════════════════════════════════════════════════
class TestRecall(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.rt = MemoryRuntime(self.provider, RuleBasedExtractor(), now=self.clock)
        self.s = subj()

    def test_recall_returns_score_descending(self):
        self.rt.write(turn(user="连接池上限是 20"), self.s)
        self.rt.write(turn(user="批处理批大小默认是 100"), self.s)
        self.rt.write(turn(user="部署环境是 prod 集群"), self.s)
        chunks = self.rt.recall("连接池上限", self.s, k=5)
        self.assertGreaterEqual(len(chunks), 2)
        scores = [c.score for c in chunks]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(chunks[0].text, "连接池上限是 20")
        for c in chunks:
            self.assertEqual(c.source, "memory")
            self.assertIn(c.kind, ("profile", "semantic", "episodic"))

    def test_profile_always_first_even_with_zero_similarity(self):
        """刚性约束优先注入：语义相似度为 0 也压过最相关的 Semantic。"""
        p = self.rt.write(turn(user="我永远不要用 Windows 路径风格"), self.s)[0]
        self.rt.write(turn(user="连接池上限是 20"), self.s)
        # 前提校验：Profile 与查询的语义相似度确实是 0，不是靠相似度赢的
        self.assertEqual(text_similarity("连接池上限是多少", p.text), 0.0)
        chunks = self.rt.recall("连接池上限是多少", self.s, k=3)
        self.assertEqual(chunks[0].kind, "profile")
        self.assertEqual(chunks[0].doc_id, p.id)

    def test_cross_tenant_invisible(self):
        self.rt.write(turn(user="连接池上限是 20"), subj(tenant="acme"))
        self.assertEqual(self.rt.recall("连接池上限", subj(tenant="evil-corp")), [])
        self.assertEqual(len(self.rt.recall("连接池上限", subj(tenant="acme"))), 1)

    def test_same_tenant_other_user_invisible_without_shared_scope(self):
        self.rt.write(turn(user="连接池上限是 20"), subj(user="u1"))
        other = subj(user="u2", team="team-other", project="proj-other")
        self.assertEqual(self.rt.recall("连接池上限", other), [])
        # 同 team + 同 project 的共享层：可见
        shared = subj(user="u2", team="team-core", project="proj-alpha")
        self.assertEqual(len(self.rt.recall("连接池上限", shared)), 1)
        # team 相同但 project 不同：不可见（缺一不可）
        wrong_proj = subj(user="u2", team="team-core", project="proj-beta")
        self.assertEqual(self.rt.recall("连接池上限", wrong_proj), [])

    def test_budget_tokens_trims_result(self):
        # 四条不同事实（避免 fact_key 相同被版本覆写）
        for text in ("分片数默认是 3", "批大小默认是 100",
                     "超时毫秒默认是 5000", "副本数默认是 2"):
            self.rt.write(turn(user=text, turn_id=f"t_{text}"), self.s)
        small = self.rt.recall("分片数", self.s, k=5, budget_tokens=20)
        big = self.rt.recall("分片数", self.s, k=5, budget_tokens=800)
        self.assertLess(len(small), len(big))
        self.assertLessEqual(len(small), 2)
        self.assertEqual(len(big), 4)

    def test_hit_extends_life(self):
        rec = self.rt.write(turn(user="连接池上限是 20"), self.s)[0]
        self.assertEqual(rec.hits, 0)
        self.rt.recall("连接池上限", self.s)
        again = self.provider.get(rec.id)
        self.assertEqual(again.hits, 1)
        self.assertEqual(again.last_hit_at, self.clock.t)
        self.clock.advance_days(5)
        self.rt.recall("连接池上限", self.s)
        third = self.provider.get(rec.id)
        self.assertEqual(third.hits, 2)
        self.assertEqual(third.last_hit_at, self.clock.t)

    def test_hits_do_not_leak_to_non_recalled_records(self):
        a = self.rt.write(turn(user="连接池上限是 20"), self.s)[0]
        b = self.rt.write(turn(user="部署环境是 prod"), self.s)[0]
        self.rt.recall("连接池上限", self.s, k=1)
        self.assertEqual(self.provider.get(a.id).hits, 1)
        self.assertEqual(self.provider.get(b.id).hits, 0)

    def test_conflict_override_marks_old_superseded(self):
        """同 key 新事实 → 旧记录 superseded_by 指向新 id，新记录 version=2。"""
        old = self.rt.write(turn(user="连接池上限是 20"), self.s)[0]
        new = self.rt.write(turn(user="连接池上限是 30", turn_id="turn_2"), self.s)[0]
        self.assertEqual(fact_key(old.text), fact_key(new.text))
        self.assertEqual(new.version, 2)
        self.assertEqual(self.provider.get(old.id).superseded_by, new.id)
        chunks = self.rt.recall("连接池上限是多少", self.s, k=5)
        ids = [c.doc_id for c in chunks]
        self.assertIn(new.id, ids)
        self.assertNotIn(old.id, ids)            # 旧版本不再召回
        self.assertTrue(any("30" in c.text for c in chunks))


# ══════════════════════════════════════════════════════════════
# ③ Reflection
# ══════════════════════════════════════════════════════════════
class TestReflection(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.rt = MemoryRuntime(self.provider, RuleBasedExtractor(), now=self.clock)
        self.s = subj()

    def test_three_same_theme_episodics_become_one_semantic(self):
        for i, text in enumerate((
                "上次重启后连接池报错了",
                "昨天重启后连接池又报错",
                "刚才重启后连接池还是报错")):
            self.rt.write(turn(user=text, turn_id=f"turn_{i}"), self.s)
        self.assertEqual(len([r for r in self.provider.all()
                              if r.kind is MemoryKind.EPISODIC]), 3)

        produced = self.rt.reflect(self.s)
        self.assertEqual(len(produced), 1)
        rec = produced[0]
        self.assertIs(rec.kind, MemoryKind.SEMANTIC)
        self.assertAlmostEqual(rec.confidence, 0.85, places=4)
        self.assertEqual(rec.ttl_days, 180.0)
        self.assertTrue(rec.source.startswith("reflect:"))

        # 原 Episodic 保留（删不删由 sweep 决定）
        self.assertEqual(len([r for r in self.provider.all()
                              if r.kind is MemoryKind.EPISODIC]), 3)

    def test_reflect_is_idempotent(self):
        for i in range(3):
            self.rt.write(turn(user=f"上次重启后连接池报错了 第{i}次"), self.s)
        first = self.rt.reflect(self.s)
        second = self.rt.reflect(self.s)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])             # 同一共识不重复产出

    def test_reflect_ignores_two_member_cluster(self):
        self.rt.write(turn(user="上次重启后连接池报错了"), self.s)
        self.rt.write(turn(user="昨天重启后连接池又报错", turn_id="turn_2"), self.s)
        self.assertEqual(self.rt.reflect(self.s), [])   # <3 条不升华


# ══════════════════════════════════════════════════════════════
# ④ Consolidation
# ══════════════════════════════════════════════════════════════
class TestConsolidation(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.rt = MemoryRuntime(self.provider, RuleBasedExtractor(), now=self.clock)
        self.s = subj()

    def _seed_reflected(self) -> MemoryRecord:
        for i, text in enumerate((
                "上次重启后连接池报错了",
                "昨天重启后连接池又报错",
                "刚才重启后连接池还是报错")):
            self.rt.write(turn(user=text, turn_id=f"turn_{i}"), self.s)
        return self.rt.reflect(self.s)[0]

    def test_consolidate_requires_confidence_and_hits(self):
        reflected = self._seed_reflected()
        low_conf = self.rt.write(turn(user="批大小默认是 100"), self.s)[0]  # confidence 0.6

        # hits 还没上来 → 无候选
        self.assertEqual(self.rt.consolidate(self.s), [])

        for _ in range(2):
            self.rt.recall("重启后连接池报错", self.s, k=10)

        self.assertGreaterEqual(self.provider.get(reflected.id).hits, 2)
        self.assertGreaterEqual(self.provider.get(low_conf.id).hits, 2)
        candidates = self.rt.consolidate(self.s)
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(set(cand), {"skill_id", "title", "from_memory", "reason", "draft"})
        self.assertTrue(cand["skill_id"].startswith("skill_"))
        self.assertIn(reflected.id, cand["from_memory"])
        self.assertNotIn(low_conf.id, cand["from_memory"])   # confidence 0.6 不够

    def test_consolidate_does_not_write_skill_library(self):
        """架构裁决：Procedural 归 Skill 库，Memory 只产出候选。"""
        self._seed_reflected()
        self.rt.recall("重启后连接池报错", self.s, k=10)
        self.rt.recall("重启后连接池报错", self.s, k=10)
        before = len(self.provider)
        self.rt.consolidate(self.s)
        self.assertEqual(len(self.provider), before)   # 不落任何新记录


# ══════════════════════════════════════════════════════════════
# ⑤ Forget (sweep)
# ══════════════════════════════════════════════════════════════
class TestSweep(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.rt = MemoryRuntime(self.provider, RuleBasedExtractor(), now=self.clock)
        self.s = subj()

    def test_expired_low_importance_episodic_deleted_profile_kept(self):
        epi = self.rt.write(turn(user="上次重启后连接池报错了"), self.s)[0]
        prof = self.rt.write(turn(user="我必须一律用 4 空格缩进"), self.s)[0]
        self.assertEqual(epi.importance, 0.4)
        # 抽取默认 0.4 高于矩阵门槛 0.30 → 需要造一条低重要度事件才会被淘汰
        self.provider.get(epi.id).importance = 0.1

        self.clock.advance_days(90)
        result = self.rt.sweep(self.s)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(self.provider.get(epi.id))
        self.assertIsNotNone(self.provider.get(prof.id))     # Profile 不动
        self.assertEqual(result["kept"], 1)

    def test_ttl_actually_bites_for_default_episodic(self):
        """矩阵阈值必须高于抽取默认重要度，否则「有规则但不生效」＝没有代谢。

        抽取默认 Episodic importance=0.4 < 矩阵门槛 0.50 → 超 60 天必删。
        """
        epi = self.rt.write(turn(user="上次重启后连接池报错了"), self.s)[0]
        self.assertLess(epi.importance, 0.50)
        self.clock.advance_days(120)
        result = self.rt.sweep(self.s)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(self.provider.get(epi.id))

    def test_high_importance_episodic_survives_ttl(self):
        """对照组：重要度够高的经验不该被时间清理掉。"""
        epi = self.rt.write(turn(user="上次重启后连接池报错了"), self.s)[0]
        self.provider.get(epi.id).importance = 0.8
        self.clock.advance_days(120)
        result = self.rt.sweep(self.s)
        self.assertEqual(result["deleted"], 0)
        self.assertIsNotNone(self.provider.get(epi.id))

    def test_sweep_details_carry_rule_reason(self):
        """INV-13：每次淘汰都能说清命中哪条规则的哪句话。"""
        epi = self.rt.write(turn(user="上次重启后连接池报错了"), self.s)[0]
        self.provider.get(epi.id).importance = 0.05
        self.clock.advance_days(61)
        result = self.rt.sweep(self.s)
        self.assertEqual(len(result["details"]), 1)
        detail = result["details"][0]
        self.assertEqual(detail["id"], epi.id)
        self.assertIn(detail["rule"], {r.reason for r in RETENTION_MATRIX})
        self.assertEqual(detail["action"], "delete")
        self.assertIn(detail["rule"], {r.reason for r in RETENTION_MATRIX})

    def test_archive_is_tombstone_not_physical_delete(self):
        rec = semantic_record(self.rt, "旧版本迁移脚本密钥轮换了", self.s,
                              importance=0.1, created_at=self.clock.t - 200 * MS_PER_DAY)
        self.clock.advance_days(1)
        result = self.rt.sweep(self.s)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(result["deleted"], 0)
        still_there = self.provider.get(rec.id)
        self.assertIsNotNone(still_there)                      # 冷存留痕
        self.assertEqual(still_there.superseded_by, ARCHIVED_TOMBSTONE)
        self.assertIn(result["details"][0]["rule"], {r.reason for r in RETENTION_MATRIX})

    def test_sweep_global_scope_covers_all_subjects(self):
        a = subj(user="u1")
        b = subj(tenant="acme", user="u9", team="team-x", project="proj-x")
        rec_a = self.rt.write(turn(user="上次重启后连接池报错了"), a)[0]
        rec_b = self.rt.write(turn(user="昨天部署失败了"), b)[0]
        for rec in (rec_a, rec_b):
            self.provider.get(rec.id).importance = 0.01
        self.clock.advance_days(90)
        result = self.rt.sweep()                               # subject=None → 全量
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(len(self.provider), 0)

    def test_recently_hit_record_gets_new_life(self):
        """命中即续命：last_hit_at 是衰减锚点，召回过的记录不该被当过期处理。"""
        rec = self.rt.write(turn(user="上次重启后连接池报错了"), self.s)[0]
        self.provider.get(rec.id).importance = 0.05
        self.clock.advance_days(50)
        self.rt.recall("重启后连接池报错", self.s)              # 续命
        self.clock.advance_days(50)                            # 距创建 100 天，距召回 50 天
        result = self.rt.sweep(self.s)
        self.assertEqual(result["deleted"], 0)
        self.assertIsNotNone(self.provider.get(rec.id))


# ══════════════════════════════════════════════════════════════
# ⑥ Metrics
# ══════════════════════════════════════════════════════════════
class TestMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.rt = MemoryRuntime(self.provider, RuleBasedExtractor(), now=self.clock)
        self.s = subj()

    def test_precision_full_and_partial(self):
        self.rt.write(turn(user="连接池上限是 20"), self.s)
        self.rt.write(turn(user="部署环境是 staging"), self.s)

        full = self.rt.metrics([("连接池上限", "连接池上限"),
                                ("部署环境", "部署环境是 staging")], k=3, subject=self.s)
        self.assertEqual(full.recall_precision, 1.0)

        partial = self.rt.metrics([("连接池上限", "连接池上限"),
                                   ("完全无关的问题", "根本不存在的片段")], k=3, subject=self.s)
        self.assertGreater(partial.recall_precision, 0.0)
        self.assertLess(partial.recall_precision, 1.0)

    def test_metrics_do_not_touch_hits(self):
        rec = self.rt.write(turn(user="连接池上限是 20"), self.s)[0]
        self.rt.metrics([("连接池上限", "连接池上限")], k=3, subject=self.s)
        self.assertEqual(self.provider.get(rec.id).hits, 0)     # 评估不污染记忆状态

    def test_token_overhead_uses_prompt_budget(self):
        self.rt.write(turn(user="连接池上限是 20"), self.s)
        m = self.rt.metrics([("连接池上限", "连接池上限")], k=3, subject=self.s)
        expected = len("连接池上限是 20") / 4000.0
        self.assertAlmostEqual(m.token_overhead, expected, places=6)

    def test_recency_correctness_penalises_archived_recall(self):
        """归档记录仍可召回（冷存留痕），但会被记成「误用过期记忆」→ 不等于 1.0。"""
        semantic_record(self.rt, "部署环境是 prod", self.s, importance=0.9,
                        created_at=self.clock.t - 300 * MS_PER_DAY,
                        superseded_by=ARCHIVED_TOMBSTONE)
        self.rt.write(turn(user="部署环境是 staging"), self.s)
        self.rt.write(turn(user="超时时间是 30 秒"), self.s)

        m = self.rt.metrics([("部署环境", "部署环境是 staging"),
                             ("超时时间是多久", "超时时间是 30 秒")], k=2, subject=self.s)
        self.assertEqual(m.recall_precision, 1.0)
        self.assertLess(m.recency_correctness, 1.0)
        self.assertGreater(m.recency_correctness, 0.0)

    def test_conflict_resolution_prefers_latest_version(self):
        self.rt.write(turn(user="连接池上限是 20"), self.s)
        self.rt.write(turn(user="连接池上限是 30", turn_id="turn_2"), self.s)
        m = self.rt.metrics([("连接池上限是多少", "连接池上限是 30")], k=3, subject=self.s)
        self.assertEqual(m.conflict_resolution, 1.0)
        self.assertEqual(m.recall_precision, 1.0)

    def test_profile_consistency_counts_profile_in_topk(self):
        self.rt.write(turn(user="我永远不要用 Windows 路径风格"), self.s)
        self.rt.write(turn(user="连接池上限是 20"), self.s)
        m = self.rt.metrics([("连接池上限", "连接池上限")], k=3, subject=self.s)
        self.assertEqual(m.profile_consistency, 1.0)

    def test_metrics_empty_probes_is_zero(self):
        m = self.rt.metrics([], k=3, subject=self.s)
        self.assertEqual(m.recall_precision, 0.0)
        self.assertEqual(m.to_dict()["recall_precision"], 0.0)


# ══════════════════════════════════════════════════════════════
# ⑦ Extractor SPI：Runtime 判形状，Agent 判内容
#     —— 本次边界重构的验收点：抽走领域正则后，能力必须能被「注入」
# ══════════════════════════════════════════════════════════════
class FakeExtractor:
    """返回固定记录的假 Extractor。用来证明 Runtime 完全听它的。"""

    def __init__(self, records: list[MemoryRecord] | None = None) -> None:
        self.records: list[MemoryRecord] = list(records or [])
        self.calls: list[tuple[dict, Subject]] = []

    def extract(self, turn: dict, subject: Subject) -> list[MemoryRecord]:
        self.calls.append((turn, subject))
        return list(self.records)


def controlled_record(s: Subject, text: str, *, kind: MemoryKind = MemoryKind.SEMANTIC,
                      fact_key_value: str = "", importance: float = 0.6,
                      confidence: float = 0.6, ttl_days: float | None = 180.0,
                      source: str = "fake:1") -> MemoryRecord:
    """Extractor 视角的记录：id / created_at 留空，由 Runtime 盖章。"""
    return MemoryRecord(
        id="", kind=kind, text=text, subject=s,
        importance=importance, confidence=confidence,
        created_at=0.0, last_hit_at=0.0, hits=0, version=1,
        superseded_by=None, ttl_days=ttl_days, source=source,
        fact_key=fact_key_value,
    )


class TestExtractorSPI(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.provider = InMemoryProvider()
        self.s = subj()

    def test_passthrough_extractor_does_not_classify(self):
        """兜底抽取器不做语义分类：一句里「我偏好」+「上次报错」不拆成两类。"""
        rt = MemoryRuntime(self.provider, PassthroughExtractor(), now=self.clock)
        created = rt.write(turn(user="我偏好只读排查，上次重启后连接池报错了"), self.s)

        self.assertEqual(len(created), 1)                       # 整轮一条，不切子句
        self.assertIs(created[0].kind, MemoryKind.EPISODIC)     # 不判 Profile
        self.assertAlmostEqual(created[0].importance, 0.5, places=4)   # 中位数，不打分
        self.assertIn("我偏好", created[0].text)
        self.assertIn("上次重启后", created[0].text)
        self.assertEqual([r.kind for r in self.provider.all()], [MemoryKind.EPISODIC])

        # 无参构造 == 同一条兜底路径：Runtime 没有偷偷启用自己的中文正则
        default_rt = MemoryRuntime(InMemoryProvider(), now=self.clock)
        default_created = default_rt.write(
            turn(user="我偏好只读排查，上次重启后连接池报错了"), self.s)
        self.assertEqual(len(default_created), 1)
        self.assertIs(default_created[0].kind, MemoryKind.EPISODIC)

    def test_write_obeys_injected_extractor(self):
        """注入自定义 Extractor 后 write() 完全听它的；Runtime 只做机械动作。"""
        fake = FakeExtractor([controlled_record(
            self.s, "纯机械：这句没有任何中文正则特征",
            kind=MemoryKind.PROFILE, importance=0.1234, confidence=0.4321,
            ttl_days=None)])
        rt = MemoryRuntime(self.provider, fake, now=self.clock)

        # 这一轮里的「好的 / 收到」在兜底与正则策略下都会被过滤 —— 但注入策略说存就存
        created = rt.write(turn(user="好的", assistant="收到"), self.s)
        self.assertEqual(len(created), 1)
        rec = created[0]
        self.assertEqual(rec.text, "纯机械：这句没有任何中文正则特征")
        self.assertIs(rec.kind, MemoryKind.PROFILE)
        self.assertAlmostEqual(rec.importance, 0.1234, places=4)   # 不按 kind 重打分
        self.assertAlmostEqual(rec.confidence, 0.4321, places=4)
        self.assertIsNone(rec.ttl_days)
        self.assertEqual(rec.source, "fake:1")
        self.assertEqual(rec.created_at, self.clock.t)             # 时间由 Runtime 盖章
        self.assertTrue(rec.id)
        self.assertEqual(len(self.provider), 1)
        self.assertEqual(len(fake.calls), 1)                       # 恰好委托一次
        self.assertEqual(fake.calls[0][1], self.s)                 # 主体原样下传

        # 反向：Extractor 什么都不给 → Runtime 不补任何记录（不做语义猜测）
        fake.records = []
        self.assertEqual(rt.write(turn(user="我必须一律用 4 空格缩进"), self.s), [])
        self.assertEqual(len(self.provider), 1)

    def test_fact_key_comes_from_extractor(self):
        """两条 text 不同但 fact_key 相同 → 同一事实，新记录覆写旧记录（v2）。"""
        fake = FakeExtractor()
        rt = MemoryRuntime(self.provider, fake, now=self.clock)

        fake.records = [controlled_record(self.s, "连接池上限 20",
                                          fact_key_value="pool.limit")]
        first = rt.write(turn(user="ignored", turn_id="turn_1"), self.s)[0]

        fake.records = [controlled_record(self.s, "pool max = 30",
                                          fact_key_value="pool.limit")]
        second = rt.write(turn(user="ignored", turn_id="turn_2"), self.s)[0]

        self.assertNotEqual(first.text, second.text)
        self.assertEqual(second.version, 2)
        self.assertEqual(self.provider.get(first.id).superseded_by, second.id)
        # 文本骨架完全不同（中文 / 英文）—— 原来的中文 fact_key() 根本抓不到，
        # 是 Extractor 的口径把它们判成同一事实的两个版本。
        self.assertNotEqual(fact_key(first.text), fact_key(second.text))
        self.assertEqual([r.id for r in self.provider.all() if r.is_alive()], [second.id])
        # 召回只给新版本
        self.assertEqual([c.doc_id for c in rt.recall("pool limit", self.s)], [second.id])

    def test_empty_fact_key_falls_back_to_kind_text_hash(self):
        """fact_key 为空 → 退化为 (kind, 归一化文本) 哈希：只有完全相同文本才同键。"""
        fake = FakeExtractor([
            controlled_record(self.s, "连接池上限 20"),
            controlled_record(self.s, "连接池上限 30"),
        ])
        rt = MemoryRuntime(self.provider, fake, now=self.clock)
        created = rt.write(turn(user="x"), self.s)

        self.assertEqual(len(created), 2)
        self.assertEqual({r.version for r in created}, {1})        # 不同文本 → 不合并
        self.assertTrue(all(r.superseded_by is None for r in self.provider.all()))

        # 完全相同的文本重写 → 同一键 → 幂等去重（不会伪造成 v2 覆写）
        self.assertEqual(rt.write(turn(user="x", turn_id="turn_2"), self.s), [])
        self.assertEqual(len(self.provider), 2)
        self.assertEqual({r.version for r in self.provider.all()}, {1})
        self.assertTrue(all(r.superseded_by is None for r in self.provider.all()))

        # 对照组：同一句话只是多了标点 → 归一化后同键 → 判为同一事实（v2）
        fake.records = [controlled_record(self.s, "连接池上限 20。")]
        bumped = rt.write(turn(user="x", turn_id="turn_3"), self.s)
        self.assertEqual(len(bumped), 1)
        self.assertEqual(bumped[0].version, 2)


# ══════════════════════════════════════════════════════════════
# ⑧ Provider SPI 可替换
# ══════════════════════════════════════════════════════════════
class FakeProvider:
    """只实现 SPI 四个方法，外加故意不提供 all() —— 验证 Runtime 的兜底。"""

    def __init__(self) -> None:
        self.store: dict[str, MemoryRecord] = {}
        self.puts = 0

    def put(self, rec: MemoryRecord) -> None:
        self.puts += 1
        self.store[rec.id] = rec

    def get(self, rec_id: str) -> MemoryRecord | None:
        return self.store.get(rec_id)

    def scan(self, subject: Subject) -> list[MemoryRecord]:
        return [r for r in self.store.values() if r.subject == subject]

    def delete(self, rec_id: str) -> bool:
        return self.store.pop(rec_id, None) is not None


class TestProviderSPI(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.s = subj()

    def test_runtime_works_on_swapped_provider(self):
        prov = FakeProvider()
        rt = MemoryRuntime(prov, RuleBasedExtractor(), now=self.clock)
        created = rt.write(turn(user="连接池上限是 20"), self.s)
        self.assertEqual(len(created), 1)
        self.assertGreater(prov.puts, 0)

        chunks = rt.recall("连接池上限", self.s)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "连接池上限是 20")

        # 没有 all() → 用主体注册表兜底做全局 sweep；Semantic 过期低分 → 归档（墓碑）
        backdated = prov.store[created[0].id]
        backdated.importance = 0.0
        backdated.created_at = self.clock.t - 200 * MS_PER_DAY
        backdated.last_hit_at = 0.0          # 上面 recall 过 → 先清掉续命时间，才能测过期
        self.clock.advance_days(1)
        result = rt.sweep()
        self.assertEqual(result["archived"], 1)
        self.assertEqual(prov.store[created[0].id].superseded_by, ARCHIVED_TOMBSTONE)

    def test_provider_protocol_is_structural(self):
        from plane.memory import Provider
        self.assertIsInstance(InMemoryProvider(), Provider)
        self.assertIsInstance(FakeProvider(), Provider)


# ══════════════════════════════════════════════════════════════
# ⑨ 全链路
# ══════════════════════════════════════════════════════════════
class TestEndToEnd(unittest.TestCase):
    def test_full_metabolism_chain(self):
        clock = FakeClock()
        provider = InMemoryProvider()
        rt = MemoryRuntime(provider, RuleBasedExtractor(), now=clock)
        s = subj()

        # ① 抽取：一个真实轮次（用户偏好 + 事实 + 事件 + 工具回包 + 噪声）
        created = rt.write(turn(
            user="好的，我更喜欢用 tabs。连接池上限是 20。上次重启后连接池报错了",
            assistant="收到，已记录",
            tools=[{"tool": "db.info", "payload": {"pool_limit": 20, "shard_count": 3}}],
            turn_id="turn_1"), s)
        # 寒暄「好的」「收到」被丢；profile + semantic + episodic + 2 条工具事实 = 5
        self.assertEqual(len(created), 5)
        kinds = {r.kind.value for r in created}
        self.assertEqual(kinds, {"profile", "semantic", "episodic"})

        # 用户口述的「连接池上限是 20」与工具回包同骨架 → 工具版本胜出（v2）
        pool = [r for r in provider.all() if "连接池上限" in r.text]
        self.assertEqual(sorted(r.version for r in pool), [1, 2])
        latest = [r for r in pool if r.is_alive()]
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0].version, 2)

        # ② 召回
        chunks = rt.recall("连接池上限", s, k=3, budget_tokens=200)
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].kind, "profile")

        # ③ 反思（再补两条同主题事件）
        for i in (2, 3):
            rt.write(turn(user=("昨天重启后连接池又报错" if i == 2
                                else "刚才重启后连接池还是报错"), turn_id=f"turn_{i}"), s)
        reflected = rt.reflect(s)
        self.assertGreaterEqual(len(reflected), 1)

        # ④ 巩固
        for _ in range(2):
            rt.recall("连接池上限", s, k=10)
        candidates = rt.consolidate(s)
        self.assertGreaterEqual(len(candidates), 1)

        # ⑤ 遗忘
        for rec in provider.all():
            if rec.kind is MemoryKind.EPISODIC:
                rec.importance = 0.01
        clock.advance_days(61)
        result = rt.sweep(s)
        self.assertEqual(result["deleted"], 3)
        self.assertGreaterEqual(result["kept"], 1)

        # ⑥ 评估
        m = rt.metrics([("连接池上限", "连接池上限 20")], k=3, subject=s)
        self.assertEqual(m.recall_precision, 1.0)
        self.assertGreater(m.profile_consistency, 0.0)


if __name__ == "__main__":
    unittest.main()
