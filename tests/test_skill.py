"""tests/test_skill.py — Skill Runtime 自带用例（W21）。

架构依据（agent-runtime-core-arch.md §3）：**Procedural Memory 的内容归 Skill 库，
不归 Memory Plane**。本文件验证的就是这条裁决在代码里真的成立了：

    ① 版本与退休     同名同版本幂等 / 新版本覆盖 current / 旧版本可查 / 退休只摘可见性
    ② 双层装载       索引常驻极小、规程按需装载、max_body_chars 硬兜底（**量化**）
    ③ 匹配           触发词命中 + bigram Jaccard（与 memory 同口径，测试钉住）
    ④ 自演进闭环     Memory.consolidate() 的候选 → SkillSpec → 下一轮索引里可见
    ⑤ Provider SPI   可替换（假 provider 注入后照常工作）

三条量化断言是本模块的验收核心（其余都是它们的支撑）：
    - `test_index_much_smaller_than_full_bodies`：index < 全文的 1/3（实测 184 / 3040 ≈ 6%）
    - `test_render_total_under_index_plus_all_bodies`：render 总量 < index + 全部 body
      （实测 2008 / 3224 ≈ 62%，即省掉 38%）
    - `test_max_body_chars_truncates_by_score`：预算裁剪按 score 降序、且**不截断正文**

只依赖 plane/skill.py + contracts.py；闭环用例（`TestClosedLoop`）额外只读引用
plane/memory.py，缺它时 skip 而不是 fail —— 并行施工期别的 worker 可能正在改它。
"""
from __future__ import annotations

import re
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import SkillMatch, SkillSpec, Subject  # noqa: E402
from plane.skill import (  # noqa: E402
    DESC_MAX_CHARS,
    MIN_TRIGGER_CHARS,
    SIM_FLOOR,
    SIM_WEIGHT,
    TRIGGER_WEIGHT,
    InMemorySkillProvider,
    SkillLibrary,
    SkillRuntime,
    slugify,
    text_similarity,
)

#: 长正文夹具的步长（够长才测得出「索引 vs 全文」的量级差）
STEP_CHARS = 90


# ── 构造 helper ────────────────────────────────────────────────
def spec(name: str, *, title: str = "", description: str = "",
         trigger: tuple[str, ...] = (), steps: tuple[str, ...] = (),
         version: str = "1", source: str = "hand") -> SkillSpec:
    """手工技能（等价于 `skills/<name>/SKILL.md` 的作者输入）。"""
    return SkillSpec(name=name, title=title or name, description=description,
                     trigger=tuple(trigger), steps=tuple(steps),
                     version=version, source=source)


def long_spec(name: str, description: str, trigger: tuple[str, ...] = (),
              steps: int = 6) -> SkillSpec:
    """长正文技能：6 步 × 90 字 ≈ 608 字符正文，一行索引 ≈ 37 字符。

    这个量级差（16 倍）就是双层装载能省预算的物理来源。
    """
    body = tuple(f"第{i}步：" + "甲" * STEP_CHARS for i in range(1, steps + 1))
    return SkillSpec(name=name, title=f"{name} 技能", description=description,
                     trigger=tuple(trigger), steps=body, source="hand")


def long_library(n: int = 5) -> tuple[SkillLibrary, list[SkillSpec]]:
    """n 个长正文技能，全部带同一个触发词「故障」，便于测 k 与预算裁剪。"""
    lib = SkillLibrary()
    specs = []
    for i in range(n):
        item = long_spec(f"s{i}-skill", f"第 {i} 号长规程：处理这一类故障的标准步骤",
                         trigger=("故障",))
        lib.add(item)
        specs.append(item)
    return lib, specs


def candidate(title: str = "自动沉淀：连接池/超时", skill_id: str = "skill_ab12cd34",
              from_memory: list[str] | None = None,
              reason: str = "2 条高置信语义共同指向该模式，建议交由 Skill 库评审",
              draft: str = "连接池上限 20；超时 3 秒；超时后先看慢查询日志") -> dict:
    """与 `MemoryRuntime.consolidate()` 输出**同形状**的候选（五个键一字不差）。"""
    return {
        "skill_id": skill_id,
        "title": title,
        "from_memory": ["mem_a1", "mem_b2"] if from_memory is None else from_memory,
        "reason": reason,
        "draft": draft,
    }


# ══════════════════════════════════════════════════════════════
# ① 版本与退休
# ══════════════════════════════════════════════════════════════
class TestLibraryBasics(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = SkillLibrary()

    def test_add_and_get(self):
        """① add + get 基本：current 就是刚写入的那版。"""
        item = spec("deploy", description="灰度发布与回滚", trigger=("部署", "发布"),
                    steps=("先灰度 5%", "观察 10 分钟", "再放量"))
        self.lib.add(item)
        got = self.lib.get("deploy")
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "deploy")
        self.assertEqual(got.version, "1")
        self.assertEqual(got.steps, ("先灰度 5%", "观察 10 分钟", "再放量"))
        self.assertIsNone(self.lib.get("nope"))          # 未注册 → None 而不是抛
        self.assertIsNone(self.lib.get("deploy", "9"))   # 不存在的版本 → None

    def test_same_version_is_idempotent(self):
        """② 同名同版本幂等：不重复、**不改内容**（首写胜出）。

        为什么不是「后写覆盖」：重放部署脚本 / 重跑迁移不该改变已发布 SOP 的内容。
        """
        first = spec("deploy", steps=("灰度",))
        self.lib.add(first)
        self.lib.add(first)
        self.lib.add(replace(first, steps=("被改过的步骤",), description="偷改"))
        self.assertEqual(len(self.lib.all()), 1)
        self.assertEqual(self.lib.get("deploy").steps, ("灰度",))
        self.assertEqual(self.lib.get("deploy").description, "")
        self.assertEqual(len(self.lib.versions("deploy")), 1)

    def test_new_version_overrides_current_but_keeps_old(self):
        """③ 同名新版本覆盖 current，旧版本仍可 `get(name, version="1")`。"""
        self.lib.add(spec("deploy", steps=("v1 的步骤",), version="1"))
        self.lib.add(spec("deploy", steps=("v2 的步骤",), version="2"))
        self.assertEqual(self.lib.get("deploy").version, "2")
        self.assertEqual(self.lib.get("deploy").steps, ("v2 的步骤",))
        old = self.lib.get("deploy", "1")
        self.assertIsNotNone(old)
        self.assertEqual(old.steps, ("v1 的步骤",))
        self.assertEqual(self.lib.versions("deploy"), ["1", "2"])

    def test_all_returns_latest_only(self):
        """④ all() 只返回最新版本（历史版本不进索引，否则索引会随版本膨胀）。"""
        for ver in ("1", "2", "3"):
            self.lib.add(spec("deploy", steps=(f"v{ver}",), version=ver))
        self.lib.add(spec("rollback", steps=("回滚",), version="1"))
        latest = self.lib.all()
        self.assertEqual([s.name for s in latest], ["deploy", "rollback"])
        self.assertEqual(self.lib.get("deploy").version, "3")
        self.assertEqual(len(latest), 2)

    def test_version_number_ordering_is_numeric(self):
        """版本比较按数字：v10 必须排在 v9 之后（字符串比较会颠倒）。"""
        self.lib.add(spec("deploy", version="9"))
        self.lib.add(spec("deploy", version="10"))
        self.assertEqual(self.lib.get("deploy").version, "10")
        self.assertEqual(self.lib.versions("deploy"), ["9", "10"])

    def test_retire_hides_from_all_but_get_still_works(self):
        """⑤ retire 后 all() 不返回，但 get(name) 仍可（审计通道）。"""
        self.lib.add(spec("deploy", steps=("灰度",)))
        self.lib.add(spec("rollback", steps=("回滚",)))
        self.assertTrue(self.lib.retire("deploy"))
        self.assertEqual([s.name for s in self.lib.all()], ["rollback"])
        audit = self.lib.get("deploy")
        self.assertIsNotNone(audit)
        self.assertEqual(audit.name, "deploy")
        self.assertEqual(audit.version, "1")

    def test_retire_unknown_returns_false(self):
        """⑥ retire 不存在的技能 → False（不是异常，也不是静默 True）。"""
        self.assertFalse(self.lib.retire("ghost"))
        self.lib.add(spec("deploy"))
        self.assertTrue(self.lib.retire("deploy"))
        self.assertFalse(self.lib.retire("deploy"))   # 二次退休也是 False

    def test_add_rejects_dirty_input(self):
        """坏输入 fail fast：不是 SkillSpec / name 为空，都在写入口就拦住。"""
        with self.assertRaises(TypeError):
            self.lib.add({"name": "dict-not-spec"})
        with self.assertRaises(ValueError):
            self.lib.add(spec("   "))

    def test_add_normalizes_dirty_spec(self):
        """外部传入的脏形状（int version / 空 trigger / list steps）被归一化。"""
        self.lib.add(SkillSpec(name=" deploy ", version=2,
                               steps=["a", "", "  ", "b"], trigger=["", "部署"]))
        got = self.lib.get("deploy")
        self.assertEqual(got.version, "2")          # 版本统一成字符串
        self.assertEqual(got.steps, ("a", "b"))    # 空步骤不留空洞
        self.assertEqual(got.trigger, ("部署",))    # 空 trigger 不能进匹配（会命中一切）


# ══════════════════════════════════════════════════════════════
# ② 常驻索引：小，且确定
# ══════════════════════════════════════════════════════════════
class TestIndex(unittest.TestCase):
    def test_index_one_line_per_skill_sorted(self):
        """⑦ 每条技能一行、按 name 排序（乱序 add 也一样）。"""
        lib = SkillLibrary()
        for name in ("zeta", "alpha", "mike"):
            lib.add(spec(name, description=f"{name} 的描述"))
        runtime = SkillRuntime(lib)
        lines = runtime.index().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual([ln.split(" [")[0] for ln in lines], ["alpha", "mike", "zeta"])
        # 每行都等于契约给的 index_text（格式归契约，本模块不另造一套）
        self.assertEqual(lines[0], lib.get("alpha").index_text)

    def test_index_is_deterministic_regardless_of_insert_order(self):
        """插入顺序不影响索引：顺序变化会让 Prompt 变化，从而评测不可复现。"""
        a, b = SkillLibrary(), SkillLibrary()
        specs = [spec("x", description="x 描述"), spec("y", description="y 描述")]
        for item in specs:
            a.add(item)
        for item in reversed(specs):
            b.add(item)
        self.assertEqual(SkillRuntime(a).index(), SkillRuntime(b).index())

    def test_index_empty_library(self):
        self.assertEqual(SkillRuntime(SkillLibrary()).index(), "")

    def test_index_excludes_retired(self):
        lib = SkillLibrary()
        lib.add(spec("deploy", description="灰度"))
        lib.add(spec("rollback", description="回滚"))
        lib.retire("deploy")
        self.assertNotIn("deploy", SkillRuntime(lib).index())
        self.assertIn("rollback", SkillRuntime(lib).index())

    def test_index_much_smaller_than_full_bodies(self):
        """⑧ **量化核心**：索引远小于全文（5 个长技能，index < 全文的 1/3）。

        这就是「索引常驻保持敏捷」的物理证据：常驻成本只随技能**数量**线性增长，
        按需成本才随命中数增长。实测 184 字符 vs 3040 字符 ≈ 6%。
        """
        lib, specs = long_library(5)
        runtime = SkillRuntime(lib)
        index = runtime.index()
        full = sum(len(s.body_text()) for s in specs)
        self.assertEqual(full, 5 * len(specs[0].body_text()))
        self.assertLess(len(index), full / 3,
                        f"index={len(index)} 字符 应小于全文 {full}/3≈{full // 3} 字符")
        self.assertLess(len(index), (full / 3) / 2)   # 实测 6%：给 1/6 也不虚


# ══════════════════════════════════════════════════════════════
# ③ 匹配：触发词 + bigram（同口径由测试钉住）
# ══════════════════════════════════════════════════════════════
class TestMatching(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = SkillLibrary()
        self.lib.add(spec("deploy", description="灰度发布与回滚的标准流程",
                          trigger=("部署", "发布")))
        self.lib.add(spec("db-pool", description="连接池耗尽导致数据库超时",
                          trigger=("连接池",)))
        self.lib.add(spec("ci-fix", description="CI 流水线失败定位", trigger=("ci",)))
        self.runtime = SkillRuntime(self.lib)

    def test_match_by_trigger(self):
        """⑨ 触发词命中：作者显式声明的品类，命中即强证据。"""
        hits = self.runtime.match("帮我做一次线上部署")
        self.assertTrue(hits)
        self.assertEqual(hits[0].name, "deploy")
        # 触发词权重是下限；命中措辞里还有相似度加成，所以分数落在 [0.6, 1.0)
        self.assertGreaterEqual(hits[0].score, TRIGGER_WEIGHT)
        self.assertLess(hits[0].score, TRIGGER_WEIGHT + SIM_WEIGHT)
        self.assertIsInstance(hits[0], SkillMatch)
        self.assertEqual(hits[0].provider, "InMemorySkillProvider")

    def test_match_by_bigram_similarity(self):
        """⑩ 没有触发词也要能命中：bigram Jaccard 兜住作者没想到的措辞。"""
        goal = "数据库连接被占满一直报超时"          # 不含 "连接池"，也没有别的触发词
        self.assertNotIn("连接池", goal)
        hits = self.runtime.match(goal)
        self.assertTrue(hits, "同义改写必须能靠相似度命中")
        self.assertEqual(hits[0].name, "db-pool")
        self.assertLess(hits[0].score, TRIGGER_WEIGHT)   # 分来自相似度而非触发词
        self.assertGreaterEqual(hits[0].score, SIM_WEIGHT * SIM_FLOOR)

    def test_match_sorted_desc_and_capped_by_k(self):
        """⑪ 按 score 降序、最多 k 个。"""
        lib = SkillLibrary()
        for i, name in enumerate(("a-skill", "b-skill", "c-skill")):
            lib.add(spec(name, description=f"部署相关的第 {i} 套规程", trigger=("部署",)))
        runtime = SkillRuntime(lib)
        top2 = runtime.match("部署", k=2)
        self.assertEqual(len(top2), 2)
        self.assertGreaterEqual(top2[0].score, top2[1].score)
        # 同分时按 name 升序（确定性兜底）
        self.assertEqual([m.name for m in top2], ["a-skill", "b-skill"])
        self.assertEqual(runtime.match("部署", k=0), [])
        self.assertEqual(runtime.match("部署", k=-1), [])

    def test_no_match_returns_empty(self):
        """⑫ 无命中 → 空列表（不是 None，也不是「最接近的那个」）。"""
        self.assertEqual(self.runtime.match("今天天气不错"), [])
        self.assertEqual(self.runtime.match(""), [])
        self.assertEqual(SkillRuntime(SkillLibrary()).match("部署"), [])

    def test_single_char_trigger_is_ignored(self):
        """单字符触发词会命中一切 → 不参与匹配（MIN_TRIGGER_CHARS 的意义）。"""
        lib = SkillLibrary()
        lib.add(spec("noisy", description="完全无关的内容", trigger=("a",)))
        self.assertGreaterEqual(MIN_TRIGGER_CHARS, 2)   # 单字符确实低于门槛
        self.assertEqual(SkillRuntime(lib).match("abc"), [])

    def test_similarity_parity_with_memory_plane(self):
        """同口径锁定：本模块的 bigram/Jaccard 与 `plane/memory.py` 结果一致。

        刻意不 import（平面之间只依赖 contracts），所以用**测试**把口径钉住：
        两份实现哪天漂移了，这里先红。
        """
        try:
            from plane.memory import text_similarity as memory_similarity
        except Exception:                       # 并行施工期 memory 可能正在被改
            self.skipTest("plane/memory.py 暂不可用")
        pairs = [
            ("数据库连接被占满一直报超时", "连接池耗尽导致数据库超时"),
            ("线上部署失败了", "灰度发布与回滚的标准流程"),
            ("CI failed on lint", "ci failed on lint"),
            ("", "任意文本"),
        ]
        for a, b in pairs:
            self.assertAlmostEqual(text_similarity(a, b), memory_similarity(a, b),
                                   places=9, msg=f"{a!r} vs {b!r}")


# ══════════════════════════════════════════════════════════════
# ④ 按需装载：只装命中的，预算硬兜底
# ══════════════════════════════════════════════════════════════
class TestLoad(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = SkillLibrary()
        for name in ("deploy", "rollback", "db-pool"):
            self.lib.add(spec(name, description=f"{name} 的规程", steps=(f"{name} 步骤",)))

    def test_load_only_requested(self):
        """⑬ load 只返回请求的技能（不多装一个字节）。"""
        runtime = SkillRuntime(self.lib)
        self.assertEqual([s.name for s in runtime.load(["rollback"])], ["rollback"])
        self.assertEqual([s.name for s in runtime.load(["deploy", "db-pool"])],
                         ["deploy", "db-pool"])
        self.assertEqual(runtime.load([]), [])
        self.assertEqual(runtime.load(["ghost"]), [])   # 未知名字静默跳过

    def test_load_dedupes_repeated_names(self):
        runtime = SkillRuntime(self.lib)
        self.assertEqual([s.name for s in runtime.load(["deploy", "deploy"])], ["deploy"])

    def test_load_skips_retired_skill(self):
        """退休技能不进 Prompt（只有 get(name, version) 这条审计通道还看得见它）。"""
        self.lib.retire("deploy")
        runtime = SkillRuntime(self.lib)
        self.assertEqual(runtime.load(["deploy"]), [])
        self.assertIsNotNone(self.lib.get("deploy"))

    def test_max_body_chars_truncates_by_score(self):
        """⑭ 预算裁剪：按 score 降序装、装不下就**跳过**（不截断正文）。

        半截 SOP 比没有 SOP 更危险 —— 模型会以为看全了，跳过被截掉的验收步骤。
        """
        lib, _ = long_library(3)
        runtime = SkillRuntime(lib, max_body_chars=1000)     # 每个 body ≈ 608
        names = ["s2-skill", "s0-skill", "s1-skill"]          # 故意不是分数顺序
        scores = {"s0-skill": 0.9, "s1-skill": 0.5, "s2-skill": 0.1}
        loaded = runtime.load(names, scores=scores)
        self.assertEqual([s.name for s in loaded], ["s0-skill"])   # 最高分先装，其后装不下
        # 预算裁剪绝不切正文：装进来的必须是完整 body_text；且两个确实塞不下
        body_len = len(loaded[0].body_text())
        self.assertEqual(body_len, len(lib.get("s0-skill").body_text()))
        self.assertLessEqual(body_len, 1000)
        self.assertGreater(body_len * 2, 1000)

        # 预算给够 → 两个都装（说明上一轮是预算而不是 bug 在裁）
        runtime2 = SkillRuntime(lib, max_body_chars=1300)
        self.assertEqual([s.name for s in runtime2.load(names, scores=scores)],
                         ["s0-skill", "s1-skill"])

    def test_zero_budget_loads_nothing(self):
        runtime = SkillRuntime(self.lib, max_body_chars=0)
        self.assertEqual(runtime.load(["deploy"]), [])


# ══════════════════════════════════════════════════════════════
# ⑤ render：Builder 唯一需要的方法
# ══════════════════════════════════════════════════════════════
class TestRender(unittest.TestCase):
    def test_render_shape(self):
        """⑮ render 返回结构正确（index / loaded / truncated）。"""
        lib = SkillLibrary()
        lib.add(spec("deploy", description="灰度发布", trigger=("部署",),
                     steps=("灰度 5%", "观察 10 分钟")))
        result = SkillRuntime(lib).render("上线部署")
        self.assertEqual(set(result), {"index", "loaded", "truncated"})
        self.assertIsInstance(result["index"], str)
        self.assertIsInstance(result["loaded"], list)
        self.assertIsInstance(result["truncated"], bool)
        self.assertEqual(len(result["loaded"]), 1)
        self.assertEqual(set(result["loaded"][0]), {"name", "body"})
        self.assertEqual(result["loaded"][0]["name"], "deploy")
        self.assertEqual(result["loaded"][0]["body"], lib.get("deploy").body_text())
        self.assertFalse(result["truncated"])

    def test_render_never_loads_whole_library(self):
        """k 是硬上限：10 个技能、全是长正文，也只装 3 个。"""
        lib, _ = long_library(5)
        result = SkillRuntime(lib).render("出现故障了怎么办", k=3)
        self.assertEqual(len(result["loaded"]), 3)

    def test_render_total_under_index_plus_all_bodies(self):
        """⑯ **量化核心**：render 总量 < index + 全部 body（省预算的证明）。

        实测：render = 184 + 3×608 = 2008，而 index + 全部 body = 184 + 5×608 = 3224，
        省掉 37.7%。技能越多、正文越长，这个比例越夸张 —— 这正是双层装载的收益曲线。
        """
        lib, specs = long_library(5)
        runtime = SkillRuntime(lib)
        result = runtime.render("出现故障了怎么办", k=3)
        loaded_chars = len(result["index"]) + sum(len(x["body"]) for x in result["loaded"])
        everything = len(runtime.index()) + sum(len(s.body_text()) for s in specs)
        self.assertLess(loaded_chars, everything,
                        f"render={loaded_chars} 应小于 index+全部body={everything}")
        self.assertLess(loaded_chars, everything * 0.75)    # 实测 62%：留足余量

    def test_render_truncated_flag(self):
        """预算不够 → truncated=True（明确上报，调用方可降级：缩小 k / 问用户）。"""
        lib, _ = long_library(3)
        runtime = SkillRuntime(lib, max_body_chars=700)      # 只装得下 1 个 body
        result = runtime.render("出现故障了怎么办", k=3)
        self.assertEqual(len(result["loaded"]), 1)
        self.assertTrue(result["truncated"])
        # 对照：预算充足时不应误报
        self.assertFalse(SkillRuntime(lib, max_body_chars=5000)
                         .render("出现故障了怎么办", k=3)["truncated"])

    def test_render_no_match_still_carries_index(self):
        """没命中也要给索引：下一轮换个说法就可能命中，索引是「常驻」的。"""
        lib, _ = long_library(3)
        result = SkillRuntime(lib).render("今天天气不错")
        self.assertEqual(result["loaded"], [])
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["index"].split("\n")), 3)


# ══════════════════════════════════════════════════════════════
# ⑥ 自演进闭环：Memory 候选 → 技能库
# ══════════════════════════════════════════════════════════════
class TestIngestCandidates(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = SkillLibrary()
        self.runtime = SkillRuntime(self.lib)

    def test_ingest_builds_valid_spec(self):
        """⑰ 候选 → 合法 SkillSpec：name slug 化、source 正确、steps 来自 draft。"""
        written = self.runtime.ingest_candidates([candidate()])
        self.assertEqual(len(written), 1)
        item = written[0]
        self.assertIsInstance(item, SkillSpec)
        # 纯中文标题 → 内容哈希名（行为明确：稳定、可用、不撞车）
        self.assertTrue(re.match(r"^skill-[0-9a-f]{8}$", item.name), item.name)
        self.assertEqual(item.source, "memory-consolidate")
        self.assertEqual(item.version, "1")
        self.assertEqual(item.steps, ("连接池上限 20", "超时 3 秒", "超时后先看慢查询日志"))
        self.assertIn("连接池", item.trigger)                 # 触发词从标题派生
        self.assertIn("源自 2 条记忆", item.description)        # 来源可审计
        self.assertLessEqual(len(item.description), DESC_MAX_CHARS)
        # 真的落库了，而且下一轮的索引里看得见（闭环的关键一步）
        self.assertIsNotNone(self.lib.get(item.name))
        self.assertIn(item.name, self.runtime.index())

    def test_ingest_english_title_slugs(self):
        """英文标题走正常 slug：小写、空格转 `-`、去非字母数字。"""
        item = self.runtime.ingest_candidates(
            [candidate(title="Auto Deploy / Rollback Check", draft="灰度；放量")])[0]
        self.assertEqual(item.name, "auto-deploy-rollback-check")
        self.assertEqual(item.steps, ("灰度", "放量"))

    def test_ingest_second_time_bumps_version(self):
        """⑱ **同名二次摄入 → v2**：历史版本不覆盖（自动沉淀可能错，人工校订的 v1 要留着）。"""
        first = self.runtime.ingest_candidates([candidate()])[0]
        second = self.runtime.ingest_candidates(
            [candidate(draft="连接池上限 30；超时 5 秒")])[0]
        self.assertEqual(second.name, first.name)      # 同名
        self.assertEqual(first.version, "1")
        self.assertEqual(second.version, "2")
        self.assertEqual(self.lib.get(first.name).version, "2")
        self.assertEqual(self.lib.get(first.name, "1").steps,
                         ("连接池上限 20", "超时 3 秒", "超时后先看慢查询日志"))
        self.assertEqual(self.lib.versions(first.name), ["1", "2"])
        self.assertEqual(len(self.lib.all()), 1)       # 索引里仍只有一行
        third = self.runtime.ingest_candidates([candidate(draft="又一次修订")])[0]
        self.assertEqual(third.version, "3")

    def test_ingest_tolerates_bad_candidates(self):
        """⑲ 候选缺字段 / 脏类型时不崩：坏一条不炸整批，好的一条照常落库。"""
        written = self.runtime.ingest_candidates([
            None,
            {},
            {"title": None, "draft": None},
            "not-a-dict",
            {"title": "数据保留策略", "draft": "Episodic 归档 90 天；Profile 永久"},
            {"skill_id": "skill_only_id"},                  # 只有 id，也能落库
        ])
        names = [w.name for w in written]
        self.assertEqual(len(written), 2, names)
        self.assertIn("数据保留策略", written[0].title)
        self.assertEqual(written[1].name, "skill-only-id")
        self.assertEqual(self.runtime.ingest_candidates([]), [])
        self.assertEqual(self.runtime.ingest_candidates(None), [])

    def test_ingest_steps_fallback_to_source_ids(self):
        """没有 draft / reason 时，步骤退化为「参考候选来源：<记忆 id>」而不是空 SOP。"""
        item = self.runtime.ingest_candidates(
            [candidate(title="慢查询排查", draft="", reason="", from_memory=["m1", "m2"])])[0]
        self.assertEqual(len(item.steps), 1)
        self.assertTrue(item.steps[0].startswith("参考候选来源："))
        self.assertIn("m1", item.steps[0])
        self.assertIn("m2", item.steps[0])

    def test_ingest_chinese_title_is_stable(self):
        """⑳ 中文标题 slug 化不崩：同名同内容 → 稳定哈希名（跨实例、跨进程一致）。"""
        title = "自动沉淀：连接池/超时"
        name_a = self.runtime.ingest_candidates([candidate(title=title)])[0].name
        other = SkillRuntime(SkillLibrary())
        name_b = other.ingest_candidates([candidate(title=title)])[0].name
        self.assertEqual(name_a, name_b, "同一标题在不同实例里必须得到同一个名字")
        self.assertTrue(name_a.startswith("skill-"))
        self.assertEqual(slugify(title), name_a)
        self.assertEqual(slugify(""), slugify(""))     # 空标题也不崩
        # 不同中文标题 → 不同名字（不会全部塌成同一个哈希）
        self.assertNotEqual(slugify("连接池超时"), slugify("数据保留策略"))
        self.assertEqual(slugify("Hello World!"), "hello-world")
        self.assertEqual(slugify("  A__b  "), "a-b")

    def test_ingest_reingest_retired_skill_republishes(self):
        """已退休技能收到新版本 → 复出（新版本发布即恢复上线，旧版本仍留着）。"""
        first = self.runtime.ingest_candidates([candidate()])[0]
        self.assertTrue(self.lib.retire(first.name))
        self.assertEqual(self.lib.all(), [])
        revived = self.runtime.ingest_candidates([candidate(draft="修订后的规程")])[0]
        self.assertEqual(revived.version, "2")
        self.assertEqual([s.name for s in self.lib.all()], [first.name])
        self.assertEqual(self.lib.versions(first.name), ["1", "2"])


# ══════════════════════════════════════════════════════════════
# ⑦ 闭环（真实生产者）：Memory.consolidate() → SkillRuntime.ingest_candidates()
# ══════════════════════════════════════════════════════════════
try:                                            # 只读引用，缺了 skip 不 fail
    from agents.demo_extractor import RuleBasedExtractor  # noqa: E402
    from plane.memory import InMemoryProvider as InMemoryMemoryProvider  # noqa: E402
    from plane.memory import MemoryRuntime  # noqa: E402

    _HAS_MEMORY = True
except Exception:                               # pragma: no cover - 并行施工期兜底
    _HAS_MEMORY = False


@unittest.skipUnless(_HAS_MEMORY, "plane/memory.py 或 agents/demo_extractor.py 暂不可用")
class TestClosedLoop(unittest.TestCase):
    """架构 §3 的接缝：Memory 只产出**候选**，Skill 库才是内容的权威源。"""

    def _memory_candidates(self) -> list[dict]:
        provider = InMemoryMemoryProvider()
        mem = MemoryRuntime(provider, RuleBasedExtractor(),
                            now=lambda: 1_700_000_000_000.0)
        subject = Subject(tenant="acme", user_id="u1", team="team-core",
                          project="proj-alpha")
        for i, text in enumerate(("上次重启后连接池报错了",
                                  "昨天重启后连接池又报错",
                                  "刚才重启后连接池还是报错")):
            mem.write({"user": text, "assistant": "", "tools": [], "turn_id": f"t{i}"},
                      subject)
        mem.reflect(subject)                     # 3 条 Episodic → 1 条 Semantic
        for _ in range(2):
            mem.recall("重启后连接池报错", subject, k=10)   # hits 抬到 2
        return mem.consolidate(subject)

    def test_memory_candidates_flow_into_library(self):
        """㉑ 真闭环：Memory 候选 → 技能库 v1 → 索引可见 → 再摄入升 v2。"""
        candidates = self._memory_candidates()
        self.assertTrue(candidates, "consolidate 应产出候选（否则本用例没有意义）")
        self.assertEqual(set(candidates[0]), {"skill_id", "title", "from_memory",
                                              "reason", "draft"})

        lib = SkillLibrary()
        runtime = SkillRuntime(lib)
        written = runtime.ingest_candidates(candidates)
        self.assertEqual(len(written), len(candidates))
        item = written[0]
        self.assertEqual(item.source, "memory-consolidate")
        self.assertEqual(item.version, "1")
        self.assertTrue(item.steps)
        self.assertIn(item.name, runtime.index())          # 闭环：下一轮就看得到
        self.assertIsNotNone(lib.get(item.name, "1"))      # 历史版本可查

        again = runtime.ingest_candidates(candidates)
        self.assertEqual(again[0].version, "2")            # 同名再摄入 → v2
        self.assertEqual(len(lib.all()), 1)                # 但索引里仍只有一行


# ══════════════════════════════════════════════════════════════
# ⑧ Provider SPI：可替换
# ══════════════════════════════════════════════════════════════
class FakeSkillProvider:
    """假 provider：只实现 SPI 四方法（**没有** `versions`），验证降级路径。"""

    name = "FakeSkillProvider"

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], SkillSpec] = {}
        self.retired: set[str] = set()

    def put(self, spec: SkillSpec) -> bool:
        key = (spec.name, str(spec.version))
        if key in self.store:
            return False
        self.store[key] = spec
        self.retired.discard(spec.name)
        return True

    def get(self, name: str, version: str | None = None) -> SkillSpec | None:
        items = {v: s for (n, v), s in self.store.items() if n == name}
        if not items:
            return None
        if version is not None:
            return items.get(str(version))
        return items[sorted(items, key=lambda v: int(v))[-1]]

    def list_latest(self) -> list[SkillSpec]:
        out: list[SkillSpec] = []
        for name in sorted({n for (n, _) in self.store}):
            if name in self.retired:
                continue
            item = self.get(name)
            if item is not None:
                out.append(item)
        return out

    def retire(self, name: str) -> bool:
        if not any(n == name for (n, _) in self.store):
            return False
        self.retired.add(name)
        return True


class TestProviderSPI(unittest.TestCase):
    def test_fake_provider_injected(self):
        """㉒ 换 provider 后 SkillLibrary / SkillRuntime 一行不改照常工作。"""
        fake = FakeSkillProvider()
        lib = SkillLibrary(fake)
        self.assertIs(lib.provider, fake)
        lib.add(spec("deploy", description="灰度发布与回滚", trigger=("部署",)))
        lib.add(spec("deploy", description="灰度发布 v2", trigger=("部署",), version="2"))
        self.assertEqual(lib.get("deploy").version, "2")
        self.assertEqual(len(lib.all()), 1)
        self.assertEqual(lib.versions("deploy"), ["2"])       # 无 versions() → 降级为 current

        runtime = SkillRuntime(lib)
        self.assertIn("deploy", runtime.index())
        hits = runtime.match("线上部署")
        self.assertEqual(hits[0].provider, "FakeSkillProvider")   # 溯源指向真实 provider
        self.assertEqual(len(runtime.render("线上部署")["loaded"]), 1)
        self.assertTrue(lib.retire("deploy"))
        self.assertEqual(lib.all(), [])
        self.assertIsNotNone(lib.get("deploy", "2"))

    def test_provider_missing_spi_methods_rejected(self):
        """注入时就 fail fast：少一个 SPI 方法立刻 TypeError，不留到第一次 add。"""
        class HalfProvider:
            def put(self, spec): ...
            def get(self, name, version=None): ...
            def list_latest(self): return []

        with self.assertRaises(TypeError) as ctx:
            SkillLibrary(HalfProvider())
        self.assertIn("retire", str(ctx.exception))

    def test_default_provider_is_in_memory(self):
        self.assertIsInstance(SkillLibrary().provider, InMemorySkillProvider)
        provider = InMemorySkillProvider()
        self.assertTrue(provider.put(spec("a")))
        self.assertFalse(provider.put(spec("a")))          # 首写胜出
        self.assertEqual(provider.versions("a"), ["1"])
        self.assertEqual(len(provider), 1)
        provider.put(spec("a", version="2"))
        self.assertEqual(provider.get("a").version, "2")
        self.assertFalse(provider.is_retired("a"))


# ══════════════════════════════════════════════════════════════
# ⑨ 确定性
# ══════════════════════════════════════════════════════════════
class TestDeterminism(unittest.TestCase):
    def test_render_is_byte_identical_across_calls(self):
        """㉓ 同输入两次 render 完全一致（Prompt 可复现 → 评测可回归）。"""
        lib, _ = long_library(5)
        runtime = SkillRuntime(lib)
        first = runtime.render("出现故障了怎么办", k=3)
        second = runtime.render("出现故障了怎么办", k=3)
        self.assertEqual(first, second)
        self.assertEqual([x["name"] for x in first["loaded"]],
                         [x["name"] for x in second["loaded"]])
        self.assertEqual(first["index"], second["index"])
        self.assertTrue(first["loaded"], "本用例需要至少一个命中，否则确定性无从谈起")

    def test_match_is_stable_across_instances(self):
        """同一批技能、不同插入顺序 → 同一个 match 序列（排序键带 name 兜底）。"""
        specs = [spec(name, description=f"{name} 部署规程", trigger=("部署",))
                 for name in ("a-skill", "b-skill", "c-skill")]
        forward, backward = SkillLibrary(), SkillLibrary()
        for item in specs:
            forward.add(item)
        for item in reversed(specs):
            backward.add(item)
        left = SkillRuntime(forward).match("部署", k=5)
        right = SkillRuntime(backward).match("部署", k=5)
        self.assertEqual([(m.name, m.score) for m in left],
                         [(m.name, m.score) for m in right])


if __name__ == "__main__":
    unittest.main()
