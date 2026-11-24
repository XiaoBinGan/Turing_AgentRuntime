"""tests/test_router.py — W20：Router 的 29 个用例。

口径声明：本文件**不 import plane/memory.py**。第 23 条用例用一份独立的
「memory 同口径」参考实现重算 text_match，用来钉住两边不许漂移。

所有断言都不依赖浮点尾数：跨阈值的行为由阈值参数决定，同文件里第 20 条
专门验证「同一输入在 threshold=0.1 / 0.9 下结论相反」，所以分数算式的
微小改动会在多处同时炸掉，不会静默漂移。
"""
from __future__ import annotations

import json
import re
import unittest

from contracts import OWNER_TABLE, AgentSpec, Owner, Principal, RouteDecision
from plane.router import (
    CLARIFY_AGENT,
    GAP_MIN,
    W_SCOPE,
    W_TAG,
    W_TEXT,
    W_PRIORITY,
    AgentRouter,
    KeywordRouter,
    bigrams,
    jaccard,
    normalize_text,
    priority_norm,
    route_patches,
    text_similarity,
)


# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════
CODER = AgentSpec(
    name="coder",
    description="写代码 重构 调试 python",
    tags=("code", "python"),
    required_scopes=("tool.write",),
    tools=("run_tests",),
)
WRITER = AgentSpec(
    name="writer",
    description="文案 润色 排版 公众号 长文",
    tags=("文案", "排版"),
    required_scopes=("tool.read",),
)
ANALYST = AgentSpec(
    name="sql_analyst",
    description="sql 查询 报表 数据分析",
    tags=("sql", "report"),
    required_scopes=("tool.read",),
)

#: 默认 principal（scopes = tool.read + tool.write）
P = Principal()
#: 只有读权限的 principal
P_READONLY = Principal(scopes=frozenset({"tool.read"}))
#: 只有写权限的 principal（连 tool.read 都没有）
P_WRITEONLY = Principal(scopes=frozenset({"tool.write"}))


def build(*specs: AgentSpec, threshold: float = 0.45, **kw) -> AgentRouter:
    r = AgentRouter(confidence_threshold=threshold, **kw)
    for s in specs:
        r.register(s)
    return r


def three_agents(**kw) -> AgentRouter:
    return build(CODER, WRITER, ANALYST, **kw)


# ══════════════════════════════════════════════════════════════
# memory 同口径参考实现（**独立重写**，用于第 23 条对照）
# ══════════════════════════════════════════════════════════════
_MEM_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _ref_normalize(text: str) -> str:
    return _MEM_PUNCT_RE.sub("", (text or "").lower())


def _ref_bigrams(text: str) -> set[str]:
    s = _ref_normalize(text)
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _ref_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b)) if inter else 0.0


def _ref_text_similarity(a: str, b: str) -> float:
    return _ref_similarity(_ref_bigrams(a), _ref_bigrams(b))


def _spec_text(spec: AgentSpec) -> str:
    """与 plane/router.py::_spec_text 同形（name + description + tags）。"""
    return " ".join([spec.name, spec.description, " ".join(spec.tags)])


# ══════════════════════════════════════════════════════════════
# 1. 选择
# ══════════════════════════════════════════════════════════════
class TestSelection(unittest.TestCase):
    def test_01_single_agent_related_goal_selected(self):
        """单一 Agent + 相关 goal → 选中，且 confidence ≥ 阈值。"""
        r = build(CODER)
        d = r.route("帮我重构这段 Python 代码", P)
        self.assertEqual(d.agent, "coder")
        self.assertGreaterEqual(d.confidence, r.confidence_threshold)
        self.assertLessEqual(d.confidence, 1.0)
        # 唯一候选不触发"差距"规则，即使 candidates 只有一项
        self.assertEqual([n for n, _ in d.candidates], ["coder"])

    def test_02_two_agents_goal_picks_matching_one(self):
        """两个 Agent，goal 明显属于其中一个 → 选对（不是"随便挑一个"）。"""
        r = build(CODER, WRITER)
        d = r.route("重构 python 代码", P)
        self.assertEqual(d.agent, "coder")
        self.assertGreaterEqual(d.confidence, 0.45)
        # 分差确实拉开了
        scores = dict(d.candidates)
        self.assertGreater(scores["coder"] - scores["writer"], GAP_MIN)

    def test_03_unrelated_goal_clarifies(self):
        """完全无关的 goal → __clarify__（不许因为"总得选一个"就硬选）。"""
        r = three_agents()
        d = r.route("今天天气怎么样", P)
        self.assertEqual(d.agent, CLARIFY_AGENT)
        self.assertLess(d.confidence, r.confidence_threshold)

    def test_04_below_threshold_clarifies_with_candidates(self):
        """分数低于阈值 → clarify，且 candidates 非空（让人看见差多少）。"""
        r = three_agents(threshold=0.9)
        d = r.route("重构 python 代码", P)
        self.assertEqual(d.agent, CLARIFY_AGENT)
        self.assertIn("低于阈值", d.reason)
        self.assertGreaterEqual(len(d.candidates), 1)
        self.assertEqual(d.candidates[0][0], "coder")
        # confidence 仍然是"最高分"，不是 0 —— 这才叫"差多少"
        self.assertAlmostEqual(d.confidence, d.candidates[0][1], places=6)

    def test_05_close_scores_clarify(self):
        """两个 Agent 分数接近（< 0.08）→ clarify，reason 含"接近"。"""
        a = AgentSpec(name="alpha", description="写代码 重构 python", tags=("code",))
        b = AgentSpec(name="beta", description="写代码 重构 python", tags=("code",))
        r = build(a, b)
        d = r.route("重构 python 代码", P)
        self.assertEqual(d.agent, CLARIFY_AGENT)
        self.assertIn("接近", d.reason)
        self.assertGreaterEqual(d.confidence, 0.45)          # 够格，但赢没赢透
        gap = d.candidates[0][1] - d.candidates[1][1]
        self.assertLess(gap, GAP_MIN)

    def test_05b_exactly_gap_threshold_selects(self):
        """分差恰好 ≥ 0.08 就算"赢透"（边界由 GAP_MIN 决定，不是拍脑袋）。"""
        # 两个同描述 Agent，priority 5 vs 0 → priority 项拉开 0.10 的分差
        hi = AgentSpec(name="coder_hi", description="写代码 重构 调试 python",
                       tags=("code", "python"), priority=5)
        lo = AgentSpec(name="coder_lo", description="写代码 重构 调试 python",
                       tags=("code", "python"), priority=0)
        r = build(hi, lo)
        d = r.route("重构 python 代码", P)
        self.assertEqual(d.agent, "coder_hi")
        self.assertGreaterEqual(d.candidates[0][1] - d.candidates[1][1], GAP_MIN)

    # ----------------------------------------------------------
    # 6/7 权限
    # ----------------------------------------------------------
    def test_06_permission_denied_spec_is_excluded_not_penalised(self):
        """权限不足的 Agent 被**剔除**，不是扣分：诊断分最高也不许选中。"""
        need_admin = AgentSpec(
            name="db_admin",
            description="报表 sql 查询 数据分析",
            tags=("report", "sql"),
            required_scopes=("db.write", "db.admin"),   # P 没有这两个
        )
        r = build(need_admin, WRITER)
        d = r.route("跑一个报表 sql 查询", P)

        # 它的文本匹配度确实最高 —— 也就是说"扣分"机制下它会被选中
        rows = {row.agent: row for row in r.explain("跑一个报表 sql 查询", P)}
        self.assertFalse(rows["db_admin"].eligible)
        self.assertEqual(rows["db_admin"].scope_ok, 0.0)
        self.assertGreater(rows["db_admin"].text_match, rows["writer"].text_match)
        # 被闸门吃掉的是 scope(0.15) + priority(0.10)：补回来它就反超 writer。
        # 这正是"剔除"与"扣分"的分水岭 —— 扣分只会让它掉到第二名，剔除才能让它消失。
        self.assertGreater(rows["db_admin"].score + W_SCOPE + W_PRIORITY,
                           rows["writer"].score)

        # 但它连候选名单都进不去
        self.assertNotEqual(d.agent, "db_admin")
        self.assertNotIn("db_admin", [n for n, _ in d.candidates])
        self.assertIn(d.agent, {CLARIFY_AGENT, "writer"})

        # 换个更匹配的 goal 也一样：只要没权限就永远不出现
        for goal in ("db_admin 帮我改这个报表", "sql 数据分析 报表"):
            self.assertNotEqual(r.route(goal, P).agent, "db_admin")

    def test_07_all_agents_lack_permission(self):
        """所有 Agent 都无权限 → clarify，confidence=0，reason 说明是权限问题。"""
        r = three_agents()
        d = r.route("重构 python 代码", P_READONLY)   # coder 要 tool.write
        # writer/analyst 有 tool.read，所以这次仍应能选出一个"能干的"
        self.assertIn(d.agent, {CLARIFY_AGENT, "writer", "sql_analyst"})
        self.assertNotEqual(d.agent, "coder")

        r2 = build(CODER, AgentSpec(name="admin", description="全部权限",
                                    required_scopes=("root",)))
        d2 = r2.route("重构 python 代码", P_READONLY)
        # coder 需要 tool.write（P_READONLY 没有）；admin 需要 root（谁都没有）
        self.assertEqual(d2.agent, CLARIFY_AGENT)
        self.assertEqual(d2.confidence, 0.0)
        self.assertIn("无可用 Agent", d2.reason)
        self.assertEqual(d2.candidates, ())

    def test_08_empty_registry(self):
        """未注册任何 Agent → clarify，不崩。"""
        r = AgentRouter()
        d = r.route("随便什么", P)
        self.assertEqual(d.agent, CLARIFY_AGENT)
        self.assertEqual(d.confidence, 0.0)
        self.assertIn("无可用 Agent", d.reason)
        self.assertEqual(d.candidates, ())
        self.assertEqual(r.specs(), [])

    # ----------------------------------------------------------
    # 9/10 注册表
    # ----------------------------------------------------------
    def test_09_duplicate_register_raises(self):
        """同名重复注册 → ValueError（绝不静默覆盖）。"""
        r = build(CODER)
        with self.assertRaises(ValueError):
            r.register(CODER)
        # 原 spec 没被替换、也没被删除
        self.assertEqual([s.name for s in r.specs()], ["coder"])
        self.assertEqual(r.specs()[0], CODER)

        sneaky = AgentSpec(name="coder", description="完全不同的描述",
                           required_scopes=("root",))
        with self.assertRaises(ValueError):
            r.register(sneaky)
        self.assertEqual(r.specs()[0].description, CODER.description)

    def test_10_unregister(self):
        """unregister 存在 → True 且路由不再选它；不存在 → False。"""
        r = build(CODER, WRITER)
        self.assertTrue(r.unregister("writer"))
        self.assertFalse(r.unregister("writer"))
        self.assertFalse(r.unregister("never_registered"))
        self.assertEqual([s.name for s in r.specs()], ["coder"])
        d = r.route("重构 python 代码", P)
        self.assertNotEqual(d.agent, "writer")

    def test_10b_specs_returns_copy(self):
        """specs() 返回副本：调用方乱改列表不影响注册表。"""
        r = build(CODER)
        got = r.specs()
        got.append(WRITER)
        self.assertEqual(len(r.specs()), 1)

    # ----------------------------------------------------------
    # 11/12 priority 与 tag
    # ----------------------------------------------------------
    def test_11_priority_breaks_tie(self):
        """两个描述完全相同的 spec，priority 高的胜出。"""
        hi = AgentSpec(name="hi", description="写代码 重构 调试 python", priority=5)
        lo = AgentSpec(name="lo", description="写代码 重构 调试 python", priority=0)
        d = build(hi, lo).route("重构 python 代码", P)
        self.assertEqual(d.agent, "hi")
        self.assertGreater(d.candidates[0][1], d.candidates[1][1])

        # 反过来注册也一样（priority 不是"后注册者赢"）
        d2 = build(lo, hi).route("重构 python 代码", P)
        self.assertEqual(d2.agent, "hi")
        self.assertEqual(d, d2)

    def test_11b_equal_priority_is_not_a_tiebreaker(self):
        """priority 全相同 → 该维度不提供信息，模棱两可照样澄清。"""
        x = AgentSpec(name="x", description="写代码 重构 调试 python", priority=0)
        y = AgentSpec(name="y", description="写代码 重构 调试 python", priority=0)
        d = build(x, y).route("重构 python 代码", P)
        self.assertEqual(d.agent, CLARIFY_AGENT)
        self.assertIn("接近", d.reason)

    def test_12_tag_hit_raises_score(self):
        """tag 命中提升分数（同 description，只有一个带匹配 tag）。"""
        plain = AgentSpec(name="plain", description="报表 查询", tags=("weekly",))
        tagged = AgentSpec(name="tagged", description="报表 查询",
                           tags=("weekly", "报表"))
        r = build(plain, tagged, threshold=0.3)
        rows = {row.agent: row for row in r.explain("查一下报表", P)}
        self.assertGreater(rows["tagged"].score, rows["plain"].score)
        self.assertEqual(rows["tagged"].tag_match, 0.5)
        self.assertEqual(rows["plain"].tag_match, 0.0)
        self.assertEqual(rows["tagged"].tag_hits, ("报表",))
        # 分差够大 → 阈值 0.3 下真的选它
        self.assertEqual(r.route("查一下报表", P).agent, "tagged")

    # ----------------------------------------------------------
    # 13-16 确定性 / 归一化
    # ----------------------------------------------------------
    def test_13_determinism(self):
        """同输入两次 route 结果完全一致；注册顺序不影响结果。"""
        r = three_agents()
        a = r.route("重构 python 代码", P)
        b = r.route("重构 python 代码", P)
        self.assertEqual(a, b)
        self.assertEqual(a.to_dict(), b.to_dict())

        r2 = build(ANALYST, WRITER, CODER)      # 反序注册
        c = r2.route("重构 python 代码", P)
        self.assertEqual(a.to_dict(), c.to_dict())

        # 重复路由不会污染内部状态
        for _ in range(5):
            self.assertEqual(r.route("重构 python 代码", P).to_dict(), a.to_dict())

    def test_14_case_and_whitespace_insensitive(self):
        """大小写 / 多余空白不影响结论。"""
        r = three_agents()
        a = r.route("REFACTOR  the   Python CODE", P)
        b = r.route("refactor the python code", P)
        c = r.route("   Refactor\tthe\npython code   ", P)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(a.to_dict(), c.to_dict())
        self.assertEqual(a.agent, "coder")
        # 归一化函数本身也一致
        self.assertEqual(normalize_text("  A_b C "), "abc")

    def test_15_chinese_goal_matches_chinese_description(self):
        """中文 goal 能匹配中文 description（bigram，不依赖分词）。"""
        r = three_agents()
        d = r.route("重构 python 代码", P)
        self.assertEqual(d.agent, "coder")
        self.assertGreaterEqual(d.confidence, 0.45)

        r2 = build(WRITER, ANALYST)
        d2 = r2.route("帮我润色这段文案并排版", P)      # 纯中文、无英文关键词
        self.assertEqual(d2.agent, "writer")
        self.assertGreaterEqual(d2.confidence, 0.45)
        # 中文 description 的 bigram 匹配确实非零（不是靠英文名兜底）
        rows = {row.agent: row for row in r2.explain("帮我润色这段文案并排版", P)}
        self.assertGreater(rows["writer"].text_match, 0.0)
        self.assertGreater(rows["writer"].score, rows["sql_analyst"].score)

    def test_16_mixed_language_goal(self):
        """中英混合 goal 能匹配（"帮我 refactor 这段 Python 代码"）。"""
        r = three_agents()
        d = r.route("帮我 refactor 这段 Python 代码", P)
        self.assertEqual(d.agent, "coder")
        self.assertGreaterEqual(d.confidence, 0.45)

    # ----------------------------------------------------------
    # 17/18 输出形状
    # ----------------------------------------------------------
    def test_17_candidates_top3_descending(self):
        """candidates 最多 3 个，按分数降序，且都来自注册表。"""
        specs = [AgentSpec(name=f"agent{i}", description="写代码 重构 python 代码")
                 for i in range(5)]
        r = build(*specs, threshold=0.99)          # 强制走 clarify 也能拿到 candidates
        d = r.route("重构 python 代码", P)
        self.assertEqual(d.agent, CLARIFY_AGENT)
        self.assertEqual(len(d.candidates), 3)
        scores = [s for _, s in d.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        registered = {s.name for s in specs}
        self.assertTrue({n for n, _ in d.candidates} <= registered)

    def test_18_decision_json_serializable(self):
        """RouteDecision.to_dict() 可 json.dumps。"""
        r = three_agents()
        for goal in ("重构 python 代码", "今天天气怎么样"):
            payload = json.dumps(r.route(goal, P).to_dict(), ensure_ascii=False)
            back = json.loads(payload)
            self.assertIn("agent", back)
            self.assertIn("confidence", back)
            self.assertIsInstance(back["candidates"], list)
            for pair in back["candidates"]:
                self.assertEqual(len(pair), 2)
        # 构造出来的空决策也能序列化
        empty = RouteDecision(agent=CLARIFY_AGENT, confidence=0.0, reason="x")
        json.dumps(empty.to_dict(), ensure_ascii=False)

    # ----------------------------------------------------------
    # 19 KeywordRouter
    # ----------------------------------------------------------
    def test_19_keyword_router(self):
        """KeywordRouter 永远返回指定 agent，confidence=1.0。"""
        kr = KeywordRouter()
        self.assertEqual(kr.agent, "default")
        for goal in ("重构 python 代码", "", "今天天气怎么样"):
            d = kr.route(goal, P)
            self.assertEqual(d.agent, "default")
            self.assertEqual(d.confidence, 1.0)
            self.assertEqual(d.reason, "single-agent fallback")
            self.assertEqual(d.candidates, ())

        solo = KeywordRouter(agent="solo")
        self.assertEqual(solo.route("随便", P).agent, "solo")
        with self.assertRaises(ValueError):
            KeywordRouter(agent="   ")

    # ----------------------------------------------------------
    # 20/21 阈值与空 goal
    # ----------------------------------------------------------
    def test_20_threshold_is_configurable(self):
        """同一输入：threshold=0.1 选中，threshold=0.9 澄清。"""
        loose = build(CODER, WRITER, threshold=0.1)
        tight = build(CODER, WRITER, threshold=0.9)
        d_loose = loose.route("重构 python 代码", P)
        d_tight = tight.route("重构 python 代码", P)
        self.assertEqual(d_loose.agent, "coder")
        self.assertEqual(d_tight.agent, CLARIFY_AGENT)
        self.assertEqual(d_loose.confidence, d_tight.confidence)   # 同一个分，不同阈值
        self.assertGreater(d_loose.confidence, 0.1)
        self.assertLess(d_loose.confidence, 0.9)

    def test_20b_threshold_validation(self):
        with self.assertRaises(ValueError):
            AgentRouter(confidence_threshold=1.5)
        with self.assertRaises(ValueError):
            AgentRouter(confidence_threshold=-0.1)
        with self.assertRaises(TypeError):
            AgentRouter(embedder=42)  # type: ignore[arg-type]

    def test_21_empty_goal_clarifies(self):
        """空 goal → 明确澄清，不崩、不"命中所有 tag"。"""
        r = three_agents()
        for goal in ("", "   ", "\n\t "):
            d = r.route(goal, P)
            self.assertEqual(d.agent, CLARIFY_AGENT)
            self.assertEqual(d.confidence, 0.0)
            self.assertIn("空", d.reason)
            self.assertEqual(d.candidates, ())

    def test_21b_whitespace_only_registration_rejected(self):
        with self.assertRaises(ValueError):
            build(AgentSpec(name="   "))
        with self.assertRaises(TypeError):
            build("not a spec")  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════
# 22-26 打分公式的可手算性
# ══════════════════════════════════════════════════════════════
class TestScoring(unittest.TestCase):
    def test_22_score_equals_hand_computation(self):
        """score 必须等于 0.55*tm + 0.20*tag + 0.15*scope + 0.10*pn（可手算）。"""
        r = three_agents()
        for row in r.explain("重构 python 代码", P):
            expect = (W_TEXT * row.text_match + W_TAG * row.tag_match
                      + W_SCOPE * row.scope_ok + 0.10 * row.priority_norm)
            self.assertAlmostEqual(row.score, expect, places=6, msg=row.agent)
            self.assertGreaterEqual(row.score, 0.0)
            self.assertLessEqual(row.score, 1.0)

    def test_22b_text_match_weight_dominates(self):
        """权重的相对大小：0.55 > 0.20 > 0.15 > 0.10（文本过半才有决定权）。"""
        self.assertGreater(W_TEXT, 0.5)
        self.assertGreater(W_TEXT, W_TAG)
        self.assertGreater(W_TAG, W_SCOPE)
        self.assertGreater(W_SCOPE, 0.10)

    def test_23_text_match_uses_memory_convention(self):
        """text_match 口径与 plane/memory.py 的 similarity 一致（独立重算对照）。"""
        goal = "重构 Python 代码"
        r = three_agents()
        rows = {row.agent: row for row in r.explain(goal, P)}
        for spec in (CODER, WRITER, ANALYST):
            expect = _ref_text_similarity(goal, _spec_text(spec))
            self.assertAlmostEqual(rows[spec.name].text_match, expect, places=9,
                                   msg=spec.name)
        # 归一化 / bigram / jaccard 三个基础函数也要与参考实现一致
        self.assertEqual(normalize_text(" Refactor_Code!! "), _ref_normalize(" Refactor_Code!! "))
        self.assertEqual(bigrams("重构代码"), _ref_bigrams("重构代码"))
        self.assertAlmostEqual(jaccard({"ab", "bc"}, {"bc", "cd"}),
                               _ref_similarity({"ab", "bc"}, {"bc", "cd"}), places=12)
        self.assertAlmostEqual(text_similarity("重构代码", "重构代码"), 1.0, places=12)
        self.assertEqual(text_similarity("", "重构代码"), 0.0)
        self.assertEqual(bigrams("写"), {"写"})       # 单字退化为自身

    def test_24_explain_marks_ineligible(self):
        """explain 标出被剔除者，且排序为分数降序 + priority 降序 + name 升序。"""
        need = AgentSpec(name="ghost", description="重构 python 代码",
                         required_scopes=("root",), priority=99)
        r = build(CODER, WRITER, need)
        rows = r.explain("重构 python 代码", P)
        by = {row.agent: row for row in rows}
        self.assertFalse(by["ghost"].eligible)
        self.assertEqual(by["ghost"].scope_ok, 0.0)
        scores = [row.score for row in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # to_dict 也可 json 化
        json.dumps([row.to_dict() for row in rows], ensure_ascii=False)

    def test_25_priority_norm_minmax(self):
        """priority_norm 是 min-max：最高 1.0、最低 0.0、全部相同则全 1.0。"""
        self.assertEqual(priority_norm([0, 5]), [0.0, 1.0])
        self.assertEqual(priority_norm([5, 0]), [1.0, 0.0])
        self.assertEqual(priority_norm([3, 3, 3]), [1.0, 1.0, 1.0])
        self.assertEqual(priority_norm([7]), [1.0])
        self.assertEqual(priority_norm([]), [])
        mid = priority_norm([0, 5, 10])
        self.assertAlmostEqual(mid[1], 0.5, places=12)

    def test_26_custom_embedder(self):
        """可注入 embedder：token 粒度由调用方定，Jaccard 口径不变。"""
        def words(text: str) -> set[str]:
            return {w for w in re.split(r"[\s\W_]+", (text or "").lower()) if w}

        goal = "重构 python 代码"
        default_rows = {row.agent: row for row in three_agents().explain(goal, P)}
        word_rows = {row.agent: row for row in
                     three_agents(embedder=words).explain(goal, P)}

        expect = jaccard(words(goal), words(_spec_text(CODER)))
        self.assertAlmostEqual(word_rows["coder"].text_match, expect, places=12)
        self.assertNotAlmostEqual(word_rows["coder"].text_match,
                                  default_rows["coder"].text_match, places=6)

        r = three_agents(embedder=words)
        d = r.route(goal, P)
        self.assertEqual(d.agent, "coder")

        # 返回空集的 embedder 不炸：text_match 全 0，但 tag_match 仍按原口径命中
        empty = three_agents(embedder=lambda _t: set())
        d2 = empty.route(goal, P)
        self.assertEqual(d2.agent, CLARIFY_AGENT)
        # 只剩 tag(python 命中 1/2 → 0.10) + scope(0.15) + priority(0.10)
        self.assertAlmostEqual(d2.confidence, W_TAG * 0.5 + 0.15 + 0.10, places=6)


# ══════════════════════════════════════════════════════════════
# 27-29 契约接缝与不变量
# ══════════════════════════════════════════════════════════════
class TestContractSeams(unittest.TestCase):
    def test_26b_spec_embedding_is_cached(self):
        """spec 描述向量按名缓存：每轮只重算 goal，不重算 N 个描述。"""
        def words(text: str) -> set[str]:
            return {w for w in re.split(r"[\s\W_]+", (text or "").lower()) if w}

        calls: list[str] = []

        def emb(text: str) -> set[str]:
            calls.append(text)
            return words(text)

        r = build(CODER, WRITER, embedder=emb)
        r.route("重构 python 代码", P)
        first = len(calls)
        self.assertEqual(first, 3)                     # goal(1) + 两个 spec(2)
        r.route("重构 python 代码", P)
        self.assertEqual(len(calls) - first, 1)        # 第二轮只算 goal

        # 注销同名后重注册新 spec → 旧向量必须作废
        r.unregister("writer")
        new_writer = AgentSpec(name="writer", description="完全换掉的描述")
        r.register(new_writer)
        r.route("完全换掉的描述", P)
        self.assertIn(_spec_text(new_writer), calls)

    def test_27_route_only_returns_registered_names(self):
        """不变量：route 返回的 agent 要么是 __clarify__，要么真的注册过。"""
        r = three_agents()
        registered = {s.name for s in r.specs()}
        goals = ["重构 python 代码", "润色文案", "sql 报表", "今天天气", "", "???"]
        for goal in goals:
            d = r.route(goal, P)
            self.assertIn(d.agent, registered | {CLARIFY_AGENT}, msg=goal)
            for name, _ in d.candidates:
                self.assertIn(name, registered)

    def test_28_single_eligible_agent_skips_gap_rule(self):
        """权限闸门之后只剩一个候选 → 不适用"差距"规则，够阈值就选。"""
        strong_but_denied = AgentSpec(
            name="denied", description="写代码 重构 调试 python",
            tags=("code", "python"), required_scopes=("root",), priority=10)
        r = build(CODER, strong_but_denied)
        d = r.route("重构 python 代码", P)
        # 若把被剔除者也纳入比较，分差会是 0 → 澄清。实际必须选中 coder。
        self.assertEqual(d.agent, "coder")
        self.assertEqual([n for n, _ in d.candidates], ["coder"])

    def test_28b_explain_agrees_with_route(self):
        """审计视图与真实决策同口径：candidates / confidence 必须能对上。"""
        r = three_agents()
        for goal in ("重构 python 代码", "帮我润色这段文案并排版", "今天天气怎么样"):
            d = r.route(goal, P)
            top3 = [(row.agent, row.score) for row in r.explain(goal, P)
                    if row.eligible][:3]
            self.assertEqual(list(d.candidates), top3, msg=goal)
            self.assertAlmostEqual(d.confidence, top3[0][1], places=6, msg=goal)

    def test_29_route_patches_owned_by_router(self):
        """Router 是 agent_state / route_confidence 的唯一 writer（OWNER_TABLE）。"""
        d = three_agents().route("重构 python 代码", P)
        patches = route_patches(d)
        self.assertEqual(len(patches), 2)
        paths = [p.path for p in patches]
        self.assertEqual(paths, ["agent_state", "route_confidence"])
        for p in patches:
            self.assertIs(p.by, Owner.ROUTER)
            self.assertIs(OWNER_TABLE[p.path], Owner.ROUTER)
            self.assertEqual(p.op, "set")
        json.dumps([p.to_dict() for p in patches], ensure_ascii=False)
        self.assertEqual(patches[0].value["agent"], d.agent)
        self.assertAlmostEqual(patches[1].value, round(d.confidence, 4), places=6)

        clarify = route_patches(three_agents().route("今天天气怎么样", P))
        self.assertEqual(clarify[0].value["agent"], CLARIFY_AGENT)


if __name__ == "__main__":
    unittest.main()
