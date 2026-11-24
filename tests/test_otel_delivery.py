"""tests/test_otel_delivery.py — **真投递**验证：OTLP exporter 真的把 span 发出去。

为什么需要这一层：
`test_otel.py` 验证的是"span 树能转成 OTel span"，用的是 `InMemorySpanExporter`
（进程内，不经过网络）。而生产里真正会坏的地方是**序列化 + HTTP + 鉴权头**那一段：
protobuf 编码、`Content-Type`、`Authorization` 头、超时、重试。

本文件起一个**本地 OTLP 接收器**（`http.server`，收 POST 并记录），
让真 `OTLPSpanExporter` 打到它，断言：

  * 真的发出了 POST，路径是 `/v1/traces`
  * body 是合法的 OTLP protobuf（能反序列化出 span）
  * `Authorization` / `Content-Type` 头正确
  * span 名字、属性、资源属性（`service.name`）都在里面

这样"Langfuse 能不能收到"就只剩 endpoint 与凭据两个变量，
中间那段（我们自己的代码）被真验证过了。
"""
from __future__ import annotations

import gzip
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plane.quality import Tracer, configure_otel, otel_available, tracer_to_otel

#: OTLP/HTTP 的 protobuf 解析要用 opentelemetry 自带的 decoder
try:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    _PROTO = True
except Exception:                                              # noqa: BLE001
    _PROTO = False


class _OTLPReceiver(BaseHTTPRequestHandler):
    """最小 OTLP/HTTP 接收器：记录请求，回 200 空 protobuf。"""

    # 类级共享（测试里单线程用，够）
    records: list[dict] = []

    def do_POST(self) -> None:                                 # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        enc = (self.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            try:
                body = gzip.decompress(body)
            except Exception:                                  # noqa: BLE001
                pass
        type(self).records.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
            "length": len(body),
        })
        # OTLP 要求回 ExportTraceServiceResponse（空消息即可）
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a: object) -> None:
        pass                                                   # 静音


def _serve(receiver_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), receiver_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@unittest.skipUnless(otel_available(), "opentelemetry-sdk 未安装")
class TestOtlpRealDelivery(unittest.TestCase):
    """真起 HTTP 接收器 + 真 exporter。**这是"能不能发出去"的唯一证据。**"""

    def setUp(self):
        _OTLPReceiver.records = []
        self.srv, self.base = _serve(_OTLPReceiver)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _tracer(self):
        t = Tracer(tenant="acme", user_id="u_alben", task_id="deliver-1")
        with t.span("loop.run", {"goal_len": 9}):
            with t.span("llm.complete", {"model": "deepseek-flash"}):
                pass
        return t

    def _configure(self, **kw):
        """每个用例用**自己的 provider**（不碰全局）。

        原因：OTel 的 `set_tracer_provider` 只能生效一次且不可撤销 ——
        第一个用例设完之后，后续用例的 exporter 仍指向已关闭的端口，
        表现为"后面全失败"。显式注入 provider 是唯一干净的解法，
        生产里也是同一套（多目标导出必须能共存）。
        """
        info = configure_otel(f"{self.base}/v1/traces", install=False, **kw)
        self.assertTrue(info["enabled"], info)
        return info["provider"]

    def test_delivers_real_http_post(self):
        """exporter 真的向本地接收器发了 POST。"""
        provider = self._configure()
        tracer_to_otel(self._tracer(), provider=provider)
        # BatchSpanProcessor 是异步的；等一会儿（最多 5s）
        import time
        for _ in range(20):                 # force_flush 已同步送达，这里只做极短兜底
            if _OTLPReceiver.records:
                break
            time.sleep(0.02)
        self.assertTrue(_OTLPReceiver.records, "接收器一个请求都没收到")

    def test_request_path_and_content_type(self):
        provider = self._configure()
        tracer_to_otel(self._tracer(), provider=provider)
        import time
        for _ in range(20):                 # force_flush 已同步送达，这里只做极短兜底
            if _OTLPReceiver.records:
                break
            time.sleep(0.02)
        rec = _OTLPReceiver.records[0]
        self.assertEqual(rec["path"], "/v1/traces")
        self.assertIn("protobuf", rec["headers"].get("content-type", ""))

    def test_authorization_header_passed(self):
        """鉴权头必须原样带上 —— Langfuse 靠 `Basic base64(pk:sk)` 认身份。"""
        provider = self._configure(headers={"Authorization": "Basic dGVzdDp0ZXN0"})
        tracer_to_otel(self._tracer(), provider=provider)
        import time
        for _ in range(20):                 # force_flush 已同步送达，这里只做极短兜底
            if _OTLPReceiver.records:
                break
            time.sleep(0.02)
        rec = _OTLPReceiver.records[0]
        self.assertEqual(rec["headers"].get("authorization"), "Basic dGVzdDp0ZXN0")

    def test_body_contains_spans(self):
        """body 能被 OTLP protobuf 反序列化，且含我们发的 span 名。"""
        if not _PROTO:
            self.skipTest("opentelemetry-proto 未安装")
        provider = self._configure()
        tracer_to_otel(self._tracer(), provider=provider)
        import time
        for _ in range(20):                 # force_flush 已同步送达，这里只做极短兜底
            if _OTLPReceiver.records:
                break
            time.sleep(0.02)
        req = ExportTraceServiceRequest()
        req.ParseFromString(_OTLPReceiver.records[0]["body"])
        names = []
        for rs in req.resource_spans:
            for ss in rs.scope_spans:
                names.extend(s.name for s in ss.spans)
        self.assertIn("loop.run", names)
        self.assertIn("llm.complete", names)

    def test_resource_service_name(self):
        if not _PROTO:
            self.skipTest("opentelemetry-proto 未安装")
        provider = self._configure(service_name="agent-runtime-verify")
        tracer_to_otel(self._tracer(), provider=provider)
        import time
        for _ in range(20):                 # force_flush 已同步送达，这里只做极短兜底
            if _OTLPReceiver.records:
                break
            time.sleep(0.02)
        req = ExportTraceServiceRequest()
        req.ParseFromString(_OTLPReceiver.records[0]["body"])
        attrs = {}
        for rs in req.resource_spans:
            for kv in rs.resource.attributes:
                attrs[kv.key] = kv.value.string_value
        self.assertEqual(attrs.get("service.name"), "agent-runtime-verify")

    def test_unreachable_endpoint_is_fail_open(self):
        """端点不可达 → 不许影响业务（INV-9）。"""
        info = configure_otel("http://127.0.0.1:1/v1/traces", install=False)
        r = tracer_to_otel(self._tracer(), provider=info.get("provider"))  # 不该抛
        self.assertIn("exported", r)


class TestNoPollution(unittest.TestCase):
    """`install=False` 不接管全局 provider —— 否则测试之间互相污染。"""

    def test_probe_only_does_not_install(self):
        info = configure_otel("http://127.0.0.1:4318/v1/traces", install=False)
        self.assertTrue(info["enabled"])
        self.assertFalse(info["installed"])
        self.assertIn("install=False", info["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
