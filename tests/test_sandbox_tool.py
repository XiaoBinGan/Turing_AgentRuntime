"""tests/test_sandbox_tool.py — W10：执行沙箱接入 Dispatcher 的自带测试。

stdlib unittest，零第三方依赖。跑法：

    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_sandbox_tool -v

覆盖点（对应任务验收清单 1-16，另加若干）：注册可见性 / 白名单 / 元字符 / 空 argv /
超时 / 截断 / 脱敏 / 未批准→needs_confirmation / 已批准→ok / 非幂等不缓存 /
manifest 风险面 / demo registry / cwd / stderr 分流 / 全链路 Normalizer /
闸门与 W3 一致性 / T3 硬拒 / 限流 / scope 缺失 / 预算。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace

from contracts import (
    Approval,
    Budget,
    ExecCtx,
    Principal,
    RiskTier,
    SideEffect,
    ToolManifest,
    args_hash,
)
from plane.tool import Dispatcher, SubprocessSandbox, ToolRegistry
from plane.sandbox_tool import (
    DEFAULT_ALLOWED_COMMANDS,
    QUERY_LOGS_MANIFEST,
    SANDBOX_MANIFEST,
    HandlerSandbox,
    make_demo_registry,
    register_sandbox,
    sandbox_handler,
)


class SandboxToolTestBase(unittest.TestCase):
    """每个用例独立的 artifact 目录 + 一个装好 sandbox_run 的 registry。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="w10-sandbox-")
        self.art = os.path.join(self.tmp, "artifacts")
        self.reg = ToolRegistry()
        self.manifest = register_sandbox(self.reg)
        self.fn = self.reg.get("sandbox_run")[1]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 便捷构造 ────────────────────────────────────────────────
    def ctx(self, **kw) -> ExecCtx:
        return ExecCtx(**kw)

    def approval_for(self, args: dict, tool: str = "sandbox_run") -> Approval:
        return Approval(tool=tool, args_hash=args_hash(args), approved=True)

    def dispatcher(self, registry: ToolRegistry | None = None) -> Dispatcher:
        return Dispatcher(registry or self.reg, artifact_dir=self.art)


# ══════════════════════════════════════════════════════════════
# 1. 注册可见性
# ══════════════════════════════════════════════════════════════
class TestRegistration(SandboxToolTestBase):
    def test_01_registered_and_visible(self) -> None:
        entry = self.reg.get("sandbox_run")
        self.assertIsNotNone(entry, "sandbox_run 必须真的进了 registry")
        manifest, fn = entry
        self.assertEqual(manifest.name, "sandbox_run")
        self.assertTrue(callable(fn))
        self.assertIn("sandbox_run", self.reg.names())
        self.assertIn("sandbox_run", self.reg)          # __contains__
        # Builder 只看 manifests()：沙箱必须出现在模型可见的工具清单里
        self.assertIn("sandbox_run", [m.name for m in self.reg.manifests()])

    def test_02_register_returns_effective_manifest(self) -> None:
        reg = ToolRegistry()
        got = register_sandbox(reg, risk_tier=RiskTier.T1_LOW)
        stored, _ = reg.get("sandbox_run")
        self.assertIs(got, stored, "返回的 manifest 必须是 registry 里真用的那一个")
        self.assertEqual(got.risk_tier, RiskTier.T1_LOW)
        # 其余风险面不能被 risk_tier 覆盖顺手改掉
        self.assertEqual(got.side_effect, SideEffect.MUTATING)
        self.assertEqual(got.required_scope, "tool.write")
        self.assertFalse(got.idempotent)

    def test_03_duplicate_register_rejected(self) -> None:
        with self.assertRaises(ValueError):
            register_sandbox(self.reg)      # 同名静默覆盖 = 权限被换掉都不知情


# ══════════════════════════════════════════════════════════════
# 2-5. handler 的执行语义（不经 Dispatcher，直接测 fn）
# ══════════════════════════════════════════════════════════════
class TestHandlerSemantics(SandboxToolTestBase):
    def test_04_allowlisted_command_runs(self) -> None:
        out = self.fn({"argv": ["echo", "hi"]}, self.ctx())
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["stdout"], "hi\n")
        self.assertEqual(out["stderr"], "")
        self.assertEqual(out["argv"], ["echo", "hi"])
        self.assertGreaterEqual(out["duration_ms"], 0.0)
        self.assertNotIn("truncated", out)

    def test_05_non_allowlisted_raises_permission_error(self) -> None:
        for argv in (["rm", "-rf", "/"], ["chmod", "+x", "f"], ["whoami"]):
            with self.subTest(argv=argv):
                with self.assertRaises(PermissionError):
                    self.fn({"argv": argv}, self.ctx())

    def test_06_injection_chars_raise_permission_error(self) -> None:
        cases = [
            ["echo", "; rm -rf /"],
            ["echo", "hi | cat"],
            ["echo", "a && whoami"],
            ["echo", "$(whoami)"],
            ["echo", "`id`"],
            ["ls", ">", "out.txt"],
            ["cat", "<", "in.txt"],
            ["echo", "a\nb"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with self.assertRaises(PermissionError):
                    self.fn({"argv": argv}, self.ctx())

    def test_07_argv_shape_errors(self) -> None:
        for args in ({}, {"argv": []}, {"argv": "echo hi"},
                     {"argv": ["echo", 1]}, {"argv": ("echo", "hi")},
                     {"argv": None}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.fn(args, self.ctx())

    def test_08_path_qualified_executable_rejected(self) -> None:
        # 白名单按 basename 比对，不能让 /tmp/ls 这种同名任意路径进来
        with self.assertRaises(PermissionError):
            self.fn({"argv": ["./echo", "hi"]}, self.ctx())

    def test_09_timeout_raises_timeout_error(self) -> None:
        slow = sandbox_handler(allowed_commands=["sleep"], timeout_ms=120)
        with self.assertRaises(TimeoutError):
            slow({"argv": ["sleep", "5"]}, self.ctx())


# ══════════════════════════════════════════════════════════════
# 6-8. 截断 / 脱敏 / stderr 分流
# ══════════════════════════════════════════════════════════════
class TestOutputHygiene(SandboxToolTestBase):
    def test_10_output_truncated_by_bytes(self) -> None:
        reg = ToolRegistry()
        register_sandbox(reg, max_output_bytes=100)
        fn = reg.get("sandbox_run")[1]
        out = fn({"argv": ["python3", "-c", "print('a'*20000)"]}, self.ctx())
        self.assertTrue(out.get("truncated"), "20001 字节输出必须被截断")
        self.assertLessEqual(len(out["stdout"].encode("utf-8")), 100)
        self.assertGreaterEqual(out["total_bytes"], 20000)
        self.assertEqual(out["exit_code"], 0)

    def test_11_truncation_keeps_utf8_intact(self) -> None:
        # 按字节截断在中文字符中间会切断，必须 errors="ignore" 兜住而不是抛 UnicodeDecodeError
        reg = ToolRegistry()
        register_sandbox(reg, max_output_bytes=10)
        fn = reg.get("sandbox_run")[1]
        out = fn({"argv": ["python3", "-c", "print('中文'*500)"]}, self.ctx())
        self.assertTrue(out["truncated"])
        self.assertIsInstance(out["stdout"], str)
        out["stdout"].encode("utf-8")          # 不抛异常 = 没有半个字符残留在里面

    def test_12_secrets_redacted(self) -> None:
        cases = {
            "OpenAI key": ("print('sk-' + 'a1b2c3d4e5f6g7h8i9j0k1l2')", "a1b2c3d4e5f6g7h8i9j0k1l2"),
            "password": ("print('password=hunter2secret')", "hunter2secret"),
            "AWS": ("print('AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI')", "wJalrXUtnFEMI"),
        }
        for name, (prog, leaked) in cases.items():
            with self.subTest(name=name):
                out = self.fn({"argv": ["python3", "-c", prog]}, self.ctx())
                self.assertIn("***REDACTED***", out["stdout"])
                self.assertNotIn(leaked, out["stdout"])
                self.assertNotIn("sk-", out["stdout"])

    def test_13_stderr_kept_separate(self) -> None:
        out = self.fn({"argv": ["python3", "-c", "raise SystemExit('bad')"]}, self.ctx())
        self.assertEqual(out["stdout"], "", "stderr 不能混进 stdout")
        self.assertIn("bad", out["stderr"])
        self.assertEqual(out["exit_code"], 1)

        ok = self.fn({"argv": ["python3", "-c", "print('to-stdout')"]}, self.ctx())
        self.assertEqual(ok["stdout"], "to-stdout\n")
        self.assertEqual(ok["stderr"], "", "正常输出不能凭空出现在 stderr")

    def test_14_cwd_respected(self) -> None:
        if not hasattr(os, "getcwd") or os.name != "posix":
            self.skipTest("cwd 用例仅在 POSIX 上验证")
        reg = ToolRegistry()
        register_sandbox(reg, allowed_commands=["pwd"])
        fn = reg.get("sandbox_run")[1]
        out = fn({"argv": ["pwd"], "cwd": self.tmp}, self.ctx())
        self.assertEqual(out["exit_code"], 0)
        got = os.path.realpath(out["stdout"].strip())
        self.assertEqual(got, os.path.realpath(self.tmp))

    def test_15_invalid_cwd_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.fn({"argv": ["echo", "hi"], "cwd": os.path.join(self.tmp, "nope")}, self.ctx())
        with self.assertRaises(ValueError):
            self.fn({"argv": ["echo", "hi"], "cwd": 123}, self.ctx())

    def test_16_aborted_or_expired_ctx_does_not_execute(self) -> None:
        e = self.ctx(deadline=1.0)          # 早已过期的 deadline（epoch ms）
        with self.assertRaises(TimeoutError):
            self.fn({"argv": ["echo", "hi"]}, e)
        e2 = self.ctx()
        e2.abort()
        with self.assertRaises(RuntimeError):
            self.fn({"argv": ["echo", "hi"]}, e2)


# ══════════════════════════════════════════════════════════════
# 9-16. 与 Dispatcher 的配合（验收重点）
# ══════════════════════════════════════════════════════════════
class TestDispatcherIntegration(SandboxToolTestBase):
    def test_17_unapproved_t2_needs_confirmation(self) -> None:
        d = self.dispatcher()
        args = {"argv": ["echo", "hi"]}
        out = d.invoke("sandbox_run", args, self.ctx())
        self.assertEqual(out.kind, "needs_confirmation")
        self.assertIn("args_hash", out.reason)
        self.assertIsNone(out.result, "未批准绝不能有结果")
        self.assertEqual(d.denied_log[-1]["kind"], "needs_confirmation")

    def test_18_approved_t2_executes(self) -> None:
        d = self.dispatcher()
        args = {"argv": ["echo", "hi"]}
        e = self.ctx(approvals=(self.approval_for(args),))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(out.result.payload["stdout"], "hi\n")
        self.assertEqual(out.result.payload["exit_code"], 0)
        self.assertEqual(out.result.tool, "sandbox_run")

    def test_19_approval_is_bound_to_args_hash(self) -> None:
        d = self.dispatcher()
        approved_args = {"argv": ["echo", "hi"]}
        e = self.ctx(approvals=(self.approval_for(approved_args),))
        self.assertEqual(d.invoke("sandbox_run", approved_args, e).kind, "ok")
        # 改一个字符 = 另一次调用，旧审批不覆盖
        other = {"argv": ["echo", "hi", "there"]}
        self.assertEqual(d.invoke("sandbox_run", other, e).kind, "needs_confirmation")

    def test_20_non_idempotent_runs_every_time(self) -> None:
        reg = ToolRegistry()
        calls: list[str] = []

        def spying(args: dict, e: ExecCtx) -> dict:
            calls.append(args_hash(args))
            return sandbox_handler()(args, e)

        reg.register(SANDBOX_MANIFEST, spying)
        d = self.dispatcher(reg)
        args = {"argv": ["echo", "hi"]}
        e = self.ctx(approvals=(self.approval_for(args),))
        o1 = d.invoke("sandbox_run", args, e)
        o2 = d.invoke("sandbox_run", args, e)
        self.assertEqual((o1.kind, o2.kind), ("ok", "ok"))
        self.assertEqual(len(calls), 2, "非幂等工具不能命中幂等缓存")
        self.assertFalse(o1.result.deduped)
        self.assertFalse(o2.result.deduped)
        self.assertEqual(d._cache, {}, "idempotent=False 的调用不得写缓存")

    def test_21_non_idempotent_real_side_effect_twice(self) -> None:
        # 用真实副作用兜底：同一个 argv 两次 → 文件被追加两次
        marker = os.path.join(self.tmp, "counter.txt").replace("'", "")
        prog = f"open({marker!r},'a').write('x')"
        d = self.dispatcher()
        args = {"argv": ["python3", "-c", prog]}
        e = self.ctx(approvals=(self.approval_for(args),))
        for _ in range(2):
            self.assertEqual(d.invoke("sandbox_run", args, e).kind, "ok")
        with open(marker, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "xx")

    def test_22_manifest_risk_surface(self) -> None:
        m = self.manifest
        self.assertEqual(m.risk_tier, RiskTier.T2_HIGH)
        self.assertEqual(m.required_scope, "tool.write")
        self.assertEqual(m.side_effect, SideEffect.MUTATING)
        self.assertFalse(m.idempotent)
        self.assertEqual(m.timeout_ms, 8000)
        self.assertEqual(m.name, "sandbox_run")

    def test_23_missing_write_scope_denied(self) -> None:
        d = self.dispatcher()
        args = {"argv": ["echo", "hi"]}
        reader = Principal(scopes=frozenset({"tool.read"}))
        e = self.ctx(principal=reader, approvals=(self.approval_for(args),))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "denied", "没有审批都不该过，有审批但缺 scope 必须 denied")
        self.assertIn("tool.write", out.reason)

    def test_24_policy_blocks_destructive_before_execution(self) -> None:
        d = self.dispatcher()
        args = {"argv": ["echo", "; rm -rf /"]}
        e = self.ctx(approvals=(self.approval_for(args),))     # 人批了也不行
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "denied")
        self.assertIn("policy", out.reason)

    def test_25_handler_error_becomes_failed(self) -> None:
        """handler 抛异常 → failed。

        注意：`{"argv": "echo hi"}` 这种形状错误现在会被 **input_schema 提前拦住**
        （`invalid args: args.argv: 期望 array`），比 handler 里的 ValueError 更早更准。
        所以这里用「schema 合法但 handler 会拒绝」的输入（路径逃逸）来验证 handler 侧。
        """
        d = self.dispatcher()
        args = {"argv": ["./echo", "hi"]}
        e = self.ctx(approvals=(self.approval_for(args),))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "failed")
        self.assertIn("PermissionError", out.reason)

    def test_25b_shape_error_caught_by_schema_first(self) -> None:
        """形状错误由 schema 拦，不落到 handler。"""
        d = self.dispatcher()
        args = {"argv": "echo hi"}
        e = self.ctx(approvals=(self.approval_for(args),))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "failed")
        self.assertIn("invalid args", out.reason)
        self.assertIn("argv", out.reason)

    def test_26_dispatcher_own_timeout_guard(self) -> None:
        # handler 的子进程超时设很长，让 Dispatcher 的 future 超时先触发
        reg = ToolRegistry()
        manifest = replace(SANDBOX_MANIFEST, timeout_ms=300)
        reg.register(manifest, sandbox_handler(
            allowed_commands=["sleep"], timeout_ms=5000))
        d = self.dispatcher(reg)
        args = {"argv": ["sleep", "1.2"]}
        e = self.ctx(approvals=(self.approval_for(args),))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "timeout")
        self.assertTrue(out.retryable)

    def test_27_per_turn_limit_enforced(self) -> None:
        d = self.dispatcher()
        args = {"argv": ["echo", "hi"]}
        e = self.ctx(approvals=(self.approval_for(args),))
        kinds = [d.invoke("sandbox_run", args, e).kind for _ in range(11)]
        self.assertEqual(kinds[:10], ["ok"] * 10)
        self.assertEqual(kinds[10], "denied")
        self.assertIn("rate limit", d.trace_log[-1]["reason"])

    def test_28_budget_exhaustion_blocks_execution(self) -> None:
        d = self.dispatcher()
        args = {"argv": ["echo", "hi"]}
        e = self.ctx(approvals=(self.approval_for(args),),
                     budget=Budget(tool_calls_total=0, tool_calls_used=1))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "failed")
        self.assertIn("budget", out.reason)
        self.assertEqual(e.budget.tool_calls_used, 1, "被预算拒的调用必须回滚计数")

    def test_29_full_chain_normalizer(self) -> None:
        # registry → Dispatcher → Policy → 审批 → 执行 → Normalizer → ToolOutcome.ok
        d = self.dispatcher()
        args = {"argv": ["python3", "-c",
                         "print('password=hunter2secret')"]}
        e = self.ctx(approvals=(self.approval_for(args),))
        out = d.invoke("sandbox_run", args, e)

        self.assertEqual(out.kind, "ok", out.reason)
        r = out.result
        self.assertEqual(r.tool, "sandbox_run")
        self.assertFalse(r.truncated)
        self.assertGreater(r.bytes_out, 0)
        self.assertGreater(r.duration_ms, 0.0)
        blob = json.dumps(r.payload, ensure_ascii=False)
        self.assertNotIn("hunter2secret", blob, "Normalizer 之后不得有明文")
        self.assertIn("***REDACTED***", blob)
        self.assertEqual(r.payload["exit_code"], 0)
        # 审计留痕
        self.assertEqual(d.trace_log[-1]["kind"], "ok")
        self.assertEqual(d.trace_log[-1]["args_hash"], args_hash(args))
        self.assertEqual(d.stats()["calls"], 1)


# ══════════════════════════════════════════════════════════════
# demo registry + 与 W3 沙箱的一致性
# ══════════════════════════════════════════════════════════════
class TestDemoRegistryAndParity(unittest.TestCase):
    def _art(self, prefix: str) -> str:
        path = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_30_demo_registry_has_both_tiers(self) -> None:
        reg, manifest = make_demo_registry()
        self.assertEqual(manifest.name, "sandbox_run")
        self.assertIn("sandbox_run", reg.names())
        self.assertIn("query_logs", reg.names())
        self.assertEqual(manifest.risk_tier, RiskTier.T2_HIGH)
        self.assertEqual(QUERY_LOGS_MANIFEST.risk_tier, RiskTier.T0_SAFE)
        self.assertEqual(QUERY_LOGS_MANIFEST.side_effect, SideEffect.READ_ONLY)

        d = Dispatcher(reg, artifact_dir=self._art("w10-demo-"))
        logs = d.invoke("query_logs", {"query": "timeout"}, ExecCtx())
        self.assertEqual(logs.kind, "ok")
        self.assertEqual(logs.result.payload["count"], 2)
        # T0 只读工具不需要审批
        self.assertEqual(d.invoke("sandbox_run",
                                  {"argv": ["echo", "hi"]}, ExecCtx()).kind,
                         "needs_confirmation")

    def test_31_demo_registry_readonly_deployment_denies(self) -> None:
        reg, manifest = make_demo_registry(allow_write=False)
        self.assertEqual(manifest.risk_tier, RiskTier.T3_CRITICAL)
        d = Dispatcher(reg, artifact_dir=self._art("w10-ro-"))
        args = {"argv": ["echo", "hi"]}
        e = ExecCtx(approvals=(Approval(tool="sandbox_run",
                                        args_hash=args_hash(args), approved=True),))
        out = d.invoke("sandbox_run", args, e)
        self.assertEqual(out.kind, "denied", "T3 不可确认，只能拒绝")
        self.assertIn("critical", out.reason)

    def test_32_gate_parity_with_w3_sandbox(self) -> None:
        """HandlerSandbox.gate 与 W3 SubprocessSandbox.run 的判定必须一致。

        允许的差异只有「类型错误」一类，且本工具一律更严：
          - 非 list 输入：W3 没有这条规则（传 "echo hi" 会被它当可执行文件 "e" 处理），
            本工具按任务要求抛 ValueError；
          - 元素不是 str：W3 抛 PermissionError，本工具抛 ValueError（类型错误不是权限问题）。
        所以对照语料只含「list[str] 形状」的输入；上面两类由 test_07 单独锁定。
        """
        allow = list(DEFAULT_ALLOWED_COMMANDS)

        def verdict(call, argv) -> str | None:
            try:
                call(argv)
                return None
            except Exception as exc:          # noqa: BLE001 — 比对的就是异常类型
                return type(exc).__name__

        w3 = SubprocessSandbox(allowlist=allow, timeout_ms=2000)
        mine = HandlerSandbox(allowlist=allow, timeout_ms=2000)
        corpus = [
            ["echo", "hi"],
            ["ls", "-1", "."],
            ["python3", "-c", "print(1)"],
            [],
            ["rm", "-rf", "/"],
            ["whoami"],
            ["/bin/echo", "x"],
            ["echo", "; id"],
            ["echo", "a|b"],
            ["echo", "a&b"],
            ["echo", "$HOME"],
            ["echo", "`id`"],
            ["echo", "a>b"],
            ["echo", "a<b"],
            ["echo", "a\nb"],
        ]
        for argv in corpus:
            with self.subTest(argv=argv):
                self.assertEqual(verdict(w3.run, argv), verdict(mine.gate, argv))


if __name__ == "__main__":
    unittest.main(verbosity=2)
