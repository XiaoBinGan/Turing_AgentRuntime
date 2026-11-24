"""tests/test_agent_extractor.py — Agent 层（运维域）的验收。

Runtime 侧 1774 个用例判的是「形状」：有哪几类记忆、管道几步、什么时候淘汰。
本文件判的是**「内容」**那一半，而这一半只有运维域的实现者能判：

    这句话是偏好、事实，还是某次事故的经过？
    它有多重要，理由说得出来吗？
    两条记录说的是不是同一件事（＝能不能去重/版本覆写）？
    寒暄要不要入库？

关键的一条是 `test_beats_demo_extractor_on_missed_sentences`：把
`agents/demo_extractor.py`（重构前的三条中文正则，留作基线）与本文件要验的
`OpsExtractor` 放在同一句话上对拍。**基线丢掉的、新实现能抽出来的**，才算真的
补上了内容，否则只是换了个写法。

只依赖 `agents/` + `contracts.py` + `plane/memory.py` + `builder.py`，
不碰 Runtime 的任何实现文件。
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import prompts as ops_prompts  # noqa: E402
from agents import tools as ops_tools  # noqa: E402
from agents.demo_extractor import RuleBasedExtractor  # noqa: E402
from agents.extractor import MAX_CLAUSE_CHARS, OpsExtractor  # noqa: E402
from builder import DEFAULT_TEMPLATE, PromptBuilder, PromptTemplate, TokenProfile  # noqa: E402
from contracts import (  # noqa: E402
    ContextSnapshot, Extractor, MemoryKind, SideEffect, Subject,
)
from plane.memory import MemoryRuntime  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MS_PER_DAY = 86_400_000.0
T0 = 1_700_000_000_000.0


# ── 构造 helper ────────────────────────────────────────────────
def subj(tenant: str = "acme", user: str = "u1",
         team: str = "sre", project: str = "order-core") -> Subject:
    return Subject(tenant=tenant, user_id=user, team=team, project=project)


def turn(user: str = "", assistant: str = "", tools: list | None = None,
         turn_id: str = "turn_1") -> dict:
    return {"user": user, "assistant": assistant, "tools": tools or [],
            "turn_id": turn_id}


def one(text: str, subject: Subject | None = None, extractor=None):
    """单句抽取的便捷口：返回 (records, kinds, importances)。"""
    ext = extractor or OpsExtractor()
    records = ext.extract(turn(user=text), subject or subj())
    return records, [r.kind for r in records], [r.importance for r in records]


class FakeClock:
    """可控时钟：只给端到端用例用，避免依赖真实时间。"""

    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance_days(self, days: float) -> None:
        self.t += days * MS_PER_DAY


# ══════════════════════════════════════════════════════════════
# ① 过滤：空 turn / 寒暄 / 代指
# ══════════════════════════════════════════════════════════════
class TestFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self.ext = OpsExtractor()

    def test_empty_turn_returns_empty_list(self):
        """空 turn → 空列表（不是 None，不是异常）。"""
        for empty in ({}, turn(), turn(user="   ", assistant="\n\n")):
            self.assertEqual(self.ext.extract(empty, subj()), [])

    def test_greetings_and_acks_are_dropped(self):
        """寒暄/确认入库 = Memory Swamp 的第一现场。"""
        for text in ("你好", "谢谢", "好的", "收到", "嗯嗯", "ok", "thanks", "辛苦了"):
            with self.subTest(text=text):
                self.assertEqual(self.ext.extract(turn(user=text), subj()), [],
                                 f"{text!r} 不该产生记忆")

    def test_filler_does_not_swallow_real_content(self):
        """寒暄过滤不能误伤：社交词后面是真句子时必须留下正文。"""
        records, kinds, _ = one("好的，连接池上限是 20")
        self.assertEqual(len(records), 1)
        self.assertEqual(kinds[0], MemoryKind.SEMANTIC)
        self.assertIn("连接池上限", records[0].text)

    def test_unresolvable_anaphora_is_dropped(self):
        """「它又挂了」「上面说的」没有上文可回指，长期记忆里就是纯垃圾。"""
        for text in ("它又挂了", "上面说的那个配置", "那个呢"):
            with self.subTest(text=text):
                self.assertEqual(self.ext.extract(turn(user=text), subj()), [])

    def test_non_dict_turn_raises_type_error(self):
        """契约要求 turn 是 dict；传错类型要立刻炸，而不是猜。"""
        with self.assertRaises(TypeError):
            self.ext.extract(["user", "hi"], subj())  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════
# ② 分类：偏好 / 事实 / 事件
# ══════════════════════════════════════════════════════════════
class TestClassification(unittest.TestCase):
    def test_personal_habit_is_profile(self):
        """「我习惯…」是长期工作方式 → PROFILE（永久、免疫衰减）。"""
        records, kinds, _ = one("我习惯先看指标再看日志")
        self.assertEqual(kinds, [MemoryKind.PROFILE])
        self.assertIsNone(records[0].ttl_days, "Profile 必须永久（ttl_days=None）")

    def test_prohibition_is_profile(self):
        """违反性偏好（「不要…」）比普通偏好更重要：它是最刚性的一类。"""
        records, kinds, imps = one("不要动生产库")
        self.assertEqual(kinds, [MemoryKind.PROFILE])
        self.assertGreaterEqual(imps[0], 0.85, "硬性约束的重要度必须高")

    def test_config_fact_is_semantic_with_high_importance(self):
        """配置类事实（含数值）→ SEMANTIC，且重要度明显高于普通事件。"""
        records, kinds, imps = one("连接池上限是 20")
        self.assertEqual(kinds, [MemoryKind.SEMANTIC])
        self.assertGreater(imps[0], 0.5)
        self.assertIsNotNone(records[0].ttl_days, "Semantic 要能被 TTL 代谢")

    def test_event_is_not_profile(self):
        """「上周…」是一个时间锚点，锚定的是某一次经过，不是规范。"""
        _, kinds, _ = one("上周灰度发布没跑回归")
        self.assertEqual(kinds, [MemoryKind.EPISODIC])
        self.assertNotIn(MemoryKind.PROFILE, kinds)

    def test_process_norm_is_profile_not_event(self):
        """「灰度发布先过回归」没有「必须/禁止」，但它讲的是流程顺序 = 规范。"""
        _, kinds, _ = one("灰度发布先过回归")
        self.assertEqual(kinds, [MemoryKind.PROFILE])

    def test_unclassifiable_sentence_is_dropped(self):
        """归不了类的闲聊不入库（宁缺毋滥）。"""
        self.assertEqual(OpsExtractor().extract(turn(user="今天天气不错"), subj()), [])


# ══════════════════════════════════════════════════════════════
# ③ 与 demo 基线对拍（本文件最关键的用例）
# ══════════════════════════════════════════════════════════════
class TestBeatsDemoBaseline(unittest.TestCase):
    def test_beats_demo_extractor_on_missed_sentences(self):
        """基线（三条中文正则）漏掉的句子，OpsExtractor 必须抽得出来。

        基线漏的原因不是正则写得差，而是**把语义压进了一个模式串**：
          * 「灰度发布先过回归」没有「我习惯/必须/是/上限」这类外壳；
          * 「日志盘占用 92%」是纯量纲，判断词一个都没有；
          * 「p99 3120ms」同上。
        这三句在换业务前就该被记住——漏掉它们等于 Agent 下次还会踩同一个坑。
        """
        demo = RuleBasedExtractor()
        ops = OpsExtractor()
        missed = ("灰度发布先过回归", "日志盘占用 92%", "p99 3120ms")
        for text in missed:
            with self.subTest(text=text):
                self.assertEqual(demo.extract(turn(user=text), subj()), [],
                                 f"基线应当漏掉 {text!r}（前提假设变了，用例需要重写）")
                records = ops.extract(turn(user=text), subj())
                self.assertTrue(records, f"OpsExtractor 漏掉了 {text!r}")
                self.assertIn(records[0].kind,
                              (MemoryKind.PROFILE, MemoryKind.SEMANTIC, MemoryKind.EPISODIC))

    def test_keeps_what_demo_keeps(self):
        """反过来也要成立：基线能抽的，新实现不能丢（不能靠「放宽」换覆盖率）。"""
        demo = RuleBasedExtractor()
        ops = OpsExtractor()
        kept = ("我习惯先看指标再看日志", "连接池上限是 20", "上次连接池耗尽靠重启恢复")
        for text in kept:
            with self.subTest(text=text):
                self.assertTrue(demo.extract(turn(user=text), subj()))
                self.assertTrue(ops.extract(turn(user=text), subj()),
                                f"OpsExtractor 丢了基线能抽的 {text!r}")


# ══════════════════════════════════════════════════════════════
# ④ 重要度：取值范围 + 可区分 + 理由说得出来
# ══════════════════════════════════════════════════════════════
class TestImportance(unittest.TestCase):
    def test_importance_in_range_and_ordered(self):
        """重要度必须落在 [0,1]，且能区分：硬约束 > 配置事实 > 一次事件。"""
        _, _, imp_profile = one("不要动生产库")
        _, _, imp_fact = one("连接池上限是 20")
        _, _, imp_event = one("上周灰度发布没跑回归")
        for value in (imp_profile[0], imp_fact[0], imp_event[0]):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertGreater(imp_profile[0], imp_fact[0])
        self.assertGreater(imp_fact[0], imp_event[0])

    def test_explain_states_the_reason(self):
        """打分必须有理由：线上调规则时要能回答「为什么是 0.70」。"""
        info = OpsExtractor().explain("日志盘占用 92%")
        self.assertEqual(info["kind"], "semantic")
        self.assertTrue(info["reasons"], "importance 的理由列表不能为空")
        self.assertTrue(any("量纲" in r for r in info["reasons"]))
        self.assertEqual(info["fact_key"], "semantic:日志盘占用#")
        dropped = OpsExtractor().explain("你好")
        self.assertIsNone(dropped["kind"])
        self.assertTrue(dropped["dropped"])

    def test_assistant_restatement_is_less_confident(self):
        """助手自述是二手转述：置信度打折，但类别不变。"""
        user_rec = OpsExtractor().extract(turn(user="连接池上限是 40"), subj())[0]
        asst_rec = OpsExtractor().extract(turn(assistant="连接池上限是 40"), subj())[0]
        self.assertEqual(user_rec.kind, asst_rec.kind)
        self.assertLess(asst_rec.confidence, user_rec.confidence)
        self.assertTrue(asst_rec.source.startswith("assistant:"))


# ══════════════════════════════════════════════════════════════
# ⑤ fact_key：什么算「同一事实」
# ══════════════════════════════════════════════════════════════
class TestFactKey(unittest.TestCase):
    def test_same_fact_twice_yields_same_key(self):
        """换一种说法讲同一个配置 → 同一个键（否则去重与版本覆写全失效）。"""
        first = OpsExtractor().extract(turn(user="连接池上限是 20"), subj())[0]
        second = OpsExtractor().extract(turn(user="连接池上限 20 个"), subj())[0]
        self.assertTrue(first.fact_key)
        self.assertEqual(first.fact_key, second.fact_key)

    def test_different_facts_yield_different_keys(self):
        """不同事实不能撞键：撞了就会被 Runtime 当成同一件事覆写掉。"""
        pool = OpsExtractor().extract(turn(user="连接池上限 20"), subj())[0]
        disk = OpsExtractor().extract(turn(user="日志盘占用 92%"), subj())[0]
        self.assertTrue(pool.fact_key and disk.fact_key)
        self.assertNotEqual(pool.fact_key, disk.fact_key)

    def test_episodic_has_no_fact_key(self):
        """一次性事件没有「同一事实」：给它键就等于允许「新事件覆写旧事件」。"""
        event = OpsExtractor().extract(turn(user="上周灰度发布没跑回归"), subj())[0]
        self.assertEqual(event.kind, MemoryKind.EPISODIC)
        self.assertEqual(event.fact_key, "")

    def test_in_turn_duplicate_is_merged(self):
        """同一轮里说两遍同一件事，只产出一条记忆。"""
        records = OpsExtractor().extract(
            turn(user="连接池上限是 20；连接池上限 20 个"), subj())
        self.assertEqual(len(records), 1)

    def test_fact_key_powers_version_overwrite(self):
        """端到端：20 → 30 是**同一条事实的新版本**，不是两条互相矛盾的记忆。"""
        rt = MemoryRuntime(extractor=OpsExtractor(), now=FakeClock())
        s = subj()
        rt.write(turn(user="连接池上限是 20", turn_id="t1"), s)
        rt.write(turn(user="连接池上限是 30", turn_id="t2"), s)

        alive = [r for r in rt.provider.scan(s) if r.is_alive()]
        self.assertEqual(len(alive), 1, "旧版本应当被覆写，而不是并存")
        self.assertEqual(alive[0].version, 2)
        self.assertIn("30", alive[0].text)
        chunks = rt.recall("连接池上限多少", s)
        self.assertTrue(all("20" not in c.text for c in chunks),
                        "召回不能取到已被覆写的旧值")


# ══════════════════════════════════════════════════════════════
# ⑥ 形状与边界：主体透传 / 长文本 / 纯工具轮 / 英文 / 中英混排
# ══════════════════════════════════════════════════════════════
class TestShapeAndEdges(unittest.TestCase):
    def test_subject_is_passed_through(self):
        """subject 必须原样透传：归属错了就是跨租户泄漏。"""
        s = subj(tenant="globex", user="u9", team="db", project="billing")
        records = OpsExtractor().extract(turn(user="连接池上限是 20"), s)
        self.assertTrue(records)
        for rec in records:
            self.assertEqual(rec.subject, s)

    def test_stamping_is_left_to_runtime(self):
        """id / created_at 属于 Runtime 的机械盖章，Extractor 不许自己编。"""
        rec = OpsExtractor().extract(turn(user="连接池上限是 20"), subj())[0]
        self.assertEqual(rec.id, "")
        self.assertEqual(rec.created_at, 0.0)
        self.assertEqual(rec.hits, 0)

    def test_long_text_is_not_garbled(self):
        """长文本按标点切，不产生残句、不超过 Runtime 的 240 字截断线。"""
        long_text = (
            "昨天凌晨两点的容量评估会开了三个小时，"
            "结论是订单核心链路的连接池上限要从 20 提到 40，"
            "同时把日志盘的保留期从 7 天压到 3 天，"
            "另外 912 条告警规则要按服务重新归并一遍，"
            "最后确认 p99 超过 800ms 就算劣化"
        )
        records = OpsExtractor().extract(turn(user=long_text), subj())
        self.assertGreaterEqual(len(records), 3, "长文本应当切成多条原子陈述")
        for rec in records:
            self.assertLessEqual(len(rec.text), 240, "不能超过 Runtime 的截断线")
            self.assertLessEqual(len(rec.text), MAX_CLAUSE_CHARS)
            self.assertIn(rec.text, long_text, "切分只能原样截取，不许改写/拼接")
        joined = " / ".join(r.text for r in records)
        self.assertIn("容量评估会", joined, "句首信息不能丢")
        self.assertIn("劣化", joined, "句尾信息不能丢")

    def test_tools_only_turn_is_handled(self):
        """纯工具调用轮（无 user 文本）：抽结构化读数，整包不入库。"""
        records = OpsExtractor().extract(turn(tools=[{
            "tool": "query_metrics",
            "payload": {"p99_ms": 3120, "error_rate": 0.42},
        }]), subj())
        self.assertGreaterEqual(len(records), 2)
        for rec in records:
            self.assertEqual(rec.kind, MemoryKind.SEMANTIC)
            self.assertTrue(rec.source.startswith("tool:query_metrics:"))
        texts = " ".join(r.text for r in records)
        self.assertIn("3120", texts)
        self.assertIn("错误率", texts)
        # 空 payload 的纯工具轮 → 空
        self.assertEqual(OpsExtractor().extract(turn(tools=[{"tool": "noop"}]), subj()), [])

    def test_tool_secrets_never_enter_memory(self):
        """工具回包里的凭据绝不进记忆（Gateway 之外的第二道闸）。"""
        records = OpsExtractor().extract(turn(tools=[{
            "tool": "describe_db",
            "payload": {"password": "hunter2", "api_token": "t-123", "p99_ms": 100},
        }]), subj())
        texts = " ".join(r.text for r in records)
        self.assertNotIn("hunter2", texts)
        self.assertNotIn("t-123", texts)
        self.assertIn("100", texts)

    def test_english_sentence_is_handled(self):
        """英文句不能崩，也不能全靠中文词典（客户可能是英文的）。"""
        facts = OpsExtractor().extract(turn(user="pool limit is 20"), subj())
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].kind, MemoryKind.SEMANTIC)
        self.assertGreaterEqual(facts[0].importance, 0.6)

        prefs = OpsExtractor().extract(turn(user="We never touch prod directly"), subj())
        self.assertEqual(len(prefs), 1)
        self.assertEqual(prefs[0].kind, MemoryKind.PROFILE)

    def test_mixed_chinese_english_sentence(self):
        """中英混排：一句里既有规范又有量纲，两条都要出来。"""
        records = OpsExtractor().extract(
            turn(user="灰度发布先过回归，p99 3120ms 才算达标"), subj())
        kinds = {r.kind for r in records}
        self.assertIn(MemoryKind.PROFILE, kinds)
        self.assertIn(MemoryKind.SEMANTIC, kinds)

    def test_numeric_fact_p99_is_recognised(self):
        """数字型事实（「p99 3120ms」）必须被识别成事实而非闲聊。"""
        records, kinds, imps = one("p99 3120ms")
        self.assertEqual(kinds, [MemoryKind.SEMANTIC])
        self.assertIn("3120", records[0].text)
        self.assertGreaterEqual(imps[0], 0.6)

    def test_protocol_conformance(self):
        """结构上照契约实现（`Extractor` 是纯 Protocol，不参与 isinstance）。"""
        members = [n for n in dir(Extractor) if not n.startswith("_")]
        self.assertIn("extract", members)
        for name in members:
            self.assertTrue(callable(getattr(OpsExtractor, name, None)),
                            f"OpsExtractor 缺少契约成员 {name}")
        params = list(inspect.signature(OpsExtractor.extract).parameters)
        self.assertEqual(params, ["self", "turn", "subject"])


# ══════════════════════════════════════════════════════════════
# ⑦ 端到端：真的能从 MemoryRuntime 召回
# ══════════════════════════════════════════════════════════════
class TestEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = MemoryRuntime(extractor=OpsExtractor(), now=FakeClock())
        self.s = subj()

    def test_write_then_recall(self):
        """端到端：写进去的运维事实能被下一轮召回。"""
        self.rt.write(turn(user="连接池上限是 40"), self.s)
        chunks = self.rt.recall("连接池上限多少", self.s)
        self.assertTrue(chunks)
        self.assertTrue(any("连接池上限" in c.text for c in chunks))
        self.assertEqual(chunks[0].source, "memory")
        self.assertEqual(chunks[0].kind, MemoryKind.SEMANTIC.value)

    def test_profile_memory_is_ranked_first(self):
        """Profile 是硬约束：哪怕查询与它零相似度，也必须排在最前。"""
        self.rt.write(turn(user="不要动生产库"), self.s)
        self.rt.write(turn(user="连接池上限是 40"), self.s)
        chunks = self.rt.recall("连接池上限多少", self.s)
        self.assertEqual(chunks[0].kind, MemoryKind.PROFILE.value)

    def test_recall_scores_are_within_unit_interval(self):
        """召回分数落在 [0,1]：非 Profile 记录的上界是 0.5+0.3+0.2=1.0。

        （Profile 例外：Runtime 的 `PROFILE_BONUS=0.30` 会把分数顶到 1.3 上限，
        这是 Runtime 的排序设计，不是 Extractor 能改的——下面单独断言。）
        """
        self.rt.write(turn(user="连接池上限是 40"), self.s)
        self.rt.write(turn(user="上周日志盘打满过一次"), self.s)
        chunks = self.rt.recall("日志盘", self.s)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertGreaterEqual(chunk.score, 0.0)
            self.assertLessEqual(chunk.score, 1.0, f"{chunk.text} 分数越界")

    def test_profile_score_bound_includes_runtime_bonus(self):
        """Profile 的分数上界是 1.0 + PROFILE_BONUS，仍然有界。"""
        self.rt.write(turn(user="不要动生产库"), self.s)
        chunks = self.rt.recall("改配置", self.s)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(chunk.score, 1.0 + 0.30 + 1e-9)

    def test_cross_tenant_isolation(self):
        """跨租户不泄漏：同 team/project 但不同 tenant，也一条都看不见。"""
        self.rt.write(turn(user="连接池上限是 40"), self.s)
        other = subj(tenant="globex")
        self.assertEqual(self.rt.recall("连接池上限", other), [])
        self.assertEqual(self.rt.recall("连接池上限", subj(user="u2", team="", project="")), [])

    def test_sweep_keeps_profile_and_ages_events(self):
        """Profile 免疫时间；事件按保留矩阵到期后走人——证明 ttl_days 给对了。"""
        clock = FakeClock()
        rt = MemoryRuntime(extractor=OpsExtractor(), now=clock)
        rt.write(turn(user="不要动生产库", turn_id="t1"), self.s)
        rt.write(turn(user="上周灰度发布没跑回归", turn_id="t2"), self.s)
        clock.advance_days(400.0)
        result = rt.sweep(self.s)
        self.assertGreaterEqual(result["deleted"] + result["archived"], 1)
        alive = [r.text for r in rt.provider.scan(self.s) if r.is_alive()]
        self.assertIn("不要动生产库", alive, "Profile 不该被时间淘汰")


# ══════════════════════════════════════════════════════════════
# ⑧ 措辞：这些业务词**就该在这里**
# ══════════════════════════════════════════════════════════════
class TestOpsPrompts(unittest.TestCase):
    #: ADR-8 点名过的业务词汇：它们属于 Agent 层，不属于 Runtime。
    #: 刻意不含「服务」——`builder.py` 里的「服务端」是传输层术语，不是业务词，
    #: 拿它当渗透证据只会得到假阳性。
    OPS_VOCAB = ("运维", "告警", "灰度", "回滚", "排障", "连接池", "指标", "日志", "发布")

    def snap(self, **over) -> ContextSnapshot:
        data = {
            "principal": {"tenant": "acme", "roles": ["sre"], "user_id": "u1"},
            "phase": "EXECUTING",
            "loop_count": 3,
            "memory_hits": [
                {"kind": "profile", "text": "不要动生产库", "score": 0.99},
                {"kind": "semantic", "text": "连接池上限 40", "score": 0.72},
            ],
            "history": [{"role": "user", "text": "订单接口变慢"}],
            "tool_results": [{"tool": "query_metrics",
                              "payload": {"p99_ms": 3120}, "trusted": False}],
        }
        data.update(over)
        return ContextSnapshot(version=5, data=data)

    def test_persona_and_constraints_are_non_empty(self):
        """人格与约束段必须非空，且含运维域词汇（反转：Runtime 没有这些词）。"""
        persona = ops_prompts.system_prompt()
        constraints = ops_prompts.constraints_prompt()
        self.assertIsInstance(persona, str)
        self.assertIsInstance(constraints, str)
        self.assertTrue(persona.strip())
        self.assertTrue(constraints.strip())
        for word in ("指标", "日志"):
            self.assertIn(word, persona, f"人格缺少运维工作方式关键词 {word!r}")
        for word in ("灰度", "回归", "生产"):
            self.assertIn(word, constraints, f"约束段缺少 {word!r}")

    def test_profile_is_a_token_profile(self):
        """`build_profile()` 给的就是 PromptBuilder 要的 `TokenProfile`。"""
        profile = ops_prompts.build_profile()
        self.assertIsInstance(profile, TokenProfile)
        self.assertTrue(profile.name)
        self.assertGreater(profile.cjk_per_token, 0.0)
        self.assertGreater(profile.tool_overhead_fixed, 0.0)
        builder = PromptBuilder(template=ops_prompts.build_template(), profile=profile)
        self.assertIs(builder.profile, profile)

    def test_template_carries_the_wording(self):
        """模板带着运维措辞，`system` 就是人格本体。"""
        template = ops_prompts.build_template()
        self.assertIsInstance(template, PromptTemplate)
        self.assertEqual(template.system, ops_prompts.system_prompt())
        self.assertEqual(template.constraint_prefix, "【硬性约束】")
        for field in ("tools_header", "memory_header", "knowledge_header",
                      "history_header", "tool_results_header", "safety_note"):
            self.assertTrue(getattr(template, field), f"模板字段 {field} 不能为空")

    def test_assembly_injects_persona_into_system_section(self):
        """装配后（不改 builder）system 段就是运维人格 + 约束前缀 + 主体行。"""
        builder = PromptBuilder(template=ops_prompts.build_template(),
                                profile=ops_prompts.build_profile())
        prompt = builder.build(self.snap())
        self.assertIn("<<<SYSTEM>>>", prompt)
        self.assertIn(ops_prompts.system_prompt(), prompt)
        self.assertIn("【硬性约束】不要动生产库", prompt, "Profile 记忆要按约束前缀注入")
        self.assertIn("租户=acme", prompt)
        self.assertIn("<external_data_untrusted>", prompt)
        self.assertIn("<<<MEMORY>>>", prompt)

    def test_runtime_template_stays_neutral(self):
        """反方向断言：Runtime 的默认模板里没有这些词，也没有人格。

        这正是「搬到 agents/」的意义——`builder.py` 保持中性，
        同一份 Builder 换模板就能服务另一个领域。
        """
        self.assertEqual(DEFAULT_TEMPLATE.system, "")
        self.assertEqual(DEFAULT_TEMPLATE.constraint_prefix, "")
        builder_src = (ROOT / "builder.py").read_text(encoding="utf-8")
        for word in self.OPS_VOCAB:
            self.assertNotIn(word, builder_src,
                             f"builder.py 里出现了业务词 {word!r}（又渗透了）")

    def test_describe_is_self_explanatory(self):
        info = ops_prompts.describe()
        self.assertEqual(info["domain"], "ops")
        self.assertGreater(info["persona_chars"], 0)
        self.assertIn("system", info["sections_with_wording"])


# ══════════════════════════════════════════════════════════════
# ⑨ 收敛指引：模型什么时候该停（B 缺陷的 Prompt 侧）
# ══════════════════════════════════════════════════════════════
class TestConvergenceGuidance(unittest.TestCase):
    """同一目标跑三次得 4 步 DONE / 7 步 BUDGET / 8 步 BUDGET —— 这是真实测。

    归因：模型**没有停止条件**。它把"不确定"当成"再查一次"，于是一直查到被步数
    上限掐断。修法有两半，缺一半都不成立：

      * 工具侧：一次调用能拿多少（批量参数，见 `test_agent_tools.BatchQueryTest`）；
      * Prompt 侧：什么时候够了（本文件这一组）。

    这一组只判「措辞是否在场、是否只说业务判断」。指引的**效果**只能接真模型量，
    这里不假装能测出来。
    """

    #: Runtime 的实现词汇：出现在 Prompt 里就是"两处规则打架"的起点
    RUNTIME_WORDS = ("max_steps", "token", "预算", "截断", "Prompt", "Context",
                     "TOOL_RESULTS", "builder", "schema", "缓存")

    def test_ops_template_carries_convergence_guidance(self):
        """⑮ 模板里有收敛指引，且装配后真的进了 Prompt。"""
        guidance = ops_prompts.OPS_TEMPLATE.convergence_note
        self.assertTrue(guidance.strip(), "模板缺少收敛指引")
        self.assertEqual(guidance, ops_prompts.convergence_prompt())
        for keyword in ("同一组参数", "不要重复调用", "不确定", "6 步", "停"):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, guidance, f"收敛指引缺少 {keyword!r}")

        built = PromptBuilder(template=ops_prompts.build_template()).build(self._snap())
        self.assertIn("【收敛要求】", built, "指引没有进 Prompt")
        self.assertIn("最多 6 步内给出结论", built)
        # 与人格同级（system 段），不是被塞进某一轮的工具结果里
        self.assertLess(built.index("【收敛要求】"),
                        built.index("<<<TOOL_RESULTS>>>"))

    def test_every_tool_description_says_when_to_stop(self):
        """⑯ 每个工具的 description 都要有「何时该停」。

        为什么不是只写进 system：模型每一步看的是**当前要调的那个工具的 schema**，
        system 只在开头读一次。停止条件必须贴着工具。

        两种写法都算合格，但必须二选一：

          * 查询类工具用统一句 `STOP_HINT`（"读数够了就停，别用同一组参数重复调"）——
            重复调用恰恰发生在查询类工具上；
          * 写类工具（T2/T3）用各自的判据（"取证没做完之前不要调它"、"这里没有何时该调"），
            统一句对它们没意义（它们的问题不是"查得太多"）。
        """
        manifests = ops_tools.MANIFESTS
        self.assertGreaterEqual(len(manifests), 8)
        read_only = [m for m in manifests if m.side_effect is SideEffect.READ_ONLY]
        self.assertGreaterEqual(len(read_only), 4)
        for manifest in manifests:
            with self.subTest(tool=manifest.name):
                desc = manifest.description
                self.assertIn("何时该停", desc, f"{manifest.name} 没写清什么时候停")
                self.assertIn("停", desc)
                if manifest.side_effect is SideEffect.READ_ONLY:
                    self.assertIn(ops_tools.STOP_HINT, desc,
                                  f"{manifest.name} 缺少统一的停止判据（查询类工具必须有）")
                else:
                    # 写类工具的判据必须提到"这道闸在哪"（审批 / 硬拒），
                    # 否则模型会以为"停不停"是自己可以决定的
                    self.assertTrue("审批" in desc or "拒绝" in desc,
                                    f"{manifest.name} 的停止判据没说明审批/拒绝语义")

    def test_convergence_guidance_has_no_markdown_headings(self):
        """⑰ 不用 markdown 标题：Prompt 是扁平的，标题会被读成"另起一段任务"。"""
        guidance = ops_prompts.convergence_prompt()
        self.assertNotIn("#", guidance)
        for line in guidance.splitlines():
            self.assertFalse(line.lstrip().startswith("#"), f"出现标题行：{line!r}")
        # 行首用【】这种与现有约束段一致的标记，而不是 markdown
        self.assertTrue(guidance.startswith("【"))

    def test_convergence_guidance_speaks_business_not_runtime(self):
        """⑱ 只讲业务判断，不写死 Runtime 的实现（谁管什么，不许越界）。"""
        guidance = ops_prompts.convergence_prompt()
        for word in self.RUNTIME_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, guidance,
                                 f"收敛指引里出现 Runtime 实现词 {word!r}：越界了")
        # 正向确认：讲的是判断与交付（结论 / 依据 / 排除），不是机制
        for word in ("结论", "证据", "判断"):
            self.assertIn(word, guidance)
        # Runtime 侧同样不含这些业务措辞（双向边界）
        self.assertEqual(DEFAULT_TEMPLATE.convergence_note, "")
        builder_src = (ROOT / "builder.py").read_text(encoding="utf-8")
        self.assertNotIn("【收敛要求】", builder_src)

    @staticmethod
    def _snap() -> ContextSnapshot:
        return ContextSnapshot(version=5, data={
            "principal": {"tenant": "acme", "roles": ["sre"], "user_id": "u1"},
            "phase": "EXECUTING",
            "loop_count": 3,
            "tool_results": [{"tool": "query_metrics",
                              "payload": {"p99_ms": 3120}, "trusted": True}],
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
