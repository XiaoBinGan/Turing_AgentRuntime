"""tests/test_input_schema.py — 工具入参契约（W17）。

要钉死的是「静默错误」这一整类故障：

    模型把 ``lines`` 写成 ``limit``
      → 老版本：adapter 发保守 schema（模型只能猜参数名）+ Dispatcher 不校验
      → 工具 ``args.get()`` 兜底拿到空值 → 返回空结果 → 模型基于空结果一本正经分析

两端补齐之后的不变量：

1. ``Dispatcher.invoke`` 在**任何治理判定之前**校验 ``manifest.input_schema``；
2. 校验失败 → ``failed``，``retryable=True``，错误信息带**具体字段名**；
3. ``input_schema=None`` → 不校验（老工具向后兼容）；
4. 校验器自身崩了 → ``failed``，``retryable=False``（fail-closed）；
5. adapter 有契约就发真 schema，没契约才退回保守形状；
6. 扁平参数**不许**被 ``{"args": {...}}`` 的解包逻辑误伤。

跑法：``python3 -m unittest tests.test_input_schema -v``
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.openai_compat import TOOL_PARAMETERS, OpenAICompatAdapter   # noqa: E402
from contracts import (                                                  # noqa: E402
    Budget,
    ExecCtx,
    Principal,
    RiskTier,
    SideEffect,
    ToolManifest,
    validate_against_schema,
)
from plane.tool import Dispatcher, Normalizer, Policy, ToolRegistry       # noqa: E402


# ══════════════════════════════════════════════════════════════
# 公共 fixture
# ══════════════════════════════════════════════════════════════
#: 与 demo/fixtures.py 里 query_logs 完全同形：真实故障就发生在这个形状上
LOGS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "lines": {"type": "integer", "minimum": 1, "maximum": 2000},
    },
    "required": ["service"],
    "additionalProperties": False,
}


def _schema(**extra) -> dict:
    """以 LOGS_SCHEMA 为底，覆盖少量键（避免每个用例都抄一遍形状）。"""
    out = json.loads(json.dumps(LOGS_SCHEMA))
    out.update(extra)
    return out


class _Recorder:
    """记录调用次数与收到的参数 —— 「工具有没有被执行」必须可断言。"""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.payload = {"rows": 3, "service": "api"} if payload is None else payload

    def __call__(self, args: dict, e: ExecCtx):
        self.calls.append(dict(args))
        return self.payload

    @property
    def count(self) -> int:
        return len(self.calls)


def _ctx(budget: Budget | None = None, **kw) -> ExecCtx:
    return ExecCtx(
        principal=Principal(user_id="u_test", tenant="acme",
                            scopes=frozenset({"tool.read", "tool.write"})),
        budget=budget if budget is not None else Budget(),
        deadline=0,
        **kw,
    )


def _manifest(name: str = "query_logs", schema: dict | None = LOGS_SCHEMA, **kw) -> ToolManifest:
    return ToolManifest(
        name=name,
        side_effect=kw.pop("side_effect", SideEffect.READ_ONLY),
        risk_tier=kw.pop("risk_tier", RiskTier.T0_SAFE),
        description=kw.pop("description", "按服务查询最近日志"),
        input_schema=schema,
        **kw,
    )


def _dispatcher(manifest: ToolManifest, fn, *, policy: Policy | None = None) -> Dispatcher:
    """每个用例独立 registry + dispatcher + 临时 artifact 目录（互不污染）。"""
    reg = ToolRegistry()
    reg.register(manifest, fn)
    return Dispatcher(
        reg,
        policy=policy if policy is not None else Policy(),
        normalizer=Normalizer(artifact_dir=tempfile.mkdtemp(prefix="w17-artifacts-")),
    )


# ══════════════════════════════════════════════════════════════
# 一、Dispatcher 侧：入参校验的位置与语义
# ══════════════════════════════════════════════════════════════
class TestDispatcherValidation(unittest.TestCase):
    """① 校验在 approval 之前；失败可重试、带字段名、不消耗预算。"""

    # ── 1. 合法参数照常执行 ─────────────────────────────────────
    def test_valid_args_execute_tool(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"service": "api", "lines": 200}, _ctx())

        self.assertEqual(out.kind, "ok")
        self.assertIsNotNone(out.result)
        self.assertEqual(fn.count, 1)
        self.assertEqual(fn.calls[0], {"service": "api", "lines": 200})
        self.assertFalse(out.retryable)

    # ── 2. 未知字段（真实故障现场：limit 而非 lines）───────────
    def test_unknown_field_rejected_and_named(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"service": "api", "limit": 200}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertTrue(out.reason.startswith("invalid args: "), out.reason)
        # 错误信息必须点名具体字段，否则模型改不了
        self.assertIn("limit", out.reason)
        self.assertIn("args.limit", out.reason)
        self.assertEqual(fn.count, 0, "参数非法时工具绝不该被执行")

    # ── 3. 缺必填字段 ──────────────────────────────────────────
    def test_missing_required_field_rejected(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"lines": 200}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.service", out.reason)
        self.assertIn("缺少必填字段", out.reason)
        self.assertEqual(fn.count, 0)

    # ── 4. 类型错误：字符串 "200" 不是 integer ─────────────────
    def test_wrong_type_rejected(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"service": "api", "lines": "200"}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.lines", out.reason)
        self.assertIn("integer", out.reason)
        self.assertIn("str", out.reason)
        self.assertEqual(fn.count, 0)

    # ── 5. 越界 ────────────────────────────────────────────────
    def test_out_of_range_rejected(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"service": "api", "lines": 99999}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.lines", out.reason)
        self.assertIn("maximum 2000", out.reason)

    # ── 5b. bool 不是 integer（Python 里 isinstance(True, int) 为真）──
    def test_boolean_is_not_integer(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"service": "api", "lines": True}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("boolean", out.reason)

    # ── 6. input_schema=None → 不校验（向后兼容）──────────────
    def test_none_schema_skips_validation(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(schema=None), fn)
        # 任意字段都能进 —— 老工具就是这么活的，不许因为它们没契约就报错
        out = d.invoke("query_logs", {"whatever": 1, "service": "api"}, _ctx())

        self.assertEqual(out.kind, "ok")
        self.assertEqual(fn.count, 1)
        self.assertEqual(fn.calls[0], {"whatever": 1, "service": "api"})

    # ── 7. 坏 schema（属性定义不是 dict）→ 不崩 ───────────────
    def test_broken_schema_property_does_not_crash(self):
        """校验器「读不懂就往下降级」：这不是放行 schema 校验，而是不假装懂。

        ``{"x": "not-a-dict"}`` 不是合法 JSON Schema 属性定义，contracts 的实现会
        跳过它（只对能理解的键报错）。本用例锁的是**不崩**这条底线；真正危险的
        「校验器自身抛异常」由下一个用例覆盖（那里必须 fail-closed）。
        """
        fn = _Recorder()
        broken = {"type": "object", "properties": {"x": "not-a-dict"}}
        d = _dispatcher(_manifest(schema=broken), fn)
        out = d.invoke("query_logs", {"x": 1}, _ctx())

        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(fn.count, 1)

    # ── 7b. 校验器抛异常 → fail-closed ────────────────────────
    def test_schema_error_fails_closed(self):
        class _BoomDict(dict):
            def get(self, *a, **k):        # 校验器的第一句话就会踩到
                raise RuntimeError("boom-schema")

        fn = _Recorder()
        d = _dispatcher(_manifest(schema=_BoomDict({"type": "object"})), fn)
        out = d.invoke("query_logs", {"service": "api"}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("input schema error", out.reason)
        self.assertIn("boom-schema", out.reason)
        self.assertFalse(out.retryable, "schema 写坏属于配置问题，重试无意义")
        self.assertEqual(fn.count, 0, "fail-closed：校验不了就不许执行")

    # ── 8. ★ 校验在 approval 之前 ──────────────────────────────
    def test_validation_runs_before_approval(self):
        """T2 工具 + 非法参数 → failed，而不是 needs_confirmation。

        这是顺序的直接证据：若校验排在 approval 之后，一次参数都不合法的调用会先
        变成「请人工确认」，人批准完才发现参数根本是错的。
        """
        fn = _Recorder({"restarted": True})
        m = _manifest(name="restart_service", schema={
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
            "additionalProperties": False,
        }, side_effect=SideEffect.MUTATING, risk_tier=RiskTier.T2_HIGH,
            idempotent=False, required_scope="tool.write")
        d = _dispatcher(m, fn)

        out = d.invoke("restart_service", {"target": "api"}, _ctx())
        self.assertEqual(out.kind, "failed", "非法参数必须在审批之前被拦下")
        self.assertNotEqual(out.kind, "needs_confirmation")
        self.assertIn("target", out.reason)
        self.assertEqual(fn.count, 0)

        # 对照组：同样没 approval，但参数合法 → 这时才该打扰人
        ok_args = d.invoke("restart_service", {"service": "api"}, _ctx())
        self.assertEqual(ok_args.kind, "needs_confirmation")

    # ── 8b. 校验也在 T3 拒绝之前（参数错先报参数错）──────────
    def test_validation_runs_before_risk_tier(self):
        fn = _Recorder()
        m = _manifest(name="nuke", risk_tier=RiskTier.T3_CRITICAL,
                      side_effect=SideEffect.IRREVERSIBLE)
        d = _dispatcher(m, fn)
        out = d.invoke("nuke", {"limit": 1}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("invalid args", out.reason)
        self.assertEqual(fn.count, 0)

    # ── 8c. 未知工具优先于参数校验 ────────────────────────────
    def test_unknown_tool_reported_before_args(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("no_such_tool", {"limit": 1}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("unknown tool", out.reason)
        self.assertNotIn("invalid args", out.reason)

    # ── 9. 校验失败不消耗 tool_calls 预算，也不执行工具 ───────
    def test_invalid_args_does_not_consume_budget(self):
        fn = _Recorder()
        budget = Budget(tool_calls_total=5)
        d = _dispatcher(_manifest(), fn)

        out = d.invoke("query_logs", {"limit": 200}, _ctx(budget=budget))

        self.assertEqual(out.kind, "failed")
        self.assertEqual(budget.tool_calls_used, 0, "被入参拦下的调用不占用预算")
        self.assertEqual(fn.count, 0)
        self.assertEqual(d._turn_calls, {}, "限流计数也不该动")

        # 同一 budget 继续可用：修正参数后真的能跑
        ok = d.invoke("query_logs", {"service": "api", "lines": 200}, _ctx(budget=budget))
        self.assertEqual(ok.kind, "ok")
        self.assertEqual(budget.tool_calls_used, 1)
        self.assertEqual(fn.count, 1)

    # ── 10. retryable=True（可纠正的错误）────────────────────
    def test_invalid_args_is_retryable(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        out = d.invoke("query_logs", {"service": "api", "limit": 200}, _ctx())

        self.assertTrue(out.retryable, "模型看到字段名后应当能改参数重试")

    # ── 10b. 失败进 trace_log（可观测，而不是消失）────────────
    def test_invalid_args_is_traced(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(), fn)
        d.invoke("query_logs", {"service": "api", "limit": 200}, _ctx())

        self.assertEqual(len(d.trace_log), 1)
        rec = d.trace_log[0]
        self.assertEqual(rec["tool"], "query_logs")
        self.assertEqual(rec["kind"], "failed")
        self.assertIn("limit", rec["reason"])
        self.assertEqual(d.stats()["kinds"], {"failed": 1})


# ══════════════════════════════════════════════════════════════
# 二、adapter 侧：真 schema 与解包边界
# ══════════════════════════════════════════════════════════════
class TestAdapterSchema(unittest.TestCase):
    """② 有契约发真 schema；扁平参数不被误包；确定性。"""

    # ── 11. 有 input_schema → parameters 就是它 ────────────────
    def test_declared_schema_is_published_verbatim(self):
        m = _manifest()
        schema = OpenAICompatAdapter.tool_schema(m)

        self.assertEqual(schema["function"]["parameters"], LOGS_SCHEMA)
        self.assertNotIn("args", schema["function"]["parameters"]["properties"])
        self.assertEqual(schema["function"]["name"], "query_logs")
        self.assertEqual(schema["type"], "function")

    # ── 12. 无 input_schema → 保守形状 ────────────────────────
    def test_no_schema_falls_back_to_conservative(self):
        schema = OpenAICompatAdapter.tool_schema(_manifest(schema=None))

        self.assertEqual(schema["function"]["parameters"], TOOL_PARAMETERS)
        self.assertEqual(list(schema["function"]["parameters"]["properties"]), ["args"])

    # ── 13. 扁平参数不被错误解包 ──────────────────────────────
    def test_flat_args_are_not_unwrapped(self):
        decoded = OpenAICompatAdapter._decode_args(
            json.dumps({"service": "api", "lines": 200}, ensure_ascii=False))

        self.assertEqual(decoded, {"service": "api", "lines": 200})
        self.assertNotIn("args", decoded)

    # ── 14. 老形状 {"args": {...}} 仍被解包 ───────────────────
    def test_legacy_args_envelope_is_unwrapped(self):
        decoded = OpenAICompatAdapter._decode_args('{"args": {"service": "api"}}')

        self.assertEqual(decoded, {"service": "api"})

    # ── 15. {"args":{...}, "other":1} 不解包（长度≠1）────────
    def test_envelope_with_extra_key_is_not_unwrapped(self):
        raw = {"args": {"service": "api"}, "other": 1}
        decoded = OpenAICompatAdapter._decode_args(raw)

        self.assertEqual(decoded, raw)
        self.assertIn("args", decoded)

    # ── 15b. {"args": "not-a-dict"} 不解包 ───────────────────
    def test_envelope_with_non_dict_args_is_not_unwrapped(self):
        decoded = OpenAICompatAdapter._decode_args('{"args": "service=api"}')

        self.assertEqual(decoded, {"args": "service=api"})

    # ── 18. 确定性：两次 tool_schema() 相等 ──────────────────
    def test_tool_schema_is_deterministic(self):
        m = _manifest()
        first = OpenAICompatAdapter.tool_schema(m)
        second = OpenAICompatAdapter.tool_schema(m)

        self.assertEqual(first, second)
        # 且各自是独立副本：下游手滑不该污染 manifest 里那份共享声明
        self.assertIsNot(first["function"]["parameters"], second["function"]["parameters"])
        first["function"]["parameters"]["properties"]["lines"]["maximum"] = 1
        self.assertEqual(m.input_schema["properties"]["lines"]["maximum"], 2000)
        self.assertEqual(second["function"]["parameters"]["properties"]["lines"]["maximum"], 2000)

    # ── 18b. tools_schema 整表一致 ───────────────────────────
    def test_tools_schema_list_stable(self):
        ms = [_manifest(), _manifest(name="query_alerts", schema={
            "type": "object", "properties": {"service": {"type": "string"}},
            "required": ["service"], "additionalProperties": False})]
        self.assertEqual(OpenAICompatAdapter.tools_schema(ms),
                         OpenAICompatAdapter.tools_schema(ms))
        self.assertEqual([s["function"]["name"] for s in OpenAICompatAdapter.tools_schema(ms)],
                         ["query_logs", "query_alerts"])


# ══════════════════════════════════════════════════════════════
# 三、端到端：adapter 发出的 schema → 模型参数 → Dispatcher
# ══════════════════════════════════════════════════════════════
class TestEndToEnd(unittest.TestCase):
    """③ 两个半场接起来才算修好：模型按真 schema 出参数必须一次过校验。"""

    @staticmethod
    def _adapter() -> OpenAICompatAdapter:
        # 只用到协议转换的静态能力，不会发网络请求（base_url 不需要可达）
        return OpenAICompatAdapter("http://127.0.0.1:9/v1", "mock-model")

    # ── 16. 按真 schema 出的参数校验通过 ──────────────────────
    def test_schema_driven_args_pass_validation(self):
        adapter = self._adapter()
        m = _manifest()
        fn = _Recorder()
        d = _dispatcher(m, fn)

        published = adapter.tool_schema(m)["function"]["parameters"]
        # 模型看到的是扁平 properties —— 它照着填
        self.assertIn("lines", published["properties"])
        model_output = json.dumps({"service": "api", "lines": 200})
        args = adapter._decode_args(model_output)

        out = d.invoke("query_logs", args, _ctx())
        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(fn.calls[0], {"service": "api", "lines": 200})

    # ── 17. 幻觉参数名被拦，且错误可指导修正 ─────────────────
    def test_hallucinated_args_are_blocked_with_actionable_error(self):
        adapter = self._adapter()
        m = _manifest()
        fn = _Recorder()
        d = _dispatcher(m, fn)

        hallucinated = adapter._decode_args('{"service": "api", "limit": 200}')
        out = d.invoke("query_logs", hallucinated, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertTrue(out.retryable)
        self.assertIn("args.limit", out.reason)     # 点名字段
        self.assertEqual(fn.count, 0, "空结果必须变成错误，不许静默通过")

        # 「可指导」的实证：按错误信息把 limit 换回 lines，立刻成功
        fixed = {k: v for k, v in hallucinated.items() if k != "limit"}
        fixed["lines"] = 200
        retry = d.invoke("query_logs", fixed, _ctx())
        self.assertEqual(retry.kind, "ok", retry.reason)
        self.assertEqual(fn.count, 1)

    # ── 17b. 无契约工具走保守形状时，端到端仍可用 ─────────────
    def test_conservative_path_still_works_end_to_end(self):
        adapter = self._adapter()
        m = _manifest(schema=None)
        fn = _Recorder()
        d = _dispatcher(m, fn)

        params = adapter.tool_schema(m)["function"]["parameters"]
        self.assertEqual(params, TOOL_PARAMETERS)
        args = adapter._decode_args('{"args": {"service": "api"}}')

        out = d.invoke("query_logs", args, _ctx())
        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(fn.calls[0], {"service": "api"})


# ══════════════════════════════════════════════════════════════
# 四、结构化参数：嵌套对象 / 数组
# ══════════════════════════════════════════════════════════════
class TestStructuredArgs(unittest.TestCase):
    """④ 契约子集必须覆盖真实工具的复合形状。"""

    FILTERED = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "filter": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["error", "warn", "info"]},
                    "since": {"type": "string"},
                },
                "required": ["level"],
                "additionalProperties": False,
            },
        },
        "required": ["service"],
        "additionalProperties": False,
    }

    BATCH = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "count": {"type": "integer"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    # ── 19. 嵌套对象：合法通过 / 非法拦下（路径可定位）────────
    def test_nested_object_valid(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(schema=self.FILTERED), fn)
        out = d.invoke("query_logs",
                       {"service": "api", "filter": {"level": "error"}}, _ctx())

        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(fn.count, 1)

    def test_nested_object_enum_violation(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(schema=self.FILTERED), fn)
        out = d.invoke("query_logs",
                       {"service": "api", "filter": {"level": "fatal"}}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.filter.level", out.reason)
        self.assertIn("枚举", out.reason)
        self.assertEqual(fn.count, 0)

    def test_nested_object_missing_required(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(schema=self.FILTERED), fn)
        out = d.invoke("query_logs", {"service": "api", "filter": {}}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.filter.level", out.reason)
        self.assertIn("缺少必填字段", out.reason)

    def test_nested_object_unknown_field(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(schema=self.FILTERED), fn)
        out = d.invoke("query_logs",
                       {"service": "api", "filter": {"level": "error", "limit": 5}}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.filter.limit", out.reason)

    # ── 20. 数组参数：合法通过 / 元素级错误带下标 ─────────────
    def test_array_items_valid(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(name="batch", schema=self.BATCH), fn)
        out = d.invoke("batch", {"items": [{"id": "a", "count": 1}, {"id": "b"}]}, _ctx())

        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(fn.count, 1)

    def test_array_item_missing_field_reports_index(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(name="batch", schema=self.BATCH), fn)
        out = d.invoke("batch", {"items": [{"id": "a"}, {"count": 2}]}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.items[1].id", out.reason)
        self.assertEqual(fn.count, 0)

    def test_array_item_wrong_type_reports_index(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(name="batch", schema=self.BATCH), fn)
        out = d.invoke("batch", {"items": [{"id": "a", "count": "two"}]}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("args.items[0].count", out.reason)

    def test_array_not_a_list(self):
        fn = _Recorder()
        d = _dispatcher(_manifest(name="batch", schema=self.BATCH), fn)
        out = d.invoke("batch", {"items": {"id": "a"}}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("期望 array", out.reason)


# ══════════════════════════════════════════════════════════════
# 五、契约校验器本身（只读使用，不重写）
# ══════════════════════════════════════════════════════════════
class TestValidatorContract(unittest.TestCase):
    """校验器来自 contracts.py（冻结），这里只钉 Dispatcher 依赖的那几条语义。"""

    def test_none_schema_returns_no_errors(self):
        self.assertEqual(validate_against_schema(None, {"a": 1}), [])

    def test_non_dict_schema_reports_error(self):
        errs = validate_against_schema(["not", "a", "dict"], {"a": 1})
        self.assertTrue(errs)
        self.assertIn("schema 必须是 dict", errs[0])

    def test_error_messages_carry_field_path(self):
        errs = validate_against_schema(LOGS_SCHEMA, {"service": "api", "limit": 1})
        self.assertEqual(len(errs), 1)
        self.assertIn("args.limit", errs[0])


if __name__ == "__main__":
    unittest.main()
