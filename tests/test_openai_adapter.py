"""tests/test_openai_adapter.py — W16：真实 LLM adapter 的协议转换 / 重试 / 计量 / 安全。

离线原则：只连本进程起的 ``127.0.0.1`` 临时端口（``setUpClass`` 启动，端口由内核分配），
不访问任何外网，重复跑不抢端口。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.openai_compat import (  # noqa: E402
    FATAL_STATUS,
    RETRY_BASE_S,
    RETRY_CAP_S,
    RETRYABLE_STATUS,
    LLMError,
    OpenAICompatAdapter,
    classify_status,
    estimate_tokens,
    from_env,
)
from contracts import (  # noqa: E402
    ExecCtx,
    RiskTier,
    SideEffect,
    ToolManifest,
    now_ms,
)

API_KEY = "sk-test-SECRET-key-do-not-leak-9876"
MODEL = "test-model"


# ══════════════════════════════════════════════════════════════
# 本地 fake OpenAI 服务
# ══════════════════════════════════════════════════════════════
class _Handler(BaseHTTPRequestHandler):
    """按 ``server.script`` 顺序回放响应，并把收到的请求体/头原样留下供断言。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):                     # 静音：测试输出要干净
        return

    def do_POST(self):                                # noqa: N802 — 标准库命名
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {"_raw": raw.decode("utf-8", "replace")}

        with server.lock:
            server.bodies.append(body)
            server.headers.append({k: v for k, v in self.headers.items()})
            item = server.script.pop(0) if server.script else {
                "status": 500,
                "json": {"error": {"message": "fake server: script exhausted"}},
            }

        if item.get("delay"):
            threading.Event().wait(item["delay"])

        status = int(item.get("status", 200))
        data = item.get("raw")
        if data is None:
            data = json.dumps(item.get("json", {}), ensure_ascii=False).encode("utf-8")
        else:
            data = data.encode("utf-8") if isinstance(data, str) else bytes(data)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端超时先走了：这是「超时用例」的预期副作用，不该污染测试输出
            pass


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script: list[dict] = []
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.lock = threading.Lock()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def push(self, status: int = 200, payload: dict | None = None, **kw) -> None:
        item: dict = {"status": status, "json": payload or {}}
        item.update(kw)
        self.script.append(item)

    def reset(self) -> None:
        with self.lock:
            self.script.clear()
            self.bodies.clear()
            self.headers.clear()


def chat(text: str = "ok", tool_calls: list | None = None,
         usage: dict | None = None, finish_reason: str | None = "stop") -> dict:
    """一个形状正确的 Chat Completion 响应体。"""
    message: dict = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def tool_call(call_id: str, name: str, arguments) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


_MANIFEST = ToolManifest(
    name="query_logs",
    side_effect=SideEffect.READ_ONLY,
    risk_tier=RiskTier.T1_LOW,
    description="按关键词查询服务日志",
)


# ══════════════════════════════════════════════════════════════
# 基类
# ══════════════════════════════════════════════════════════════
class AdapterCase(unittest.TestCase):
    srv: _FakeServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = _FakeServer()
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      name="fake-openai", daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.srv.port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.srv.reset()
        self.slept: list[float] = []

    # ── 装配助手 ────────────────────────────────────────────────
    def adapter(self, **kw) -> OpenAICompatAdapter:
        kw.setdefault("base_url", self.base_url)
        kw.setdefault("model", MODEL)
        kw.setdefault("api_key", API_KEY)
        kw.setdefault("max_retries", 0)
        kw.setdefault("sleeper", self.slept.append)
        return OpenAICompatAdapter(**kw)

    @staticmethod
    def ctx() -> ExecCtx:
        return ExecCtx()

    def complete(self, adapter: OpenAICompatAdapter, prompt: str = "hello",
                 tools=()) -> object:
        return adapter.complete(prompt, tools, self.ctx())


# ══════════════════════════════════════════════════════════════
# ① 协议转换
# ══════════════════════════════════════════════════════════════
class TestProtocolConversion(AdapterCase):

    def test_text_usage_finish_reason_parsed(self):
        self.srv.push(200, chat(
            text="done", usage={"prompt_tokens": 12, "completion_tokens": 7},
            finish_reason="stop"))
        resp = self.complete(self.adapter())

        self.assertEqual(resp.text, "done")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.usage.input_tokens, 12)
        self.assertEqual(resp.usage.output_tokens, 7)
        self.assertEqual(resp.usage.total, 19)
        self.assertEqual(resp.usage.model, MODEL)
        self.assertEqual(resp.tool_calls, ())

    def test_tool_call_arguments_json_string_parsed(self):
        self.srv.push(200, chat(text="", finish_reason="tool_calls", tool_calls=[
            tool_call("call_1", "query_logs",
                      json.dumps({"args": {"service": "api", "lines": 50}},
                                 ensure_ascii=False)),
            tool_call("call_2", "query_alerts", json.dumps({"service": "api"})),
        ]))
        resp = self.complete(self.adapter())

        self.assertEqual(len(resp.tool_calls), 2)
        first, second = resp.tool_calls
        self.assertEqual(first.id, "call_1")
        self.assertEqual(first.name, "query_logs")
        # 保守 schema 的 {"args": {...}} 壳必须被解包，否则 Dispatcher 会收到假参数
        self.assertEqual(first.args, {"service": "api", "lines": 50})
        self.assertEqual(second.args, {"service": "api"})
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(resp.warnings, ())

    def test_malformed_arguments_dropped_others_kept(self):
        adapter = self.adapter()
        self.srv.push(200, chat(text="partial", finish_reason="tool_calls", tool_calls=[
            tool_call("call_1", "query_logs", '{"args": {"service": "api"}'),
            tool_call("call_2", "query_alerts", json.dumps({"args": {"service": "api"}})),
            tool_call("call_3", "query_logs", "[1, 2, 3]"),
            {"id": "call_4", "type": "function", "function": {"arguments": "{}"}},
        ]))
        resp = self.complete(adapter)

        self.assertEqual([c.id for c in resp.tool_calls], ["call_2"])
        self.assertEqual(resp.text, "partial")
        self.assertEqual(len(adapter.last_warnings), 3)
        self.assertIn("call_1", adapter.last_warnings[0])
        self.assertIn("call_3", adapter.last_warnings[1])
        self.assertIn("call_4", adapter.last_warnings[2])
        self.assertEqual(adapter.stats()["warnings"], 3)
        # 告警不影响正常返回
        self.assertEqual(resp.finish_reason, "tool_calls")

    def test_tool_manifest_to_schema(self):
        adapter = self.adapter()
        self.srv.push(200, chat())
        self.complete(adapter, tools=[_MANIFEST])

        schema = adapter.tool_schema(_MANIFEST)
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "query_logs")
        self.assertEqual(schema["function"]["description"], "按关键词查询服务日志")
        self.assertEqual(schema["function"]["parameters"],
                         {"type": "object",
                          "properties": {"args": {"type": "object"}},
                          "required": ["args"]})

        sent = self.srv.bodies[0]["tools"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], schema)
        self.assertEqual(self.srv.bodies[0]["tool_choice"], "auto")

        # 空描述时降级成「副作用 + 风险等级」提示，而不是空字符串
        bare = ToolManifest(name="bare", side_effect=SideEffect.MUTATING)
        desc = adapter.tool_schema(bare)["function"]["description"]
        self.assertIn("bare", desc)
        self.assertIn("mutating", desc)
        self.assertIn("T0", desc)

    def test_request_body_messages_shape(self):
        self.srv.push(200, chat())
        self.complete(self.adapter(), prompt="把这段日志总结成三行")

        body = self.srv.bodies[0]
        self.assertEqual(body["messages"],
                         [{"role": "user", "content": "把这段日志总结成三行"}])
        self.assertEqual(body["model"], MODEL)
        self.assertAlmostEqual(body["temperature"], 0.2)
        self.assertEqual(self.srv.headers[0]["Authorization"], f"Bearer {API_KEY}")
        self.assertIn("application/json", self.srv.headers[0]["Content-Type"])

    def test_no_tools_key_when_tools_empty(self):
        self.srv.push(200, chat())
        self.complete(self.adapter(), tools=())
        body = self.srv.bodies[0]
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_content_parts_and_null_content(self):
        parts = chat(text="", usage={"prompt_tokens": 1, "completion_tokens": 1})
        parts["choices"][0]["message"]["content"] = [
            {"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]
        self.srv.push(200, parts)
        self.assertEqual(self.complete(self.adapter()).text, "第一段第二段")

        null_content = chat(text="", finish_reason="tool_calls",
                            tool_calls=[tool_call("c1", "query_logs", '{}')])
        null_content["choices"][0]["message"]["content"] = None
        self.srv.push(200, null_content)
        resp = self.complete(self.adapter())
        self.assertEqual(resp.text, "")
        self.assertEqual(resp.tool_calls[0].args, {})

    def test_finish_reason_defaults_when_absent(self):
        self.srv.push(200, chat(finish_reason=None, tool_calls=[
            tool_call("c1", "query_logs", "{}")]))
        self.assertEqual(self.complete(self.adapter()).finish_reason, "tool_calls")

        self.srv.push(200, chat(text="plain", finish_reason=None))
        self.assertEqual(self.complete(self.adapter()).finish_reason, "stop")


# ══════════════════════════════════════════════════════════════
# ② 重试与 fallback
# ══════════════════════════════════════════════════════════════
class TestRetryAndFallback(AdapterCase):

    def test_retry_on_429_then_success_with_backoff_sequence(self):
        adapter = self.adapter(max_retries=3)
        for _ in range(3):
            self.srv.push(429, {"error": {"message": "rate limited"}})
        self.srv.push(200, chat(text="after-retry",
                                usage={"prompt_tokens": 5, "completion_tokens": 2}))

        resp = self.complete(adapter)

        self.assertEqual(resp.text, "after-retry")
        self.assertEqual(self.slept, [RETRY_BASE_S * 2 ** i for i in range(3)])
        self.assertEqual(self.slept, [0.5, 1.0, 2.0])
        self.assertEqual(len(self.srv.bodies), 4)
        self.assertEqual({b["model"] for b in self.srv.bodies}, {MODEL})
        self.assertEqual(adapter.stats()["retries"], 3)
        self.assertEqual([h["event"] for h in adapter.history],
                         ["retry", "retry", "retry"])

    def test_backoff_capped_at_eight_seconds(self):
        adapter = self.adapter(max_retries=6)
        for _ in range(7):
            self.srv.push(503, {"error": {"message": "overloaded"}})

        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)

        self.assertEqual(self.slept, [0.5, 1.0, 2.0, 4.0, RETRY_CAP_S, RETRY_CAP_S])
        self.assertEqual(ctx.exception.kind, "server")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(len(self.srv.bodies), 7)

    def test_retries_exhausted_switch_to_fallback(self):
        adapter = self.adapter(model="primary", max_retries=0,
                               fallback_models=("fallback-a",))
        self.srv.push(500, {"error": {"message": "boom"}})
        self.srv.push(200, chat(text="from-fallback"))

        resp = self.complete(adapter)

        self.assertEqual(resp.text, "from-fallback")
        self.assertEqual([b["model"] for b in self.srv.bodies],
                         ["primary", "fallback-a"])
        self.assertEqual(adapter.stats()["fallback_switches"], 1)
        events = [h for h in adapter.history if h["event"] == "fallback"]
        self.assertEqual(events[0]["from"], "primary")
        self.assertEqual(events[0]["model"], "fallback-a")

    def test_fallback_chain_order(self):
        adapter = self.adapter(model="primary", max_retries=0,
                               fallback_models=("fallback-a", "fallback-b"))
        self.srv.push(502, {"error": {"message": "bad gateway"}})
        self.srv.push(504, {"error": {"message": "timeout"}})
        self.srv.push(200, chat(text="third-time"))

        self.assertEqual(self.complete(adapter).text, "third-time")
        self.assertEqual([b["model"] for b in self.srv.bodies],
                         ["primary", "fallback-a", "fallback-b"])
        self.assertEqual(adapter.stats()["fallback_switches"], 2)

    def test_401_no_retry_single_request(self):
        adapter = self.adapter(max_retries=3)
        self.srv.push(401, {"error": {"message": "invalid api key"}})

        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)

        self.assertEqual(len(self.srv.bodies), 1)
        self.assertEqual(self.slept, [])
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(adapter.stats()["errors"], 1)

    def test_auth_error_does_not_try_fallback(self):
        adapter = self.adapter(max_retries=3, fallback_models=("fallback-a",))
        self.srv.push(403, {"error": {"message": "forbidden"}})
        self.srv.push(200, chat())

        with self.assertRaises(LLMError):
            self.complete(adapter)

        self.assertEqual(len(self.srv.bodies), 1)
        self.assertEqual(self.srv.bodies[0]["model"], MODEL)

    def _stub_transport(self, adapter: OpenAICompatAdapter,
                        exc: BaseException) -> list:
        """换掉 urllib 这一层，注入确定性的传输失败（不碰真实 socket）。"""
        calls: list[dict] = []

        def fake_open(req, timeout=None):
            calls.append({"url": req.full_url, "timeout": timeout})
            raise exc

        adapter._open = fake_open
        return calls

    def test_network_error_retried_then_llm_error(self):
        """连接层失败 = 抖动：重试到耗尽，最终 LLMError(kind=network)。"""
        import urllib.error
        adapter = self.adapter(max_retries=2)
        calls = self._stub_transport(
            adapter, urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))

        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)

        self.assertEqual(ctx.exception.kind, "network")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.slept, [RETRY_BASE_S, RETRY_BASE_S * 2])
        self.assertEqual(adapter.stats()["requests"], 3)
        self.assertEqual([h["kind"] for h in adapter.history if "kind" in h],
                         ["network", "network", "network"])
        self.assertIn("Connection refused", adapter.history[0]["error"])

    def test_timeout_is_network_error_and_retried(self):
        adapter = self.adapter(max_retries=1)
        calls = self._stub_transport(adapter, TimeoutError("timed out"))
        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)
        self.assertEqual(ctx.exception.kind, "network")
        self.assertEqual(len(calls), 2)
        self.assertIn("timed out", adapter.history[0]["error"])

    def test_single_request_timeout_is_timeout_ms(self):
        adapter = self.adapter(timeout_ms=1234)
        calls = self._stub_transport(adapter, TimeoutError("timed out"))
        with self.assertRaises(LLMError):
            self.complete(adapter)
        self.assertAlmostEqual(calls[0]["timeout"], 1.234, places=6)

    def test_request_timeout_is_capped_by_deadline(self):
        adapter = self.adapter(timeout_ms=60000)
        calls = self._stub_transport(adapter, TimeoutError("timed out"))
        e = ExecCtx(deadline=now_ms() + 1500.0)
        with self.assertRaises(LLMError):
            adapter.complete("hello", (), e)
        self.assertLessEqual(calls[0]["timeout"], 1.5)
        self.assertGreater(calls[0]["timeout"], 0.005)

    def test_requests_record_retry_and_fatal_events(self):
        adapter = self.adapter(max_retries=1)
        self.srv.push(400, {"error": {"message": "bad request"}})

        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)

        self.assertEqual(ctx.exception.kind, "bad_request")
        self.assertEqual([h["event"] for h in adapter.history], ["fatal"])
        self.assertEqual(len(self.srv.bodies), 1)


# ══════════════════════════════════════════════════════════════
# ③ 超时 / 取消 / 安全
# ══════════════════════════════════════════════════════════════
class TestLimitsAndSafety(AdapterCase):

    def test_deadline_already_passed_makes_zero_requests(self):
        adapter = self.adapter(max_retries=3)
        e = ExecCtx(deadline=now_ms() - 1000.0)

        with self.assertRaises(LLMError) as ctx:
            adapter.complete("hello", (), e)

        self.assertEqual(len(self.srv.bodies), 0)
        self.assertEqual(ctx.exception.kind, "deadline")
        self.assertIn("deadline exceeded", str(ctx.exception))
        self.assertEqual(self.slept, [])

    def test_aborted_makes_zero_requests(self):
        adapter = self.adapter(max_retries=3)
        e = ExecCtx()
        e.abort()

        with self.assertRaises(LLMError) as ctx:
            adapter.complete("hello", (), e)

        self.assertEqual(len(self.srv.bodies), 0)
        self.assertEqual(ctx.exception.kind, "aborted")

    def test_abort_during_backoff_stops_before_next_request(self):
        e = ExecCtx()

        def sleeper(delay: float) -> None:
            self.slept.append(delay)
            e.abort()

        adapter = self.adapter(max_retries=3, sleeper=sleeper)
        self.srv.push(429, {"error": {"message": "rate limited"}})
        self.srv.push(200, chat())

        with self.assertRaises(LLMError) as ctx:
            adapter.complete("hello", (), e)

        self.assertEqual(ctx.exception.kind, "aborted")
        self.assertEqual(self.slept, [RETRY_BASE_S])
        self.assertEqual(len(self.srv.bodies), 1)

    def test_error_message_never_contains_api_key(self):
        adapter = self.adapter(max_retries=0)
        self.srv.push(400, {"error": {"message": f"invalid key {API_KEY} for org"}})

        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)

        message = str(ctx.exception)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn("SECRET", message)
        self.assertIn("***", message)

        blob = json.dumps(adapter.stats(), ensure_ascii=False)
        self.assertNotIn(API_KEY, blob)

    def test_authorization_header_uses_bearer(self):
        self.srv.push(200, chat())
        self.complete(self.adapter())
        self.assertEqual(self.srv.headers[0]["Authorization"], f"Bearer {API_KEY}")

        # 本地无鉴权服务（Ollama / vLLM）不该被塞一个空 Bearer
        self.srv.push(200, chat())
        self.complete(self.adapter(api_key=""))
        self.assertNotIn("Authorization", self.srv.headers[1])

    def test_endpoint_and_base_url_normalization(self):
        self.assertEqual(self.adapter().endpoint, self.base_url + "/chat/completions")
        self.assertEqual(self.adapter(base_url=self.base_url + "/").endpoint,
                         self.base_url + "/chat/completions")
        full = "http://127.0.0.1:9/v1/chat/completions"
        self.assertEqual(self.adapter(base_url=full).endpoint, full)
        with self.assertRaises(ValueError):
            OpenAICompatAdapter("", MODEL)
        with self.assertRaises(ValueError):
            OpenAICompatAdapter("127.0.0.1:9/v1", MODEL)

    def test_protocol_error_without_choices_raises_llm_error(self):
        adapter = self.adapter(max_retries=0)
        self.srv.push(200, {"id": "x", "choices": []})
        with self.assertRaises(LLMError) as ctx:
            self.complete(adapter)
        self.assertEqual(ctx.exception.kind, "protocol")


# ══════════════════════════════════════════════════════════════
# ④ 计量与成本
# ══════════════════════════════════════════════════════════════
class TestUsageAndCost(AdapterCase):

    def test_usage_missing_is_estimated(self):
        adapter = self.adapter()
        self.srv.push(200, chat(text="一个中文答案", usage=None))

        resp = self.complete(adapter, prompt="请解释这段配置的含义")

        self.assertTrue(adapter.last_estimated)
        self.assertGreater(resp.usage.input_tokens, 0)
        self.assertGreater(resp.usage.output_tokens, 0)
        self.assertEqual(resp.usage.total,
                         resp.usage.input_tokens + resp.usage.output_tokens)
        self.assertEqual(adapter.stats()["estimated_calls"], 1)

    def test_usage_present_is_not_marked_estimated(self):
        adapter = self.adapter()
        self.srv.push(200, chat(usage={"prompt_tokens": 3, "completion_tokens": 4}))
        self.complete(adapter)
        self.assertFalse(adapter.last_estimated)
        self.assertEqual(adapter.stats()["estimated_calls"], 0)

    def test_cost_exact_for_given_prices(self):
        adapter = self.adapter(price_in_per_m=0.5, price_out_per_m=1.5)
        self.srv.push(200, chat(usage={"prompt_tokens": 1000, "completion_tokens": 500}))
        self.complete(adapter)

        # 1000/1e6*0.5 + 500/1e6*1.5 = 0.0005 + 0.00075
        self.assertAlmostEqual(adapter.last_cost_usd, 0.00125, places=12)
        self.assertAlmostEqual(adapter.cost_of(1000, 500), 0.00125, places=12)
        self.assertAlmostEqual(adapter.stats()["cost_usd"], 0.00125, places=12)

    def test_stats_accumulate_calls_retries_cost(self):
        adapter = self.adapter(max_retries=1, price_in_per_m=1.0, price_out_per_m=2.0)
        self.srv.push(429, {"error": {"message": "rate limited"}})
        self.srv.push(200, chat(usage={"prompt_tokens": 100, "completion_tokens": 50}))
        self.srv.push(200, chat(usage={"prompt_tokens": 200, "completion_tokens": 100}))

        self.complete(adapter)
        self.complete(adapter)

        stats = adapter.stats()
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["requests"], 3)
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["prompt_tokens"], 300)
        self.assertEqual(stats["completion_tokens"], 150)
        self.assertEqual(stats["total_tokens"], 450)
        # (100*1 + 50*2 + 200*1 + 100*2)/1e6 = 600/1e6
        self.assertAlmostEqual(stats["cost_usd"], 0.0006, places=12)

    def test_deepseek_style_usage_field_names(self):
        adapter = self.adapter()
        self.srv.push(200, chat(usage={"input_tokens": 9, "output_tokens": 4,
                                       "prompt_cache_hit_tokens": 3}))
        resp = self.complete(adapter)
        self.assertEqual(resp.usage.input_tokens, 9)
        self.assertEqual(resp.usage.output_tokens, 4)
        self.assertEqual(resp.usage.cache_hit_tokens, 3)


# ══════════════════════════════════════════════════════════════
# ⑤ 工厂 / 口径
# ══════════════════════════════════════════════════════════════
_ENV_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
             "OPENAI_TIMEOUT_MS", "OPENAI_FALLBACK_MODELS", "OPENAI_TEMPERATURE",
             "OPENAI_PRICE_IN_PER_M", "OPENAI_PRICE_OUT_PER_M")


@contextmanager
def env(**kw):
    """隔离环境变量：先清空本测试关心的键，退出时恢复原值。"""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(kw)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestFactory(unittest.TestCase):

    def test_from_env_missing_returns_none(self):
        with env():
            self.assertIsNone(from_env())
        with env(OPENAI_MODEL="m"):
            self.assertIsNone(from_env())
        with env(OPENAI_BASE_URL="http://127.0.0.1:1/v1"):
            self.assertIsNone(from_env())
        with env(OPENAI_BASE_URL="not-a-url", OPENAI_MODEL="m"):
            self.assertIsNone(from_env())

    def test_from_env_complete_returns_adapter(self):
        with env(OPENAI_BASE_URL="http://127.0.0.1:1/v1",
                 OPENAI_API_KEY=API_KEY, OPENAI_MODEL="deepseek-chat"):
            adapter = from_env()
        self.assertIsInstance(adapter, OpenAICompatAdapter)
        self.assertEqual(adapter.model, "deepseek-chat")
        self.assertEqual(adapter.api_key, API_KEY)
        self.assertTrue(adapter.endpoint.endswith("/chat/completions"))

    def test_from_env_reads_optional_settings(self):
        with env(OPENAI_BASE_URL="http://127.0.0.1:1/v1", OPENAI_MODEL="m",
                 OPENAI_TIMEOUT_MS="1234", OPENAI_FALLBACK_MODELS="a, b",
                 OPENAI_TEMPERATURE="0.7", OPENAI_PRICE_IN_PER_M="2.5"):
            adapter = from_env()
        self.assertEqual(adapter.timeout_ms, 1234)
        self.assertEqual(adapter.fallback_models, ("a", "b"))
        self.assertAlmostEqual(adapter.temperature, 0.7)
        self.assertAlmostEqual(adapter.price_in_per_m, 2.5)

    def test_custom_prefix(self):
        """前缀按大写化处理；小写传入也要能命中 QWEN_*。"""
        extra = {"QWEN_BASE_URL": "http://127.0.0.1:1/v1", "QWEN_MODEL": "qwen3"}
        with env(), mock.patch.dict(os.environ, extra, clear=False):
            self.assertIsNone(from_env())          # OPENAI_* 未设
            adapter = from_env("qwen")
        self.assertIsInstance(adapter, OpenAICompatAdapter)
        self.assertEqual(adapter.model, "qwen3")
        self.assertEqual(adapter.api_key, "")      # 本地服务允许无 key


class TestTokenEstimate(unittest.TestCase):

    def test_chinese_prompt_estimates_higher_than_english(self):
        chinese = "请把这段中文配置压成一句话"
        english = "x" * len(chinese)
        self.assertEqual(len(chinese), len(english))
        self.assertGreater(estimate_tokens(chinese), estimate_tokens(english))

    def test_matches_builder_formula(self):
        from builder import estimate_tokens as builder_estimate
        for sample in ("", "hello world", "请把这段中文配置压成一句话",
                       "mixed 中英 mixed text with 中文 and symbols !@#",
                       "日志" * 200):
            self.assertEqual(estimate_tokens(sample), builder_estimate(sample),
                             f"口径与 builder 不一致: {sample[:20]!r}")

    def test_classify_status_table(self):
        self.assertEqual(classify_status(401), "auth")
        self.assertEqual(classify_status(403), "auth")
        self.assertEqual(classify_status(400), "bad_request")
        self.assertEqual(classify_status(429), "rate_limit")
        self.assertEqual(classify_status(500), "server")
        self.assertEqual(classify_status(504), "server")
        self.assertEqual(classify_status(408), "transient")
        self.assertEqual(classify_status(404), "client")
        for status in (429, 500, 502, 503, 504):
            self.assertIn(status, RETRYABLE_STATUS)
        for status in (400, 401, 403):
            self.assertNotIn(status, RETRYABLE_STATUS)
        # 两个集合互斥：一个状态不可能既「该退避」又「重试无意义」
        self.assertEqual(FATAL_STATUS & RETRYABLE_STATUS, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
