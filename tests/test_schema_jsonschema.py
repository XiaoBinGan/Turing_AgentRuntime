"""tests/test_schema_jsonschema.py — 入参校验器换成 jsonschema 之后**新获得**的语义。

分工（别重复测）：

* ``tests/test_input_schema.py``（37 例，冻结）继续锁**兼容面**：老错误文案
  （``args.limit: 未声明的字段`` / ``args.service: 缺少必填字段`` / ``args.lines: 9999 > maximum 1000``）、
  Dispatcher 的「校验先于审批」「失败可重试」「校验器崩了 fail-closed」。
* 本文件只锁**手写子集做不到、现在能做到**的那一类。手写版支持
  ``type/properties/required/additionalProperties/items/enum/minimum/maximum/minLength/maxLength``，
  其余关键字一律被静默忽略 —— 而「静默忽略」正是「模型瞎传参数、工具返回空结果」的入口：

      1  oneOf 只允许一种形态              7  dependentRequired
      2  anyOf 允许多种                    8  multipleOf
      3  allOf 全部满足                    9  uniqueItems
      4  not 反向约束                     10  $ref + definitions / $defs
      5  pattern 正则                     11  复杂嵌套（对象数组 + 每项独立 schema）
      6  patternProperties 按 key 名匹配   12  错误消息仍带精确路径（args.rows[1].id）
     13  没装 jsonschema → 退化到手写实现且基础用例仍通过
     14  非法 schema（{"type": "not-a-type"}）→ 明确报错，不静默通过
     15  format（date-time / uuid）        16  校验器缓存（同 schema 复用，不串味）

跑法：``.venv/bin/python -m unittest tests.test_schema_jsonschema -v``
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import contracts                                                          # noqa: E402
from contracts import Budget, ExecCtx, Principal, SideEffect, ToolManifest, validate_against_schema  # noqa: E402
from plane.tool import Dispatcher, Normalizer, Policy, ToolRegistry        # noqa: E402


def _errs(schema: dict, args, path: str = "args") -> list[str]:
    return validate_against_schema(schema, args, path)


def _ctx() -> ExecCtx:
    return ExecCtx(
        principal=Principal(user_id="u_test", tenant="acme",
                            scopes=frozenset({"tool.read", "tool.write"})),
        budget=Budget(),
        deadline=0,
    )


#: 与 test_input_schema.py 同形的「真实故障现场」schema：兼容面的基准
LOGS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "lines": {"type": "integer", "minimum": 1, "maximum": 2000},
    },
    "required": ["service"],
    "additionalProperties": False,
}

#: 复杂嵌套：对象数组，每项自己有独立 schema（含唯一性 / 枚举 / 额外属性）
ROWS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "uniqueItems": True},
                    "kind": {"enum": ["metric", "log"]},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rows"],
    "additionalProperties": False,
}


# ══════════════════════════════════════════════════════════════
# 一、组合关键字：oneOf / anyOf / allOf / not
# ══════════════════════════════════════════════════════════════
class TestCombinators(unittest.TestCase):
    """① 手写版完全不看这四个键：写进 schema 等于没写。"""

    ONE_OF: dict = {
        "oneOf": [
            {"type": "object", "properties": {"text": {"type": "string"}},
             "required": ["text"]},
            {"type": "object", "properties": {"id": {"type": "integer"}},
             "required": ["id"]},
        ],
    }

    def test_01_oneOf_exactly_one_branch_passes(self):
        self.assertEqual(_errs(self.ONE_OF, {"text": "hi"}), [])
        self.assertEqual(_errs(self.ONE_OF, {"id": 7}), [])

    def test_02_oneOf_rejects_when_no_branch_matches(self):
        errs = _errs(self.ONE_OF, {"other": 1})
        self.assertEqual(len(errs), 1)
        self.assertIn("oneOf", errs[0])
        self.assertIn("args", errs[0])

    def test_03_oneOf_rejects_ambiguous_shape(self):
        """同时满足两种形态 → 也是错（这正是 oneOf 与 anyOf 的区别）。"""
        errs = _errs(self.ONE_OF, {"text": "hi", "id": 7})
        self.assertEqual(len(errs), 1)
        self.assertIn("多种", errs[0])

    def test_04_anyOf_accepts_several_branches(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(_errs(schema, "service=api"), [])
        self.assertEqual(_errs(schema, 42), [])
        errs = _errs(schema, ["not", "allowed"])
        self.assertEqual(len(errs), 1)
        self.assertIn("anyOf", errs[0])

    def test_05_allOf_requires_every_branch(self):
        schema = {"allOf": [{"type": "integer"}, {"minimum": 10}, {"maximum": 20}]}
        self.assertEqual(_errs(schema, 15), [])

        low = _errs(schema, 5)
        self.assertEqual(len(low), 1)
        self.assertIn("< minimum 10", low[0])       # 文案与老格式一致

        high = _errs(schema, 25)
        self.assertIn("> maximum 20", high[0])

    def test_06_allOf_accumulates_field_errors(self):
        """allOf 里的 required 必须逐条报 —— 一次报全，模型才不用挤牙膏式试错。"""
        schema = {
            "type": "object",
            "properties": {"a": {}, "b": {}},
            "allOf": [{"required": ["a"]}, {"required": ["b"]}],
        }
        self.assertEqual(_errs(schema, {"a": 1, "b": 2}), [])
        errs = _errs(schema, {})
        self.assertIn("args.a: 缺少必填字段", errs)
        self.assertIn("args.b: 缺少必填字段", errs)

    def test_07_not_reverses_a_constraint(self):
        schema = {"type": "string", "not": {"pattern": "^internal-"}}
        self.assertEqual(_errs(schema, "public-api"), [])
        errs = _errs(schema, "internal-billing")
        self.assertEqual(len(errs), 1)
        self.assertIn("not", errs[0])


# ══════════════════════════════════════════════════════════════
# 二、字符串 / 对象键名 / 数值 / 数组的新约束
# ══════════════════════════════════════════════════════════════
class TestNewConstraints(unittest.TestCase):
    """② pattern / patternProperties / dependentRequired / multipleOf / uniqueItems。"""

    def test_08_pattern_regex_is_enforced(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": "^[a-z]+$"}},
            "additionalProperties": False,
        }
        self.assertEqual(_errs(schema, {"name": "billing"}), [])

        errs = _errs(schema, {"name": "Billing-Service"})
        self.assertEqual(len(errs), 1)
        self.assertIn("args.name", errs[0])
        self.assertIn("pattern", errs[0])

    def test_09_patternProperties_matches_by_key_name(self):
        """key 名匹配 ``^x-`` 的字段走子 schema；其余仍受更宽的 schema 约束。"""
        schema = {
            "type": "object",
            "patternProperties": {"^x-": {"type": "string"}},
            "additionalProperties": False,
        }
        self.assertEqual(_errs(schema, {"x-trace-id": "abc", "x-tenant": "acme"}), [])

        wrong_type = _errs(schema, {"x-trace-id": 42})
        self.assertEqual(len(wrong_type), 1)
        self.assertIn("args.x-trace-id", wrong_type[0])
        self.assertIn("期望 string", wrong_type[0])

        # 关键差别：patternProperties 匹配上的键**不算**额外属性
        extra = _errs(schema, {"trace-id": "abc"})
        self.assertEqual(len(extra), 1)
        self.assertIn("args.trace-id", extra[0])
        self.assertIn("未声明的字段", extra[0])

    def test_10_dependentRequired_only_when_trigger_present(self):
        schema = {
            "type": "object",
            "properties": {"password": {"type": "string"},
                           "confirm": {"type": "string"}},
            "dependentRequired": {"password": ["confirm"]},
        }
        self.assertEqual(_errs(schema, {}), [])
        self.assertEqual(_errs(schema, {"password": "a", "confirm": "a"}), [])

        errs = _errs(schema, {"password": "a"})
        self.assertEqual(len(errs), 1)
        self.assertIn("args.confirm", errs[0])
        self.assertIn("缺少必填字段", errs[0])
        self.assertIn("password", errs[0])          # 指出是谁触发的依赖

    def test_11_multipleOf(self):
        schema = {"type": "object", "properties": {"step": {"type": "integer",
                                                            "multipleOf": 30}}}
        self.assertEqual(_errs(schema, {"step": 90}), [])

        errs = _errs(schema, {"step": 45})
        self.assertEqual(len(errs), 1)
        self.assertIn("args.step", errs[0])
        self.assertIn("multipleOf", errs[0])

    def test_12_uniqueItems(self):
        schema = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
        self.assertEqual(_errs(schema, ["a", "b"]), [])

        errs = _errs(schema, ["a", "b", "a"])
        self.assertEqual(len(errs), 1)
        self.assertIn("uniqueItems", errs[0])
        self.assertIn("'a'", errs[0])               # 报出重复的那个值

    def test_13_format_datetime_and_uuid(self):
        """format 默认只是注解；这里必须**真校验**（否则 format 又是一句空话）。"""
        dt = {"type": "string", "format": "date-time"}
        self.assertEqual(_errs(dt, "2024-01-02T03:04:05Z"), [])
        self.assertEqual(_errs(dt, "2024-01-02T03:04:05.123+08:00"), [])
        bad = _errs(dt, "yesterday")
        self.assertEqual(len(bad), 1)
        self.assertIn("format", bad[0])

        uuid_schema = {"type": "string", "format": "uuid"}
        self.assertEqual(_errs(uuid_schema, "8f14e45f-ea3a-4b8f-9f37-2f8a1c1d0001"), [])
        self.assertIn("format", _errs(uuid_schema, "not-a-uuid")[0])


# ══════════════════════════════════════════════════════════════
# 三、$ref 与深层路径
# ══════════════════════════════════════════════════════════════
class TestRefsAndPaths(unittest.TestCase):
    """③ 同一份 shape 复用一次定义；错误路径精确到下标与字段。"""

    REF_DEFINITIONS: dict = {
        "type": "object",
        "properties": {"user": {"$ref": "#/definitions/user"}},
        "required": ["user"],
        "additionalProperties": False,
        "definitions": {
            "user": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    }

    def test_14_ref_to_local_definitions(self):
        self.assertEqual(_errs(self.REF_DEFINITIONS, {"user": {"id": "u_1"}}), [])

        errs = _errs(self.REF_DEFINITIONS, {"user": {}})
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0], "args.user.id: 缺少必填字段")

        wrong_type = _errs(self.REF_DEFINITIONS, {"user": {"id": 1}})
        self.assertIn("args.user.id: 期望 string，收到 int", wrong_type)

    def test_15_ref_to_dollar_defs(self):
        """2020-12 的正名是 ``$defs``（``definitions`` 只是历史别名）。"""
        schema = {
            "$defs": {"peer": {"type": "string", "minLength": 2}},
            "type": "object",
            "properties": {"peer": {"$ref": "#/$defs/peer"}},
        }
        self.assertEqual(_errs(schema, {"peer": "p1"}), [])
        errs = _errs(schema, {"peer": "p"})
        self.assertIn("args.peer: 长度 1 < minLength 2", errs)

    def test_16_deep_nested_object_array(self):
        """复杂嵌套：合法全过、非法逐层报（数组下标 + 字段名都在路径里）。"""
        self.assertEqual(
            _errs(ROWS_SCHEMA, {"rows": [{"id": 1, "tags": ["a", "b"], "kind": "log"},
                                         {"id": 2}]}), [])

        self.assertIn("args.rows[0].tags: 数组元素必须唯一（uniqueItems），重复元素 'a'",
                      _errs(ROWS_SCHEMA, {"rows": [{"id": 1, "tags": ["a", "a"]}]}))
        self.assertIn("args.rows[0].id: 期望 integer，收到 str",
                      _errs(ROWS_SCHEMA, {"rows": [{"id": "one"}]}))
        self.assertIn("args.rows: 元素数 0 < minItems 1", _errs(ROWS_SCHEMA, {"rows": []}))
        self.assertIn("args.rows[0].oops: 未声明的字段（schema 禁止额外属性）",
                      _errs(ROWS_SCHEMA, {"rows": [{"id": 1, "oops": 2}]}))
        self.assertIn("args.rows[1].kind: 值 'trace' 不在枚举 ['metric', 'log'] 内",
                      _errs(ROWS_SCHEMA, {"rows": [{"id": 1}, {"id": 2, "kind": "trace"}]}))

    def test_17_error_path_points_at_the_failing_row(self):
        """第 2 项缺 id → 路径必须是 ``args.rows[1].id``，不是 ``args.rows.id``。"""
        errs = _errs(ROWS_SCHEMA, {"rows": [{"id": 1}, {"kind": "log"}]})
        self.assertTrue(errs)
        self.assertTrue(errs[0].startswith("args.rows[1].id: "), errs[0])
        self.assertIn("缺少必填字段", errs[0])

    def test_18_custom_path_prefix_is_kept(self):
        """path 参数是调用方的命名空间（Dispatcher 传默认 args），换前缀不能乱。"""
        errs = _errs(LOGS_SCHEMA, {"service": "api", "limit": 1}, path="query_logs.args")
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("query_logs.args.limit: "), errs[0])


# ══════════════════════════════════════════════════════════════
# 四、合法/非法 schema 本身
# ══════════════════════════════════════════════════════════════
class TestSchemaItself(unittest.TestCase):
    """④ schema 写错必须炸出来：静默「当作通过」等于没有契约。"""

    def test_19_invalid_type_keyword_raises(self):
        with self.assertRaises(ValueError) as caught:
            _errs({"type": "not-a-type"}, {"any": 1})
        message = str(caught.exception)
        self.assertIn("schema", message)
        self.assertIn("not-a-type", message)

    def test_20_invalid_nested_schema_raises(self):
        with self.assertRaises(ValueError):
            _errs({"type": "object", "properties": {"a": {"type": "nope"}}}, {"a": 1})

    def test_21_unreadable_subschema_slot_degrades_instead_of_crashing(self):
        """与冻结用例 7 同一口径：``"x": "not-a-dict"`` 本来就没约束什么，摘掉即可。"""
        schema = {"type": "object", "properties": {"x": "not-a-dict"}}
        self.assertEqual(_errs(schema, {"x": 1}), [])

    def test_22_none_and_non_dict_schema_compat(self):
        self.assertEqual(_errs(None, {"a": 1}), [])
        errs = _errs(["not", "a", "dict"], {"a": 1})       # type: ignore[arg-type]
        self.assertTrue(errs)
        self.assertIn("schema 必须是 dict", errs[0])

    def test_23_broken_schema_makes_dispatcher_fail_closed(self):
        """端到端：非法 schema → failed + 不可重试，工具绝不执行。"""
        calls: list[dict] = []
        reg = ToolRegistry()
        reg.register(
            ToolManifest(name="broken", side_effect=SideEffect.READ_ONLY,
                         input_schema={"type": "not-a-type"}),
            lambda args, e: calls.append(dict(args)) or {"ok": True},
        )
        dispatcher = Dispatcher(
            reg, policy=Policy(),
            normalizer=Normalizer(artifact_dir=tempfile.mkdtemp(prefix="w17-js-")),
        )
        out = dispatcher.invoke("broken", {"any": 1}, _ctx())

        self.assertEqual(out.kind, "failed")
        self.assertIn("input schema error", out.reason)
        self.assertIn("schema", out.reason)
        self.assertFalse(out.retryable, "schema 写坏是配置问题，重试无意义")
        self.assertEqual(calls, [], "fail-closed：校验不了就不许执行")


# ══════════════════════════════════════════════════════════════
# 五、可选依赖：没装 jsonschema 就退化（而不是 import 就炸）
# ══════════════════════════════════════════════════════════════
class TestFallbackWithoutLibrary(unittest.TestCase):
    """⑤ ``jsonschema`` 是可选依赖：降级后必须**行为等于老实现**。"""

    def test_24_falls_back_to_subset_implementation(self):
        with mock.patch.object(contracts, "jsonschema", None):
            # 基础用例照旧
            self.assertEqual(_errs(LOGS_SCHEMA, {"service": "api", "lines": 200}), [])
            self.assertEqual(_errs(None, {"a": 1}), [])

            # 老文案一字不差（冻结的 37 个用例依赖它们）
            self.assertEqual(
                _errs(LOGS_SCHEMA, {"service": "api", "limit": 1}),
                ["args.limit: 未声明的字段（schema 禁止额外属性）"])
            self.assertEqual(_errs(LOGS_SCHEMA, {"lines": 200}),
                             ["args.service: 缺少必填字段"])
            self.assertEqual(_errs(LOGS_SCHEMA, {"service": "api", "lines": 99999}),
                             ["args.lines: 99999 > maximum 2000"])
            self.assertIn("期望 integer，收到 str",
                          _errs(LOGS_SCHEMA, {"service": "api", "lines": "200"})[0])

            # 与手写实现逐字一致（降级不是「另一套近似实现」）
            for schema, args in ((LOGS_SCHEMA, {"service": "api", "limit": 1}),
                                 (ROWS_SCHEMA, {"rows": [{"id": "x"}]}),
                                 (ROWS_SCHEMA, {"rows": [{"id": 1}, {}]})):
                self.assertEqual(_errs(schema, args),
                                 contracts._validate_subset(schema, args))

    def test_25_fallback_documents_its_ceiling(self):
        """降级后组合关键字确实不生效 —— 这是诚实的短板，不是靠测试掩盖的东西。"""
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        with mock.patch.object(contracts, "jsonschema", None):
            self.assertEqual(_errs(schema, ["neither"]), [])     # 手写版看不见 oneOf
        self.assertTrue(_errs(schema, ["neither"]))              # 有库时必须报错

    def test_26_fallback_still_reports_unknown_type(self):
        with mock.patch.object(contracts, "jsonschema", None):
            errs = _errs({"type": "not-a-type"}, {})
        self.assertEqual(errs, ["args: 未知 type 'not-a-type'"])

    def test_27_module_import_survives_absent_library(self):
        """真的没有库时 import contracts 不能炸 —— 用独立命名空间跑一遍源码验证。

        刻意不 ``importlib.reload(contracts)``：那会把模块里的 dataclass / 常量全部换成
        新对象，同一进程里其它测试持有的类引用立刻对不上（跨测试污染）。所以这里
        把 contracts.py 加载成一个**临时模块**，只为了证明「ImportError 被接住」。
        """
        import importlib.util
        name = "contracts_isolated_without_jsonschema"
        spec = importlib.util.spec_from_file_location(name, ROOT / "contracts.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            with mock.patch.dict(sys.modules, {"jsonschema": None}):
                spec.loader.exec_module(module)
            self.assertIsNone(module.jsonschema)
            self.assertEqual(module.validate_against_schema(
                LOGS_SCHEMA, {"service": "api", "lines": 200}), [])
            self.assertEqual(module.validate_against_schema(
                LOGS_SCHEMA, {"service": "api", "limit": 1}),
                ["args.limit: 未声明的字段（schema 禁止额外属性）"])
        finally:
            sys.modules.pop(name, None)
        self.assertIsNotNone(contracts.jsonschema)              # 本进程的库没被动过


# ══════════════════════════════════════════════════════════════
# 六、编译缓存：省 CPU 但不许串味
# ══════════════════════════════════════════════════════════════
class TestValidatorCache(unittest.TestCase):
    """⑥ 按 schema 内容缓存：内容相同必复用，内容一变必须重新编译。"""

    def test_28_same_content_is_reused(self):
        first = {"type": "object", "properties": {"a": {"type": "string"}}}
        twin = {"properties": {"a": {"type": "string"}}, "type": "object"}  # 键序不同
        v1 = contracts._compile_validator(contracts._sanitize_schema(first))
        v2 = contracts._compile_validator(contracts._sanitize_schema(twin))
        self.assertIs(v1, v2)

    def test_29_changed_content_recompiles(self):
        strict = contracts._compile_validator(
            contracts._sanitize_schema({"type": "integer", "minimum": 10}))
        loose = contracts._compile_validator(
            contracts._sanitize_schema({"type": "integer", "minimum": 1}))
        self.assertIsNot(strict, loose)
        self.assertEqual(len(list(strict.iter_errors(5))), 1)   # minimum 10：5 不合法
        self.assertEqual(len(list(loose.iter_errors(5))), 0)    # minimum 1：5 合法
        self.assertEqual(len(list(strict.iter_errors(50))), 0)

    def test_30_cache_hits_on_repeated_validation(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        before = len(contracts._VALIDATOR_CACHE)
        for _ in range(50):
            self.assertEqual(_errs(schema, {"n": 1}), [])
        self.assertEqual(len(contracts._VALIDATOR_CACHE), before + 1)

    def test_31_cache_is_bounded(self):
        self.assertGreater(contracts._VALIDATOR_CACHE_LIMIT, 0)
        for i in range(contracts._VALIDATOR_CACHE_LIMIT + 40):
            contracts._compile_validator(
                contracts._sanitize_schema({"type": "integer", "minimum": i}))
        self.assertLessEqual(len(contracts._VALIDATOR_CACHE),
                             contracts._VALIDATOR_CACHE_LIMIT)


# ══════════════════════════════════════════════════════════════
# 七、schema 自己声明的 $schema 要尊重（draft-07 / draft-04）
# ══════════════════════════════════════════════════════════════
class TestDraftSelection(unittest.TestCase):
    """⑦ 默认 2020-12，但 schema 声明了旧草案就按旧草案判 —— 否则合法 schema 会被误判。"""

    def test_32_draft07_tuple_items(self):
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "array",
            "items": [{"type": "string"}, {"type": "integer"}],   # 元组式 items
            "additionalItems": False,
        }
        self.assertEqual(_errs(schema, ["a", 1]), [])
        self.assertIn("args[1]: 期望 integer，收到 str", _errs(schema, ["a", "b"]))
        self.assertTrue(_errs(schema, ["a", 1, 2]))              # additionalItems: False

    def test_33_draft04_boolean_exclusive_maximum(self):
        schema = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "type": "number",
            "maximum": 10,
            "exclusiveMaximum": True,
        }
        self.assertEqual(_errs(schema, 9.5), [])
        errs = _errs(schema, 10)
        self.assertEqual(len(errs), 1)
        self.assertIn(">= exclusiveMaximum 10", errs[0])

    def test_34_default_draft_2020_12_prefix_items(self):
        schema = {"type": "array", "prefixItems": [{"type": "string"}], "items": False}
        self.assertEqual(_errs(schema, ["a"]), [])
        self.assertIn("args[0]: 期望 string，收到 int", _errs(schema, [1]))


if __name__ == "__main__":
    unittest.main()
