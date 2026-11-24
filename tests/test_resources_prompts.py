"""tests/test_resources_prompts.py — W22 MCP 另外两根支柱：Resources / Prompts。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && \
    python3 -m unittest tests.test_resources_prompts -v

覆盖两件事：
1. **Resources**：登记闭环 + `read` 的八步执行顺序（形状 → 注册表 → 授权 →
   缓存 → reader → 截断 → 写缓存 → 护栏）+ fail-closed + LRU + 可见性过滤
2. **Prompts**：登记/覆盖/列举 + 白名单渲染（JSON 花括号必须原样保留）+
   缺参补空串不抛错 + `validate` 找未声明占位符

原则：**零第三方依赖、只依赖 contracts 里的形状**，护栏用本地 fake
（真 OutputGuard 的细节归 W4 的 test_governance.py，这里只证明「出口过了护栏、
掩码生效、拦不住就拒绝」这三件事）。
"""
from __future__ import annotations

import re
import unittest

from contracts import ExecCtx, GuardVerdict, NamedPrompt, Principal, ResourceRef
from plane.prompts import PromptError, PromptRegistry
from plane.resources import (
    DEFAULT_MAX_BYTES,
    ResourceError,
    ResourcePermissionError,
    ResourceRegistry,
    ResourceRuntime,
    is_valid_uri,
    scheme_of,
)


# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════
def principal(*scopes: str, tenant: str = "acme") -> Principal:
    """按 scope 造主体。注意默认 Principal 只有 tool.* 权限，读不了资源。"""
    return Principal(user_id="u_w22", tenant=tenant, scopes=frozenset(scopes))


def ctx(*scopes: str, tenant: str = "acme") -> ExecCtx:
    return ExecCtx(principal=principal(*scopes, tenant=tenant), task_id="task_w22")


class CountingReader:
    """reader 计数器：证明缓存真的省掉了调用（不是「看起来像缓存」）。"""

    def __init__(self, body: str = "hello") -> None:
        self.body = body
        self.calls: list[str] = []

    def __call__(self, uri: str) -> str:
        self.calls.append(uri)
        return self.body


class MaskingGovernance:
    """假护栏：把 `sk-*` 掩码掉，并记录被扫过几次。

    形状与真 `Governance.before_output` 一致（返回 GuardVerdict）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[str] = []

    def before_output(self, text: str, allow_hosts=()) -> GuardVerdict:
        self.calls += 1
        self.seen.append(text)
        out = re.sub(r"sk-[A-Za-z0-9_-]+", "***REDACTED***", text)
        hits = ("credential",) if out != text else ()
        return GuardVerdict.ok(payload=out, hits=hits)


class BlockingGovernance:
    """假护栏：一律拒绝放行（用来验证 fail-closed 落到读取结果上）。"""

    def before_output(self, text: str, allow_hosts=()) -> GuardVerdict:
        return GuardVerdict.block("policy: 该资源不允许出现在输出里", payload=text)


class ExplodingGovernance:
    """假护栏：内部抛异常（真实护栏会自己 fail-closed，这里测 Runtime 也不放行）。"""

    def before_output(self, text: str, allow_hosts=()) -> GuardVerdict:
        raise RuntimeError("guard crashed")


def make_registry(*pairs: tuple[str, str]) -> ResourceRegistry:
    """`pairs` = (uri, body)，agent 用一个含 uri 的回显 reader 方便断言。"""
    reg = ResourceRegistry()
    for uri, body in pairs:
        reg.register(ResourceRef(uri=uri, mime="text/plain"),
                     CountingReader(body))
    return reg


READ = "resource.read"


# ══════════════════════════════════════════════════════════════
# Resources：登记
# ══════════════════════════════════════════════════════════════
class ResourceRegistryTest(unittest.TestCase):
    def test_01_register_and_read_roundtrip(self):
        """注册 + 读取基本闭环：返回的六字段全部正确。"""
        reg = ResourceRegistry()
        reader = CountingReader("diff --git a/x b/x")
        reg.register(ResourceRef(uri="git://repo/diff", name="diff", mime="text/x-diff"),
                     reader)
        rt = ResourceRuntime(reg)

        out = rt.read("git://repo/diff", ctx(READ))
        # 返回结构就是契约说的六个字段，不多不少（上层可以按键名硬取）
        self.assertEqual(set(out), {"uri", "mime", "content", "bytes",
                                    "cached", "truncated"})
        self.assertEqual(out["uri"], "git://repo/diff")
        self.assertEqual(out["mime"], "text/x-diff")
        self.assertEqual(out["content"], "diff --git a/x b/x")
        self.assertEqual(out["bytes"], len("diff --git a/x b/x".encode()))
        self.assertFalse(out["cached"])
        self.assertFalse(out["truncated"])
        self.assertEqual(reader.calls, ["git://repo/diff"])

    def test_02_duplicate_uri_raises_value_error(self):
        """同 uri 重复注册 → ValueError（不静默覆盖）。"""
        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="db://schema/users"), CountingReader("a"))
        with self.assertRaises(ValueError) as cm:
            reg.register(ResourceRef(uri="db://schema/users"), CountingReader("b"))
        self.assertIn("db://schema/users", str(cm.exception))
        # 原来的 reader 没被顶掉
        self.assertEqual(reg.get("db://schema/users")[1]("db://schema/users"), "a")

    def test_03_unknown_uri_raises_resource_error(self):
        reg = make_registry(("file://a.txt", "a"))
        rt = ResourceRuntime(reg)
        with self.assertRaises(ResourceError) as cm:
            rt.read("file://nope.txt", ctx(READ))
        self.assertIn("unknown resource: file://nope.txt", str(cm.exception))

    def test_04_invalid_uri_shape_raises(self):
        """无 scheme / 空串 / 非字符串 / 带空白 → ResourceError（形状先于注册表）。"""
        reg = make_registry(("file://a.txt", "a"))
        rt = ResourceRuntime(reg)
        for bad in ("users/schema", "/etc/passwd", "", "file:/a.txt", "a b://x", None):
            with self.subTest(uri=bad):
                with self.assertRaises(ResourceError):
                    rt.read(bad, ctx(READ))
        self.assertFalse(is_valid_uri("users/schema"))
        self.assertTrue(is_valid_uri("db://schema/users"))
        self.assertEqual(scheme_of("db://schema/users"), "db")
        self.assertEqual(scheme_of("nope"), "")

    def test_05_registry_unregister_and_list(self):
        """unregister 幂等；register 拒绝非 callable reader。"""
        reg = make_registry(("a://1", "1"))
        self.assertTrue(reg.unregister("a://1"))
        self.assertFalse(reg.unregister("a://1"))
        self.assertIsNone(reg.get("a://1"))
        with self.assertRaises(TypeError):
            reg.register(ResourceRef(uri="a://2"), "not-callable")
        with self.assertRaises(ValueError):
            reg.register(ResourceRef(uri="no-scheme"), CountingReader())
        self.assertEqual(len(reg), 0)


# ══════════════════════════════════════════════════════════════
# Resources：授权（fail-closed）
# ══════════════════════════════════════════════════════════════
class ResourceAuthTest(unittest.TestCase):
    def test_06_no_scope_denied_fail_closed(self):
        """默认 Principal 只有 tool.* → ResourcePermissionError，且 reader 一次都没跑。"""
        reg = ResourceRegistry()
        reader = CountingReader("secret")
        reg.register(ResourceRef(uri="db://secret"), reader)
        rt = ResourceRuntime(reg)

        with self.assertRaises(ResourcePermissionError):
            rt.read("db://secret", ctx())          # 无任何 scope
        with self.assertRaises(ResourcePermissionError):
            rt.read("db://secret", ExecCtx(principal=None))
        self.assertEqual(reader.calls, [])
        self.assertEqual(rt.stats["denied"], 2)

    def test_07_resource_read_scope_allows(self):
        reg = make_registry(("db://schema/users", "users(id, name)"))
        rt = ResourceRuntime(reg)
        self.assertEqual(rt.read("db://schema/users", ctx(READ))["content"],
                         "users(id, name)")

    def test_08_scheme_scope_allows(self):
        """`resource.<scheme>` 只放行该 scheme，别的 scheme 仍然拒绝。"""
        reg = make_registry(("db://schema/users", "u"), ("git://repo/diff", "d"))
        rt = ResourceRuntime(reg)
        self.assertEqual(rt.read("db://schema/users", ctx("resource.db"))["content"], "u")
        with self.assertRaises(ResourcePermissionError):
            rt.read("git://repo/diff", ctx("resource.db"))

    def test_09_wildcards_and_empty_scopes(self):
        """`*` / `resource.*` 覆盖全部；空 scope 集合 → 拒绝。"""
        reg = make_registry(("db://x", "x"))
        rt = ResourceRuntime(reg)
        self.assertEqual(rt.read("db://x", ctx("*"))["content"], "x")
        self.assertEqual(rt.read("db://x", ctx("resource.*"))["content"], "x")
        with self.assertRaises(ResourcePermissionError):
            rt.read("db://x", ctx())

    def test_10_cache_never_bypasses_auth(self):
        """先高权限预热缓存，再用低权限身份读：仍然拒绝（缓存不是权限旁路）。"""
        reg = make_registry(("db://schema/users", "u"))
        rt = ResourceRuntime(reg)
        rt.read("db://schema/users", ctx(READ))
        self.assertTrue(rt.is_cached("db://schema/users"))
        with self.assertRaises(ResourcePermissionError):
            rt.read("db://schema/users", ctx())


# ══════════════════════════════════════════════════════════════
# Resources：缓存 / 截断 / reader 失败
# ══════════════════════════════════════════════════════════════
class ResourceCacheTest(unittest.TestCase):
    def test_11_cache_hit_single_reader_call(self):
        reg = ResourceRegistry()
        reader = CountingReader("body")
        reg.register(ResourceRef(uri="db://t"), reader)
        rt = ResourceRuntime(reg)

        first = rt.read("db://t", ctx(READ))
        second = rt.read("db://t", ctx(READ))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(reader.calls, ["db://t"])       # 只调一次
        self.assertEqual(first["content"], second["content"])
        self.assertEqual(rt.stats["hits"], 1)

    def test_12_invalidate_forces_refetch(self):
        reg = ResourceRegistry()
        reader = CountingReader("body")
        reg.register(ResourceRef(uri="db://t"), reader)
        rt = ResourceRuntime(reg)
        rt.read("db://t", ctx(READ))
        rt.read("db://t", ctx(READ))
        self.assertEqual(len(reader.calls), 1)

        self.assertTrue(rt.invalidate("db://t"))
        self.assertFalse(rt.invalidate("db://t"))        # 幂等
        again = rt.read("db://t", ctx(READ))
        self.assertFalse(again["cached"])
        self.assertEqual(len(reader.calls), 2)

        rt.read("db://t", ctx(READ))
        self.assertEqual(rt.clear_cache(), 1)
        rt.read("db://t", ctx(READ))
        self.assertEqual(len(reader.calls), 3)

    def test_13_truncation_over_max_bytes(self):
        """超上限 → truncated=True 且内容变短；**不抛错**。"""
        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="file://big.txt", max_bytes=64),
                     CountingReader("A" * 200))
        rt = ResourceRuntime(reg, max_bytes_default=DEFAULT_MAX_BYTES)
        out = rt.read("file://big.txt", ctx(READ))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["content"], "A" * 64)
        self.assertEqual(out["bytes"], 64)
        self.assertEqual(rt.stats["truncated"], 1)

    def test_14_runtime_limit_is_a_ceiling_not_a_floor(self):
        """min(ref.max_bytes, max_bytes_default)：全局上限只能更严。"""
        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="file://big", max_bytes=10_000),
                     CountingReader("B" * 100))
        rt = ResourceRuntime(reg, max_bytes_default=20)
        out = rt.read("file://big", ctx(READ))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["bytes"], 20)

        # 反过来：ref 声明得更小 → 听 ref 的
        reg2 = ResourceRegistry()
        reg2.register(ResourceRef(uri="file://s", max_bytes=5), CountingReader("C" * 50))
        out2 = ResourceRuntime(reg2, max_bytes_default=1000).read("file://s", ctx(READ))
        self.assertEqual(out2["bytes"], 5)

    def test_15_multibyte_truncation_keeps_text_valid(self):
        """按字节截断不能切出半个汉字（errors='ignore' → 不留 U+FFFD）。"""
        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="file://cn.txt", max_bytes=7),  # 「中文」6B + 半个字
                     CountingReader("中文测试"))
        out = ResourceRuntime(reg).read("file://cn.txt", ctx(READ))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["content"], "中文")
        self.assertNotIn("\ufffd", out["content"])
        self.assertEqual(out["bytes"], 6)

    def test_16_reader_exception_wrapped_with_uri(self):
        """reader 抛异常 → ResourceError，消息必须带 uri（否则多资源共用一条日志）。"""
        reg = ResourceRegistry()

        def boom(uri: str) -> str:
            raise ConnectionError("db down")

        reg.register(ResourceRef(uri="db://schema/users"), boom)
        rt = ResourceRuntime(reg)
        with self.assertRaises(ResourceError) as cm:
            rt.read("db://schema/users", ctx(READ))
        msg = str(cm.exception)
        self.assertIn("db://schema/users", msg)
        self.assertIn("ConnectionError", msg)
        self.assertIn("db down", msg)
        self.assertEqual(rt.stats["reader_errors"], 1)
        self.assertFalse(rt.is_cached("db://schema/users"))   # 失败不写缓存

    def test_17_lru_eviction(self):
        """cache_size=2：读第 3 个 uri 后，最久未用的第一个被淘汰。"""
        reg = ResourceRegistry()
        readers = {}
        for name in ("a", "b", "c"):
            r = CountingReader(name)
            readers[name] = r
            reg.register(ResourceRef(uri=f"db://{name}"), r)
        rt = ResourceRuntime(reg, cache_size=2)

        rt.read("db://a", ctx(READ))
        rt.read("db://b", ctx(READ))
        self.assertTrue(rt.is_cached("db://a"))
        rt.read("db://c", ctx(READ))            # 挤掉 a
        self.assertFalse(rt.is_cached("db://a"))
        self.assertTrue(rt.is_cached("db://b"))
        self.assertTrue(rt.is_cached("db://c"))
        self.assertEqual(rt.cache_len(), 2)

        rt.read("db://a", ctx(READ))            # a 要重新取
        self.assertEqual(len(readers["a"].calls), 2)

    def test_18_cache_size_zero_disables_cache(self):
        reg = ResourceRegistry()
        reader = CountingReader("x")
        reg.register(ResourceRef(uri="db://x"), reader)
        rt = ResourceRuntime(reg, cache_size=0)
        rt.read("db://x", ctx(READ))
        rt.read("db://x", ctx(READ))
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(rt.cache_len(), 0)
        with self.assertRaises(ValueError):
            ResourceRuntime(reg, cache_size=-1)


# ══════════════════════════════════════════════════════════════
# Resources：输出护栏（读出来的数据也要过护栏）
# ══════════════════════════════════════════════════════════════
class ResourceGovernanceTest(unittest.TestCase):
    def test_19_governance_masks_content(self):
        """注入护栏 → 含 sk-xxx 的内容被脱敏。缓存命中也必须重算脱敏。"""
        reg = ResourceRegistry()
        body = '{"api_key": "sk-live-abc123456", "owner": "ops"}'
        reg.register(ResourceRef(uri="db://config/app"), CountingReader(body))
        gov = MaskingGovernance()
        rt = ResourceRuntime(reg, governance=gov)

        first = rt.read("db://config/app", ctx(READ))
        self.assertNotIn("sk-live-abc123456", first["content"])
        self.assertIn("***REDACTED***", first["content"])
        self.assertEqual(gov.calls, 1)
        self.assertEqual(rt.stats["redacted"], 1)

        # 第 4 步命中缓存，但第 8 步照样执行：缓存不能洗白原文
        second = rt.read("db://config/app", ctx(READ))
        self.assertTrue(second["cached"])
        self.assertNotIn("sk-live-abc123456", second["content"])
        self.assertEqual(gov.calls, 2)
        self.assertIn("sk-live-abc123456", gov.seen[-1])   # 缓存里存的是原文

    def test_20_governance_block_raises(self):
        """护栏 allowed=False → ResourceError（拒绝放行，不是返回原文）。"""
        reg = make_registry(("db://secret/keys", "k"))
        rt = ResourceRuntime(reg, governance=BlockingGovernance())
        with self.assertRaises(ResourceError) as cm:
            rt.read("db://secret/keys", ctx(READ))
        self.assertIn("governance", str(cm.exception))
        self.assertEqual(rt.stats["blocked"], 1)

    def test_21_governance_crash_fails_closed(self):
        """护栏内部抛异常 → 拒绝（fail-closed），绝不「没护栏就当干净」。"""
        reg = make_registry(("db://x", "x"))
        rt = ResourceRuntime(reg, governance=ExplodingGovernance())
        with self.assertRaises(ResourceError):
            rt.read("db://x", ctx(READ))

        class WrongShape:
            pass

        rt2 = ResourceRuntime(make_registry(("db://x", "x")), governance=WrongShape())
        with self.assertRaises(ResourceError) as cm:
            rt2.read("db://x", ctx(READ))
        self.assertIn("不可识别", str(cm.exception))

    def test_22_no_governance_returns_raw(self):
        """未注入护栏 → 原样返回（不同时造第二套脱敏清单）。"""
        reg = make_registry(("db://cfg", "sk-test-1"))
        out = ResourceRuntime(reg).read("db://cfg", ctx(READ))
        self.assertEqual(out["content"], "sk-test-1")


# ══════════════════════════════════════════════════════════════
# Resources：列举与可见性
# ══════════════════════════════════════════════════════════════
class ResourceListingTest(unittest.TestCase):
    def _registry(self) -> ResourceRegistry:
        reg = ResourceRegistry()
        for uri in ("git://repo/diff", "db://schema/users", "file://notes.md",
                    "git://repo/log"):
            reg.register(ResourceRef(uri=uri), CountingReader(uri))
        return reg

    def test_23_list_sorted_and_filtered(self):
        reg = self._registry()
        self.assertEqual([r.uri for r in reg.list()],
                         ["db://schema/users", "file://notes.md",
                          "git://repo/diff", "git://repo/log"])
        self.assertEqual([r.uri for r in reg.list("git")],
                         ["git://repo/diff", "git://repo/log"])
        self.assertEqual(reg.list("nope"), [])
        self.assertEqual(reg.schemes(), ["db", "file", "git"])

    def test_24_list_visible_filters_by_scope(self):
        """`resource.read` 看全部；`resource.git` 只看 git；无 scope 什么都看不见。"""
        reg = self._registry()
        rt = ResourceRuntime(reg)

        all_uris = [r.uri for r in rt.list_visible(principal(READ))]
        self.assertEqual(all_uris, [r.uri for r in reg.list()])

        git_only = [r.uri for r in rt.list_visible(principal("resource.git"))]
        self.assertEqual(git_only, ["git://repo/diff", "git://repo/log"])

        self.assertEqual(rt.list_visible(principal()), [])
        self.assertEqual(rt.list_visible(None), [])


# ══════════════════════════════════════════════════════════════
# Prompts
# ══════════════════════════════════════════════════════════════
REVIEW_TEMPLATE = (
    "你是代码评审员。语言：{language}。\n"
    "只评审 diff，不要重写整个文件。\n"
    "diff:\n{diff}\n"
)


def review_prompt(description: str = "v1") -> NamedPrompt:
    return NamedPrompt(name="review", template=REVIEW_TEMPLATE,
                       arguments=("language", "diff"), description=description)


class PromptRegistryTest(unittest.TestCase):
    def test_25_register_and_render(self):
        reg = PromptRegistry()
        reg.register(review_prompt())
        out = reg.render("review", language="python", diff="+ print(1)")
        self.assertIn("语言：python", out)
        self.assertIn("+ print(1)", out)
        self.assertNotIn("{language}", out)
        self.assertNotIn("{diff}", out)

    def test_26_missing_argument_becomes_empty(self):
        """未传 argument → 空串替代，不抛错（模板容错优先）。"""
        reg = PromptRegistry()
        reg.register(review_prompt())
        out = reg.render("review", diff="+ x")
        self.assertIn("语言：。", out)
        self.assertIn("+ x", out)
        self.assertNotIn("{language}", out)

        blank = reg.render("review")           # 全缺
        self.assertNotIn("{language}", blank)   # 声明的占位符都被换掉了
        self.assertNotIn("{diff}", blank)
        self.assertIn("只评审 diff", blank)     # 固定文本还在

    def test_27_undeclared_braces_preserved(self):
        """模板里的无关 `{...}`（JSON 花括号）原样保留，不被替换也不炸。"""
        reg = PromptRegistry()
        reg.register(NamedPrompt(
            name="tool_call",
            template='{"tool": "search", "query": "{query}", "limit": 10, "opt": {}}',
            arguments=("query",),
        ))
        out = reg.render("tool_call", query="runtime")
        self.assertEqual(out, '{"tool": "search", "query": "runtime", "limit": 10, "opt": {}}')

    def test_28_validate_reports_undeclared(self):
        reg = PromptRegistry()
        bad = NamedPrompt(name="bad", template="a {x} b {y} c {x}", arguments=("y",))
        self.assertEqual(reg.validate(bad), ["x"])
        # 排序去重 + 多占位符
        worse = NamedPrompt(name="worse", template="{b} {a} {b} {c}", arguments=())
        self.assertEqual(reg.validate(worse), ["a", "b", "c"])
        # 格式化形态也算占位符
        fmt = NamedPrompt(name="fmt", template="{name!r} {score:>3}", arguments=("name",))
        self.assertEqual(reg.validate(fmt), ["score"])

    def test_29_validate_ok_returns_empty_list(self):
        reg = PromptRegistry()
        self.assertEqual(reg.validate(review_prompt()), [])
        reg.register(review_prompt())
        self.assertEqual(reg.validate_registered("review"), [])
        # 声明了但模板没用 → 仍然通过（多声明是兼容手段）
        extra = NamedPrompt(name="extra", template="hi {name}", arguments=("name", "mood"))
        self.assertEqual(reg.validate(extra), [])

    def test_30_render_unknown_prompt_raises(self):
        reg = PromptRegistry()
        with self.assertRaises(PromptError) as cm:
            reg.render("nope")
        self.assertIn("nope", str(cm.exception))
        with self.assertRaises(PromptError):
            reg.validate_registered("nope")

    def test_31_overwrite_wins(self):
        """同名覆盖：后注册的生效（version 语义写在 description 里）。"""
        reg = PromptRegistry()
        reg.register(review_prompt("v1"))
        reg.register(review_prompt("v2: 加入 diff 上下文"))
        self.assertEqual(len(reg), 1)
        self.assertIn("v2", reg.get("review").description)

        reg.register(NamedPrompt(name="review", template="SECOND {diff}",
                                 arguments=("diff",), description="v3"))
        self.assertEqual(reg.render("review", diff="D"), "SECOND D")

    def test_32_list_sorted_by_name(self):
        reg = PromptRegistry()
        for name in ("zeta", "alpha", "mid"):
            reg.register(NamedPrompt(name=name, template=name))
        self.assertEqual([p.name for p in reg.list()], ["alpha", "mid", "zeta"])
        self.assertEqual(reg.names(), ["alpha", "mid", "zeta"])
        self.assertIn("alpha", reg)
        self.assertNotIn("nope", reg)

    def test_33_multi_argument_render(self):
        reg = PromptRegistry()
        reg.register(NamedPrompt(
            name="plan",
            template="goal={goal}\nsteps={steps}\nbudget={budget}",
            arguments=("goal", "steps", "budget"),
        ))
        out = reg.render("plan", goal="修 CI", steps="3", budget="8")
        self.assertEqual(out, "goal=修 CI\nsteps=3\nbudget=8")

    def test_34_extra_kwargs_ignored_and_unregister(self):
        """多余 kwargs 被忽略；unregister 后 render 抛 PromptError。"""
        reg = PromptRegistry()
        reg.register(review_prompt())
        out = reg.render("review", language="go", diff="+1", unexpected="ignored")
        self.assertNotIn("ignored", out)
        self.assertTrue(reg.unregister("review"))
        self.assertFalse(reg.unregister("review"))
        with self.assertRaises(PromptError):
            reg.render("review")

    def test_35_register_rejects_bad_shape(self):
        reg = PromptRegistry()
        with self.assertRaises(TypeError):
            reg.register({"name": "x", "template": "y"})
        with self.assertRaises(ValueError):
            reg.register(NamedPrompt(name="   ", template="x"))
        with self.assertRaises(ValueError):
            reg.register(NamedPrompt(name="x", template=123))   # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════
# 两根支柱一起用：Provider 把 prompt 当模板、把 resource 当数据源
# ══════════════════════════════════════════════════════════════
class ResourcesWithPromptsTest(unittest.TestCase):
    def test_36_prompt_rendered_from_resource_content(self):
        """真实形态：prompt 模板 + resource 读出的数据 → 渲染出最终 Prompt。"""
        reg = ResourceRegistry()
        reg.register(ResourceRef(uri="git://repo/diff"), CountingReader("+ x = 1\n"))
        gov = MaskingGovernance()
        rt = ResourceRuntime(reg, governance=gov)
        prompts = PromptRegistry()
        prompts.register(review_prompt())

        diff = rt.read("git://repo/diff", ctx(READ))["content"]
        prompt = prompts.render("review", language="python", diff=diff)
        self.assertIn("+ x = 1", prompt)
        self.assertIn("语言：python", prompt)
        self.assertEqual(gov.calls, 1)


if __name__ == "__main__":
    unittest.main()
