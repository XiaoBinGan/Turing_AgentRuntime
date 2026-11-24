"""adapters/openai_compat.py — OpenAI Chat Completions 兼容协议 adapter（中性 Runtime 能力）。

一份实现同时接 OpenAI / DeepSeek / Qwen（DashScope 兼容模式）/ vLLM / Ollama /
任何实现了 ``POST {base_url}/chat/completions`` 的服务。

设计边界（与 ``llm_mock.py`` 同属 adapter 层）：

1. **零第三方依赖**：只用 ``urllib.request``。审计一棵 SDK 依赖树比读 200 行
   urllib 贵得多，而 adapter 的全部价值就在「屏蔽协议差异」这件事本身。
2. **不改 budget**：预算记账归 Loop（mock 时代的约定，避免双记账）。本 adapter 只
   *报*成本（``last_cost_usd`` / ``stats()``），花不花由上层决定。
3. **失败分类先于重试**：401/403/400 重试是纯流量浪费；只有 429/5xx/网络抖动才值得
   退避。见 :func:`classify_status`。
4. **畸形参数不炸整轮**：单个 ``tool_call`` 的 ``arguments`` 解析失败只丢那一个调用，
   其余照常返回，并把原因记进 ``last_warnings``。
5. **api_key 永不出现在任何异常/日志里**：所有对外消息都过 :meth:`_scrub`。
6. **工具 schema 以契约为准**：``manifest.input_schema`` 声明了就用它原样发出（模型
   看到的是真实字段名），没声明才退回保守形状 ``{"args": object}``，让模型自己塞
   语义参数；响应侧再把这层壳解包，Dispatcher 只吃扁平 args。
   「声明了却发保守形状」的代价是实测过的：模型把 ``lines`` 写成 ``limit``，
   Dispatcher 静默接受，工具返回空结果，模型基于空结果一本正经地分析。
"""
from __future__ import annotations

import copy
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

from contracts import (
    ExecCtx,
    LLMResponse,
    NormalizedToolCall,
    ToolManifest,
    Usage,
    now_ms,
)

# ══════════════════════════════════════════════════════════════
# 失败分类表（这是 adapter 的核心判断，不是装饰）
# ══════════════════════════════════════════════════════════════
#: 值得退避重试的状态码：限流 / 网关抖动 / 上游过载 / 请求超时。
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 529})

#: 确定性失败：鉴权错、参数错。换模型可能有用，但**不重试同一请求**。
FATAL_STATUS: frozenset[int] = frozenset({400, 401, 403})

#: 退避：base * 2**i，封顶 8s（避免一次抖动把 turn 拖到 deadline 之后）。
RETRY_BASE_S: float = 0.5
RETRY_CAP_S: float = 8.0

#: 服务端错误体只取前 N 个字符进异常消息（防止 HTML 错误页灌爆日志）。
ERROR_BODY_CHARS = 400

#: 保守工具参数 schema：不假装知道工具的真实参数，只给一个 object 容器。
TOOL_PARAMETERS: dict = {
    "type": "object",
    "properties": {"args": {"type": "object"}},
    "required": ["args"],
}

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{3,}")
_KEYLIKE_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9._\-]{4,}")


class LLMError(RuntimeError):
    """模型调用失败。**消息已脱敏**，``kind`` 给上层做分流（重试 / 降级 / 上报）。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        status: int = 0,
        model: str = "",
        retryable: bool = False,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = int(status)
        self.model = model
        self.retryable = bool(retryable)
        self.attempts = int(attempts)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status, "model": self.model,
                "retryable": self.retryable, "attempts": self.attempts,
                "message": str(self)}


class _AttemptError(Exception):
    """内部单次尝试失败（不对外暴露，由 complete() 翻译成重试或 LLMError）。"""

    def __init__(self, kind: str, detail: str, *, status: int = 0,
                 retryable: bool = False) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = int(status)
        self.retryable = bool(retryable)


class _MalformedArguments(Exception):
    """``tool_call.function.arguments`` 解不成 dict。"""


def classify_status(status: int) -> str:
    """HTTP 状态 → 失败类别。类别决定「要不要重试」，不决定文案。"""
    if status in (401, 403):
        return "auth"
    if status == 400:
        return "bad_request"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    if status in RETRYABLE_STATUS:
        return "transient"
    return "client"


def estimate_tokens(text: str) -> int:
    """确定性 token 估算（与 ``builder.estimate_tokens`` 同口径）。

    协议不返回 usage 时用它兜底：CJK 1 char ≈ 1 token，其余 3.5 char ≈ 1 token。
    只求排序正确、可复现，不追求与真实 tokenizer 精确对齐。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    other = len(text) - cjk
    return max(1, int(cjk + other / 3.5))


def _as_int(value: Any) -> int:
    """usage 字段可能是 int / float / 字符串，统一收口成非负 int。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return 0
    return 0


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _conservative_parameters() -> dict:
    """每次返回新对象：schema 会被塞进请求体，任何下游手滑都不该污染模块常量。"""
    return copy.deepcopy(TOOL_PARAMETERS)


# ══════════════════════════════════════════════════════════════
# Adapter
# ══════════════════════════════════════════════════════════════
class OpenAICompatAdapter:
    """OpenAI Chat Completions 兼容协议（OpenAI / DeepSeek / Qwen / vLLM / Ollama 都吃这一套）。"""

    RETRY_BASE_S = RETRY_BASE_S
    RETRY_CAP_S = RETRY_CAP_S
    HISTORY_LIMIT = 500          # 长跑进程的流水上限，防止 history 无限增长

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        timeout_ms: int = 60000,
        max_retries: int = 3,
        fallback_models: Sequence[str] = (),
        temperature: float = 0.2,
        max_tokens: int | None = None,
        price_in_per_m: float = 0.5,
        price_out_per_m: float = 1.5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        base = (base_url or "").strip()
        if not base:
            raise ValueError("base_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        if not (model or "").strip():
            raise ValueError("model 不能为空")

        self.base_url = base
        self.endpoint = (
            base if base.rstrip("/").endswith("/chat/completions")
            else base.rstrip("/") + "/chat/completions"
        )
        self.model = model.strip()
        self.api_key = (api_key or "").strip()
        self.timeout_ms = int(timeout_ms)
        self.max_retries = max(0, int(max_retries))
        self.fallback_models = tuple(m for m in (str(x).strip() for x in fallback_models) if m)
        self.temperature = float(temperature)
        #: 不设时交给服务端默认值。**reasoning 模型必须显式设**：
        #: 实测 deepseek-flash 在 max_tokens=60 时把预算全花在 reasoning 上，
        #: 返回 content=null + finish_reason=length —— 看起来像"模型不说话"。
        self.max_tokens = int(max_tokens) if max_tokens else None
        self.price_in_per_m = float(price_in_per_m)
        self.price_out_per_m = float(price_out_per_m)
        self.sleeper = sleeper
        #: 传输接缝：默认 urllib.request.urlopen。测试用它注入确定性失败
        #: （本机可能挂着系统代理，真实 socket 层的失败形态不可移植）。
        self._open: Callable[..., Any] = urllib.request.urlopen

        # ── 观测/计量（只读快照，不改 budget）────────────────
        self.last_estimated: bool = False        # 本次响应是否走了 token 估算
        self.last_usage: Usage = Usage()
        self.last_cost_usd: float = 0.0
        self.last_status: int = 0
        self.last_warnings: list[str] = []       # 本次响应的解析警告（畸形 tool_call 等）
        self.warnings: list[str] = []            # 累计警告
        self.history: list[dict] = []            # 每次尝试 / 切换 fallback 的流水

        self._calls = 0
        self._requests = 0
        self._retries = 0
        self._fallback_switches = 0
        self._errors = 0
        self._estimated_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd = 0.0

    # ── 模型链 ──────────────────────────────────────────────────
    @property
    def models(self) -> tuple[str, ...]:
        """主模型 + 有序 fallback。顺序即降级优先级。"""
        return (self.model, *self.fallback_models)

    # ── 对外入口 ────────────────────────────────────────────────
    def complete(self, prompt: str, tools: Sequence[ToolManifest],
                 e: ExecCtx) -> LLMResponse:
        self._calls += 1
        self._guard(e)

        tools_schema = self.tools_schema(tools)
        base_body = self._request_body(prompt, tools_schema)

        last: _AttemptError | None = None
        for index, model in enumerate(self.models):
            if index and last is not None:
                self._fallback_switches += 1
                self._log({
                    "event": "fallback",
                    "from": self.models[index - 1],
                    "model": model,
                    "reason": f"{last.kind}: {last.detail}"[:300],
                })
            for attempt in range(self.max_retries + 1):
                self._guard(e)                      # deadline / abort 在每次尝试前重查
                body = dict(base_body)
                body["model"] = model
                self._requests += 1
                try:
                    doc = self._post(body, e)
                except _AttemptError as exc:
                    last = exc
                    will_retry = exc.retryable and attempt < self.max_retries
                    self._log({
                        "event": ("retry" if will_retry
                                  else ("exhausted" if exc.retryable else "fatal")),
                        "model": model,
                        "attempt": attempt,
                        "status": exc.status,
                        "kind": exc.kind,
                        "error": self._scrub(exc.detail)[:300],
                    })
                    if not exc.retryable:
                        # 鉴权 / 参数错：重试同一 body 一定是再错一次，直接上抛
                        raise self._fail(
                            f"llm request rejected ({exc.kind}) by {model}: {exc.detail}",
                            kind=exc.kind, status=exc.status, model=model,
                            retryable=False, attempts=attempt + 1,
                        ) from None
                    if not will_retry:
                        break                       # 该模型重试耗尽 → 下一个 fallback
                    delay = min(self.RETRY_BASE_S * (2 ** attempt), self.RETRY_CAP_S)
                    self._sleep(delay, e)
                    self._retries += 1
                    continue
                return self._finish(doc, model, prompt, tools_schema)

        assert last is not None
        raise self._fail(
            f"all models exhausted after {self._requests} request(s); "
            f"last error {last.kind}({last.status}): {last.detail}",
            kind=last.kind, status=last.status, model=self.models[-1],
            retryable=False, attempts=self._requests,
        )

    # ── 协议转换：请求侧 ────────────────────────────────────────
    @staticmethod
    def tool_schema(manifest: ToolManifest) -> dict:
        """ToolManifest → OpenAI function schema。

        ``parameters`` 优先用 ``manifest.input_schema`` **原样发出**：它本来就是一份
        JSON Schema 子集，编造/翻译只会引入偏差。声明了契约的工具，模型看到的是
        真实字段名（``service`` / ``lines``），而不是只能靠猜的 ``{"args": {}}``。

        没有声明契约才退回保守形状 —— 老工具（以及还没补 schema 的工具）必须继续
        能跑，这是向后兼容的边界，不是「暂时这样」。

        ``deepcopy`` 保证每次返回新对象（schema 会被塞进请求体，下游手滑不该污染
        manifest 里那份共享声明），同时保证同 manifest 两次调用结果相等（确定性）。
        """
        name = str(getattr(manifest, "name", "") or "unknown_tool")
        description = str(getattr(manifest, "description", "") or "")
        if not description:
            side = getattr(getattr(manifest, "side_effect", None), "value", "read_only")
            try:
                tier = int(getattr(manifest, "risk_tier", 0))
            except (TypeError, ValueError):
                tier = 0
            description = f"{name} (side_effect={side}, risk_tier=T{tier})"
        declared = getattr(manifest, "input_schema", None)
        # 空 dict 不是合法 schema：严格的 OpenAI 兼容服务会 400（tools[].function.
        # parameters 必须是完整 schema）。所以 `{}` 与 `None` 一样退回保守形状。
        parameters = (copy.deepcopy(declared) if isinstance(declared, dict) and declared
                      else _conservative_parameters())
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    @classmethod
    def tools_schema(cls, tools: Sequence[ToolManifest] | None) -> list[dict]:
        return [cls.tool_schema(m) for m in (tools or ())]

    def _request_body(self, prompt: str, tools_schema: list[dict]) -> dict:
        body: dict = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if tools_schema:
            body["tools"] = tools_schema
            body["tool_choice"] = "auto"
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        return body

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "agent-runtime-openai-compat/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ── 传输 ────────────────────────────────────────────────────
    def _post(self, body: dict, e: ExecCtx) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=data, method="POST", headers=self._headers())
        timeout_s = self._timeout_s(e)
        try:
            with self._open(req, timeout=timeout_s) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read()
            self.last_status = status
        except urllib.error.HTTPError as exc:
            self.last_status = int(exc.code)
            kind = classify_status(exc.code)
            raise _AttemptError(
                kind,
                f"HTTP {exc.code} {self._error_detail(exc)}".strip(),
                status=exc.code,
                retryable=exc.code in RETRYABLE_STATUS,
            ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            # URLError 把 socket 超时 / 连接拒绝都包在 reason 里，这里统一按网络抖动
            reason = getattr(exc, "reason", exc)
            raise _AttemptError(
                "network", f"{type(exc).__name__}: {_as_text(reason)}",
                retryable=True,
            ) from None
        except Exception as exc:                        # noqa: BLE001 — 传输层兜底
            raise _AttemptError(
                "transport", f"{type(exc).__name__}: {_as_text(exc)}",
                retryable=True,
            ) from None

        try:
            doc = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            # 200 但不是 JSON = 通常是网关插了一页 HTML，属于抖动
            raise _AttemptError("protocol", "response body is not valid JSON",
                                status=status, retryable=True) from None
        if not isinstance(doc, dict):
            raise _AttemptError("protocol",
                                f"response JSON root is {type(doc).__name__}, require object",
                                status=status, retryable=False)
        return doc

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read()
        except Exception:                               # noqa: BLE001
            raw = b""
        finally:
            try:
                exc.close()                             # 不 close 会漏文件描述符/临时文件
            except Exception:                           # noqa: BLE001
                pass
        return _as_text(raw)[:ERROR_BODY_CHARS].strip()

    def _timeout_s(self, e: ExecCtx) -> float:
        """单次请求超时 = min(timeout_ms, deadline 剩余)。"""
        timeout_s = max(0.05, self.timeout_ms / 1000.0)
        if e.deadline > 0:
            remaining = (e.deadline - now_ms()) / 1000.0
            if remaining > 0:
                timeout_s = min(timeout_s, remaining)
        return timeout_s

    # ── 协议转换：响应侧 ────────────────────────────────────────
    def _finish(self, doc: dict, model: str, prompt: str,
                tools_schema: list[dict]) -> LLMResponse:
        choices = doc.get("choices")
        if not isinstance(choices, list) or not choices:
            err = doc.get("error")
            detail = json.dumps(err, ensure_ascii=False)[:ERROR_BODY_CHARS] if err else ""
            raise self._fail(
                f"protocol error: no choices in response{': ' + detail if detail else ''}",
                kind="protocol", status=self.last_status, model=model,
            )
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}

        text = self._content_text(message.get("content"))
        calls, warnings = self._parse_tool_calls(message.get("tool_calls"))
        self.last_warnings = warnings
        if warnings:
            self.warnings.extend(warnings)

        usage = self._usage(doc.get("usage"), prompt, tools_schema, text, model)
        self._record_usage(usage)

        finish = first.get("finish_reason")
        if not isinstance(finish, str) or not finish:
            finish = "tool_calls" if calls else "stop"

        resp = LLMResponse(text=text, tool_calls=calls, usage=usage,
                           finish_reason=finish)
        # 契约里的 LLMResponse 没有 warnings 字段：挂一个非契约装饰属性，方便调用方
        # 就地看到警告，而不是非得回头查 adapter。失败也不影响主流程。
        try:
            object.__setattr__(resp, "warnings", tuple(warnings))
        except Exception:                               # noqa: BLE001
            pass
        return resp

    @staticmethod
    def _content_text(content: Any) -> str:
        """content 可能是 str / None / 多段 list（部分兼容实现会返回分段结构）。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif isinstance(part.get("content"), str):
                        parts.append(part["content"])
            return "".join(parts)
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            return content["text"]
        return str(content)

    def _parse_tool_calls(
        self, raw_calls: Any,
    ) -> tuple[tuple[NormalizedToolCall, ...], list[str]]:
        """归一 tool_calls。单个调用畸形 → 丢它 + 记 warning，绝不炸整轮。"""
        if not raw_calls:
            return (), []
        if isinstance(raw_calls, dict):
            raw_calls = [raw_calls]
        if not isinstance(raw_calls, list):
            return (), [f"tool_calls 不是数组（{type(raw_calls).__name__}），已全部丢弃"]

        calls: list[NormalizedToolCall] = []
        warnings: list[str] = []
        for idx, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                warnings.append(f"tool_calls[{idx}] 不是对象，已丢弃")
                continue
            fn = raw.get("function")
            if not isinstance(fn, dict):
                fn = {}
            call_id = str(raw.get("id") or f"call_{idx}")
            name = fn.get("name") or raw.get("name")
            if not isinstance(name, str) or not name.strip():
                warnings.append(
                    f"tool_calls[{idx}] (id={call_id}) 缺少函数名，已丢弃")
                continue
            name = name.strip()
            arguments = fn.get("arguments", raw.get("arguments"))
            try:
                args = self._decode_args(arguments)
            except _MalformedArguments as exc:
                warnings.append(
                    f"tool_calls[{idx}] (id={call_id}, {name}) 参数解析失败，已丢弃：{exc}")
                continue
            calls.append(NormalizedToolCall(id=call_id, name=name, args=args))
        return tuple(calls), warnings

    @staticmethod
    def _decode_args(arguments: Any) -> dict:
        """``arguments`` 是 JSON 字符串 → dict；解析不了就抛 _MalformedArguments。

        解包规则**刻意收得很紧**：只有 ``{"args": {...}}`` 且顶层长度为 1 才解包。

        原因：保守 schema（无 ``input_schema`` 的老工具）声明成 ``{"args": {...}}``，
        模型按那条 schema 调用时会带一层壳；而声明了 ``input_schema`` 的工具，模型
        返回的是**扁平参数**（``{"service": "api", "lines": 200}``）。若在这里无脑
        解包/重排，扁平参数里的字段就会被误伤 —— 那正是接手时最不该有的"聪明"。

        已知边界：某个工具若真的声明了一个顶层名为 ``args`` 的 object 参数，这条规则
        会把它解包。宁可留着这个窄口子（老形状兼容是硬需求），也不要为了它放宽条件
        而误伤所有扁平调用 —— 出错时 Dispatcher 的入参校验会明确报 ``args.args``。
        """
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            raw = arguments
        elif isinstance(arguments, str):
            try:
                raw = json.loads(arguments)
            except ValueError as exc:
                raise _MalformedArguments(str(exc)[:120]) from None
        else:
            raise _MalformedArguments(f"arguments 类型 {type(arguments).__name__} 不支持")
        if not isinstance(raw, dict):
            raise _MalformedArguments(
                f"arguments 解析出 {type(raw).__name__}，要求 object")
        if len(raw) == 1 and isinstance(raw.get("args"), dict):
            return raw["args"]
        return raw

    # ── 计量 ────────────────────────────────────────────────────
    def _usage(self, raw: Any, prompt: str, tools_schema: list[dict],
               text: str, model: str) -> Usage:
        input_tokens = output_tokens = cache_tokens = 0
        if isinstance(raw, dict):
            input_tokens = _as_int(raw.get("prompt_tokens") or raw.get("input_tokens"))
            output_tokens = _as_int(raw.get("completion_tokens") or raw.get("output_tokens"))
            cache_tokens = _as_int(
                raw.get("prompt_cache_hit_tokens")
                or raw.get("cached_tokens")
                or raw.get("cache_read_input_tokens"))

        if input_tokens or output_tokens:
            self.last_estimated = False
            return Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                         cache_hit_tokens=cache_tokens, model=model)

        # 协议没给 usage：按 builder 的口径估算，并明确标记「这是估的」
        self.last_estimated = True
        self._estimated_calls += 1
        tools_text = json.dumps(tools_schema, ensure_ascii=False) if tools_schema else ""
        return Usage(
            input_tokens=estimate_tokens(prompt) + estimate_tokens(tools_text),
            output_tokens=estimate_tokens(text),
            cache_hit_tokens=0,
            model=model,
        )

    def cost_of(self, input_tokens: int, output_tokens: int) -> float:
        """USD。价格按「每百万 token」给，所以除 1e6。"""
        return (input_tokens * self.price_in_per_m
                + output_tokens * self.price_out_per_m) / 1_000_000.0

    def _record_usage(self, usage: Usage) -> None:
        cost = self.cost_of(usage.input_tokens, usage.output_tokens)
        self.last_usage = usage
        self.last_cost_usd = cost
        self._prompt_tokens += usage.input_tokens
        self._completion_tokens += usage.output_tokens
        self._cost_usd += cost

    # ── 守卫 / 退避 / 脱敏 ──────────────────────────────────────
    def _log(self, record: dict) -> None:
        self.history.append(record)
        if len(self.history) > self.HISTORY_LIMIT:
            del self.history[: len(self.history) - self.HISTORY_LIMIT]

    def _guard(self, e: ExecCtx) -> None:
        if getattr(e, "aborted", False):
            raise self._fail("aborted by caller", kind="aborted")
        if e.deadline_passed():
            raise self._fail("deadline exceeded", kind="deadline")

    def _sleep(self, delay: float, e: ExecCtx) -> None:
        """注入的 sleeper + deadline 双重约束：不在「睡着的时候」越过 deadline。"""
        self._guard(e)
        if e.deadline > 0:
            remaining = (e.deadline - now_ms()) / 1000.0
            if remaining <= 0:
                raise self._fail("deadline exceeded", kind="deadline")
            delay = min(delay, remaining)
        self.sleeper(delay)
        self._guard(e)

    def _scrub(self, text: Any) -> str:
        out = _as_text(text)
        if self.api_key:
            out = out.replace(self.api_key, "***")
        out = _BEARER_RE.sub("Bearer ***", out)
        out = _KEYLIKE_RE.sub("***", out)
        return out

    def _fail(self, message: str, *, kind: str = "unknown", status: int = 0,
              model: str = "", retryable: bool = False,
              attempts: int = 0) -> LLMError:
        self._errors += 1
        return LLMError(self._scrub(message), kind=kind, status=status,
                        model=model or self.model, retryable=retryable,
                        attempts=attempts)

    # ── 观测 ────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {
            "model": self.model,
            "fallback_models": list(self.fallback_models),
            "calls": self._calls,
            "requests": self._requests,
            "retries": self._retries,
            "fallback_switches": self._fallback_switches,
            "errors": self._errors,
            "warnings": len(self.warnings),
            "estimated_calls": self._estimated_calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._prompt_tokens + self._completion_tokens,
            "cost_usd": self._cost_usd,
            "history": list(self.history),
        }


# ══════════════════════════════════════════════════════════════
# 工厂：按环境变量装配（缺关键变量返回 None，调用方据此跳过真实模型用例）
# ══════════════════════════════════════════════════════════════
def from_env(prefix: str = "OPENAI") -> OpenAICompatAdapter | None:
    """读 ``{PREFIX}_BASE_URL`` / ``{PREFIX}_API_KEY`` / ``{PREFIX}_MODEL``。

    * ``BASE_URL`` 与 ``MODEL`` 必填；``API_KEY`` 允许为空（本地 Ollama / vLLM 常常不校验）。
    * 可选：``TIMEOUT_MS`` / ``FALLBACK_MODELS``（逗号分隔）/ ``TEMPERATURE`` /
      ``PRICE_IN_PER_M`` / ``PRICE_OUT_PER_M``。
    * 缺关键变量或值不可用 → 返回 ``None``，**不抛错**：真实模型测试应当 skip 而不是红。
    """
    head = (prefix or "OPENAI").strip().upper()

    def read(name: str) -> str:
        return (os.environ.get(f"{head}_{name}") or "").strip()

    base_url = read("BASE_URL")
    model = read("MODEL")
    if not base_url or not model:
        return None

    kwargs: dict = {}
    raw_timeout = read("TIMEOUT_MS")
    if raw_timeout.isdigit():
        kwargs["timeout_ms"] = int(raw_timeout)
    fallback = tuple(x.strip() for x in read("FALLBACK_MODELS").split(",") if x.strip())
    if fallback:
        kwargs["fallback_models"] = fallback
    for env_name, attr in (("TEMPERATURE", "temperature"),
                           ("PRICE_IN_PER_M", "price_in_per_m"),
                           ("PRICE_OUT_PER_M", "price_out_per_m")):
        raw = read(env_name)
        if not raw:
            continue
        try:
            kwargs[attr] = float(raw)
        except ValueError:
            continue
    try:
        return OpenAICompatAdapter(base_url, model, read("API_KEY"), **kwargs)
    except (TypeError, ValueError):
        return None


__all__ = [
    "LLMError",
    "OpenAICompatAdapter",
    "RETRYABLE_STATUS",
    "FATAL_STATUS",
    "RETRY_BASE_S",
    "RETRY_CAP_S",
    "TOOL_PARAMETERS",
    "classify_status",
    "estimate_tokens",
    "from_env",
]
