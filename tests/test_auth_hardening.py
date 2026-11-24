"""tests/test_auth_hardening.py — 鉴权 fail-closed（默认拒绝）与 HTTP 层加固的验收用例。

设计原则（与 test_serve.py 一致，且理由相同）：

1. **不启真实端口**：走 `fastapi.testclient.TestClient`（进程内 ASGI）。
   只有一处例外 —— 要证明"读了 Content-Length 就拒、一个字节都没读"时，必须
   直接驱动 ASGI（`_asgi_request`），因为 TestClient 会先把 body 交给传输层。
2. **不连外部服务**：gateway / metrics / logger / shutdown / deps 全注入假件。
3. **断言可观测事实**：状态码、`Retry-After`、`/v1/info` 的 `auth.mode`、异常消息里
   有没有那三条修复命令、以及"超时之后线程到底死了还是在跑"。

覆盖清单（对应任务书的 24 条）：

    ① `create_app(auth_required=True, authenticator=None)` → `StartupRefused`
    ②③④ 异常消息含 `AGENT_RUNTIME_API_KEYS` / `_JWT_SECRET` / `_ALLOW_ANONYMOUS`
    ⑤ `auth_required=False, authenticator=None` → 旧行为（principal 由请求体给出）
    ⑥ `auth_required=True` + 有效 authenticator → 正常服务
    ⑦⑧ `require_auth_config` 配 API key / JWT secret → Authenticator
    ⑨ `require_auth_config` 都没配 → `StartupRefused`
    ⑩ `require_auth_config(allow_anonymous=True)` → None（见该用例的注释）
    ⑪ `resolve_auth_mode` 三种情况（另加显式开关与 explicit 参数）
    ⑫ `main()` 无凭据 → 真的没启动（uvicorn 没被调用）
    ⑬ `main()` 显式 `ALLOW_ANONYMOUS=1` → 启动，且有 WARN 日志
    ⑭⑮ `/v1/info` 的 `auth.mode` = `enforced` / `disabled`
    ⑯⑰⑱ 请求体上限：超限 413 / 1 MiB-1 通过 / 只凭 header 就拒
    ⑲⑳㉑㉒ 限流：429 + Retry-After / `/healthz` 豁免 / `/metrics` 豁免 / 按 IP 分桶
    ㉓㉔ 超时：504；且**线程仍在跑**（把这条限制钉成事实，而不是写成愿望）
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import serve
from core.gateway import TurnResult
from core.observability import JsonLogger, Metrics, ShutdownCoordinator

#: 一份合法的 principal（scope 必须含 runtime:run，否则 Gateway 判 forbidden）
PRINCIPAL = {"user_id": "u_alben", "tenant": "acme", "roles": [],
             "scopes": ["runtime:run"]}

#: 绝对静音的日志 sink：测试失败时不该被一堆 JSON 行淹没
SILENT = lambda _line: None  # noqa: E731

#: 测试用的 API key 表（格式：key:tenant:scopes[#user_id]）
API_KEY = "sk-hardening-1"
API_KEY_SPEC = f"{API_KEY}:acme:runtime:run tool.read#u_alben"

#: 测试用的 JWT secret（32 字节以上，HS256 只要求"够长"，不要求内容）
JWT_SECRET = "hardening-test-secret-0123456789abcdefghijklmnop"


# ══════════════════════════════════════════════════════════════
# 一、假件
# ══════════════════════════════════════════════════════════════
def make_result(**kwargs) -> TurnResult:
    base = dict(ok=True, text="ok", phase="DONE", term_reason="DONE",
                task_id="task_fake", steps=1)
    base.update(kwargs)
    return TurnResult(**base)


class FakeGateway:
    """最小 Gateway 替身：记录请求，返回固定成功结果。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def handle(self, request):
        self.requests.append(request)
        return make_result()

    def handle_stream(self, request):
        self.requests.append(request)
        yield {"type": "accepted", "seq": 0, "data": {"task_id": "task_fake"}}
        yield {"type": "completed", "seq": 1,
               "data": {"text": "ok", "steps": 1, "ok": True, "phase": "DONE",
                        "term_reason": "DONE", "error": "", "audit_id": "aud_1"}}


class SlowGateway:
    """慢 Gateway：证明"超时之后线程还在跑"。

    `started` 在**进入** handle 时置位，`finished` 在**跑完**时追加 ——
    两个时刻分开记，才能断言"504 之后线程仍在工作"这条限制是真的。
    """

    def __init__(self, sleep_s: float = 0.4) -> None:
        self.sleep_s = sleep_s
        self.started = threading.Event()
        self.finished: list[float] = []

    def handle(self, request):
        self.started.set()
        time.sleep(self.sleep_s)
        self.finished.append(time.monotonic())
        return make_result(text="慢回合终于跑完了")

    def handle_stream(self, request):
        self.started.set()
        time.sleep(self.sleep_s)
        self.finished.append(time.monotonic())
        yield {"type": "completed", "seq": 0, "data": {"ok": True}}


class GoodDep:
    """健康依赖：`info()` 自报 available=True（与 test_serve 的同名假件同形状）。"""

    def info(self):
        return {"kind": "fake", "available": True, "reason": "ok"}


class RecordingLogger:
    """记录事件名的假 logger（`main()` 内部的日志断言用它）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, level, event, fields):
        self.events.append((level, dict(event=event, **fields)))
        return dict(event=event, **fields)

    def debug(self, event, /, **fields):
        return self._record("DEBUG", event, fields)

    def info(self, event, /, **fields):
        return self._record("INFO", event, fields)

    def warn(self, event, /, **fields):
        return self._record("WARN", event, fields)

    warning = warn

    def error(self, event, /, **fields):
        return self._record("ERROR", event, fields)

    def has(self, event: str) -> bool:
        return any(fields.get("event") == event for _level, fields in self.events)

    def find(self, event: str) -> dict:
        for _level, fields in self.events:
            if fields.get("event") == event:
                return fields
        raise AssertionError(f"没有 {event!r} 这条日志；实际有："
                             f"{[f.get('event') for _l, f in self.events]}")


def make_app(*, gateway=None, authenticator=None, **kwargs):
    """造 app：除被测参数外，全部依赖用最小假件（不启端口、不连外部服务）。"""
    return serve.create_app(
        gateway=gateway if gateway is not None else FakeGateway(),
        metrics=Metrics(),
        logger=JsonLogger(sink=SILENT),
        shutdown=ShutdownCoordinator(),
        deps={"store": GoodDep()},
        authenticator=authenticator,
        **kwargs,
    )


def authenticator_from(spec: str = API_KEY_SPEC, **overrides):
    """按环境变量装配一个真实 `Authenticator`（不 mock：要测的就是它）。"""
    env = {"AGENT_RUNTIME_API_KEYS": spec}
    env.update(overrides)
    return serve.build_authenticator_from_env(env=env)


def turn_body(pad: int = 0) -> dict:
    """一个合法请求体；`pad` 用来把 body 撑到指定大小（`task_id` 没有长度上限）。"""
    return {"goal": "排查 api 超时", "principal": PRINCIPAL,
            "task_id": "t" * pad}


def body_of_size(size: int) -> bytes:
    """生成**正好** `size` 字节的合法 JSON 请求体（用来卡在 1 MiB 边界上）。"""
    base = turn_body(0)
    raw = json.dumps(base, ensure_ascii=False).encode("utf-8")
    pad = size - len(raw)
    if pad < 0:
        raise ValueError(f"{size} 太小，装不下一个合法请求体（至少 {len(raw)}）")
    out = json.dumps(turn_body(pad), ensure_ascii=False).encode("utf-8")
    assert len(out) == size, (len(out), size)
    return out


def _asgi_request(app, *, method: str = "POST", path: str = "/v1/turns",
                  headers=(), body: bytes = b"",
                  client=("10.0.0.9", 12345)) -> dict:
    """直接驱动 ASGI，返回 `{status, headers, body, received}`。

    `received` 是**下游被要求读 body 的次数**：这是"读了 header 就拒"唯一的硬证据 ——
    只要它是 0，就证明一个字节都没读（TestClient 做不到这个断言，它在传输层
    已经把 body 准备好了）。
    """
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "client": client, "server": ("testserver", 80),
        "headers": [(k.encode("latin-1") if isinstance(k, str) else k,
                     v.encode("latin-1") if isinstance(v, str) else v)
                    for k, v in headers],
        "state": {},
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    received = {"n": 0}
    sent: list[dict] = []

    async def receive():
        received["n"] += 1
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    raw_headers = next((m.get("headers", ()) for m in sent
                        if m["type"] == "http.response.start"), ())
    payload = b"".join(m.get("body", b"") for m in sent
                       if m["type"] == "http.response.body")
    return {"status": status,
            "headers": {bytes(k).decode("latin-1").lower():
                        bytes(v).decode("latin-1") for k, v in raw_headers},
            "body": payload.decode("utf-8", "replace"),
            "received": received["n"]}


def env_patch(**overrides):
    """把环境变量换成**只有指定项**的样子（其余 AGENT_RUNTIME_* 全部清掉）。

    为什么 clear=True：本机很可能已经配了 `AGENT_RUNTIME_API_KEYS` 或在 shell 里
    留了 `ALLOW_ANONYMOUS=1`。不隔离的话，"没凭据就拒绝启动"这条用例会因为开发机的
    环境而通过/失败 —— 那种测试等于没测。
    """
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp")}
    base.update(overrides)
    return mock.patch.dict(os.environ, base, clear=True)


# ══════════════════════════════════════════════════════════════
# 二、fail-closed 启动判定（①②③④⑤⑥）
# ══════════════════════════════════════════════════════════════
class TestFailClosedStartup(unittest.TestCase):
    # ① create_app(auth_required=True, authenticator=None) → StartupRefused
    def test_create_app_refuses_when_auth_required_without_authenticator(self):
        with self.assertRaises(serve.StartupRefused):
            make_app(auth_required=True)

    def test_startup_refused_is_runtime_error(self):
        """继承 RuntimeError：调用方现有的 `except RuntimeError` 兜底能接住它。"""
        self.assertTrue(issubclass(serve.StartupRefused, RuntimeError))

    # ② 消息含 AGENT_RUNTIME_API_KEYS
    def test_refusal_message_mentions_api_keys(self):
        with self.assertRaises(serve.StartupRefused) as ctx:
            serve.require_auth_config(env={})
        self.assertIn("AGENT_RUNTIME_API_KEYS", str(ctx.exception))

    # ③ 消息含 AGENT_RUNTIME_JWT_SECRET
    def test_refusal_message_mentions_jwt_secret(self):
        with self.assertRaises(serve.StartupRefused) as ctx:
            serve.require_auth_config(env={})
        self.assertIn("AGENT_RUNTIME_JWT_SECRET", str(ctx.exception))

    # ④ 消息含 AGENT_RUNTIME_ALLOW_ANONYMOUS
    def test_refusal_message_mentions_allow_anonymous(self):
        with self.assertRaises(serve.StartupRefused) as ctx:
            serve.require_auth_config(env={})
        self.assertIn("AGENT_RUNTIME_ALLOW_ANONYMOUS", str(ctx.exception))

    def test_refusal_message_is_a_runnable_recipe(self):
        """不是"配置错误"四个字：三条 `export` 命令要能直接复制执行。"""
        message = serve.STARTUP_REFUSED_MESSAGE
        self.assertIn("拒绝启动", message)
        self.assertIn("修复方式（任选其一）", message)
        self.assertEqual(message.count("export AGENT_RUNTIME_"), 3)
        self.assertIn("openssl rand -base64 48", message)

    def test_create_app_refusal_happens_before_any_side_effect(self):
        """拒绝要**最早**发生：不建 gateway、不探依赖（探一个连不上的 Redis 再死是浪费）。"""
        gateway = FakeGateway()
        with self.assertRaises(serve.StartupRefused):
            serve.create_app(gateway=gateway, auth_required=True)
        self.assertEqual(gateway.requests, [])

    # ⑤ auth_required=False + authenticator=None → 旧行为
    def test_legacy_anonymous_mode_still_works(self):
        client = TestClient(make_app())
        response = client.post("/v1/turns", json=turn_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "ok")
        # 未启用鉴权时 /v1/whoami 不存在（能力不存在，比一个假身份更诚实）
        self.assertEqual(client.get("/v1/whoami").status_code, 404)

    # ⑥ auth_required=True + 有效 authenticator → 正常服务
    def test_auth_required_with_authenticator_serves(self):
        authenticator = authenticator_from()
        client = TestClient(make_app(authenticator=authenticator,
                                     auth_required=True))
        response = client.post("/v1/turns", json=turn_body(),
                               headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)

    def test_whoami_200_when_auth_enabled(self):
        authenticator = authenticator_from()
        client = TestClient(make_app(authenticator=authenticator,
                                     auth_required=True))
        self.assertEqual(client.get("/v1/whoami").status_code, 401)  # 没凭据 → 拒绝
        ok = client.get("/v1/whoami", headers={"x-api-key": API_KEY})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["principal"]["user_id"], "u_alben")
        self.assertEqual(ok.json()["principal"]["tenant"], "acme")
        self.assertEqual(ok.json()["scheme"], "api_key")

    def test_missing_credentials_rejected_when_enforced(self):
        client = TestClient(make_app(authenticator=authenticator_from(),
                                     auth_required=True))
        self.assertEqual(client.post("/v1/turns", json=turn_body()).status_code, 401)


# ══════════════════════════════════════════════════════════════
# 三、require_auth_config / resolve_auth_mode（⑦⑧⑨⑩⑪）
# ══════════════════════════════════════════════════════════════
class TestRequireAuthConfig(unittest.TestCase):
    # ⑦ 配了 API key → 返回 Authenticator
    def test_api_keys_env_returns_authenticator(self):
        authenticator = serve.require_auth_config(
            env={"AGENT_RUNTIME_API_KEYS": API_KEY_SPEC})
        self.assertIsNotNone(authenticator)
        self.assertTrue(authenticator.configured)
        self.assertIn("api_key", authenticator.schemes)
        principal = authenticator.api_keys[API_KEY]
        self.assertEqual(principal.tenant, "acme")
        self.assertEqual(principal.user_id, "u_alben")

    # ⑧ 配了 JWT secret → 返回 Authenticator
    def test_jwt_secret_env_returns_authenticator(self):
        authenticator = serve.require_auth_config(
            env={"AGENT_RUNTIME_JWT_SECRET": JWT_SECRET})
        self.assertIsNotNone(authenticator)
        self.assertTrue(authenticator.configured)
        self.assertIn("jwt", authenticator.schemes)

    # ⑨ 都没配 → StartupRefused
    def test_no_credentials_refuses(self):
        with self.assertRaises(serve.StartupRefused):
            serve.require_auth_config(env={})

    def test_empty_strings_count_as_missing(self):
        """`export AGENT_RUNTIME_API_KEYS=` 是"配了个空值"，不是"配好了"。"""
        with self.assertRaises(serve.StartupRefused):
            serve.require_auth_config(
                env={"AGENT_RUNTIME_API_KEYS": "   ",
                     "AGENT_RUNTIME_JWT_SECRET": ""})

    def test_malformed_spec_is_value_error_not_refusal(self):
        """写错了 ≠ 没写：格式错误必须是 ValueError（带第几项），不能混成"拒绝启动"。"""
        with self.assertRaises(ValueError):
            serve.require_auth_config(
                env={"AGENT_RUNTIME_API_KEYS": "只有一段"})

    # ⑩ allow_anonymous=True → None（选"返回 None"，理由见下）
    def test_allow_anonymous_returns_none(self):
        """显式匿名 → 返回 **None**（不返回打空 scope 的 Authenticator）。

        选 None 而不是"匿名 Authenticator"的理由：返回 None 的语义是**调用方不会
        安装鉴权中间件** —— 这正是"principal 由请求体给出"这条历史路径的定义。
        若返回一个 anonymous Authenticator，`/v1/whoami` 会被注册、每个请求都会
        被中间件标记，调用方会以为自己"有鉴权"，而实际身份仍来自请求体 ——
        两种语义混在一处的代码，下一次改动就会有人把 `is None` 的判断漏掉。
        """
        self.assertIsNone(serve.require_auth_config(env={}, allow_anonymous=True))

    def test_allow_anonymous_by_env_returns_none(self):
        self.assertIsNone(serve.require_auth_config(
            env={"AGENT_RUNTIME_ALLOW_ANONYMOUS": "1"}))

    def test_falsey_allow_anonymous_is_pointed_out(self):
        """`ALLOW_ANONYMOUS=0/false/no` 不是真值 —— 拒绝消息要指出这一点。

        这是最常见的误配：运维以为自己放行了，进程却在启动时退出。
        """
        with self.assertRaises(serve.StartupRefused) as ctx:
            serve.require_auth_config(
                env={"AGENT_RUNTIME_ALLOW_ANONYMOUS": "false"})
        self.assertIn("不是真值", str(ctx.exception))


class TestResolveAuthMode(unittest.TestCase):
    # ⑪ 三种情况各一条
    def test_enforced_when_credentials_configured(self):
        self.assertEqual(
            serve.resolve_auth_mode(env={"AGENT_RUNTIME_API_KEYS": API_KEY_SPEC}),
            "enforced")
        self.assertEqual(
            serve.resolve_auth_mode(env={"AGENT_RUNTIME_JWT_SECRET": JWT_SECRET}),
            "enforced")

    def test_disabled_when_explicitly_allowed(self):
        self.assertEqual(
            serve.resolve_auth_mode(env={"AGENT_RUNTIME_ALLOW_ANONYMOUS": "1"}),
            "disabled")
        self.assertEqual(
            serve.resolve_auth_mode(env={"AGENT_RUNTIME_AUTH": "off"}),
            "disabled")

    def test_enforced_by_default_without_any_config(self):
        """**这一条是本次改动的全部意义**：什么都没配 = 默认拒绝，不是默认放行。"""
        self.assertEqual(serve.resolve_auth_mode(env={}), "enforced")

    def test_explicit_argument_wins(self):
        self.assertEqual(
            serve.resolve_auth_mode(env={"AGENT_RUNTIME_API_KEYS": API_KEY_SPEC},
                                    explicit=False), "disabled")
        self.assertEqual(
            serve.resolve_auth_mode(env={"AGENT_RUNTIME_ALLOW_ANONYMOUS": "1"},
                                    explicit=True), "enforced")

    def test_auth_off_beats_configured_credentials(self):
        """显式硬开关压过配置（并会在 main() 里留一条 WARN，凭据未被使用）。"""
        self.assertEqual(
            serve.resolve_auth_mode(
                env={"AGENT_RUNTIME_AUTH": "off",
                     "AGENT_RUNTIME_API_KEYS": API_KEY_SPEC}), "disabled")

    def test_credentials_beat_leftover_allow_anonymous(self):
        """DEBUG 残留的 ALLOW_ANONYMOUS=1 不能把已配好的鉴权悄悄关掉。"""
        self.assertEqual(
            serve.resolve_auth_mode(
                env={"AGENT_RUNTIME_API_KEYS": API_KEY_SPEC,
                     "AGENT_RUNTIME_ALLOW_ANONYMOUS": "1"}), "enforced")

    def test_auth_on_forces_enforced(self):
        self.assertEqual(
            serve.resolve_auth_mode(
                env={"AGENT_RUNTIME_AUTH": "on",
                     "AGENT_RUNTIME_ALLOW_ANONYMOUS": "1"}), "enforced")


# ══════════════════════════════════════════════════════════════
# 四、main()：真的不启动 / 真的启动（⑫⑬）
# ══════════════════════════════════════════════════════════════
class TestMainEntrypoint(unittest.TestCase):
    def _run_main(self, logger: RecordingLogger | None = None):
        """跑 `serve.main()`，把 uvicorn 与 logger 换成可控替身。"""
        logger = logger if logger is not None else RecordingLogger()
        with mock.patch.object(serve, "JsonLogger", return_value=logger), \
                mock.patch("uvicorn.Server.run") as server_run, \
                mock.patch("uvicorn.run") as uvicorn_run:
            serve.main()
        return logger, server_run, uvicorn_run

    # ⑫ 无凭据 → 真的没启动
    def test_main_refuses_to_start_without_credentials(self):
        called = {"server_run": 0, "uvicorn_run": 0}
        with env_patch():
            with mock.patch.object(serve, "JsonLogger", return_value=RecordingLogger()), \
                    mock.patch("uvicorn.Server.run",
                               side_effect=lambda *a, **k: called.__setitem__(
                                   "server_run", called["server_run"] + 1)), \
                    mock.patch("uvicorn.run",
                               side_effect=lambda *a, **k: called.__setitem__(
                                   "uvicorn_run", called["uvicorn_run"] + 1)):
                with self.assertRaises(serve.StartupRefused):
                    serve.main()
        self.assertEqual(called, {"server_run": 0, "uvicorn_run": 0})

    # ⑬ 显式 ALLOW_ANONYMOUS=1 → 启动，且有 WARN
    def test_main_starts_with_explicit_anonymous_and_warns(self):
        logger = RecordingLogger()
        with env_patch(AGENT_RUNTIME_ALLOW_ANONYMOUS="1"):
            logger, server_run, uvicorn_run = self._run_main(logger)
        self.assertEqual(server_run.call_count, 1)
        self.assertEqual(uvicorn_run.call_count, 0)
        warn = logger.find("auth.disabled")
        # WARN 必须写明"principal 由请求体给"——这是唯一的裸奔路径，必须刺眼
        self.assertIn("请求体", warn["hint"])
        self.assertIn("任意租户", warn["hint"])
        self.assertEqual(warn["mode"], "disabled")

    def test_main_starts_with_api_keys(self):
        logger = RecordingLogger()
        with env_patch(AGENT_RUNTIME_API_KEYS=API_KEY_SPEC):
            logger, server_run, _uvicorn_run = self._run_main(logger)
        self.assertEqual(server_run.call_count, 1)
        record = logger.find("auth.enforced")
        self.assertIn("api_key", record["schemes"])
        self.assertFalse(logger.has("auth.disabled"))

    def test_main_warns_when_credentials_ignored(self):
        """配了凭据却被 AUTH=off 关掉：必须有一条点名日志（否则是静默降级）。"""
        logger = RecordingLogger()
        with env_patch(AGENT_RUNTIME_API_KEYS=API_KEY_SPEC,
                       AGENT_RUNTIME_AUTH="off"):
            logger, server_run, _ = self._run_main(logger)
        self.assertEqual(server_run.call_count, 1)
        self.assertTrue(logger.has("auth.disabled"))
        self.assertTrue(logger.has("auth.credentials_ignored"))


# ══════════════════════════════════════════════════════════════
# 五、/v1/info 的 auth.mode（⑭⑮）
# ══════════════════════════════════════════════════════════════
class TestInfoAuthMode(unittest.TestCase):
    # ⑭ 配了凭据 → enforced
    def test_mode_enforced_when_authenticator_present(self):
        authenticator = authenticator_from()
        client = TestClient(make_app(authenticator=authenticator,
                                     auth_required=True))
        body = client.get("/v1/info", headers={"x-api-key": API_KEY}).json()
        self.assertEqual(body["auth"]["mode"], "enforced")
        self.assertTrue(body["auth"]["enabled"])
        self.assertEqual(body["auth"]["api_keys"], 1)

    # ⑮ 显式放行 → disabled
    def test_mode_disabled_when_explicitly_anonymous(self):
        client = TestClient(make_app())
        body = client.get("/v1/info").json()
        self.assertEqual(body["auth"]["mode"], "disabled")
        self.assertFalse(body["auth"]["enabled"])

    def test_info_never_echoes_credentials(self):
        """`mode` 是新增字段，既有纪律不变：不回显密钥、长度、哈希。

        `/v1/info` 常常被放在不需要鉴权的内部端口上，一次回显就是一次全量泄漏。
        """
        authenticator = authenticator_from()
        client = TestClient(make_app(authenticator=authenticator,
                                     auth_required=True))
        text = client.get("/v1/info", headers={"x-api-key": API_KEY}).text
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(JWT_SECRET, text)
        # "1 把 key" 是运维需要的部署事实；"key 有多长"会缩小搜索空间，不给
        self.assertNotIn("key_length", text)
        self.assertNotIn("sk-hardening", text)

    def test_info_reports_hardening_switches(self):
        """/v1/info 还要能回答"三道边界到底关了没有"（值都是数字，不是秘密）。"""
        client = TestClient(make_app(max_body_bytes=2048,
                                     rate_limit_per_minute=7,
                                     request_timeout_s=0.5))
        body = client.get("/v1/info").json()["hardening"]
        self.assertEqual(body["max_body_bytes"], 2048)
        self.assertTrue(body["body_limit"])
        self.assertEqual(body["rate_limit_per_minute"], 7)
        self.assertTrue(body["rate_limit"])
        self.assertEqual(body["request_timeout_s"], 0.5)
        self.assertTrue(body["timeout"])

    def test_info_reports_hardening_defaults(self):
        client = TestClient(make_app())
        body = client.get("/v1/info").json()["hardening"]
        self.assertEqual(body["max_body_bytes"], serve.DEFAULT_MAX_BODY_BYTES)
        self.assertFalse(body["rate_limit"])
        self.assertFalse(body["timeout"])


# ══════════════════════════════════════════════════════════════
# 六、请求体上限（⑯⑰⑱）
# ══════════════════════════════════════════════════════════════
class TestBodyLimit(unittest.TestCase):
    @property
    def limit(self) -> int:
        return serve.DEFAULT_MAX_BODY_BYTES

    # ⑯ 超 1 MiB → 413
    def test_body_over_one_mib_is_rejected(self):
        client = TestClient(make_app())
        response = client.post("/v1/turns", content=b"x" * (self.limit + 1),
                               headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"], "payload_too_large")

    # ⑰ 刚好 1 MiB - 1 → 通过
    def test_body_just_under_limit_passes(self):
        client = TestClient(make_app())
        response = client.post("/v1/turns", content=body_of_size(self.limit - 1),
                               headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text[:200])

    def test_body_exactly_at_limit_passes(self):
        """边界是 `> limit` 而不是 `>= limit`：上限就是上限。"""
        client = TestClient(make_app())
        response = client.post("/v1/turns", content=body_of_size(self.limit),
                               headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text[:200])

    # ⑱ Content-Length 超限但 body 很短 → 仍然 413（读了 header 就拒）
    def test_oversized_content_length_rejected_without_reading_body(self):
        app = make_app()
        result = _asgi_request(
            app, headers=[("content-type", "application/json"),
                          ("content-length", str(4 * 1024 * 1024))],
            body=b"{}")
        self.assertEqual(result["status"], 413)
        # **核心断言**：下游一次都没被要求读 body —— 一个字节都没进内存
        self.assertEqual(result["received"], 0)
        # 请求体没被消费，这条连接不能复用：必须明说（而不是让客户端等到超时）
        self.assertEqual(result["headers"].get("connection"), "close")

    def test_chunked_upload_over_limit_is_rejected(self):
        """chunked（无 Content-Length）也要挡住：边读边量，越界立刻停手。"""
        client = TestClient(make_app(max_body_bytes=4096))
        chunks = [b"a" * 2000] * 5            # 10 KB > 4 KB，且是流式发出去的
        response = client.post("/v1/turns", content=iter(chunks),
                               headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_connection_still_usable_after_stream_rejection(self):
        """流式超限之后：丢弃性读掉剩余部分，连接可以继续用（不需要重连）。"""
        client = TestClient(make_app(max_body_bytes=4096))
        bad = client.post("/v1/turns", content=iter([b"a" * 2000] * 3),
                          headers={"content-type": "application/json"})
        self.assertEqual(bad.status_code, 413)
        good = client.post("/v1/turns", json=turn_body())
        self.assertEqual(good.status_code, 200)

    def test_chunked_streaming_turn_still_emits_sse(self):
        """**回归用例**：chunked 上传 + SSE 必须仍然逐帧发事件。

        踩过的坑：体量中间件为了"下游能重新读 body"，把 `receive` 换成了回放版；
        回放版最初在第二次调用时返回 `http.disconnect`（本意是"别挂住"），结果
        Starlette 的 `StreamingResponse` 用它监听客户端断开 —— 一个伪造的
        disconnect 让整个流在发出第一帧之前就被取消，客户端拿到 200 但零事件。
        这类"状态看起来对、内容却是空"的故障最难发现，所以钉成用例。
        """
        client = TestClient(make_app(max_body_bytes=65536))
        payload = json.dumps(turn_body()).encode()
        response = client.post("/v1/turns/stream",
                               content=iter([payload[:10], payload[10:]]),
                               headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.text.count("data:"), 2, response.text[:300])

    def test_streaming_turn_works_with_content_length(self):
        client = TestClient(make_app(max_body_bytes=65536))
        response = client.post("/v1/turns/stream", json=turn_body())
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.text.count("data:"), 2)

    def test_body_limit_can_be_disabled(self):
        """0 = 关闭（显式关闭，不是"配错了"）：大 body 会走到 pydantic 的 422/200。"""
        client = TestClient(make_app(max_body_bytes=0))
        response = client.post("/v1/turns", content=body_of_size(512 * 1024),
                               headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 200, response.text[:200])


# ══════════════════════════════════════════════════════════════
# 七、限流（⑲⑳㉑㉒）
# ══════════════════════════════════════════════════════════════
class TestRateLimit(unittest.TestCase):
    # ⑲ 超阈值 → 429 + Retry-After
    def test_over_limit_returns_429_with_retry_after(self):
        client = TestClient(make_app(rate_limit_per_minute=2))
        self.assertEqual(client.post("/v1/turns", json=turn_body()).status_code, 200)
        self.assertEqual(client.post("/v1/turns", json=turn_body()).status_code, 200)
        blocked = client.post("/v1/turns", json=turn_body())
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in blocked.headers})
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        self.assertEqual(blocked.json()["error"], "rate_limited")

    # ⑳ /healthz 不受限
    def test_healthz_is_exempt(self):
        client = TestClient(make_app(rate_limit_per_minute=1))
        for _ in range(5):
            self.assertEqual(client.get("/healthz").status_code, 200)

    # ㉑ /metrics 不受限
    def test_metrics_is_exempt(self):
        client = TestClient(make_app(rate_limit_per_minute=1))
        for _ in range(5):
            self.assertEqual(client.get("/metrics").status_code, 200)

    # ㉒ 不同 IP 各自计数
    def test_limits_are_per_ip(self):
        # **XFF 只在可信代理后面才被采信**（防伪造）。TestClient 的对端是
        # testclient，所以要显式声明信任它，否则三个请求会共用同一个桶。
        client = TestClient(make_app(rate_limit_per_minute=1,
                                     trusted_proxies=("testclient", "127.0.0.1")))
        first = client.post("/v1/turns", json=turn_body(),
                            headers={"x-forwarded-for": "203.0.113.7"})
        second = client.post("/v1/turns", json=turn_body(),
                             headers={"x-forwarded-for": "203.0.113.7"})
        other = client.post("/v1/turns", json=turn_body(),
                            headers={"x-forwarded-for": "198.51.100.9"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(other.status_code, 200)

    def test_forwarded_for_uses_first_hop_only(self):
        """`XFF: client, proxy` —— 只认第一段；整串当 key 等于"随便塞一段就换桶"。"""
        client = TestClient(make_app(rate_limit_per_minute=1,
                                     trusted_proxies=("testclient", "127.0.0.1")))
        same = client.post("/v1/turns", json=turn_body(),
                           headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
        second = client.post("/v1/turns", json=turn_body(),
                             headers={"x-forwarded-for": "203.0.113.7, 10.0.0.2"})
        self.assertEqual(same.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_rate_limit_disabled_by_default(self):
        client = TestClient(make_app())
        for _ in range(10):
            self.assertEqual(client.post("/v1/turns", json=turn_body()).status_code,
                             200)

    def test_window_expiry_restores_quota(self):
        """窗口滑过去以后配额回来（用假时钟，不 sleep 60 秒）。"""
        now = {"t": 1000.0}
        limiter = serve.RateLimitMiddleware(lambda *a: None, limit_per_minute=1,
                                            clock=lambda: now["t"])
        self.assertIsNone(limiter._hit("1.1.1.1"))
        self.assertIsNotNone(limiter._hit("1.1.1.1"))
        now["t"] += serve.RATE_WINDOW_S + 0.1
        self.assertIsNone(limiter._hit("1.1.1.1"))


# ══════════════════════════════════════════════════════════════
# 八、超时（㉓㉔）
# ══════════════════════════════════════════════════════════════
class TestTimeout(unittest.TestCase):
    # ㉓ 超时 → 504
    def test_slow_turn_returns_504(self):
        gateway = SlowGateway(sleep_s=0.5)
        # **必须用 `with`**：Starlette 的 TestClient 不在上下文里时，每个请求都会
        # 新建一个 portal，退出时 `shutdown_default_executor()` 会等线程池跑空 ——
        # 那样"超时"就被 TestClient 自己偷偷等成了 0.5s，测的就不是我们的中间件了。
        with TestClient(make_app(gateway=gateway, request_timeout_s=0.1)) as client:
            started = time.monotonic()
            response = client.post("/v1/turns", json=turn_body())
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"], "request_timeout")
        # 不等整回合：客户端在超时那一刻就拿到答案，而不是等 0.5s
        self.assertLess(elapsed, 0.45)

    # ㉔ 超时后 gateway 线程仍在跑 —— 这条限制是**真的**，文档化行为
    def test_gateway_thread_keeps_running_after_timeout(self):
        """断言"线程杀不掉"这个事实（而不是假装我们杀了它）。

        `Gateway.handle` 跑在 `asyncio.to_thread` 的线程里。超时只能停止等待：
        504 之后那一回合仍在后台跑完（继续烧 token / 调工具 / 写 Context）。
        谁要真正中止执行，正确的位置是 Gateway 的 deadline/预算。
        本用例把这个限制钉成测试：如果哪天有人"优化"成能杀线程，这里会红，
        那时才该去改文档（以及好好想想是怎么做到的）。
        """
        gateway = SlowGateway(sleep_s=0.5)
        with TestClient(make_app(gateway=gateway, request_timeout_s=0.1)) as client:
            response = client.post("/v1/turns", json=turn_body())
            self.assertEqual(response.status_code, 504)
            # 504 已经是事实，但回合还在跑
            self.assertTrue(gateway.started.is_set(), "gateway 根本没被调用")
            self.assertEqual(gateway.finished, [], "线程在 504 时就已经结束了？")
            # 给它足够时间跑完，然后证明"它确实跑完了"（而不是被杀了）
            deadline = time.monotonic() + 2.0
            while not gateway.finished and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(gateway.finished), 1,
                             "超时之后线程应当仍然跑完（Python 不能安全杀线程）")

    def test_fast_turn_unaffected(self):
        client = TestClient(make_app(request_timeout_s=5.0))
        self.assertEqual(client.post("/v1/turns", json=turn_body()).status_code, 200)

    def test_timeout_disabled_by_default(self):
        with TestClient(make_app(gateway=SlowGateway(sleep_s=0.2))) as client:
            self.assertEqual(client.post("/v1/turns", json=turn_body()).status_code,
                             200)


if __name__ == "__main__":              # pragma: no cover
    unittest.main()
