"""tests/test_langfuse.py — Langfuse 导出层（plane/langfuse_export.py）单测。

全部**不联网**：
  * 绝大多数用例注入假客户端（`FakeClient`），只用它记录"被调了什么"；
  * 有 1 条用例用**真** langfuse 客户端，但塞进自带的 `span_exporter` +
    注入 `tracer_provider`，并把 `socket.connect` 屏蔽掉 —— 目的是验证我们
    用的是 5.2.0 的真实 API 形状，而不是只验证自己编的假对象。

覆盖的三件事：自述能力不撒谎 / 纯转换可测 / 导出 fail-open 且不泄密。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import socket
import sys
import unittest
from unittest import mock

from plane import langfuse_export as lf
from plane.quality import Tracer


# ══════════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════════
class FakeSpan:
    """假 span：只记 id / 是否 end。"""

    def __init__(self, span_id: str) -> None:
        self.id = span_id
        self.trace_id = ""
        self.ended = False
        self.end_time = None
        self.updates: list[dict] = []

    def end(self, *, end_time=None):
        self.ended = True
        self.end_time = end_time
        return self

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self


class FakeClient:
    """假 Langfuse 客户端。

    fail=True        → 每次 start_observation 都抛错（验证 fail-open）
    echo             → 把 `%s` 替换成 observation 名字后写进异常消息（验证脱敏）
    flush_error      → flush 抛错的消息（验证导出结果里的消息也脱敏）
    """

    def __init__(self, *, fail: bool = False, echo: str = "",
                 flush_error: str = "") -> None:
        self.calls: list[dict] = []
        self.scopes: list[dict] = []
        self.span_ids: list[str] = []
        self.flushed = 0
        self.shutdown_calls = 0
        self.fail = fail
        self.echo = echo
        self.flush_error = flush_error

    # ── 被本模块用到的三个 API ──────────────────────────────
    def create_trace_id(self, *, seed=None) -> str:
        return hashlib.sha256((str(seed) or "x").encode()).hexdigest()[:32]

    def start_observation(self, **kwargs):
        self.calls.append(kwargs)
        if self.echo:
            raise RuntimeError(self.echo % kwargs.get("name", "?"))
        if self.fail:
            raise RuntimeError("boom: 后端 503")
        span = FakeSpan(f"obs-{len(self.calls):02d}")
        self.span_ids.append(span.id)
        return span

    def propagate_attributes(self, **kwargs):
        self.scopes.append(kwargs)
        return contextlib.nullcontext()

    def flush(self) -> None:
        self.flushed += 1
        if self.flush_error:
            raise RuntimeError(self.flush_error)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class CountingFactory:
    """工厂替身：记录被建了几次。"""

    def __init__(self) -> None:
        self.made: list[dict] = []

    def __call__(self, public_key="", secret_key="", host=""):
        self.made.append({"host": host, "pk": public_key})
        return FakeClient()


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _tracer_with_tree() -> Tracer:
    """三层树：turn → (llm, tool → retry)。"""
    t = Tracer(tenant="acme", user_id="u1", task_id="t1")
    with t.span("turn", {"phase": "EXECUTING"}):
        with t.span("llm", {"model": "mock-1", "input_tokens": 10,
                            "output_tokens": 5}):
            pass
        with t.span("tool", {"tool": "search"}):
            with t.span("retry", {"level": "WARNING", "attempt": 2}):
                pass
    return t


def _probe(sdk: bool, otlp: bool) -> tuple[dict, dict]:
    """构造两个探测结果，用于 patch。"""
    return (
        {"available": sdk, "version": "5.2.0" if sdk else "",
         "reason": "" if sdk else "langfuse 未安装: ImportError"},
        {"available": otlp,
         "reason": "" if otlp else "opentelemetry-exporter-otlp-proto-http 未安装"},
    )


class _LFTest(unittest.TestCase):
    """统一清状态：每个用例都从「未配置」开始。"""

    def setUp(self) -> None:
        lf.reset()

    def tearDown(self) -> None:
        lf.reset()


# ══════════════════════════════════════════════════════════════
# ① detect()：自述能力
# ══════════════════════════════════════════════════════════════
class TestDetect(_LFTest):
    def test_detect_shape_and_priority(self) -> None:
        """1. detect() 返回三个键 + mode + reason，且 mode 与能力一致。"""
        info = lf.detect()
        for key in ("langfuse_sdk", "otel_otlp", "mode", "reason"):
            self.assertIn(key, info)
        self.assertIsInstance(info["langfuse_sdk"], bool)
        self.assertIsInstance(info["otel_otlp"], bool)
        self.assertIn(info["mode"], ("sdk", "otlp", "none"))
        self.assertTrue(info["reason"], "reason 必须说清依据，不许空")
        expect = ("sdk" if info["langfuse_sdk"]
                  else "otlp" if info["otel_otlp"] else "none")
        self.assertEqual(info["mode"], expect, "mode 必须按 sdk > otlp > none 优先级")

    def test_detect_without_langfuse_module(self) -> None:
        """2. sys.modules 里把 langfuse 置 None（等价于未装）→ langfuse_sdk=False。"""
        with mock.patch.dict(sys.modules, {"langfuse": None}):
            info = lf.detect()
        self.assertFalse(info["langfuse_sdk"])
        self.assertNotEqual(info["mode"], "sdk")
        self.assertIn("langfuse", info["reason"].lower())

    def test_detect_no_backends_at_all(self) -> None:
        """探测全空 → mode=none，reason 提到缺什么、怎么装。"""
        sdk, otlp = _probe(False, False)
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=otlp):
            info = lf.detect()
        self.assertEqual(info["mode"], "none")
        self.assertIn("pip install langfuse", info["reason"])


# ══════════════════════════════════════════════════════════════
# ② configure_langfuse()：配置了必须真生效
# ══════════════════════════════════════════════════════════════
class TestConfigure(_LFTest):
    def test_without_credentials_is_disabled(self) -> None:
        """3. 无凭据 → enabled=False，reason 指出缺 public_key/secret_key。"""
        cfg = lf.configure_langfuse()
        self.assertFalse(cfg["enabled"])
        self.assertIn("public_key", cfg["reason"])
        self.assertIn("secret_key", cfg["reason"])
        self.assertFalse(lf.current_state()["enabled"])

    def test_creds_without_sdk_falls_to_otlp(self) -> None:
        """4. 有凭据但 SDK 未装、OTLP 可用 → 走 otlp 且真的配上了。"""
        sdk, otlp = _probe(False, True)
        seen: dict = {}

        def fake_configure_otel(endpoint, *, service_name="", headers=None):
            seen.update({"endpoint": endpoint, "service_name": service_name,
                         "headers": headers or {}})
            return {"enabled": True, "endpoint": endpoint, "reason": ""}

        import plane.quality as q
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=otlp), \
                mock.patch.object(q, "configure_otel", fake_configure_otel):
            cfg = lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec",
                                        host="https://lf.example.com", service_name="svc")

        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "otlp")
        self.assertEqual(seen["endpoint"],
                         "https://lf.example.com/api/public/otel/v1/traces")
        self.assertEqual(seen["service_name"], "svc")
        token = base64.b64encode(b"pk-lf-pub:sk-lf-sec").decode()
        self.assertEqual(seen["headers"]["Authorization"], f"Basic {token}")

    def test_otlp_exporter_missing_does_not_fake_success(self) -> None:
        """4b/6. 有凭据 + mode=otlp 但 exporter 没挂上 → enabled=False + 原因。"""
        sdk, otlp = _probe(False, True)
        import plane.quality as q
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=otlp), \
                mock.patch.object(q, "configure_otel",
                                  return_value={"enabled": False,
                                                "endpoint": "",
                                                "reason": "exporter 包未安装"}):
            cfg = lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec",
                                        mode="otlp")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["mode"], "otlp")
        self.assertIn("exporter 包未安装", cfg["reason"])
        self.assertFalse(lf.current_state()["enabled"])

    def test_otlp_without_credentials(self) -> None:
        """otlp 模式没有 pk/sk → 明确说不齐（Basic 头拼不出来）。"""
        sdk, otlp = _probe(False, True)
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=otlp):
            cfg = lf.configure_langfuse(mode="otlp")
        self.assertFalse(cfg["enabled"])
        self.assertIn("public_key", cfg["reason"])

    def test_sdk_with_injected_client(self) -> None:
        """5. 有凭据 + SDK 可用（假客户端注入）→ enabled=True, mode=sdk。"""
        fake = FakeClient()
        sdk, otlp = _probe(True, True)
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk):
            cfg = lf.configure_langfuse(public_key="pk-lf-pub",
                                        secret_key="sk-lf-sec", client=fake)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "sdk")
        self.assertTrue(lf.current_state()["enabled"])

    def test_sdk_package_missing_reports_not_crash(self) -> None:
        """18. mode=sdk 但包没装 → enabled=False 且说明，不崩。"""
        sdk, otlp = _probe(False, True)
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk):
            cfg = lf.configure_langfuse(public_key="pk-lf-pub",
                                        secret_key="sk-lf-sec", mode="sdk")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["mode"], "sdk")
        self.assertIn("pip install langfuse", cfg["reason"])

    def test_client_factory_failure_is_reported_not_raised(self) -> None:
        """6. 建客户端炸了（且消息里带密钥）→ enabled=False，原因已脱敏。"""
        secret = "sk-lf-TOPSECRET-999"

        def bad_factory(public_key="", secret_key="", host=""):
            raise RuntimeError(f"auth failed for {secret}")

        cfg = lf.configure_langfuse(public_key="pk-lf-pub", secret_key=secret,
                                    client_factory=bad_factory)
        self.assertFalse(cfg["enabled"])
        self.assertIn("客户端创建失败", cfg["reason"])
        self.assertNotIn(secret, cfg["reason"])
        self.assertIn(lf.REDACTED, cfg["reason"])

    def test_unknown_mode_is_rejected(self) -> None:
        """未知 mode 不猜，直接 false + 可选值。"""
        cfg = lf.configure_langfuse(public_key="pk", secret_key="sk", mode="grpc")
        self.assertFalse(cfg["enabled"])
        self.assertIn("未知 mode", cfg["reason"])

    def test_mode_none_is_disabled(self) -> None:
        """显式 mode=none → 不导出，reason 说明。"""
        cfg = lf.configure_langfuse(public_key="pk", secret_key="sk", mode="none")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["mode"], "none")
        self.assertTrue(cfg["reason"])

    def test_configure_is_idempotent(self) -> None:
        """17. 同一套配置重复调用只建一次客户端；换 host 才重建。"""
        factory = CountingFactory()
        sdk, otlp = _probe(True, True)
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk):
            first = lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec",
                                          host="https://a.example.com",
                                          client_factory=factory)
            second = lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec",
                                           host="https://a.example.com",
                                           client_factory=factory)
            third = lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec",
                                          host="https://b.example.com",
                                          client_factory=factory)
        self.assertTrue(first["enabled"] and second["enabled"] and third["enabled"])
        self.assertEqual(len(factory.made), 2,
                         "同配置第二次应复用客户端，只有换 host 才重建")


# ══════════════════════════════════════════════════════════════
# ③ trace_summary()：纯转换
# ══════════════════════════════════════════════════════════════
class TestTraceSummary(_LFTest):
    def test_flattens_nested_tree(self) -> None:
        """7. 嵌套 span 树被压平，父在子前且 parent_id 指得对。"""
        summary = lf.trace_summary(_tracer_with_tree())
        names = [o["name"] for o in summary["observations"]]
        self.assertEqual(names, ["turn", "llm", "tool", "retry"])
        obs = summary["observations"]
        self.assertEqual(obs[1]["parent_id"], obs[0]["id"])
        self.assertEqual(obs[2]["parent_id"], obs[0]["id"])
        self.assertEqual(obs[3]["parent_id"], obs[2]["id"])
        # 根没有父；id 带 trace_id 前缀，便于跨次对齐
        self.assertEqual(obs[0]["parent_id"], "")
        self.assertTrue(obs[0]["id"].endswith("-000"))

    def test_keeps_name_time_and_attrs(self) -> None:
        """8. 保留 name / 时间 / 属性，level 与类型按约定推断。"""
        summary = lf.trace_summary(_tracer_with_tree())
        self.assertEqual(summary["trace_id"], summary["trace_id"].lower())
        self.assertIn("t1", summary["name"])
        llm = summary["observations"][1]
        self.assertEqual(llm["name"], "llm")
        self.assertEqual(llm["type"], "generation", "带 model 的 span 应识别为 generation")
        self.assertEqual(llm["model"], "mock-1")
        self.assertEqual(llm["usage"], {"input": 10, "output": 5, "total": 15})
        # 时间是 ISO-8601 UTC 字符串，可直接进 Langfuse
        self.assertIn("T", llm["start_time"])
        self.assertTrue(llm["start_time"].endswith("+00:00"))
        self.assertGreaterEqual(llm["end_time"], llm["start_time"])
        self.assertEqual(llm["metadata"]["model"], "mock-1")
        self.assertIn("duration_ms", llm["metadata"])
        retry = summary["observations"][3]
        self.assertEqual(retry["level"], "WARNING")
        self.assertEqual(retry["type"], "span")
        self.assertEqual(summary["observations"][2]["type"], "tool")

    def test_empty_tracer_gives_empty_observations(self) -> None:
        """9. 空 Tracer → observations 为空，不抛。"""
        summary = lf.trace_summary(Tracer())
        self.assertEqual(summary["observations"], [])
        self.assertEqual(summary["totals"].get("llm_calls"), 0)
        self.assertEqual(summary["totals"].get("span_count"), 0)
        self.assertTrue(summary["trace_id"])

    def test_summary_is_json_serializable(self) -> None:
        """属性里有 datetime / 自定义对象也要能 json.dumps（Langfuse metadata 是 JSON）。"""
        import datetime as dt

        class Weird:
            def __repr__(self) -> str:
                return "<weird>"

        t = Tracer(task_id="t2")
        with t.span("tool", {"when": dt.datetime(2026, 1, 2, 3, 4, 5),
                             "obj": Weird(), "nested": {"a": [1, 2, {"b": float("inf")}]}}):
            pass
        summary = lf.trace_summary(t)
        blob = json.dumps(summary)          # 不许抛
        self.assertIn("weird", blob)

    def test_summary_of_broken_tracer_fails_open(self) -> None:
        """finish() 炸了 → 返回空 observations + reason，不向上抛。"""
        class Broken:
            def finish(self):
                raise ValueError("no spans for you")

        summary = lf.trace_summary(Broken())
        self.assertEqual(summary["observations"], [])
        self.assertIn("finish", summary["reason"])


# ══════════════════════════════════════════════════════════════
# ④ export_tracer()：计数 / fail-open / 身份透传
# ══════════════════════════════════════════════════════════════
class TestExport(_LFTest):
    def test_export_counts_with_fake_client(self) -> None:
        """10. 假客户端 → exported 计数正确、span 均被 end、flush 被调用。"""
        fake = FakeClient()
        lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec", client=fake)
        res = lf.export_tracer(_tracer_with_tree())
        self.assertTrue(res["exported"] == 4, res)
        self.assertEqual(res["skipped"], 0)
        self.assertEqual(res["mode"], "sdk")
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(fake.flushed, 1)
        self.assertTrue(all(c["as_type"] in lf._SDK_TYPES for c in fake.calls))
        # 子 span 的 trace_context 带上了父 id
        self.assertNotIn("parent_span_id", fake.calls[0]["trace_context"])
        self.assertIn("parent_span_id", fake.calls[1]["trace_context"])

    def test_export_fails_open_when_client_raises(self) -> None:
        """11. 客户端每次都抛 → 返回统计、不抛，且状态翻回 enabled=False。"""
        fake = FakeClient(fail=True)
        lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec", client=fake)
        res = lf.export_tracer(_tracer_with_tree())   # 不许抛
        self.assertEqual(res["exported"], 0)
        self.assertEqual(res["skipped"], 4)
        self.assertIn("503", res["reason"])
        state = lf.current_state()
        self.assertFalse(state["enabled"], "一条都没发出去就不许继续宣称 enabled")
        self.assertIn("全部", state["reason"])

    def test_export_unconfigured(self) -> None:
        """12. 未配置 → exported=0, mode=none + reason。"""
        res = lf.export_tracer(_tracer_with_tree())
        self.assertEqual(res["exported"], 0)
        self.assertEqual(res["mode"], "none")
        self.assertTrue(res["reason"])
        self.assertIn("configure_langfuse", res["reason"])

    def test_export_empty_tracer_reason(self) -> None:
        """空 trace 也返回统计，reason 说明没有 span。"""
        fake = FakeClient()
        lf.configure_langfuse(public_key="pk", secret_key="sk", client=fake)
        res = lf.export_tracer(Tracer())
        self.assertEqual(res["exported"], 0)
        self.assertEqual(res["skipped"], 0)
        self.assertIn("没有 span", res["reason"])

    def test_export_with_injected_client_without_configure(self) -> None:
        """注入 client 可越过模块状态直接导出（多租户/临时导出用）。"""
        fake = FakeClient()
        res = lf.export_tracer(_tracer_with_tree(), client=fake)
        self.assertEqual(res["exported"], 4)
        self.assertEqual(res["mode"], "sdk")

    def test_identity_passed_to_export(self) -> None:
        """16. user_id / session_id / tags 被传到导出（假客户端断言）。"""
        fake = FakeClient()
        lf.configure_langfuse(public_key="pk-lf-pub", secret_key="sk-lf-sec", client=fake)
        lf.export_tracer(_tracer_with_tree(), user_id="u-42", session_id="s-7",
                         tags=["prod", "eval"])
        self.assertEqual(len(fake.scopes), 1, "应通过 propagate_attributes 传 trace 级属性")
        scope = fake.scopes[0]
        self.assertEqual(scope["user_id"], "u-42")
        self.assertEqual(scope["session_id"], "s-7")
        self.assertEqual(scope["tags"], ["prod", "eval"])

    def test_identity_falls_back_to_root_metadata(self) -> None:
        """客户端没有 propagate_attributes 时，身份降级写进根 span metadata。"""
        class NoScopeClient(FakeClient):
            propagate_attributes = None

        fake = NoScopeClient()
        lf.configure_langfuse(public_key="pk", secret_key="sk", client=fake)
        lf.export_tracer(_tracer_with_tree(), user_id="u-1", session_id="s-1")
        self.assertEqual(fake.calls[0]["metadata"]["langfuse_scope"],
                         {"user_id": "u-1", "session_id": "s-1"})

    def test_partial_failure_keeps_enabled(self) -> None:
        """部分失败 → skipped 计数、状态仍 enabled（不全军覆没就不摘牌）。"""
        class HalfBroken(FakeClient):
            def start_observation(self, **kwargs):
                if kwargs.get("name") == "retry":
                    self.calls.append(kwargs)
                    raise RuntimeError("单条失败")
                return super().start_observation(**kwargs)

        fake = HalfBroken()
        lf.configure_langfuse(public_key="pk", secret_key="sk", client=fake)
        res = lf.export_tracer(_tracer_with_tree())
        self.assertEqual(res["exported"], 3)
        self.assertEqual(res["skipped"], 1)
        self.assertIn("部分失败", res["reason"])
        self.assertTrue(lf.current_state()["enabled"])

    def test_otlp_mode_uses_otel_bridge(self) -> None:
        """otlp 模式下 export_tracer 走 plane.quality.tracer_to_otel。"""
        sdk, otlp = _probe(False, True)
        import plane.quality as q
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=otlp), \
                mock.patch.object(q, "configure_otel",
                                  return_value={"enabled": True, "endpoint": "e",
                                                "reason": ""}):
            cfg = lf.configure_langfuse(public_key="pk", secret_key="sk", mode="otlp")
        self.assertTrue(cfg["enabled"])
        with mock.patch.object(q, "tracer_to_otel",
                               return_value={"exported": 4, "skipped": 0, "reason": ""}) as bridge:
            res = lf.export_tracer(_tracer_with_tree())
        self.assertEqual(res["mode"], "otlp")
        self.assertEqual(res["exported"], 4)
        bridge.assert_called_once()


# ══════════════════════════════════════════════════════════════
# ⑤ 密钥不外泄
# ══════════════════════════════════════════════════════════════
class TestSecrets(_LFTest):
    PK = "pk-lf-PUBLIC-abc123"
    SK = "sk-lf-SECRET-xyz789"

    def test_secret_not_in_configure_result(self) -> None:
        """13. 密钥不出现在 configure_langfuse 返回值里（sdk 与 otlp 两条路）。"""
        sdk, otlp = _probe(True, True)
        with mock.patch.object(lf, "_probe_langfuse", return_value=sdk):
            cfg = lf.configure_langfuse(public_key=self.PK, secret_key=self.SK,
                                        client=FakeClient())
        blob = json.dumps(cfg)
        self.assertNotIn(self.SK, blob)
        self.assertNotIn(self.PK, blob)
        self.assertNotIn(self.SK, json.dumps(lf.current_state()))

        # otlp 路径：Basic token（base64 后可逆）同样不许出现在返回值里
        lf.reset()
        import plane.quality as q
        with mock.patch.object(lf, "_probe_langfuse", return_value=_probe(False, True)[0]), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=otlp), \
                mock.patch.object(q, "configure_otel",
                                  return_value={"enabled": True, "endpoint": "e",
                                                "reason": ""}):
            cfg2 = lf.configure_langfuse(public_key=self.PK, secret_key=self.SK,
                                         mode="otlp")
        token = base64.b64encode(f"{self.PK}:{self.SK}".encode()).decode()
        for blob in (json.dumps(cfg2), json.dumps(lf.current_state())):
            self.assertNotIn(self.SK, blob)
            self.assertNotIn(token, blob)

    def test_secret_not_in_export_result(self) -> None:
        """14. 密钥不出现在 export_tracer 返回值里（含 flush 抛错的消息）。"""
        secret = self.SK
        fake = FakeClient(flush_error=f"401 for key {secret}")
        lf.configure_langfuse(public_key=self.PK, secret_key=secret, client=fake)
        res = lf.export_tracer(_tracer_with_tree())
        blob = json.dumps(res)
        self.assertNotIn(secret, blob)
        self.assertNotIn(self.PK, res["reason"])
        self.assertIn(lf.REDACTED, res["reason"])

    def test_secret_not_in_exception_message(self) -> None:
        """15. 会回显 key 的客户端：异常消息进了 reason 但密钥被抹掉。"""
        secret = self.SK
        fake = FakeClient(echo=f"upstream rejected {secret} for %s")
        lf.configure_langfuse(public_key=self.PK, secret_key=secret, client=fake)
        res = lf.export_tracer(_tracer_with_tree())
        self.assertEqual(res["exported"], 0)
        self.assertNotIn(secret, res["reason"])
        self.assertNotIn(secret, json.dumps(res))
        self.assertIn(lf.REDACTED, res["reason"])
        # 模块状态里的原因同样不许漏
        self.assertNotIn(secret, json.dumps(lf.current_state()))

    def test_redact_covers_base64_and_quote_variants(self) -> None:
        """redact() 覆盖明文 / base64 / URL 编码三种形态。"""
        pk, sk = "pk-lf-AbCdEf", "sk-lf-B+/=GhIjK"
        lf.configure_langfuse(public_key=pk, secret_key=sk, client=FakeClient())
        sk_b64 = base64.b64encode(sk.encode()).decode()
        sk_quoted = sk.replace("/", "%2F").replace("+", "%2B").replace("=", "%3D")
        for text in (sk, sk_b64, sk_quoted):
            masked = lf.redact(text)
            self.assertNotIn(sk, masked, f"未脱敏: {text}")
            self.assertIn(lf.REDACTED, masked, f"未脱敏: {text}")
        # 复合 Basic token 在 otlp 配置时登记，同样必须被抹掉
        lf.reset()
        import plane.quality as q
        with mock.patch.object(lf, "_probe_langfuse", return_value=_probe(False, True)[0]), \
                mock.patch.object(lf, "_probe_otel_otlp", return_value=_probe(False, True)[1]), \
                mock.patch.object(q, "configure_otel",
                                  return_value={"enabled": True, "endpoint": "e",
                                                "reason": ""}):
            lf.configure_langfuse(public_key=pk, secret_key=sk, mode="otlp")
        token = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        self.assertNotIn(sk, lf.redact(token))
        self.assertIn(lf.REDACTED, lf.redact(token))


# ══════════════════════════════════════════════════════════════
# ⑥ 真 langfuse 5.2.0 客户端（离线：自带 span_exporter + 屏蔽 socket）
# ══════════════════════════════════════════════════════════════
class TestRealLangfuseClient(unittest.TestCase):
    """验证我们打的是 5.2.0 的**真实** API，而不是只对假对象成立。"""

    def test_real_client_offline_export(self) -> None:
        """真客户端 + 捕获式 exporter：span 形状、trace_id、user.id 都对得上。"""
        try:
            from langfuse import Langfuse
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
        except Exception as exc:                            # noqa: BLE001
            self.skipTest(f"langfuse/otel 不可用: {exc}")

        captured: list = []
        attempts: list = []

        class CaptureExporter(SpanExporter):
            def export(self, spans):
                captured.extend(spans)
                return SpanExportResult.SUCCESS

            def shutdown(self):
                return None

        def blocked(self, address):
            attempts.append(address)
            raise RuntimeError(f"NETWORK BLOCKED {address}")

        lf.reset()
        client = None
        with mock.patch.object(socket.socket, "connect", blocked), \
                mock.patch.object(socket, "create_connection", blocked):
            try:
                # 注入独立 tracer_provider：不污染进程全局 provider；
                # span_exporter 直接收 span，不经过 httpx → 全程无网络。
                client = Langfuse(public_key="pk-lf-real", secret_key="sk-lf-real",
                                  host="https://cloud.langfuse.com",
                                  tracer_provider=TracerProvider(),
                                  span_exporter=CaptureExporter())
                cfg = lf.configure_langfuse(public_key="pk-lf-real",
                                            secret_key="sk-lf-real", client=client)
                self.assertTrue(cfg["enabled"])
                tracer = _tracer_with_tree()
                res = lf.export_tracer(tracer, user_id="u-real", session_id="s-real",
                                       tags=["test"])
                expected_tid = client.create_trace_id(seed=tracer.trace_id)
                client.flush()
            finally:
                if client is not None:
                    try:
                        client.shutdown()
                    except Exception:                       # noqa: BLE001
                        pass

        self.assertEqual(res["exported"], 4, res)
        self.assertEqual(attempts, [], "不该有任何网络连接尝试")
        self.assertEqual(len(captured), 4)

        by_name = {s.name: s for s in captured}
        self.assertEqual(set(by_name), {"turn", "llm", "tool", "retry"})
        gen = by_name["llm"]
        attrs = dict(gen.attributes or {})
        self.assertEqual(attrs.get("langfuse.observation.type"), "generation")
        self.assertEqual(attrs.get("user.id"), "u-real")
        self.assertEqual(attrs.get("session.id"), "s-real")
        self.assertIn("test", str(attrs.get("langfuse.trace.tags")))
        # trace_id 必须按 Tracer.trace_id 播种 → 可复现、可对齐
        self.assertTrue(all(s.context.trace_id == int(expected_tid, 16)
                            for s in captured))
        # 父子关系：retry 的父亲是 tool
        self.assertEqual(by_name["retry"].parent.span_id,
                         int(by_name["tool"].context.span_id))


if __name__ == "__main__":
    unittest.main()
