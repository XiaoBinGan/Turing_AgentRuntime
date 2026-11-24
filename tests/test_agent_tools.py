"""tests/test_agent_tools.py — Agent 层（工具 / 技能 / 注册表 / 配置）的验收用例。

跑法：

    cd /Users/supre/Documents/tmp/agent-runtime
    .venv/bin/python -m unittest tests.test_agent_tools -v

这组用例守的是**四件容易被"跑得顺"妥协掉的事**：

1. **副作用定级不许降级**。`rollback_deployment` / `delete_namespace` 必须是
   IRREVERSIBLE / T3；T3 在 Dispatcher 里是**硬拒**（连审批都不收），
   所以用例 ⑳ 会带着「已批准」的 ctx 去调它，仍然必须拿到 `denied`。
2. **`get_config` 必须自己脱敏**。不是靠 Dispatcher 的 Normalizer 兜底 ——
   Normalizer 的敏感键正则里没有裸 `key`（`signing_key` 会原样漏出），
   所以用例 ⑨ 既验直调（值必须是我们自己的 `***MASKED***`），
   也验过 Dispatcher 之后原值一个都不出现。
3. **不连真网络**。用例 ④ 直接扫源码，禁止 `requests` / `socket` /
   `http.client` / `urllib` 出现在工具与假后端里 —— 演示的可复现性依赖这一点。
4. **配置面与代码面不许漂移**。`agents/tools.yaml` 与 `agents/tools.py`
   的 side_effect / risk_tier / input_schema 逐字段比对（用例 ㊲ ㊳）。

每个用例都用一个**新的 `FakeOpsBackend`**（`setUp` 里造）：工具分两种，
读的没有状态、写的会改 `_state`，共用一个后端会让「重启过一次」泄漏到
下一个用例的断言里。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from contracts import (
    Approval,
    ExecCtx,
    Principal,
    RiskTier,
    SideEffect,
    args_hash,
)
from plane.router import CLARIFY_AGENT
from plane.skill import SkillRuntime
from plane.tool import Dispatcher, ToolRegistry
from toolconfig import load_tools

from agents import registry as agent_registry
from agents import skills as agent_skills
from agents import tools as agent_tools
from agents.fake_backend import FakeOpsBackend

ROOT = Path(__file__).resolve().parent.parent

#: 完整运维账号（读写都有）。要测"缺 scope 被拒"时自己造一个只有读的。
FULL = Principal(scopes=frozenset({"tool.read", "tool.write"}))


# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════
class AgentToolBase(unittest.TestCase):
    """独立的假后端 + 独立的 artifact 目录 + 一个绑好 8 个工具的 Dispatcher。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="agent-tools-")
        self.backend = FakeOpsBackend()
        self.reg = ToolRegistry()
        impls = agent_tools.build_handlers(self.backend)
        for manifest in agent_tools.MANIFESTS:
            self.reg.register(manifest, impls[manifest.name])
        self.disp = Dispatcher(self.reg, artifact_dir=os.path.join(self.tmp, "artifacts"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 便捷构造 ────────────────────────────────────────────────
    def ctx(self, *, principal: Principal = FULL, approvals=(), **kw) -> ExecCtx:
        return ExecCtx(principal=principal, approvals=tuple(approvals), **kw)

    def call(self, name: str, args: dict, **kw):
        return self.disp.invoke(name, args, self.ctx(**kw))

    def approval(self, name: str, args: dict) -> Approval:
        return Approval(tool=name, args_hash=args_hash(args), approved=True)

    def manifest(self, name: str):
        entry = self.reg.get(name)
        self.assertIsNotNone(entry, f"工具 {name} 未注册")
        return entry[0]


# ══════════════════════════════════════════════════════════════
# ① manifest 契约（schema / 描述 / 名字对齐 / 不连网络）
# ══════════════════════════════════════════════════════════════
class ManifestContractTest(AgentToolBase):

    def test_input_schemas_are_valid_json_schema(self):
        """① 每个 `input_schema` 都要过 JSON Schema 的形状校验。

        为什么要这条：Dispatcher 对**坏 schema** 是 fail-closed（每次调用都
        `input schema error`），等于工具全废 —— 而这类错误在运行期只表现为
        "工具不工作"，根因却藏在 schema 的一个拼写里。
        """
        try:
            from jsonschema.validators import validator_for
        except ImportError:                       # pragma: no cover - 环境相关
            self.skipTest("未安装 jsonschema，跳过形状校验")
        self.assertGreaterEqual(len(agent_tools.MANIFESTS), 6)
        for manifest in agent_tools.MANIFESTS:
            schema = manifest.input_schema
            self.assertIsInstance(schema, dict, f"{manifest.name} 没有 input_schema")
            with self.subTest(tool=manifest.name):
                # check_schema 不抛异常即合法；同时要求根是 object 且 required 字段有声明
                validator_for(schema).check_schema(schema)
                self.assertEqual(schema.get("type"), "object")
                props = schema.get("properties", {})
                for field in schema.get("required", []):
                    self.assertIn(field, props,
                                  f"{manifest.name}: required 里的 {field} 没在 properties 声明")

    def test_descriptions_are_meaningful(self):
        """② 描述要能让模型判断「什么时候用它」，不是空串或复述工具名。"""
        for manifest in agent_tools.MANIFESTS:
            with self.subTest(tool=manifest.name):
                desc = manifest.description.strip()
                self.assertGreaterEqual(len(desc), 12, f"{manifest.name} 描述太短")
                self.assertNotEqual(desc.lower(), manifest.name.lower())
                # 至少有一个中文字符：这套工具是给中文运维场景写的
                self.assertTrue(re.search(r"[\u4e00-\u9fff]", desc),
                                f"{manifest.name} 描述里没有业务语义")

    def test_manifest_and_handler_names_align(self):
        """③ MANIFESTS 与 handlers() 必须一一对应（少一个就是"注册了但调不动"）。"""
        declared = agent_tools.tool_names()
        impls = agent_tools.handlers()
        self.assertEqual(sorted(declared), sorted(impls))
        self.assertEqual(len(declared), len(set(declared)), "工具名重复")
        for name in declared:
            self.assertTrue(callable(impls[name]), f"{name} 的实现不可调用")

    def test_no_real_network_modules(self):
        """④ 工具与假后端不许 import 真网络库（演示必须可复现、可离线）。"""
        forbidden = ("requests", "httpx", "socket", "http.client", "urllib",
                     "aiohttp", "paramiko")
        for filename in ("tools.py", "fake_backend.py"):
            src = (ROOT / "agents" / filename).read_text(encoding="utf-8")
            for module in forbidden:
                with self.subTest(file=filename, module=module):
                    self.assertNotRegex(
                        src, rf"(?m)^\s*(?:import|from)\s+{re.escape(module)}\b",
                        f"{filename} 不该连真网络：出现了 {module}")


# ══════════════════════════════════════════════════════════════
# ② 四个只读工具（形状 + 语义）
# ══════════════════════════════════════════════════════════════
class ReadToolTest(AgentToolBase):

    def test_query_metrics_returns_curve_and_stats(self):
        """⑤ 查指标：返回点 + 统计，且 latest 与最后一个点一致。"""
        out = agent_tools.query_metrics(
            {"service": "api", "metric": "p99", "window_minutes": 60})
        self.assertEqual(out["service"], "api")
        self.assertEqual(out["metric"], "p99")
        self.assertEqual(out["unit"], "ms")
        self.assertGreaterEqual(len(out["points"]), 2)
        self.assertEqual(out["stats"]["latest"], out["points"][-1]["value"])
        self.assertGreater(out["stats"]["delta_ratio"], 0.3,
                           "api 的 p99 是预置的劣化形状，delta_ratio 应当明显为正")
        for key in ("latest", "avg", "min", "max", "delta_ratio"):
            self.assertIn(key, out["stats"])

    def test_query_logs_returns_entries_newest_first(self):
        """⑥ 查日志：按服务过滤、新→旧、支持级别过滤。"""
        out = agent_tools.query_logs({"service": "api", "window_minutes": 120})
        self.assertEqual(out["service"], "api")
        self.assertGreater(out["total_matched"], 0)
        stamps = [row["ts_ms"] for row in out["entries"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "日志必须新→旧")
        for row in out["entries"]:
            self.assertEqual(row["service"], "api")
            self.assertIn(row["level"], ("DEBUG", "INFO", "WARN", "ERROR"))

        only_errors = agent_tools.query_logs(
            {"service": "api", "window_minutes": 120, "level": "ERROR"})
        self.assertTrue(only_errors["entries"])
        self.assertTrue(all(r["level"] == "ERROR" for r in only_errors["entries"]))

    def test_list_deployments_returns_history(self):
        """⑦ 查发布：按时间倒序、带可疑标注、不传 service 时跨服务。"""
        out = agent_tools.list_deployments({"service": "api", "limit": 5})
        self.assertGreaterEqual(out["count"], 2)
        self.assertEqual(out["deployments"][0]["version"], "v42")
        self.assertTrue(any(d["suspect"] for d in out["deployments"]),
                        "api 的 v42 应当是预置的可疑发布")

        everything = agent_tools.list_deployments({"limit": 20})
        self.assertGreaterEqual(everything["total"], out["total"])
        self.assertGreaterEqual(len({d["service"] for d in everything["deployments"]}), 2)

    def test_get_config_keeps_structure(self):
        """⑧ 查配置：结构保留（键都在）、带 revision、非敏感值原样。"""
        out = agent_tools.get_config({"service": "api"})
        self.assertEqual(out["service"], "api")
        self.assertEqual(out["environment"], "prod")
        self.assertTrue(out["revision"].startswith("cfg-api-"))
        values = out["values"]
        self.assertEqual(values["pool_max"], 20)
        self.assertEqual(values["db"]["host"], "pg-prod-01.internal")
        self.assertTrue(values["feature_sync_order_query"],
                        "prod 打开了同步调用开关（这正是 v42 的变更）")

    def test_get_config_masks_password_token_secret_key(self):
        """⑨【关键】password / token / secret / key 一律打码，原值不许出站。

        这条用例故意挑四种键名，其中 `signing_key` 是**只有本工具的脱敏能挡住**
        的那种（Dispatcher 的 Normalizer 敏感键正则里没有裸 `key`）——
        所以它同时证明了「脱敏发生在工具层」，而不是"恰好被 Normalizer 兜住了"。
        """
        out = agent_tools.get_config({"service": "api"})
        values = out["values"]
        mask = agent_tools.MASK

        # 四种敏感键名都必须在
        self.assertEqual(values["db_password"], mask)
        self.assertEqual(values["api_token"], mask)
        self.assertEqual(values["signing_key"], mask)
        self.assertEqual(values["db"]["password"], mask)
        self.assertEqual(values["tokens"], mask, "密钥列表整块打码")

        # 原值一个都不许出现（最直接的反向断言）
        blob = json.dumps(out, ensure_ascii=False)
        for secret in ("pg-Prod-Pa55w0rd", "ed25519-prod-signing-7f3a91",
                       "sk-live-9f8e7d6c5b4a3210", "sk-live-aaaaaaaaaaaaaaaa"):
            self.assertNotIn(secret, blob, f"敏感值泄漏：{secret}")

        # 非敏感键不许被误伤（过脱敏 = 看不见配置，同样是坏结果）
        self.assertEqual(values["db"]["user"], "app_rw")
        self.assertEqual(values["db"]["port"], 5432)
        self.assertEqual(values["pool_max"], 20)

    def test_get_config_reports_masked_fields(self):
        """⑩ 打码字段要列出来：让人知道"有但看不到"，而不是"没配"。"""
        out = agent_tools.get_config({"service": "payments", "environment": "prod"})
        self.assertIn("bank_client_secret", out["masked_fields"])
        self.assertEqual(out["masked_total"], len(out["masked_fields"]))
        self.assertGreater(out["masked_total"], 0)
        self.assertIn("打码", out["masking"])

    def test_mask_secrets_is_recursive_and_shape_preserving(self):
        """⑪ 脱敏要走进嵌套结构与列表，且保留结构。"""
        sample = {
            "db": {"user": "u", "password": "p", "nested": {"api_key": "k"}},
            "tokens": ["a", "b"],
            "items": [{"secret": "s"}, {"plain": "ok"}],
            "dsn": "postgres://user:pw@host:5432/db",
            "raw": "sk-abcdefghijklmnop",
        }
        masked, hits = agent_tools.mask_secrets(sample)
        self.assertEqual(masked["db"]["password"], agent_tools.MASK)
        self.assertEqual(masked["db"]["nested"]["api_key"], agent_tools.MASK)
        self.assertEqual(masked["tokens"], agent_tools.MASK)
        self.assertEqual(masked["items"][0]["secret"], agent_tools.MASK)
        self.assertEqual(masked["items"][1]["plain"], "ok", "非敏感字段不许误伤")
        self.assertNotIn("pw@", masked["dsn"])
        self.assertNotIn("sk-abcdefghijklmnop", masked["raw"])
        self.assertIn("db.password", hits)
        self.assertIn("db.nested.api_key", hits)


# ══════════════════════════════════════════════════════════════
# ③ 副作用定级（**这一组是本文件的重点，不许为了跑得顺降级**）
# ══════════════════════════════════════════════════════════════
class SideEffectTierTest(AgentToolBase):

    def test_read_only_tools_are_t0(self):
        """⑫ 四个读工具的定级：READ_ONLY / T0 / 幂等 / tool.read。"""
        for name in ("query_metrics", "query_logs", "list_deployments", "get_config"):
            with self.subTest(tool=name):
                manifest = self.manifest(name)
                self.assertIs(manifest.side_effect, SideEffect.READ_ONLY)
                self.assertIs(manifest.risk_tier, RiskTier.T0_SAFE)
                self.assertTrue(manifest.idempotent)
                self.assertEqual(manifest.required_scope, "tool.read")

    def test_restart_service_is_mutating_t2(self):
        """⑬ 重启：MUTATING / T2（可再重启→可回改）/ 不幂等 / tool.write。"""
        manifest = self.manifest("restart_service")
        self.assertIs(manifest.side_effect, SideEffect.MUTATING)
        self.assertIs(manifest.risk_tier, RiskTier.T2_HIGH)
        self.assertFalse(manifest.idempotent,
                         "重启必须不幂等：同参数第二次调用不能被幂等缓存吃掉")
        self.assertEqual(manifest.required_scope, "tool.write")

    def test_scale_service_is_mutating_t2(self):
        """⑭ 扩缩容：MUTATING / T2（改错了能再改回来）。"""
        manifest = self.manifest("scale_service")
        self.assertIs(manifest.side_effect, SideEffect.MUTATING)
        self.assertIs(manifest.risk_tier, RiskTier.T2_HIGH)
        self.assertFalse(manifest.idempotent)
        self.assertEqual(manifest.required_scope, "tool.write")

    def test_rollback_deployment_is_irreversible_t3(self):
        """⑮ 回滚：IRREVERSIBLE / T3（schema 迁移与已写数据不随回滚恢复）。"""
        manifest = self.manifest("rollback_deployment")
        self.assertIs(manifest.side_effect, SideEffect.IRREVERSIBLE)
        self.assertIs(manifest.risk_tier, RiskTier.T3_CRITICAL)
        self.assertFalse(manifest.idempotent)
        self.assertEqual(manifest.required_scope, "tool.write")

    def test_delete_namespace_is_irreversible_t3(self):
        """⑯ 删命名空间：IRREVERSIBLE / T3。"""
        manifest = self.manifest("delete_namespace")
        self.assertIs(manifest.side_effect, SideEffect.IRREVERSIBLE)
        self.assertIs(manifest.risk_tier, RiskTier.T3_CRITICAL)
        self.assertFalse(manifest.idempotent)
        self.assertEqual(manifest.required_scope, "tool.write")

    def test_no_t2_or_t3_tool_is_marked_idempotent(self):
        """⑰ 所有 T2+ 都不许标幂等（标了就会把"第二次真的执行"缓存成假成功）。"""
        for manifest in agent_tools.MANIFESTS:
            if int(manifest.risk_tier) >= int(RiskTier.T2_HIGH):
                with self.subTest(tool=manifest.name):
                    self.assertFalse(manifest.idempotent,
                                     f"{manifest.name} 是 T{int(manifest.risk_tier)}，不能幂等")


# ══════════════════════════════════════════════════════════════
# ④ 真 Dispatcher（权限 / 审批 / 硬拒）
# ══════════════════════════════════════════════════════════════
class DispatcherIntegrationTest(AgentToolBase):

    def test_missing_required_arg_fails(self):
        """⑱ 缺必填参数 → failed（retryable，模型能改参数重来）。"""
        outcome = self.call("query_metrics", {"service": "api"})
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("invalid args", outcome.reason)
        self.assertIn("metric", outcome.reason)
        self.assertTrue(outcome.retryable)

    def test_unknown_field_is_rejected(self):
        """⑲ 未声明字段 → failed（不给"静默接受 + 空结果"的机会）。"""
        outcome = self.call("query_metrics",
                            {"service": "api", "metric": "p99", "limit": 5})
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("invalid args", outcome.reason)

    def test_t0_tool_passes_directly(self):
        """⑳ T0 读工具：无审批、直接执行，结果形状与直调一致。"""
        outcome = self.call("query_metrics", {"service": "api", "metric": "p99"})
        self.assertEqual(outcome.kind, "ok", outcome.reason)
        payload = outcome.result.payload
        self.assertEqual(payload["metric"], "p99")
        self.assertIn("stats", payload)
        self.assertFalse(outcome.result.truncated)

    def test_t2_tool_needs_confirmation_without_approval(self):
        """㉑ T2 无审批 → needs_confirmation（问人，不是拒绝），且不执行副作用。"""
        outcome = self.call("restart_service", {"service": "api", "reason": "验证"})
        self.assertEqual(outcome.kind, "needs_confirmation", outcome.reason)
        self.assertIn("requires approval", outcome.reason)
        self.assertEqual(self.backend.events(), [], "待确认的调用不许产生副作用")

    def test_t2_tool_runs_with_approval(self):
        """㉒ T2 带正确审批 → ok，且副作用真的落到后端。"""
        args = {"service": "api", "reason": "连接池异常，取证后重启"}
        outcome = self.call("restart_service", args,
                            approvals=(self.approval("restart_service", args),))
        self.assertEqual(outcome.kind, "ok", outcome.reason)
        self.assertEqual(outcome.result.payload["restarted_pods"], 12)
        events = self.backend.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "restart")

    def test_t2_approval_for_other_args_does_not_count(self):
        """㉓ 审批是**绑 args_hash** 的：批了 A 参数不能拿去跑 B 参数。"""
        approved = {"service": "api", "reason": "批过的那次"}
        other = {"service": "api", "reason": "没批过的那次"}
        outcome = self.call("restart_service", other,
                            approvals=(self.approval("restart_service", approved),))
        self.assertEqual(outcome.kind, "needs_confirmation", outcome.reason)
        self.assertEqual(self.backend.events(), [])

    def test_t3_tool_is_denied_not_pending(self):
        """㉔【关键】T3 是**硬拒**，不是待确认；带着"已批准"也照样拒绝。"""
        for name, args in (
            ("rollback_deployment", {"service": "api"}),
            ("delete_namespace", {"namespace": "prod-core"}),
        ):
            with self.subTest(tool=name):
                outcome = self.call(name, args,
                                    approvals=(self.approval(name, args),))
                self.assertEqual(outcome.kind, "denied", outcome.reason)
                self.assertIn("cannot be confirmed", outcome.reason)
                self.assertIn("T3", outcome.reason)
        self.assertEqual(self.backend.events(), [],
                         "T3 被拒时 handler 绝不该执行，后端状态必须原封不动")
        self.assertEqual(self.backend.state()["services"]["api"]["version"], "v42")
        self.assertIn("prod-core", self.backend.state()["namespaces"])

    def test_missing_scope_is_denied(self):
        """㉕ 缺 scope → denied（权限在审批之前，模型没有"求一次"的机会）。"""
        reader = Principal(scopes=frozenset({"tool.write"}))
        outcome = self.call("query_metrics", {"service": "api", "metric": "p99"},
                            principal=reader)
        self.assertEqual(outcome.kind, "denied")
        self.assertIn("lacks scope", outcome.reason)

    def test_t0_result_is_redacted_by_tool_not_only_by_normalizer(self):
        """㉖ 过 Dispatcher 后原值不出现；`signing_key` 仍是我们自己的掩码。

        区分点：`db_password` 会被 Normalizer 的敏感键规则二次替换成
        `***REDACTED***`，而 `signing_key` **只有**本工具会挡（Normalizer 的正则
        里没有裸 `key`）。所以后者的值是 `***MASKED***`，正好证明脱敏在工具层发生。
        """
        outcome = self.call("get_config", {"service": "api"})
        self.assertEqual(outcome.kind, "ok", outcome.reason)
        payload = outcome.result.payload
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("pg-Prod-Pa55w0rd", blob)
        self.assertNotIn("ed25519-prod-signing-7f3a91", blob)
        # signing_key 只有工具层挡得住 → 过 Dispatcher 之后仍是我们的掩码
        self.assertEqual(payload["values"]["signing_key"], agent_tools.MASK)
        # db.password 会被 Normalizer 的敏感键规则二次替换（它认 password），
        # 所以这里接受两种掩码之一 —— 关键断言是"原值不出现"（上面两条）
        self.assertIn(payload["values"]["db"]["password"],
                      {agent_tools.MASK, "***REDACTED***"})


# ══════════════════════════════════════════════════════════════
# ⑤ 技能（3 个 + 双层装载）
# ══════════════════════════════════════════════════════════════
class SkillSpecTest(AgentToolBase):

    def test_three_skills_have_complete_fields(self):
        """㉗ 三个技能字段完整：名字 / 标题 / 描述 / 触发词 / 步骤 / 避坑 / 验收 / 工具 / 版本。"""
        specs = agent_skills.default_skills()
        self.assertEqual([s.name for s in specs],
                         ["investigate_latency", "safe_rollback", "capacity_check"])
        for spec in specs:
            with self.subTest(skill=spec.name):
                self.assertTrue(spec.title)
                self.assertTrue(spec.description)
                self.assertTrue(spec.trigger)
                self.assertGreaterEqual(len(spec.steps), 3, "SOP 至少三步")
                self.assertTrue(spec.pitfalls, "没有避坑的技能等于只写了 happy path")
                self.assertTrue(spec.verification, "没有验收标准就无法判定做完没做完")
                self.assertTrue(spec.tools, "技能必须引用工具，否则只是文档")
                self.assertEqual(spec.version, agent_skills.SKILL_VERSION)
                self.assertEqual(spec.source, "手工")

    def test_index_text_is_much_shorter_than_body(self):
        """㉘ 索引常驻必须短，规程按需必须长：index_text ≤ body / 3。"""
        for spec in agent_skills.default_skills():
            with self.subTest(skill=spec.name):
                index = spec.index_text
                body = spec.body_text()
                self.assertNotIn("\n", index, "索引必须一行（常驻 Prompt）")
                self.assertLessEqual(len(index) * 3, len(body),
                                     f"{spec.name}: index={len(index)} body={len(body)}")
                # 规程全文里必须能看到步骤与避坑，否则 body_text 拼装有问题
                self.assertIn("## 步骤", body)
                self.assertIn("## 避坑", body)

    def test_skill_tools_are_all_registered(self):
        """㉙ 技能引用的工具必须都存在（悬空引用 = 模型照着 SOP 调不到工具）。"""
        registered = set(agent_tools.tool_names())
        for spec in agent_skills.default_skills():
            for tool in spec.tools:
                with self.subTest(skill=spec.name, tool=tool):
                    self.assertIn(tool, registered)
        # 反向：回滚工具必须被 safe_rollback 显式提到（并说明它是 T3 硬拒），
        # 而不是让模型自己发现"有个工具叫 rollback_deployment，试试看"
        by_name = {s.name: s.tools for s in agent_skills.default_skills()}
        self.assertIn("rollback_deployment", by_name["safe_rollback"])

    def test_skill_runtime_loads_body_only_on_match(self):
        """㉚ SkillRuntime 双层装载：索引常驻全量，正文只装命中的那一个。"""
        runtime = SkillRuntime(agent_skills.build_library())
        index = runtime.index()
        for spec in agent_skills.default_skills():
            self.assertIn(spec.name, index, "索引必须包含全部技能")

        rendered = runtime.render("p99 上涨怎么排查")
        loaded = [item["name"] for item in rendered["loaded"]]
        self.assertEqual(loaded, ["investigate_latency"])
        self.assertIn("query_metrics", rendered["loaded"][0]["body"])
        self.assertFalse(rendered["truncated"])

        rollback = runtime.render("帮我回滚上个版本")
        self.assertEqual([i["name"] for i in rollback["loaded"]], ["safe_rollback"])

    def test_skill_bodies_mention_side_effect_of_dangerous_steps(self):
        """㉛ SOP 里对 T2/T3 动作必须写明"要审批/会被拒"，而不是让模型自己猜。"""
        bodies = {s.name: s.body_text() for s in agent_skills.default_skills()}
        self.assertIn("T3", bodies["safe_rollback"])
        self.assertIn("审批", bodies["safe_rollback"])
        self.assertIn("T2", bodies["capacity_check"])
        self.assertIn("审批", bodies["investigate_latency"])


# ══════════════════════════════════════════════════════════════
# ⑥ Agent 注册表 + Router
# ══════════════════════════════════════════════════════════════
class RegistryTest(AgentToolBase):

    def test_default_agents_returns_three(self):
        """㉜ 三个 Agent，各有描述 / tags / scope / tools / skills / priority。"""
        agents = agent_registry.default_agents()
        self.assertEqual([a.name for a in agents], ["sre", "release", "capacity"])
        for spec in agents:
            with self.subTest(agent=spec.name):
                self.assertTrue(spec.description)
                self.assertTrue(spec.tags)
                self.assertTrue(spec.required_scopes)
                self.assertTrue(spec.tools)
                self.assertTrue(spec.skills)
                self.assertIsInstance(spec.priority, int)
        # 每次调用都是新对象（frozen dataclass，但列表本身要能安全共享）
        self.assertEqual(len(agent_registry.default_agents()), 3)

    def test_agent_tools_and_skills_exist(self):
        """㉝ Agent 引用的工具与技能都必须存在（没有悬空引用）。"""
        registered_tools = set(agent_tools.tool_names())
        registered_skills = set(agent_skills.skill_names())
        for spec in agent_registry.default_agents():
            for tool in spec.tools:
                with self.subTest(agent=spec.name, tool=tool):
                    self.assertIn(tool, registered_tools)
            for skill in spec.skills:
                with self.subTest(agent=spec.name, skill=skill):
                    self.assertIn(skill, registered_skills)

    def test_only_release_holds_write_scope(self):
        """㉞ 写权限只给 release：权限在 Router 里是**剔除**，不是扣分。"""
        scopes = {a.name: set(a.required_scopes) for a in agent_registry.default_agents()}
        self.assertIn("tool.write", scopes["release"])
        self.assertNotIn("tool.write", scopes["sre"])
        self.assertNotIn("tool.write", scopes["capacity"])

    def test_route_latency_goal_to_sre(self):
        """㉟「p99 上涨怎么排查」→ sre，且领先第二名足够多。"""
        router = agent_registry.build_router()
        decision = router.route("p99 上涨怎么排查", FULL)
        self.assertEqual(decision.agent, "sre", decision.reason)
        self.assertGreaterEqual(decision.confidence,
                                agent_registry.DEFAULT_CONFIDENCE_THRESHOLD)
        self.assertGreaterEqual(decision.confidence - decision.candidates[1][1], 0.08)

    def test_route_rollback_goal_to_release(self):
        """㊱「帮我回滚上个版本」→ release。"""
        router = agent_registry.build_router()
        decision = router.route("帮我回滚上个版本", FULL)
        self.assertEqual(decision.agent, "release", decision.reason)
        self.assertGreaterEqual(decision.confidence,
                                agent_registry.DEFAULT_CONFIDENCE_THRESHOLD)

    def test_route_capacity_goal_to_capacity(self):
        """㊲「要不要扩容」→ capacity。"""
        router = agent_registry.build_router()
        decision = router.route("要不要扩容", FULL)
        self.assertEqual(decision.agent, "capacity", decision.reason)
        self.assertGreaterEqual(decision.confidence,
                                agent_registry.DEFAULT_CONFIDENCE_THRESHOLD)

    def test_unmatched_goal_clarifies(self):
        """㊳ 无匹配 goal → `__clarify__`（宁可问一句，不许硬选）。"""
        router = agent_registry.build_router()
        for goal in ("今天天气怎么样", "给我写一首关于秋天的诗", ""):
            with self.subTest(goal=goal):
                decision = router.route(goal, FULL)
                self.assertEqual(decision.agent, CLARIFY_AGENT, decision.reason)

    def test_read_only_principal_cannot_reach_release(self):
        """㊴ 只有读权限的账号，回滚请求必须回澄清（release 被权限剔除）。"""
        reader = Principal(scopes=frozenset({"tool.read"}))
        router = agent_registry.build_router()
        decision = router.route("帮我回滚上个版本", reader)
        self.assertEqual(decision.agent, CLARIFY_AGENT, decision.reason)
        self.assertNotIn("release", dict(decision.candidates))


# ══════════════════════════════════════════════════════════════
# ⑦ 配置面：agents/tools.yaml 必须能被 toolconfig 真加载
# ══════════════════════════════════════════════════════════════
class ToolConfigYamlTest(AgentToolBase):

    def _load(self):
        loaded = load_tools(ROOT / "agents" / "tools.yaml", cwd=ROOT)
        self.addCleanup(loaded.close)
        return loaded

    def test_yaml_loads_with_toolconfig(self):
        """㊵ `load_tools()` 真加载成功，且没有需要人看的 warning。"""
        loaded = self._load()
        self.assertEqual(list(loaded.warnings), [],
                         f"加载有告警，说明配置与预期不一致：{loaded.warnings}")
        self.assertTrue(loaded.manifests)

    def test_loaded_tool_count_matches_code(self):
        """㊶ 配置里的工具数 == 代码里声明的工具数（一一对应）。"""
        loaded = self._load()
        self.assertEqual(len(loaded.names()), len(agent_tools.MANIFESTS))
        self.assertEqual(sorted(loaded.names()), sorted(agent_tools.tool_names()))

    def test_yaml_side_effects_match_code(self):
        """㊷ side_effect / risk_tier / scope / 幂等 四项逐工具一致。

        漂移的后果很具体：YAML 说 T3、代码说 T2，就是"审批一下就把数据删了"。
        """
        loaded = self._load()
        code = {m.name: m for m in agent_tools.MANIFESTS}
        for manifest in loaded.manifests:
            with self.subTest(tool=manifest.name):
                expected = code[manifest.name]
                self.assertIs(manifest.side_effect, expected.side_effect)
                self.assertIs(manifest.risk_tier, expected.risk_tier)
                self.assertEqual(manifest.required_scope, expected.required_scope)
                self.assertEqual(manifest.idempotent, expected.idempotent)

    def test_yaml_input_schema_matches_code(self):
        """㊸ 入参契约也必须一致（模型看到的参数名只来自这里）。"""
        loaded = self._load()
        code = {m.name: m for m in agent_tools.MANIFESTS}
        for manifest in loaded.manifests:
            with self.subTest(tool=manifest.name):
                self.assertEqual(manifest.input_schema, code[manifest.name].input_schema)

    def test_yaml_end_to_end_dispatch(self):
        """㊹ 端到端：YAML 加载 → 注册进 ToolRegistry → 真 Dispatcher 调通。

        顺带验 T3 在**配置加载出来的那套 manifest** 上同样是硬拒 ——
        否则"改配置就能绕过 T3"就成了一个没人试过的假设。
        """
        loaded = self._load()
        registry = ToolRegistry()
        loaded.register_into(registry)
        dispatcher = Dispatcher(registry, artifact_dir=os.path.join(self.tmp, "yaml-art"))

        ok = dispatcher.invoke("query_metrics", {"service": "search", "metric": "cpu"},
                               self.ctx())
        self.assertEqual(ok.kind, "ok", ok.reason)
        self.assertGreater(ok.result.payload["stats"]["latest"], 0.5)

        masked = dispatcher.invoke("get_config", {"service": "search"}, self.ctx())
        self.assertEqual(masked.kind, "ok", masked.reason)
        self.assertNotIn("es-Prod-Pa55",
                         json.dumps(masked.result.payload, ensure_ascii=False))

        denied = dispatcher.invoke("delete_namespace", {"namespace": "prod-search"},
                                   self.ctx(approvals=(self.approval(
                                       "delete_namespace", {"namespace": "prod-search"}),)))
        self.assertEqual(denied.kind, "denied", denied.reason)
        self.assertIn("cannot be confirmed", denied.reason)
        # 注意：这里用的是**模块级** BACKEND（YAML 的 handler 是普通函数），
        # 只断言"被拒"，不断言状态（模块级实例由别的用例共享）。
        self.assertEqual(denied.result, None)

    def test_yaml_explicitly_allows_irreversible(self):
        """㊺ 配置里必须显式写 `allow_irreversible: true`，不许靠默认值。

        这条守的是"能力必须显式签字"：T3 工具能被加载，是因为有人在配置里
        写了这一行、并且它会进 code review —— 而不是因为加载器默默放行。
        """
        text = (ROOT / "agents" / "tools.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^\s*allow_irreversible:\s*true\s*$")
        self.assertIn("irreversible", text)
        # 反面确认：不写 defaults 时同一份工具表会被拒绝加载 —— 这不测（要改文件），
        # 但可以用代码里那条闸门的语义确认一次：T3 工具确实存在
        irreversibles = [m.name for m in agent_tools.MANIFESTS
                         if m.side_effect is SideEffect.IRREVERSIBLE]
        self.assertEqual(sorted(irreversibles),
                         ["delete_namespace", "rollback_deployment"])


# ══════════════════════════════════════════════════════════════
# ⑧ 批量取证：`query_logs` / `get_config` 也要能一次查多个服务
# ══════════════════════════════════════════════════════════════
class BatchQueryTest(AgentToolBase):
    """工具粒度复核（缺陷 B 的一半）。

    `query_metrics` 早就支持 `services=[...]` + `metrics=[...]`，但 `query_logs`
    与 `get_config` 一次只能查一个服务 —— 模型要横向对比三个服务就得发三次，
    而真模型恰恰是在这种"重复往返"里把步数烧光的（实测 18 次 `query_metrics`）。

    两条硬约束：

      * **批量返回统一外壳** `{results, ok_count, error_count}`：模型一眼能看出
        "这批发了几条、有没有失败"，不必再发一次确认；
      * **单查形状不许变**：老调用方（demo、技能 SOP、既有用例）读的是
        `out["entries"]` / `out["values"]`，多一个 `results` 键就会把它们弄坏。
    """

    def test_query_logs_supports_services_batch(self):
        """⑨ `services=[...]` 一次扫多个服务，外壳带 ok/error 计数。"""
        out = agent_tools.query_logs(
            {"services": ["api", "payments"], "window_minutes": 120},
            backend=self.backend)
        self.assertEqual(set(out) >= {"results", "ok_count", "error_count"}, True)
        self.assertEqual(out["ok_count"], 2)
        self.assertEqual(out["error_count"], 0)
        self.assertEqual([r["service"] for r in out["results"]], ["api", "payments"])
        for row in out["results"]:
            data = row["data"]
            self.assertEqual(data["service"], row["service"], "每条结果必须是自己的服务")
            self.assertIn("entries", data)
            for entry in data["entries"]:
                self.assertEqual(entry["service"], row["service"])
        # api 是预置的"出事服务"：批量里必须还能看见它的错误行
        api = [r for r in out["results"] if r["service"] == "api"][0]["data"]
        self.assertTrue(any(e["level"] == "ERROR" for e in api["entries"]))

    def test_query_logs_single_shape_unchanged(self):
        """⑩ 单查（`service` 或长度为 1 的 `services`）形状与老版本逐字段相同。"""
        via_single = agent_tools.query_logs(
            {"service": "api", "window_minutes": 120}, backend=self.backend)
        expected = self.backend.logs("api", 120, "", 20)
        self.assertEqual(via_single, expected)
        self.assertNotIn("results", via_single, "单查不许套批量外壳")
        self.assertEqual(set(via_single),
                         {"service", "window_minutes", "level",
                          "total_matched", "returned", "entries"})
        # 长度为 1 的 services 也走单查路径（同一个老形状）
        via_list = agent_tools.query_logs(
            {"services": ["api"], "window_minutes": 120}, backend=self.backend)
        self.assertEqual(via_list, expected)

    def test_get_config_supports_services_batch(self):
        """⑪ `get_config` 同样支持 `services=[...]`，每条的 `data` 就是单查对象。"""
        out = agent_tools.get_config(
            {"services": ["api", "search"], "environment": "prod"},
            backend=self.backend)
        self.assertEqual(out["ok_count"], 2)
        self.assertEqual(out["error_count"], 0)
        self.assertEqual(out["environment"], "prod")
        for row in out["results"]:
            data = row["data"]
            self.assertEqual(data["service"], row["service"])
            self.assertEqual(data["environment"], "prod")
            self.assertIn("values", data)
            # 脱敏在批量路径上同样生效（两条路径共用一个实现，这条守着它）
            self.assertGreater(data["masked_total"], 0, "批量路径漏了脱敏")
        api_values = [r["data"] for r in out["results"] if r["service"] == "api"][0]["values"]
        self.assertEqual(api_values["db_password"], agent_tools.MASK)
        self.assertEqual(api_values["signing_key"], agent_tools.MASK)
        # 逐条与单查完全一致（同构，模型不需要学两套解析）
        api_single = agent_tools.get_config({"service": "api"}, backend=self.backend)
        api_batch = [r["data"] for r in out["results"] if r["service"] == "api"][0]
        self.assertEqual(api_batch, api_single)

    def test_get_config_single_shape_unchanged(self):
        """⑫ 单查形状不变：老字段一个不少、也不多出批量外壳。"""
        out = agent_tools.get_config({"service": "api"}, backend=self.backend)
        self.assertEqual(set(out),
                         {"service", "environment", "revision", "values",
                          "masked_fields", "masked_total", "masking"})
        self.assertEqual(out["service"], "api")
        self.assertTrue(out["revision"].startswith("cfg-api-"))

    def test_batch_partial_failure_keeps_the_rest(self):
        """⑬ 批量里某个服务不存在 → 那一条 error，其余照常返回（不整批失败）。"""
        out = agent_tools.get_config(
            {"services": ["api", "ghost"]}, backend=self.backend)
        self.assertEqual(out["ok_count"], 1)
        self.assertEqual(out["error_count"], 1)
        ok = [r for r in out["results"] if r["service"] == "api"][0]
        bad = [r for r in out["results"] if r["service"] == "ghost"][0]
        self.assertIn("data", ok)
        self.assertNotIn("data", bad)
        self.assertIn("error", bad)
        self.assertIn("ghost", bad["error"], "错误文本要指认是哪个服务")

        logs = agent_tools.query_logs(
            {"services": ["api", "ghost"]}, backend=self.backend)
        self.assertEqual((logs["ok_count"], logs["error_count"]), (1, 1))

    def test_yaml_schema_and_dispatcher_agree_on_batch_args(self):
        """⑭ `tools.yaml` 与代码的 schema 一致，且 Dispatcher 真的放行 `services`。

        配置面漂移的后果很具体：代码支持批量、配置里没有 `services` →
        模型**看不到**这个参数（schema 是它唯一的说明书），等于没做。
        """
        loaded = load_tools(ROOT / "agents" / "tools.yaml", cwd=ROOT)
        self.addCleanup(loaded.close)
        by_name = {m.name: m for m in loaded.manifests}
        code = {m.name: m for m in agent_tools.MANIFESTS}
        for name in ("query_logs", "get_config"):
            with self.subTest(tool=name):
                self.assertEqual(by_name[name].input_schema, code[name].input_schema)
                props = by_name[name].input_schema["properties"]
                self.assertIn("services", props)
                self.assertIn("service", props, "单查参数必须保留（向后兼容）")
                self.assertEqual(len(props["services"]["items"]["enum"]),
                                 len(agent_tools.SERVICE_NAMES))

        # 端到端：真 Dispatcher（走 schema 校验）放行批量参数
        out = self.call("query_logs", {"services": ["api", "search"]})
        self.assertEqual(out.kind, "ok", out.reason)
        self.assertEqual(out.result.payload["ok_count"], 2)
        cfg = self.call("get_config", {"services": ["api", "search"]})
        self.assertEqual(cfg.kind, "ok", cfg.reason)
        self.assertEqual(cfg.result.payload["ok_count"], 2)
        # 反方向：两个都不给 → 校验层拒掉（anyOf 生效，不是靠 handler 抛异常）
        bad = self.call("query_logs", {"window_minutes": 60})
        self.assertEqual(bad.kind, "failed", bad.reason)
        self.assertIn("invalid args", bad.reason)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main(verbosity=2)
