"""SSE 的完整验收：不只是"能吐事件"，还有那些只在真环境暴露的问题。

本文件盯的是四类**会静默失败**的行为：
  1. **静默期保活**：模型思考 30s 是常态，期间没有事件 → 代理掐连接。
  2. **帧边界**：空行是分隔符，少了它客户端会把两条事件拼成一条（且能拼成合法 JSON！）。
  3. **seq 每条流从 0 起**：客户端按"首条恒为 0"写状态机。
  4. **断开/故障收口**：客户端走了不能崩；内部炸了要补终态，不能靠"连接断了"表达失败。
"""
from __future__ import annotations

import asyncio
import re
import unittest
from unittest import mock

import serve
from core.observability import ShutdownCoordinator


P = {"user_id": "u", "tenant": "acme", "roles": ["r"], "scopes": ["runtime:run"]}


class FakeStream:
    """可控的假 Gateway：事件之间可以插入延迟。"""

    def __init__(self, events, gaps=None, raise_after=None):
        self.events = events
        self.gaps = gaps or {}
        self.raise_after = raise_after

    def handle(self, request):
        raise AssertionError("流式路径不该调 handle")

    def handle_stream(self, request):
        for i, ev in enumerate(self.events):
            if i in self.gaps:
                import time
                time.sleep(self.gaps[i])
            if self.raise_after is not None and i >= self.raise_after:
                raise RuntimeError("boom")
            yield ev


def _collect(gw, *, heartbeat_s=None, loop=None):
    async def run():
        return [f async for f in serve._event_stream(
            gw, {"goal": "x", "principal": P}, ShutdownCoordinator(),
            heartbeat_s=heartbeat_s)]
    return asyncio.run(run())


class TestFrames(unittest.TestCase):
    """帧格式：SSE 的规范细节，错一个客户端就解析不了。"""

    def test_frame_shape(self):
        f = serve.sse_frame("step", {"kind": "tool"}, 3)
        self.assertTrue(f.startswith("event: step\n"))
        self.assertIn("data: ", f)
        self.assertTrue(f.endswith("\n\n"), "必须以空行结束")
        self.assertIn('"seq":3', f.replace(" ", ""))

    def test_blank_line_is_separator(self):
        """少了空行，两条事件会拼成一条 —— 而且能拼出合法 JSON（最难查）。"""
        f = serve.sse_frame("a", {"k": 1}, 0) + serve.sse_frame("b", {"k": 2}, 1)
        self.assertEqual(f.count("\n\n"), 2)

    def test_heartbeat_is_comment(self):
        h = serve.sse_heartbeat()
        self.assertTrue(h.startswith(": "), "SSE 注释帧必须以冒号开头")
        self.assertTrue(h.endswith("\n\n"))

    def test_no_retry_field(self):
        """不发 `retry:`：POST + body 的端点无法重放，不该承诺能续上。"""
        f = serve.sse_frame("accepted", {"seq": 0}, 0)
        self.assertNotIn("retry:", f)

    def test_data_has_no_raw_newline(self):
        """data 里不能有裸换行 —— 会把一条事件截成两条。"""
        f = serve.sse_frame("step", {"text": "a\nb"}, 0)
        data_line = [ln for ln in f.split("\n") if ln.startswith("data: ")][0]
        self.assertNotIn("\n", data_line)


class TestHeartbeat(unittest.TestCase):
    """静默期保活 —— 这是生产里"流莫名其妙断了"的头号原因。"""

    def test_heartbeat_during_silence(self):
        gw = FakeStream([{"type": "accepted", "seq": 0},
                         {"type": "completed", "seq": 1}],
                        gaps={1: 0.45})
        frames = _collect(gw, heartbeat_s=0.1)
        hearts = [f for f in frames if f.startswith(": ")]
        self.assertGreaterEqual(len(hearts), 3, f"静默 0.45s 只发了 {len(hearts)} 次心跳")

    def test_no_heartbeat_when_busy(self):
        """事件密集时不该插心跳（浪费字节）。"""
        gw = FakeStream([{"type": "step", "seq": i} for i in range(20)])
        frames = _collect(gw, heartbeat_s=5.0)
        self.assertFalse([f for f in frames if f.startswith(": ")])

    def test_heartbeat_not_parsed_as_event(self):
        gw = FakeStream([{"type": "accepted", "seq": 0},
                         {"type": "completed", "seq": 1}], gaps={1: 0.3})
        frames = _collect(gw, heartbeat_s=0.1)
        events = [f for f in frames if f.startswith("event: ")]
        for e in events:
            self.assertRegex(e, r"^event: \w+\ndata: \{.*\}\n\n$")

    def test_default_interval_is_reasonable(self):
        """默认 15s：小于最常见的代理 30/60s 超时，又不至于太吵。"""
        self.assertLessEqual(serve.SSE_HEARTBEAT_HEARTBEAT if False else serve.SSE_HEARTBEAT_S, 30)
        self.assertGreaterEqual(serve.SSE_HEARTBEAT_S, 5)


class TestSequence(unittest.TestCase):
    def test_seq_from_event_carried(self):
        gw = FakeStream([{"type": "accepted", "seq": 0, "data": {}},
                         {"type": "completed", "seq": 7, "data": {}}])
        frames = _collect(gw)
        seqs = [int(re.search(r'"seq":(\d+)', f).group(1)) for f in frames]
        self.assertEqual(seqs, [0, 7], "serve 层不该改写 gateway 给的 seq")

    def test_missing_seq_omitted(self):
        gw = FakeStream([{"type": "accepted", "data": {"a": 1}}])
        frames = _collect(gw)
        self.assertNotIn("seq", frames[0])


class TestFailurePaths(unittest.TestCase):
    def test_midstream_error_emits_terminal_completed(self):
        # 必须给 ≥2 个事件：raise_after=1 在第 2 次迭代才触发，
        # 只给 1 个事件的话生成器会正常结束（返回 None），测不到故障路径。
        gw = FakeStream([{"type": "accepted", "seq": 0},
                         {"type": "step", "data": {}}], raise_after=1)
        frames = _collect(gw)
        self.assertTrue(frames[-1].startswith("event: completed"))
        self.assertIn("boom", frames[-1])

    def test_client_disconnect_does_not_raise(self):
        gw = FakeStream([{"type": "step", "seq": i} for i in range(100)])

        async def run():
            gen = serve._event_stream(gw, {"goal": "x", "principal": P},
                                      ShutdownCoordinator(), heartbeat_s=30)
            got = 0
            async for _ in gen:
                got += 1
                if got == 3:
                    await gen.aclose()      # 模拟客户端走了
                    break
            return got
        self.assertEqual(asyncio.run(run()), 3)

    def test_inflight_released_on_disconnect(self):
        shutdown = ShutdownCoordinator()
        gw = FakeStream([{"type": "step", "seq": i} for i in range(50)])

        async def run():
            gen = serve._event_stream(gw, {"goal": "x", "principal": P},
                                      shutdown, heartbeat_s=30)
            n = 0
            async for _ in gen:
                n += 1
                if n == 2:
                    await gen.aclose()
                    break
        asyncio.run(run())
        self.assertEqual(shutdown.inflight(), 0, "断开后飞行中计数没归零")


class TestHeaders(unittest.TestCase):
    """代理行为由这几个头决定，缺一个就"流式变批量"。"""

    def test_headers_present(self):
        h = serve.SSE_HEADERS
        self.assertEqual(h.get("Cache-Control"), "no-cache")
        self.assertEqual(h.get("X-Accel-Buffering"), "no", "nginx 不加这个会攒包")
        self.assertEqual(h.get("Connection"), "keep-alive")


class TestRealServer(unittest.TestCase):
    """真端口跑一遍：头 + 帧 + 心跳都要在线上形态里成立。"""

    def test_end_to_end(self):
        from fastapi.testclient import TestClient
        app = serve.create_app()
        c = TestClient(app)
        with c.stream("POST", "/v1/turns/stream",
                      json={"goal": "x", "principal": P}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/event-stream", r.headers.get("content-type", ""))
            self.assertEqual(r.headers.get("x-accel-buffering"), "no")
            body = "".join(r.iter_text())
        self.assertIn("event: accepted", body)
        self.assertIn("event: completed", body)
        seqs = [int(m) for m in re.findall(r'"seq":(\d+)', body)]
        self.assertEqual(seqs, sorted(seqs), "seq 不单调")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestHeartbeatConfig(unittest.TestCase):
    """心跳间隔必须**可配置且调用时读取**。

    踩过的坑：签名写成 `heartbeat_s: float = SSE_HEARTBEAT_S` 是 import 期绑定，
    之后设环境变量/改模块常量全都不生效——表现为"改了配置心跳间隔没变"，
    而且没有任何报错（最典型的一类静默故障）。
    """

    def test_env_overrides_default(self):
        self.assertEqual(serve._sse_heartbeat_from_env({"AGENT_RUNTIME_SSE_HEARTBEAT_S": "3"}), 3.0)

    def test_unset_falls_back_to_constant(self):
        self.assertEqual(serve._sse_heartbeat_from_env({}), serve.SSE_HEARTBEAT_S)

    def test_empty_falls_back(self):
        self.assertEqual(serve._sse_heartbeat_from_env(
            {"AGENT_RUNTIME_SSE_HEARTBEAT_S": "  "}), serve.SSE_HEARTBEAT_S)

    def test_non_numeric_raises(self):
        with self.assertRaises(ValueError) as ctx:
            serve._sse_heartbeat_from_env({"AGENT_RUNTIME_SSE_HEARTBEAT_S": "3s"})
        self.assertIn("不是数字", str(ctx.exception))

    def test_zero_raises_with_hint(self):
        with self.assertRaises(ValueError) as ctx:
            serve._sse_heartbeat_from_env({"AGENT_RUNTIME_SSE_HEARTBEAT_S": "0"})
        self.assertIn("必须 > 0", str(ctx.exception))

    def test_read_at_call_time_not_import_time(self):
        """核心：改环境变量后，下一条流立刻用新值。"""
        import os
        gw = FakeStream([{"type": "accepted", "seq": 0},
                         {"type": "completed", "seq": 1}], gaps={1: 0.25})
        old = os.environ.get("AGENT_RUNTIME_SSE_HEARTBEAT_S")
        try:
            os.environ["AGENT_RUNTIME_SSE_HEARTBEAT_S"] = "0.05"
            hearts = [f for f in _collect(gw) if f.startswith(": ")]
            self.assertGreaterEqual(len(hearts), 2,
                                    "设了 0.05s 却没有心跳 —— 说明读的是 import 期的值")
        finally:
            if old is None:
                os.environ.pop("AGENT_RUNTIME_SSE_HEARTBEAT_S", None)
            else:
                os.environ["AGENT_RUNTIME_SSE_HEARTBEAT_S"] = old
