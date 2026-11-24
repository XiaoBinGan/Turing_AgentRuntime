"""tests/test_agent_planner.py —— `agents/planner.py` 的契约验收（Agent 层）。

分三组：

  A. `StaticDAGPlanner`：DAG 选择、Patch 归属、确定性、非法 DAG、只读快照；
  B. `LLMPlanner`：JSON 解析、坏输入不崩、max_steps、危险工具不过滤；
  C. 与 Runtime 集成：`Loop(planner=...)` 构造期校验、死锁强制 replan 一次、
     Patch 真的被应用到 Context（`ctx.patch_log` + `ctx.get("plan")`）。

约定：Runtime 侧只当黑盒用（`Loop` / `PromptBuilder` / `ContextHandle` /
`ContextSnapshot` 都是公开入口），**不改也不 import 内部私有函数**。
LLM 与 Dispatcher 用本文件内的极简 fake —— 这组测试要证明的是「照 SPI 实现能被
Runtime 正常调用」，不是再验一遍 Runtime。
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import (  # noqa: E402
    OWNER_TABLE,
    Budget,
    ContextSnapshot,
    DAGNode,
    DAGSpec,
    ExecCtx,
    LLMResponse,
    NormalizedToolCall,
    Owner,
    Patch,
    Principal,
    SideEffect,
    TermReason,
    ToolManifest,
    ToolOutcome,
    ToolResult,
    Usage,
)
from agents.planner import PLANNER_FIELDS, LLMPlanner, StaticDAGPlanner  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 夹具：DAG / 快照 / fake LLM / fake dispatcher
# ══════════════════════════════════════════════════════════════
def publish_dag() -> DAGSpec:
    """发布流程：流程确定，典型「静态 DAG 比问模型靠谱」的场景。"""
    return DAGSpec(name="publish", nodes=(
        DAGNode(name="freeze", agent="release-bot", goal="冻结当前线上版本基线"),
        DAGNode(name="canary", agent="release-bot", goal="灰度 5% 流量观察告警",
                depends_on=("freeze",)),
        DAGNode(name="verify", agent="sre", goal="看 p99 与错误率是否达标",
                depends_on=("canary",)),
        DAGNode(name="promote", agent="release-bot", goal="全量发布",
                depends_on=("verify",)),
    ))


def rollback_dag() -> DAGSpec:
    """回滚流程：与发布互斥的另一条 SOP。"""
    return DAGSpec(name="rollback", nodes=(
        DAGNode(name="pick_prev", agent="release-bot", goal="确定上一个稳定版本"),
        DAGNode(name="restore", agent="release-bot", goal="回滚并确认实例就绪",
                depends_on=("pick_prev",)),
    ))


def diamond_dag() -> DAGSpec:
    """分叉 + 汇合：用来钉「步骤顺序 = 拓扑序，依赖关系原样保留」。"""
    return DAGSpec(name="diagnose", nodes=(
        DAGNode(name="collect", agent="sre", goal="收集日志与指标"),
        DAGNode(name="logs", agent="sre", goal="查日志", depends_on=("collect",)),
        DAGNode(name="metrics", agent="sre", goal="查指标", depends_on=("collect",)),
        DAGNode(name="conclude", agent="sre", goal="汇总结论", depends_on=("logs", "metrics")),
    ))


SNAPSHOT = {
    "goal": "把 order-svc 发到 1.4.2，p99 不许超过 300ms",
    "phase": "EXECUTING",
    "loop_count": 4,
    "history": [
        {"role": "user", "text": "把 order-svc 发到 1.4.2"},
        {"role": "assistant", "step": 1, "text": "我先查一下当前版本。"},
    ],
    "tool_results": [{"tool": "query_release", "payload": {"current": "1.4.1"}}],
}

REASON = "L1 连续 4 轮动作完全相同：query_release({'service': 'order-svc'})"


def resp(text: str = "", calls=(), finish: str = "stop") -> LLMResponse:
    norm = tuple(NormalizedToolCall(id=c[0], name=c[1], args=c[2]) for c in calls)
    return LLMResponse(
        text=text, tool_calls=norm,
        usage=Usage(input_tokens=10, output_tokens=5, model="mock-1"),
        finish_reason=finish,
    )


class FakeLLM:
    """最小 LLM：按设定吐文本 / 抛异常 / 调回调。只实现 `complete`。"""

    model = "fake-1"

    def __init__(self, text: str | None = None, *, exc: Exception | None = None,
                 fn=None):
        self.text = text
        self.exc = exc
        self.fn = fn
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt, tools, e):
        self.prompts.append(prompt)
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.fn is not None:
            return self.fn(prompt, tools, e)
        return LLMResponse(
            text=self.text or "", tool_calls=(),
            usage=Usage(input_tokens=10, output_tokens=5, model=self.model),
            finish_reason="stop",
        )


class FakeDispatcher:
    """只认 `lookup`：返回确定 payload，让死锁构造保持可控。"""

    manifests = [ToolManifest(name="lookup", side_effect=SideEffect.READ_ONLY)]

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, name, args, e):
        self.calls.append((name, dict(args)))
        return ToolOutcome.ok(ToolResult(tool=name, payload={"echo": args}))


def deadlock_llm() -> object:
    """永远重复**同一个**动作：第 4 步后必命中 L1 死锁；replan 后仍照旧。"""
    from adapters.llm_mock import CallableLLM

    def fn(index, prompt):
        return resp(text="再查一次。", calls=[("c1", "lookup", {"q": "order-svc"})])

    return CallableLLM(fn)


def make_exec(max_steps: int = 24) -> ExecCtx:
    return ExecCtx(principal=Principal(), budget=Budget(max_steps=max_steps))


def make_loop(planner, llm=None):
    """真 `Loop` + 真 `PromptBuilder`；只有 LLM/Dispatcher 是 fake。"""
    from builder import PromptBuilder
    from core.loop import Loop

    llm = llm if llm is not None else deadlock_llm()
    dispatcher = FakeDispatcher()
    loop = Loop(PromptBuilder(total_tokens=4000), llm, dispatcher,
                planner=planner, window=4, tools=list(dispatcher.manifests))
    return loop, llm, dispatcher


# ══════════════════════════════════════════════════════════════
# A. StaticDAGPlanner
# ══════════════════════════════════════════════════════════════
class TestStaticDAGPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = StaticDAGPlanner(
            {"publish": publish_dag(), "rollback": rollback_dag()},
            default="publish",
        )

    # ① 命中已知 DAG → 返回 Patch（写 plan 字段）
    def test_01_hit_known_dag_returns_patch(self):
        patch = self.planner.replan({**SNAPSHOT, "plan_hint": "rollback"}, REASON)

        self.assertIsInstance(patch, Patch)
        self.assertEqual(patch.op, "set")
        self.assertEqual(patch.path, "plan")
        plan = patch.value
        self.assertEqual(plan["dag"], "rollback")
        self.assertEqual(plan["source"], "static_dag")
        self.assertEqual([s["name"] for s in plan["steps"]], ["pick_prev", "restore"])

    # ② 未命中且无 default → None（Loop 据此判 NO_PROGRESS）
    def test_02_miss_without_default_returns_none(self):
        planner = StaticDAGPlanner({"publish": publish_dag()})
        self.assertIsNone(planner.replan({"goal": "x"}, REASON))
        self.assertTrue(planner.notes, "没选出来也要留下理由")
        self.assertIn("default", planner.notes[-1])

    # ③ 未命中但有 default → 用 default
    def test_03_miss_with_default_uses_default(self):
        patch = self.planner.replan({"goal": "x"}, "L2 语义指纹相同")
        self.assertIsNotNone(patch)
        self.assertEqual(patch.value["dag"], "publish")
        self.assertIn("default", patch.value["why"])

    # ④ Patch.by 必须是 Owner.PLANNER（不是 LOOP / 别的）
    def test_04_patch_by_is_planner_owner(self):
        patch = self.planner.replan({**SNAPSHOT, "plan_hint": "publish"}, REASON)
        self.assertIs(patch.by, Owner.PLANNER)
        # 反证：这个字段的 owner 也是 PLANNER，所以 Runtime apply 不会拒
        self.assertIs(OWNER_TABLE["plan"], Owner.PLANNER)

    # ⑤ Patch.path 只能落在 Planner 有权写的字段里
    def test_05_patch_path_in_planner_fields(self):
        patch = self.planner.replan({**SNAPSHOT, "plan_hint": "publish"}, REASON)
        self.assertIn(patch.path, PLANNER_FIELDS)
        # 与 Runtime 的 OWNER_TABLE 对齐：这些字段的 writer 必须都是 PLANNER
        for field in PLANNER_FIELDS:
            self.assertIs(OWNER_TABLE[field], Owner.PLANNER, field)
        # 反例钉住一个易错点：observations 归 LOOP，Planner 写它会被拒
        self.assertIs(OWNER_TABLE["observations"], Owner.LOOP)

    # ⑥ 多次调用结果稳定（同快照同输出）
    def test_06_repeat_stable(self):
        snap = {**SNAPSHOT, "plan_hint": "publish"}
        first = self.planner.replan(snap, REASON)
        second = self.planner.replan(snap, REASON)
        third = self.planner.replan(snap, REASON)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(first.to_dict(), third.to_dict())
        self.assertIsNot(first, third, "每次返回新的 Patch 对象，但值相等")

    # ⑦ 空 DAG 表 → None
    def test_07_empty_dag_table_returns_none(self):
        self.assertIsNone(StaticDAGPlanner({}).replan(SNAPSHOT, REASON))
        self.assertIsNone(StaticDAGPlanner({}, default="publish").replan(SNAPSHOT, REASON))
        self.assertEqual(len(StaticDAGPlanner({})), 0)

    # ⑧ reason 被记录进 Patch 内容（能追溯「为什么 re-plan」）
    def test_08_reason_recorded(self):
        patch = self.planner.replan({"plan_hint": "publish"}, REASON)
        self.assertEqual(patch.value["reason"], REASON)
        self.assertIn("L1", patch.value["reason"])
        # 同时进 notes（日志侧可读）
        self.assertTrue(any("publish" in n for n in self.planner.notes))

    # ⑨ 非法 DAG（成环）→ None，不抛
    def test_09_cycle_dag_returns_none(self):
        broken = DAGSpec(name="broken", nodes=(
            DAGNode(name="a", goal="甲", depends_on=("b",)),
            DAGNode(name="b", goal="乙", depends_on=("a",)),
        ))
        planner = StaticDAGPlanner({"broken": broken}, default="broken")
        self.assertIsNone(planner.replan({}, REASON))
        self.assertIn("成环", "".join(planner.notes))

    # ⑩ 两种快照形状都接住：Mapping 与 Runtime 的 ContextSnapshot
    def test_10_accepts_context_snapshot_object(self):
        data = {**SNAPSHOT, "plan_hint": "rollback"}
        via_mapping = self.planner.replan(data, REASON)
        via_object = self.planner.replan(ContextSnapshot(version=7, data=copy.deepcopy(data)), REASON)
        self.assertEqual(via_mapping, via_object)

    # ⑪ 快照只读：调用前后字节一致（改它没用，索性不碰）
    def test_11_does_not_mutate_snapshot(self):
        snap = {"plan": {"dag": "publish", "steps": []}, "history": [{"role": "user", "text": "发布"}]}
        before = copy.deepcopy(snap)
        self.planner.replan(snap, REASON)
        self.assertEqual(snap, before)

    # ⑫ 步骤顺序 = 拓扑序，依赖关系原样保留
    def test_12_topo_order_and_deps(self):
        planner = StaticDAGPlanner({"diagnose": diamond_dag()}, default="diagnose")
        steps = planner.replan({}, REASON).value["steps"]
        self.assertEqual([s["name"] for s in steps], ["collect", "logs", "metrics", "conclude"])
        self.assertEqual(steps[0]["depends_on"], [])
        self.assertEqual(steps[2]["depends_on"], ["collect"])
        self.assertEqual(steps[3]["depends_on"], ["logs", "metrics"])
        self.assertEqual([s["index"] for s in steps], [1, 2, 3, 4])


# ══════════════════════════════════════════════════════════════
# B. LLMPlanner
# ══════════════════════════════════════════════════════════════
GOOD_JSON = (
    '{"steps": ['
    '{"step": "先看 p99 与错误率，确认是回归还是容量", "tool": "query_metrics",'
    ' "args": {"service": "order-svc", "window": "15m"}, "why": "日志里没有异常，换指标面"},'
    '{"step": "把新版本流量回切到 5%", "tool": "shift_traffic",'
    ' "args": {"percent": 5}, "why": "缩小影响面再定位"}'
    ']}'
)


class TestLLMPlanner(unittest.TestCase):
    # ⑬ 模型返回合法 JSON → Patch
    def test_13_valid_json_returns_patch(self):
        planner = LLMPlanner(FakeLLM(GOOD_JSON))
        patch = planner.replan(SNAPSHOT, REASON)

        self.assertIsInstance(patch, Patch)
        self.assertEqual((patch.op, patch.path), ("set", "plan"))
        self.assertIs(patch.by, Owner.PLANNER)
        self.assertEqual(patch.value["source"], "llm")
        self.assertEqual(patch.value["reason"], REASON)
        self.assertIsNone(planner.last_error)

    # ⑭ 坏 JSON → None（不崩）
    def test_14_bad_json_returns_none(self):
        for bad in ("{steps: 1, 2}", '{"steps": [', "好的，我的计划是：先看指标",
                    '{"steps": "先看指标"}'):
            planner = LLMPlanner(FakeLLM(bad))
            self.assertIsNone(planner.replan(SNAPSHOT, REASON), bad)
            self.assertIn("无法解析", planner.last_error)

    # ⑮ 模型返回空 → None
    def test_15_empty_output_returns_none(self):
        for empty in ("", "   \n\t ", "```json\n```", '{"steps": []}', "[]"):
            planner = LLMPlanner(FakeLLM(empty))
            self.assertIsNone(planner.replan(SNAPSHOT, REASON), repr(empty))

    # ⑯ 模型抛异常 → None（刻意选择「不 re-raise」，见下）
    def test_16_llm_raises_returns_none(self):
        """选择返回 None 而不是让异常冒泡。

        Runtime 会包住 `replan` 并记 warning，所以冒泡也不会让回合失败；但那样
        异常只变成一条 warning，Loop 继续循环、下次死锁再烧一次模型调用。而
        `None` 是契约里**有语义**的返回值（"我也想不出"），直接换来确定的
        `NO_PROGRESS` 收尾。失败原因记在 `last_error` / `notes`，可观测性不丢。
        """
        planner = LLMPlanner(FakeLLM(exc=RuntimeError("429 rate limited")))
        self.assertIsNone(planner.replan(SNAPSHOT, REASON))
        self.assertIn("RuntimeError", planner.last_error)
        self.assertIn("429", planner.last_error)
        self.assertTrue(planner.notes)

    # ⑰ max_steps 生效（模型给 20 步 → 截到 6）
    def test_17_max_steps_truncates(self):
        twenty = "{\"steps\": [" + ",".join(
            '{"step": "第 %d 步"}' % i for i in range(1, 21)) + "]}"
        planner = LLMPlanner(FakeLLM(twenty))
        patch = planner.replan(SNAPSHOT, REASON)

        self.assertEqual(len(patch.value["steps"]), 6)
        self.assertTrue(patch.value["truncated"])
        self.assertEqual(patch.value["dropped"], 14)
        self.assertEqual(patch.value["steps"][0]["step"], "第 1 步")
        self.assertEqual(patch.value["steps"][-1]["step"], "第 6 步")

        # 上限可配，且非法值被夹到 >= 1（不会产出空计划）
        self.assertEqual(len(LLMPlanner(FakeLLM(twenty), max_steps=2)
                             .replan(SNAPSHOT, REASON).value["steps"]), 2)
        self.assertEqual(len(LLMPlanner(FakeLLM(twenty), max_steps=0)
                             .replan(SNAPSHOT, REASON).value["steps"]), 1)

    # ⑱ Patch 内容含步骤列表
    def test_18_patch_contains_steps(self):
        patch = LLMPlanner(FakeLLM(GOOD_JSON)).replan(SNAPSHOT, REASON)
        steps = patch.value["steps"]

        self.assertIsInstance(steps, list)
        self.assertEqual(len(steps), 2)
        for step in steps:
            self.assertEqual(set(step), {"step", "tool", "args", "why"})
            self.assertTrue(step["step"])
        self.assertEqual(steps[0]["tool"], "query_metrics")
        self.assertEqual(steps[0]["args"], {"service": "order-svc", "window": "15m"})
        self.assertIn("指标", steps[0]["why"])

    # ⑲ 快照里没有 history 也能工作
    def test_19_snapshot_without_history(self):
        planner = LLMPlanner(FakeLLM(GOOD_JSON))
        patch = planner.replan({}, REASON)
        self.assertIsNotNone(patch)
        self.assertIn("死锁原因", planner.last_prompt)
        # 不应把缺失的 history 渲染成 "None"
        self.assertNotIn("None", planner.last_prompt)

    # ⑳ 危险工具**不在** Planner 过滤（Governance 的职责）
    def test_20_dangerous_tools_not_filtered(self):
        """Planner 不拦高危工具名：闸门在 Governance（`plane/governance.py`）。

        在这里靠名字黑名单过滤会有三个具体后果：
          1. Planner 手上没有工具清单 / principal / 租户策略，只能猜名字，
             名单永远不全，且与 Governance 的判定不一致；
          2. 计划被**悄悄改写**：模型写 6 步变 4 步，事后复盘看到一份
             「谁也没写过、也没人批准」的计划；
          3. 「模型在往危险方向想」这件事本身是有价值的信号，应当被记录送审，
             而不是在规划层被抹掉。
        所以本用例的期望是：**原样保留**，交给执行期的 RBAC / 审批去拒。
        """
        risky = ('{"steps": ['
                 '{"step": "直接重建全部节点", "tool": "drop_all_nodes", "args": {}},'
                 '{"step": "删掉生产库再恢复", "tool": "drop_database",'
                 ' "args": {"db": "orders"}, "why": "彻底重来"}'
                 ']}')
        patch = LLMPlanner(FakeLLM(risky)).replan(SNAPSHOT, REASON)

        tools = [s["tool"] for s in patch.value["steps"]]
        self.assertEqual(tools, ["drop_all_nodes", "drop_database"])
        self.assertEqual(len(patch.value["steps"]), 2, "一步都不许被悄悄删掉")

    # ㉑ Markdown 围栏 / 裸列表 / 解释性前缀都能解析
    def test_21_lenient_container_strict_content(self):
        fenced = '好的，计划如下：\n```json\n' + GOOD_JSON + '\n```\n以上。'
        patch = LLMPlanner(FakeLLM(fenced)).replan(SNAPSHOT, REASON)
        self.assertIsNotNone(patch)
        self.assertEqual(len(patch.value["steps"]), 2)

        bare = '["先看指标", "再回切 5% 流量"]'
        patch = LLMPlanner(FakeLLM(bare)).replan(SNAPSHOT, REASON)
        self.assertEqual([s["step"] for s in patch.value["steps"]], ["先看指标", "再回切 5% 流量"])
        self.assertEqual(patch.value["steps"][0]["tool"], "")

    # ㉒ 可调用对象当 LLM 也接住（不只认 complete）
    def test_22_callable_llm_supported(self):
        planner = LLMPlanner(lambda prompt: GOOD_JSON)
        patch = planner.replan(SNAPSHOT, REASON)
        self.assertIsNotNone(patch)
        self.assertEqual(len(patch.value["steps"]), 2)

    # ㉓ Prompt 里带死锁原因、步数上限与 JSON 形状要求
    def test_23_prompt_shape(self):
        llm = FakeLLM(GOOD_JSON)
        LLMPlanner(llm, max_steps=3).replan(SNAPSHOT, REASON)
        prompt = llm.prompts[0]

        self.assertIn(REASON, prompt)
        self.assertIn("最多 3 步", prompt)
        self.assertIn('"steps"', prompt)
        self.assertIn("只输出一个 JSON", prompt)
        self.assertIn("order-svc", prompt)          # 快照里的目标被压缩进来了
        self.assertIn("query_release", prompt)      # 工具回包也在

    # ㉔ 构造期校验：没给 LLM / 给了个不会说话的
    def test_24_constructor_validation(self):
        with self.assertRaises(ValueError):
            LLMPlanner(None)
        with self.assertRaises(TypeError):
            LLMPlanner(object())

    # ㉕ plan 的形状就是 Runtime 惯用的 {"steps": [...]}
    def test_25_plan_shape_matches_runtime_convention(self):
        patch = StaticDAGPlanner({"publish": publish_dag()},
                                 default="publish").replan({}, REASON)
        self.assertIsInstance(patch.value, dict)
        self.assertIsInstance(patch.value["steps"], list)
        self.assertTrue(patch.value["steps"])
        # 顶层的几个键固定，方便 Builder / 复盘脚本按 key 读
        self.assertEqual(set(patch.value),
                         {"source", "dag", "reason", "why", "steps"})


# ══════════════════════════════════════════════════════════════
# C. 与 Runtime 集成
# ══════════════════════════════════════════════════════════════
class TestRuntimeIntegration(unittest.TestCase):
    # ㉖ 真 Loop(planner=StaticDAGPlanner(...)) 能构造
    def test_26_loop_accepts_static_dag_planner(self):
        planner = StaticDAGPlanner({"publish": publish_dag()}, default="publish")
        loop, _llm, _dispatcher = make_loop(planner)
        self.assertIsNotNone(loop)
        self.assertEqual(loop.warnings, [], "工具清单解析与 planner 校验都不该告警")

    # ㉗ 不符合契约的对象 → TypeError（Runtime 构造期校验）
    def test_27_loop_rejects_non_conforming_planner(self):
        from builder import PromptBuilder
        from core.loop import Loop

        class NoReplan:
            pass

        class BadReplan:
            replan = "不是可调用的"

        for bad in (NoReplan(), BadReplan(), object(), 42):
            with self.assertRaises(TypeError, msg=repr(bad)):
                Loop(PromptBuilder(total_tokens=4000), deadlock_llm(), FakeDispatcher(),
                     planner=bad)
        # 反证：真实现能过同一道校验
        Loop(PromptBuilder(total_tokens=4000), deadlock_llm(), FakeDispatcher(),
             planner=LLMPlanner(FakeLLM(GOOD_JSON)))

    # ㉘ 端到端：死锁 → planner 被调用一次 → 之后判 NO_PROGRESS
    def test_28_deadlock_replans_once_then_no_progress(self):
        class CountingPlanner:
            """返回 None 的合法实现：钉住「想不出也不许重试」。"""

            def __init__(self):
                self.calls: list[str] = []

            def replan(self, snapshot, reason):
                self.calls.append(reason)
                return None

        planner = CountingPlanner()
        loop, _llm, dispatcher = make_loop(planner)
        from core.context import ContextHandle

        ctx = ContextHandle()
        result = loop.run("查 order-svc 的发布状态", ctx, make_exec(max_steps=24))

        self.assertEqual(result.term_reason, TermReason.NO_PROGRESS, result.text)
        self.assertTrue(result.partial)
        self.assertEqual(len(planner.calls), 1, "只强制一次 replan，绝不无限重试")
        self.assertIn("L1", planner.calls[0])
        self.assertGreaterEqual(dispatcher.calls.__len__(), 8, "replan 前后各跑满一个窗口")
        self.assertTrue(any("replan" in str(m.get("text") or "")
                            for m in ctx.get("history")))

    # ㉙ 端到端：planner 返回的 Patch 真被应用到 Context
    def test_29_patch_applied_to_context(self):
        planner = StaticDAGPlanner({"publish": publish_dag()}, default="publish")
        loop, _llm, _dispatcher = make_loop(planner)
        from core.context import ContextHandle

        ctx = ContextHandle()
        result = loop.run("把 order-svc 发到 1.4.2", ctx, make_exec(max_steps=24))

        plan_patches = [p for p in ctx.patch_log if p.path == "plan"]
        self.assertTrue(plan_patches, "planner 的 Patch 必须出现在 ctx.patch_log")
        self.assertEqual(plan_patches[0].by, Owner.PLANNER)
        self.assertEqual(plan_patches[0], planner.last_patch)

        plan = ctx.get("plan")
        self.assertEqual(plan["dag"], "publish")
        self.assertEqual(len(plan["steps"]), 4)
        self.assertEqual(result.term_reason, TermReason.NO_PROGRESS, result.text)
        self.assertEqual(len(planner.notes), 1, "planner 只被调用一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
