"""plane/langfuse_export.py — 把 `plane.quality.Tracer` 的 span 树接到 Langfuse。

================================================================================
为什么是「导出目标」而不是「第二套埋点」
================================================================================

`plane/quality.py` 已经用手写的 `Span` / `Tracer` 产生了**完整**的执行树，
它同时是业务逻辑（`Evaluator` / `Replayer` / demo 打印都吃它）和可测面。
所以这里从头到尾只做一件事：**把已经产生的事实搬到 Langfuse**，不新增埋点：

    Tracer.finish()  ──> trace_summary()  ──> 扁平的 observations
                              │                    ├─> sdk  模式：Langfuse 客户端直发
                              │                    └─> otlp 模式：plane.quality.configure_otel
                              └─ 纯转换，不碰网络，可单测

三档能力按可用性降级，**绝不假装可用**：

    sdk   装了 `langfuse`（5.x）        → 用 `Langfuse` 客户端批量发 observation
    otlp  没装 SDK 但有 OTel exporter   → `configure_otel()` 指向 Langfuse 的 OTLP endpoint
    none  两样都没有                    → enabled=False + reason 说清缺什么、怎么装

Langfuse 5.2.0 的实际形态（`inspect` 现场确认，不是猜的）：

  * `Langfuse(public_key=, secret_key=, host=, tracing_enabled=, tracer_provider=,
              span_exporter=, ...)` —— 它本身就是个 **OTel 客户端**：
    内部持有 TracerProvider + BatchSpanProcessor，把 span 转成 OTLP 发出去。
    因此 `langfuse` 一旦装好，`opentelemetry-sdk` 与
    `opentelemetry-exporter-otlp-proto-http` 一定也在了（它的硬依赖）。
  * `client.create_trace_id(seed=...) -> str` 产生 32 位小写 hex trace_id，
    同 seed 确定性复现 —— 正好拿 `Tracer.trace_id` 当 seed，跨次导出可对齐。
  * `client.start_observation(*, trace_context={"trace_id","parent_span_id"},
    name=, as_type="span"|"generation"|"tool"|"agent"|..., input=, output=,
    metadata=, level="DEBUG"|"DEFAULT"|"WARNING"|"ERROR", model=,
    usage_details=) -> LangfuseSpan`；返回对象有 `.id` / `.trace_id` /
    `.update()` / `.end(end_time=纳秒)`。
  * 注意：客户端实例上**没有** `client.propagate_attributes` 这个方法。
    `propagate_attributes(user_id=, session_id=, tags=, trace_name=)` 是
    **模块级函数**（`from langfuse import propagate_attributes`），
    写进 OTel context，之后创建的 span 才会带上 `user.id` / `session.id` /
    `langfuse.trace.tags`。所以本模块先找客户端上的同名方法（便于注入假客户端
    做断言），找不到才退回模块级函数，两者都没有就降级塞进根 span 的 metadata。
  * 初始化**不发网络请求**（`auth_check()` 才是显式联网校验，本模块不调用），
    但为了测试彻底不联网，用例一律注入假客户端；另有 1 个用例用真客户端 +
    自带 `span_exporter` + 注入 `tracer_provider`，在屏蔽 socket 的前提下验证
    **真实 API 路径**（而不是只验证假对象）。

设计约束（与 `plane/quality.py`、`core/observability.py` 同一条纪律）：

1. **fail-open（INV-9）**：导出是观测面。`export_tracer()` 任何时候都返回统计
   字典，绝不向上抛异常；单个 span 发送失败也只计入 `skipped`。
2. **不泄漏密钥**：`public_key` / `secret_key` 及其派生凭据（Basic token、
   URL 编码变体）一律登记进 `_SECRETS`，任何返回值 / 异常消息 / 日志都过
   `redact()`；模块状态里只留凭据指纹（sha256 前缀），不留明文密钥。
3. **配置了必须真生效**：`configure_langfuse()` 的 `enabled` 只在真的拿到
   可用客户端（或 OTLP exporter 真的挂上）时才为 True；导出全军覆没时
   会把状态翻回 `enabled=False` 并写明原因——"配置成功但什么也没发出去"
   是本项目反复踩的坑，这里用 `current_state()` 把它暴露出来。
4. **纯转换可单测**：`trace_summary()` 只做 dict 搬运，不读时间以外的环境、
   不碰网络；测试里直接构造 `Tracer` 断言形状。

用法::

    from plane.quality import Tracer
    from plane import langfuse_export as lf

    print(lf.detect())                    # 先看自述能力，别猜
    cfg = lf.configure_langfuse(public_key="pk-lf-...", secret_key="sk-lf-...")
    if cfg["enabled"]:
        with Tracer(task_id="t1") as ...: # 业务跑完
            ...
        print(lf.export_tracer(t, user_id="u1", session_id="s1", tags=["prod"]))
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import inspect
import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = [
    "DEFAULT_HOST",
    "OTLP_TRACE_PATH",
    "REDACTED",
    "detect",
    "configure_langfuse",
    "export_tracer",
    "trace_summary",
    "current_state",
    "reset",
    "redact",
]

#: Langfuse Cloud 的默认 host（自建/区域化部署用参数覆盖）
DEFAULT_HOST = "https://cloud.langfuse.com"

#: Langfuse 的 OTLP/HTTP trace 接收路径（拼在 host 后面）
OTLP_TRACE_PATH = "/api/public/otel/v1/traces"

#: 脱敏占位符（与 core.observability 的 REDACTED 同一个视觉标记）
REDACTED = "[REDACTED]"

#: Langfuse 认识的 observation level，其余一律归一到 DEFAULT
_LEVELS = ("DEBUG", "DEFAULT", "WARNING", "ERROR")

#: Langfuse observation 类型里我们会用到的子集
_SDK_TYPES = ("span", "generation", "tool", "agent", "chain", "retriever")


# ══════════════════════════════════════════════════════════════
# 密钥脱敏：任何出站字符串都必须先过这里
# ══════════════════════════════════════════════════════════════
_SECRETS: set[str] = set()


def _register_secret(*values: Any) -> None:
    """登记敏感串（含常见派生形式），供 `redact()` 全文替换。"""
    for v in values:
        if v is None:
            continue
        s = str(v)
        # 太短的串（如 "sk"、"pk"）不登记：它们会误伤 "skipped" 这类正常字段名，
        # 真实 Langfuse 凭据都是 pk-lf-/sk-lf- 前缀的长串，这个下限足够安全。
        if len(s) < 6:
            continue
        _SECRETS.add(s)
        # base64 / URL 编码变体也一并登记：凭据常以这两种形态出现在 URL 或头部里
        try:
            raw = s.encode()
            _SECRETS.add(base64.b64encode(raw).decode())
            _SECRETS.add(base64.b64encode(raw).decode().rstrip("="))
            _SECRETS.add(urllib.parse.quote(s, safe=""))
        except Exception:                                       # noqa: BLE001
            pass


def redact(value: Any) -> str:
    """把已登记密钥从任意文本里抹掉。**导出层所有字符串的唯一出口。**

    长串优先替换，避免短子串先吃掉长密钥的一部分（`sk-lf-ab` 先于 `sk-lf-abc`
    匹配会留下 `REDACTEDc` 这种尾巴）。
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:                                           # noqa: BLE001
        return REDACTED
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _scrub(obj: Any, _depth: int = 0) -> Any:
    """递归脱敏返回值（dict 的键与值、list/tuple 元素、字符串）。"""
    if _depth > 6:
        return REDACTED
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        # 键是我们自己的结构名（exported / skipped / mode...），不动；
        # 只脱敏值 —— 否则 "sk" 这类短密钥会把 "skipped" 打成 "[REDACTED]ipped"。
        return {k: _scrub(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_scrub(v, _depth + 1) for v in obj]
    if isinstance(obj, float) and obj != obj:                   # NaN
        return 0.0
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return redact(obj)


def _describe_exc(exc: BaseException) -> str:
    """异常 → 可安全外出的短消息（类型名 + 脱敏后的消息）。"""
    try:
        msg = redact(str(exc))[:300]
    except Exception:                                           # noqa: BLE001
        msg = ""
    name = type(exc).__name__
    return f"{name}: {msg}" if msg else name


# ══════════════════════════════════════════════════════════════
# 能力探测：自述「我到底能用哪种模式」，不许假装
# ══════════════════════════════════════════════════════════════
def _probe_langfuse() -> dict:
    """探测 `langfuse` 包（惰性 import：模块导入时不该拉起 OTel 后端）。"""
    try:
        import langfuse  # noqa: F401  —— 故意放在函数里，monkeypatch sys.modules 才能生效

        client_cls = getattr(langfuse, "Langfuse", None)
        if client_cls is None:
            return {"available": False, "version": "",
                    "reason": "langfuse 包存在但缺少 Langfuse 类（版本过旧？）"}
        version = str(getattr(langfuse, "__version__", "") or "")
        return {"available": True, "version": version, "reason": ""}
    except Exception as exc:                                    # noqa: BLE001
        return {"available": False, "version": "",
                "reason": f"langfuse 未安装: {type(exc).__name__}"}


def _probe_otel_otlp() -> dict:
    """探测 OTel SDK + OTLP/HTTP exporter（otlp 模式的全部前提）。"""
    try:
        import opentelemetry.sdk.trace  # noqa: F401
    except Exception as exc:                                    # noqa: BLE001
        return {"available": False,
                "reason": f"opentelemetry-sdk 未安装: {type(exc).__name__}"}
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: F401
            OTLPSpanExporter,
        )
    except Exception as exc:                                    # noqa: BLE001
        return {"available": False,
                "reason": ("opentelemetry-exporter-otlp-proto-http 未安装 "
                           f"(pip install opentelemetry-exporter-otlp-proto-http): "
                           f"{type(exc).__name__}")}
    return {"available": True, "reason": ""}


def detect() -> dict:
    """自述能力：装了哪些包、能用哪种模式。**不许假装可用。**

    返回::

        {"langfuse_sdk": bool, "otel_otlp": bool,
         "mode": "sdk"|"otlp"|"none", "reason": str,
         "langfuse_version": str, "otel_reason": str}

    优先级 sdk > otlp > none：SDK 模式是 Langfuse 的原生形态（能设 level /
    model / usage，还能拿 trace URL）；otlp 模式只保留 gen_ai.* 语义约定。
    """
    sdk = _probe_langfuse()
    otlp = _probe_otel_otlp()
    if sdk.get("available"):
        mode = "sdk"
        ver = sdk.get("version") or "?"
        reason = f"langfuse SDK {ver} 可用：客户端直发 observation（推荐）"
    elif otlp.get("available"):
        mode = "otlp"
        reason = ("langfuse SDK 未安装，降级 OTLP 模式：span 经 "
                  "configure_otel() 发往 Langfuse 的 OTLP endpoint")
    else:
        mode = "none"
        reason = ("langfuse SDK 与 OTel OTLP exporter 都不可用（"
                  f"{sdk.get('reason', '')}；{otlp.get('reason', '')}）；"
                  "pip install langfuse 可一次带齐")
    return {
        "langfuse_sdk": bool(sdk.get("available")),
        "otel_otlp": bool(otlp.get("available")),
        "mode": mode,
        "reason": redact(reason),
        "langfuse_version": str(sdk.get("version") or ""),
        "otel_reason": redact(otlp.get("reason", "")),
    }


# ══════════════════════════════════════════════════════════════
# 模块状态：只留指纹，不留明文密钥
# ══════════════════════════════════════════════════════════════
_STATE: dict = {
    "enabled": False,
    "mode": "none",
    "reason": "尚未配置（先调用 configure_langfuse）",
    "host": "",
}
_CLIENT: Any = None          # sdk 模式的客户端（真客户端或注入的假客户端）
_FACTORY: Any = None         # 客户端工厂（懒创建 / 测试计数用）
_FINGERPRINT: str = ""       # 凭据+host+mode 的 sha256 前缀，幂等判据
_OTLP_HEADERS: dict = {}     # 已登记的 Basic 头（内部用，永不返回）


def _fingerprint(public_key: str, secret_key: str, host: str, mode: str) -> str:
    """凭据指纹：用来判「同一次配置」而不必保存明文。"""
    blob = "\x00".join([public_key or "", secret_key or "", host or "", mode or ""])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def current_state() -> dict:
    """当前导出状态快照（已脱敏）。**不含任何凭据。**"""
    return _scrub({
        "enabled": bool(_STATE.get("enabled")),
        "mode": str(_STATE.get("mode") or "none"),
        "reason": str(_STATE.get("reason") or ""),
        "host": str(_STATE.get("host") or ""),
    })


def reset() -> None:
    """清空模块状态（测试用；生产误调也只是回到「未配置」）。"""
    global _CLIENT, _FACTORY, _FINGERPRINT, _OTLP_HEADERS
    _CLIENT = None
    _FACTORY = None
    _FINGERPRINT = ""
    _OTLP_HEADERS = {}
    _SECRETS.clear()
    _STATE.update({"enabled": False, "mode": "none",
                   "reason": "尚未配置（reset 后，先调用 configure_langfuse）",
                   "host": ""})


def _disable(reason: str) -> None:
    """把状态翻回未启用（"配置了但没生效"必须可见，不许静默）。"""
    _STATE["enabled"] = False
    _STATE["reason"] = redact(reason)


def _result(enabled: bool, mode: str, reason: str, host: str) -> dict:
    """configure_langfuse 的返回形状。**只有这四个键，且已脱敏。**"""
    return _scrub({"enabled": bool(enabled), "mode": str(mode),
                   "reason": str(reason or ""), "host": str(host or "")})


def _accepts_kwargs(fn: Any, names: tuple[str, ...]) -> bool:
    """工厂/客户端是否接受这些关键字（决定要不要带参调用）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


def _make_client(factory: Any, *, public_key: str, secret_key: str,
                 host: str, service_name: str) -> Any:
    """建客户端：优先注入的工厂（测试/自建封装），否则用真 Langfuse。

    工厂的调用约定：能接 `public_key/secret_key/host` 就带参调，接不了就空参调
    —— 让测试可以写 `lambda **kw: FakeClient(**kw)` 或 `FakeClient`。
    """
    names = ("public_key", "secret_key", "host")
    if factory is not None:
        if _accepts_kwargs(factory, names):
            return factory(public_key=public_key, secret_key=secret_key, host=host)
        return factory()
    from langfuse import Langfuse  # 惰性导入：只有 sdk 模式才会走到
    return Langfuse(public_key=public_key or None, secret_key=secret_key or None,
                    host=host or None, tracing_enabled=True)


def configure_langfuse(*, public_key: str = "", secret_key: str = "",
                       host: str = "", mode: str = "auto",
                       service_name: str = "agent-runtime",
                       client: Any = None, client_factory: Any = None) -> dict:
    """配置导出。返回 `{"enabled": bool, "mode": str, "reason": str, "host": str}`。

    参数
    ----
    public_key / secret_key : Langfuse 项目的 pk-lf-... / sk-lf-...
    host                    : 默认 `https://cloud.langfuse.com`（自建填自己的）
    mode                    : "auto"（按 detect 降级）| "sdk" | "otlp" | "none"
    service_name            : OTLP 模式的 service.name（otlp 模式才用得上）
    client                  : 直接注入一个已建好的客户端（**测试/复用**用；
                              sdk 模式下有了它就不再要求凭据）
    client_factory          : 懒建客户端；同一次配置只调用一次（幂等）

    **纪律**：`enabled=True` 只在真的拿到可用客户端、或 OTLP exporter 真的
    挂上时才返回。任何失败都返回 `enabled=False` + 具体原因，绝不假装成功。
    """
    global _CLIENT, _FACTORY, _FINGERPRINT, _OTLP_HEADERS

    # 先登记密钥：后面任何失败原因都可能是被 SDK 回显的凭据，必须先可脱敏
    _register_secret(public_key, secret_key)

    host = (host or DEFAULT_HOST).strip().rstrip("/")
    wanted = str(mode or "auto").strip().lower() or "auto"
    caps = detect()
    if wanted == "auto":
        wanted = caps["mode"]

    if wanted not in ("sdk", "otlp", "none"):
        _STATE.update({"enabled": False, "mode": "none", "host": host,
                       "reason": f"未知 mode: {mode!r}（可选 auto/sdk/otlp/none）"})
        return _result(False, "none", _STATE["reason"], host)

    if wanted == "none":
        reason = f"mode=none：不导出（{caps['reason']}）"
        _STATE.update({"enabled": False, "mode": "none", "host": host, "reason": reason})
        return _result(False, "none", reason, host)

    # 幂等：同一套凭据 + host + mode 且客户端/OTLP 已在位 → 直接复用，不重建
    fp = _fingerprint(public_key, secret_key, host, wanted)
    if (fp == _FINGERPRINT and _STATE.get("enabled")
            and _STATE.get("mode") == wanted
            and (_CLIENT is not None or wanted == "otlp")):
        return _result(True, wanted, _STATE.get("reason", ""), host)

    if wanted == "sdk":
        if client is None and client_factory is None and not caps["langfuse_sdk"]:
            reason = ("mode=sdk 但 langfuse 包不可用：pip install langfuse；"
                      "或改用 mode='otlp'（需 opentelemetry-exporter-otlp-proto-http）")
            _STATE.update({"enabled": False, "mode": "sdk", "host": host, "reason": reason})
            return _result(False, "sdk", reason, host)
        if client is None and client_factory is None and not (public_key and secret_key):
            reason = ("缺少 public_key / secret_key：Langfuse 需要项目凭据才能导出"
                      "（或注入 client= 复用已认证客户端）")
            _STATE.update({"enabled": False, "mode": "sdk", "host": host, "reason": reason})
            return _result(False, "sdk", reason, host)

        try:
            built = client if client is not None else _make_client(
                client_factory, public_key=public_key, secret_key=secret_key,
                host=host, service_name=service_name)
            if built is None:
                raise RuntimeError("客户端工厂返回 None")
        except Exception as exc:                                # noqa: BLE001
            reason = f"Langfuse 客户端创建失败: {_describe_exc(exc)}"
            _CLIENT = None
            _STATE.update({"enabled": False, "mode": "sdk", "host": host, "reason": reason})
            return _result(False, "sdk", reason, host)

        _CLIENT = built
        _FACTORY = client_factory
        _OTLP_HEADERS = {}
        _FINGERPRINT = fp
        reason = ("sdk 客户端就绪"
                  + ("（注入客户端）" if client is not None else ""))
        _STATE.update({"enabled": True, "mode": "sdk", "host": host, "reason": reason})
        return _result(True, "sdk", reason, host)

    # ── otlp 模式：靠 plane.quality.configure_otel 挂 exporter ──
    if not (public_key and secret_key):
        reason = ("otlp 模式缺少 public_key / secret_key：Basic 认证头需要两者"
                  "（base64(public_key:secret_key)）")
        _STATE.update({"enabled": False, "mode": "otlp", "host": host, "reason": reason})
        return _result(False, "otlp", reason, host)

    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    _register_secret(token, token.rstrip("="))
    headers = {"Authorization": f"Basic {token}"}
    endpoint = f"{host}{OTLP_TRACE_PATH}"

    try:
        from plane.quality import configure_otel       # 惰性导入：不给别的测试添负担
        info = configure_otel(endpoint, service_name=service_name, headers=headers)
    except Exception as exc:                                # noqa: BLE001
        reason = f"OTLP 配置抛错: {_describe_exc(exc)}"
        _STATE.update({"enabled": False, "mode": "otlp", "host": host, "reason": reason})
        return _result(False, "otlp", reason, host)

    if not (isinstance(info, dict) and info.get("enabled")):
        detail = (info or {}).get("reason") if isinstance(info, dict) else info
        reason = f"OTLP 未生效（exporter 没挂上）: {redact(detail or '未知原因')}"
        _STATE.update({"enabled": False, "mode": "otlp", "host": host, "reason": reason})
        return _result(False, "otlp", reason, host)

    _CLIENT = None
    _FACTORY = None
    _OTLP_HEADERS = headers
    _FINGERPRINT = fp
    reason = f"OTLP 已生效: {redact(endpoint)}"
    _STATE.update({"enabled": True, "mode": "otlp", "host": host, "reason": reason})
    return _result(True, "otlp", reason, host)


# ══════════════════════════════════════════════════════════════
# trace_summary：Tracer → Langfuse 友好的扁平结构（纯转换）
# ══════════════════════════════════════════════════════════════
def _jsonable(value: Any, _depth: int = 0) -> Any:
    """把任意属性值压成 JSON 可序列化的形状（Langfuse 的 metadata 必须是 JSON）。

    不认识的类型退化成 `str()`，深度超过 4 层截断——宁可少信息，
    也不让一个自定义对象把整次导出搞崩（fail-open）。
    """
    if _depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, _depth + 1) for v in value]
    try:
        return _jsonable(value.to_dict(), _depth + 1)
    except Exception:                                       # noqa: BLE001
        pass
    try:
        return str(value)
    except Exception:                                       # noqa: BLE001
        return "<unprintable>"


def _iso(ms: Any) -> str:
    """epoch 毫秒 → ISO-8601 UTC 字符串（Langfuse 前端直接吃）。"""
    try:
        v = float(ms or 0.0)
    except Exception:                                       # noqa: BLE001
        return ""
    if v <= 0:
        return ""
    try:
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc).isoformat()
    except Exception:                                       # noqa: BLE001
        return ""


def _level_of(attrs: dict) -> str:
    """推断 observation level：显式写了的优先，其次看 error 标记。"""
    raw = str(attrs.get("level", "") or "").upper()
    if raw in _LEVELS:
        return raw
    if attrs.get("error") or attrs.get("exception"):
        return "ERROR"
    status = str(attrs.get("status", "") or "").lower()
    if status in ("failed", "error", "timeout"):
        return "ERROR"
    if status in ("denied", "needs_confirmation", "partial"):
        return "WARNING"
    return "DEFAULT"


def _type_of(name: str, attrs: dict) -> str:
    """按名字/属性推断 Langfuse observation 类型（决定 UI 上的呈现）。"""
    low = name.lower()
    if (attrs.get("model") or attrs.get("gen_ai.request.model")
            or "llm" in low or "generation" in low or "completion" in low):
        return "generation"
    if "tool" in low or attrs.get("tool") or attrs.get("tool_name"):
        return "tool"
    if "agent" in low:
        return "agent"
    if "retriev" in low or "recall" in low:
        return "retriever"
    if "chain" in low:
        return "chain"
    return "span"


def _usage_of(attrs: dict) -> dict:
    """从 gen_ai.usage.* / 简写键里拼 usage_details（Langfuse 的入参形状）。"""
    inp = attrs.get("gen_ai.usage.input_tokens", attrs.get("input_tokens"))
    out = attrs.get("gen_ai.usage.output_tokens", attrs.get("output_tokens"))
    usage: dict[str, int] = {}
    for key, raw in (("input", inp), ("output", out)):
        try:
            n = int(raw)                                # type: ignore[arg-type]
        except Exception:                               # noqa: BLE001
            continue
        if n:
            usage[key] = n
    if len(usage) == 2:
        usage["total"] = usage["input"] + usage["output"]
    return usage


def trace_summary(tracer) -> dict:
    """把 Tracer 的 span 树压成 Langfuse 友好的扁平结构（**纯转换，不联网**）。

    返回::

        {"name": str,                     # trace 名（task_id 有值时带上）
         "trace_id": str,                 # 原样保留 Tracer.trace_id
         "observations": [                # 先序压平，父在子前
            {"id", "parent_id", "name", "type", "level",
             "start_time", "end_time",            # ISO-8601 UTC
             "metadata",                          # span.attrs + duration_ms + path
             "input", "output",                   # 若 attrs 里带了
             "model", "usage"}                    # generation 专用
         ],
         "totals": dict, "metadata": dict}

    结构上的选择：
      * `id` 用 `{trace_id}-{序号}` 而不是随机 uuid —— 同一棵树转两次结果相同，
        测试和重放都能对上；`parent_id` 指向父的同一个 id。
      * 时间是 ISO 字符串而不是 epoch 数字：Langfuse 前端和 SDK 都直接吃，
        也避免"毫秒还是秒"的歧义（Tracer 用 epoch 毫秒，这里明确换算）。
      * `attrs` 不丢：原样进 metadata（过 `_jsonable`），未来加语义约定不用改这里。
    """
    out: dict = {"name": "agent-runtime", "trace_id": "", "observations": [],
                 "totals": {}, "metadata": {}}
    try:
        report = tracer.finish()
    except Exception as exc:                                # noqa: BLE001
        out["reason"] = f"tracer.finish() 失败: {_describe_exc(exc)}"
        return out
    if not isinstance(report, dict):
        out["reason"] = f"tracer.finish() 返回了 {type(report).__name__}，不是 dict"
        return out

    trace_id = str(report.get("trace_id") or "")
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    task_id = str((meta or {}).get("task_id") or "")
    out["trace_id"] = trace_id
    out["name"] = f"agent-runtime:{task_id}" if task_id else "agent-runtime"
    out["metadata"] = _jsonable({"tenant": (meta or {}).get("tenant", ""),
                                 "user_id": (meta or {}).get("user_id", ""),
                                 "task_id": task_id})
    out["totals"] = _jsonable(report.get("totals") or {})

    observations: list[dict] = []
    seq = [0]

    def walk(node: Any, parent_id: str, path: str) -> None:
        if not isinstance(node, dict):
            return
        idx = seq[0]
        seq[0] = idx + 1
        obs_id = f"{trace_id or 'trace'}-{idx:03d}"
        raw_attrs = node.get("attrs")
        attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
        name = str(node.get("name") or "span")
        obs_type = _type_of(name, attrs)
        start_time = _iso(node.get("start_ms"))
        end_time = _iso(node.get("end_ms"))

        metadata = {k: v for k, v in attrs.items() if k not in ("input", "output")}
        metadata["duration_ms"] = node.get("duration_ms")
        metadata["path"] = path or name
        if trace_id:
            metadata["trace_id"] = trace_id

        obs: dict = {
            "id": obs_id,
            "parent_id": parent_id,
            "name": name,
            "type": obs_type,
            "level": _level_of(attrs),
            "start_time": start_time,
            "end_time": end_time,
            "metadata": _jsonable(metadata),
        }
        if "input" in attrs:
            obs["input"] = _jsonable(attrs.get("input"))
        if "output" in attrs:
            obs["output"] = _jsonable(attrs.get("output"))
        if obs_type == "generation":
            model = attrs.get("model") or attrs.get("gen_ai.request.model")
            if model:
                obs["model"] = str(model)
            usage = _usage_of(attrs)
            if usage:
                obs["usage"] = usage
        observations.append(obs)

        for child in (node.get("children") or ()):
            walk(child, obs_id, f"{path}/{child.get('name', 'span')}"
                 if isinstance(child, dict) else path)

    roots = report.get("spans") or []
    for root in roots:
        walk(root, "", "")
    out["observations"] = observations
    return out


# ══════════════════════════════════════════════════════════════
# 导出到 Langfuse
# ══════════════════════════════════════════════════════════════
def _valid_trace_id(candidate: Any) -> str:
    """Langfuse 要求 32 位小写 hex；不合规就换成确定性哈希（可对齐、可复现）。"""
    s = str(candidate or "")
    if len(s) == 32 and all(c in "0123456789abcdef" for c in s):
        return s
    return hashlib.sha256((s or "trace").encode()).hexdigest()[:32]


def _trace_id_for(client: Any, summary: dict) -> str:
    """优先让客户端按 seed 生成（同 seed 确定），否则本地哈希兜底。"""
    seed = str(summary.get("trace_id") or "")
    fn = getattr(client, "create_trace_id", None)
    if callable(fn):
        try:
            tid = fn(seed=seed)
            if tid:
                return _valid_trace_id(tid)
        except Exception:                                   # noqa: BLE001
            pass
    return _valid_trace_id(seed)


@contextlib.contextmanager
def _scope_ctx(client: Any, scope: dict) -> Iterator[bool]:
    """把 user_id / session_id / tags 挂到「这段代码里创建的 span」上。

    Langfuse 5.x 的正路是 `propagate_attributes(...)` 上下文（写 OTel context，
    之后创建的 span 才带 `user.id` / `session.id` / `langfuse.trace.tags`）。
    先试客户端实例上的同名方法（**假客户端就是这样断言参数被传到的**），
    再试模块级函数；两者都没有 → yield False，由调用方降级写根 span metadata。
    """
    if not any(scope.values()):
        yield False
        return
    cm = None
    declared = hasattr(client, "propagate_attributes")
    method = getattr(client, "propagate_attributes", None)
    if callable(method):
        try:
            cm = method(**scope)
        except Exception:                                   # noqa: BLE001
            cm = None
    elif not declared:
        # 客户端**根本没这个属性** → 才去用模块级函数（真 Langfuse 走这条）。
        # 显式声明 propagate_attributes=None 的客户端视为"不支持"，不偷用模块级，
        # 这样封装层可以自己决定要不要透传。
        try:
            from langfuse import propagate_attributes as _pa
            cm = _pa(**scope)
        except Exception:                                   # noqa: BLE001
            cm = None
    if cm is None:
        yield False
        return
    try:
        with cm:
            yield True
    except Exception:                                       # noqa: BLE001
        # 上下文管理器自己炸了不该拖垮导出：降级为「没挂上」
        yield False


def _end_span(span: Any, end_time: str) -> None:
    """结束一个回填的 span。`LangfuseSpan.end(end_time=纳秒)`，不传就当前时刻。"""
    end = getattr(span, "end", None)
    if not callable(end):
        return
    ns = 0
    if end_time:
        try:
            ns = int(datetime.fromisoformat(end_time).timestamp() * 1_000_000_000)
        except Exception:                                   # noqa: BLE001
            ns = 0
    try:
        end(end_time=ns) if ns > 0 else end()
    except TypeError:
        end()
    except Exception:                                       # noqa: BLE001
        pass


def _export_sdk(client: Any, summary: dict, scope: dict,
                extra_metadata: dict | None = None) -> tuple[int, int, str, str]:
    """sdk 模式：逐条 observation 发给客户端。

    返回 `(exported, skipped, 首个发送错误, flush 错误)` —— 两者分开：
    "span 建起来了但 flush 没送达" 和 "span 根本建不起来" 是两种故障，
    混在一起会把「已提交但可能丢」误报成「全失败」。
    """
    trace_id = _trace_id_for(client, summary)
    id_map: dict[str, str] = {}
    exported = skipped = 0
    first_err = ""
    observations = summary.get("observations") or []

    with _scope_ctx(client, scope) as applied:
        for idx, obs in enumerate(observations):
            try:
                parent_id = id_map.get(str(obs.get("parent_id") or ""), "")
                ctx: dict = {"trace_id": trace_id}
                if parent_id:
                    ctx["parent_span_id"] = parent_id
                metadata = dict(obs.get("metadata") or {})
                if extra_metadata:
                    metadata.update(extra_metadata)
                if idx == 0 and not applied and any(scope.values()):
                    # 降级通道：客户端不支持 propagate_attributes 时，
                    # 至少让 trace 级身份信息在根 span 上可见
                    metadata["langfuse_scope"] = {k: v for k, v in scope.items() if v}
                kwargs: dict = {
                    "trace_context": ctx,
                    "name": obs.get("name") or "span",
                    "as_type": obs.get("type") if obs.get("type") in _SDK_TYPES else "span",
                    "metadata": metadata,
                    "level": obs.get("level") or "DEFAULT",
                }
                if obs.get("input") is not None:
                    kwargs["input"] = obs["input"]
                if obs.get("output") is not None:
                    kwargs["output"] = obs["output"]
                if obs.get("model"):
                    kwargs["model"] = obs["model"]
                if obs.get("usage"):
                    kwargs["usage_details"] = obs["usage"]

                span = client.start_observation(**kwargs)
                id_map[str(obs.get("id"))] = str(getattr(span, "id", "") or "")
                _end_span(span, str(obs.get("end_time") or ""))
                exported += 1
            except Exception as exc:                        # noqa: BLE001
                skipped += 1
                if not first_err:
                    first_err = _describe_exc(exc)

    # 收尾 flush：把缓冲里的 span 真发出去，失败也不抛（fail-open）
    flush_err = ""
    flush = getattr(client, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception as exc:                            # noqa: BLE001
            flush_err = f"flush 失败: {_describe_exc(exc)}"
    return exported, skipped, first_err, flush_err


def export_tracer(tracer, *, user_id: str = "", session_id: str = "",
                  tags: tuple[str, ...] | list[str] = (),
                  client: Any = None, **kw: Any) -> dict:
    """把一次 Tracer 会话导出到 Langfuse。

    返回 `{"exported": n, "skipped": n, "mode": str, "reason": str}` ——
    **永远返回统计，不抛异常**（观测面 fail-open，INV-9）。

    参数
    ----
    tracer     : `plane.quality.Tracer`（要有 `finish()`；任何 duck-type 都行）
    user_id    : 传到 Langfuse 的 `user.id`（按用户聚合成本/延迟用）
    session_id : 传到 `session.id`（多轮会话归组用）
    tags       : trace 标签
    client     : 临时用某个客户端导出（不改变已配置状态；测试/多租户用）

    附加关键字（`**kw`）：`trace_name`（覆盖 trace 名）、`metadata`（并入根
    span 的 metadata）——其余忽略，保持向前兼容。

    「导出失败」不静默：若一条都没发出去，会把模块状态翻回 `enabled=False`
    并写明原因，`current_state()` 立刻能看见。
    """
    out: dict = {"exported": 0, "skipped": 0,
                 "mode": str(_STATE.get("mode") or "none"), "reason": ""}
    try:
        scope = {
            "user_id": str(user_id or ""),
            "session_id": str(session_id or ""),
            "tags": [str(t) for t in (tags or ()) if str(t)],
        }
        extra_metadata: dict = {}
        if kw.get("trace_name"):
            extra_metadata["trace_name"] = str(kw["trace_name"])
        if isinstance(kw.get("metadata"), dict):
            extra_metadata.update(_jsonable(kw["metadata"]))

        if client is not None:
            active_mode, active_client = "sdk", client
        elif _STATE.get("enabled") and _STATE.get("mode") == "sdk":
            active_mode, active_client = "sdk", _CLIENT
        elif _STATE.get("enabled") and _STATE.get("mode") == "otlp":
            active_mode, active_client = "otlp", None
        else:
            out["mode"] = str(_STATE.get("mode") or "none")
            out["reason"] = str(_STATE.get("reason")
                                or "未配置 Langfuse 导出（先调用 configure_langfuse）")
            return _scrub(out)

        out["mode"] = active_mode
        summary = trace_summary(tracer)
        observations = summary.get("observations") or []
        if not observations:
            out["reason"] = str(summary.get("reason") or "没有 span 可导出")
            return _scrub(out)

        if active_mode == "sdk":
            if active_client is None:
                out["reason"] = "sdk 模式但没有可用客户端（先 configure_langfuse）"
                _disable(out["reason"])
                return _scrub(out)
            exported, skipped, err, flush_err = _export_sdk(
                active_client, summary, scope, extra_metadata)
            out["exported"], out["skipped"] = exported, skipped
            if exported == 0:
                out["reason"] = f"全部 {len(observations)} 条 observation 导出失败: {err}"
                _disable(out["reason"])
            elif flush_err:
                # span 都建起来了，但 flush 没送达：数据可能丢，reason 必须带出来，
                # 但不因此摘牌（客户端下一轮也许就好了）
                out["reason"] = f"span 已提交但未确认送达: {flush_err}"
                _STATE["reason"] = redact(out["reason"])
            elif skipped:
                out["reason"] = f"部分失败：{skipped} 条跳过（首个错误: {err}）"
                _STATE["reason"] = redact(out["reason"])
            return _scrub(out)

        # ── otlp 模式：把同一棵树交给 plane.quality 的 OTel 桥 ──
        from plane.quality import tracer_to_otel
        res = tracer_to_otel(tracer)
        out["exported"] = int((res or {}).get("exported", 0) or 0)
        out["skipped"] = int((res or {}).get("skipped", 0) or 0)
        out["reason"] = str((res or {}).get("reason") or "")
        if out["exported"] == 0:
            out["reason"] = (f"OTLP 导出未发出任何 span: "
                             f"{out['reason'] or '未知原因'}")
            _disable(out["reason"])
        return _scrub(out)
    except Exception as exc:                                # noqa: BLE001
        # 最后一道 fail-open：连 trace_summary 都炸了也要返回统计
        out["reason"] = f"导出异常（已 fail-open）: {_describe_exc(exc)}"
        try:
            _disable(out["reason"])
        except Exception:                                   # noqa: BLE001
            pass
        return _scrub(out)
