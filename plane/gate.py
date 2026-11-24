"""plane/gate.py — W15：把 Governance Plane 接进**全链路**（组合，不是继承）。

════════════════════════════════════════════════════════════════════════
问题：护栏有缝。原先 Governance 只在 Gateway 层用（demo 里），于是：

    Tool 回包 ──(没过滤)──> Context         工具日志里带密钥 / 带间接注入指令
    Subagent 输出 ──(没过滤)──> 聚合结果    W9 主动留的口子

════════════════════════════════════════════════════════════════════════
立场：**接缝必须补在你控制得住的那一侧，而不是改动已经稳定的组件。**
════════════════════════════════════════════════════════════════════════

`plane/tool.py`（35 个用例）与 `plane/workflow.py`（36 个用例）已经稳定，
它们是「唯一的副作用出口」和「多 Agent 编排内核」。往里面加治理钩子，等于：

    - 给一个已经被 35+36 个用例钉死的执行链加分支 → 回归风险；
    - 让 Tool Plane / Workflow Plane **依赖** Governance 的构造参数 →
      两个模块从此不能独立测试（BRIEF 的硬约束 7：只依赖契约里的 Protocol）；
    - 治理策略变更（企业加一条 token 规则）要重新编译调度器。

所以这里用**装饰器式组合**：

    GuardedDispatcher(inner)   满足 Dispatcher 的形状：invoke / registry / manifests
    GuardedRunner(inner)       满足 SubagentRunner 的形状：dispatch / dispatch_many

上层（Loop / Builder / Orchestrator）拿到的对象形状不变，看到的却是「已经过
输出护栏」的结果。`tool.py` 与 `workflow.py` **一行没动**。

关键细节（每一条都对应一个真实的失效模式）：

1. **透传 `registry`**：`SubagentRunner._resolve_allowlist` 靠
   `dispatcher.registry` 做只读白名单判定。不透传 → 取不到 registry →
   fail-closed → **所有子 Agent 全被拒**。治理壳不能顺手打死编排能力。
2. **fail-closed**：护栏自己抛异常 → `ToolOutcome.denied("output guardrail: ...")`，
   不是把裸结果放行。治理面异常只允许往"更严"的方向退化。
3. **非 ok 原样透传**：`needs_confirmation` / `failed` / `denied` / `timeout`
   已经由 Tool Plane 定性，护栏不许改写它们的语义（把 `needs_confirmation`
   改成 `denied`，等于替用户把审批按钮砸了）。
4. **脱敏计数并入 `ToolResult.redacted`**，`truncated` / `deduped` /
   `bytes_in` 等字段原样保留 —— 治理只改内容，不改账本。

--------------------------------------------------------------------------------
payload 的两种处置（`wrap_untrusted`）
--------------------------------------------------------------------------------

`wrap_untrusted=True`（默认）：payload 统一收口到**文本平面**再包裹。
    工具回包进 Prompt 的最终形态就是文本，所以用 `<external_data_untrusted>`
    把整段回包包住，是确定性的注入边界。结构型 payload 会被序列化成 JSON
    文本（`json_text`）后包裹。
`wrap_untrusted=False`：只脱敏不包裹，结构型 payload **保留结构**（递归对每个
    字符串叶子做凭据掩码 / 外联阻断）。适合需要继续按 key 读 payload 的调用方；
    代价是失去了结构化注入边界。

--------------------------------------------------------------------------------
明确不做的
--------------------------------------------------------------------------------

- 不改 `plane/tool.py` / `plane/workflow.py`（组合，不继承，不打补丁）。
- 不改 `contracts.py`：`PolicyPack` 是契约里已有的 dataclass。
- 不在这里做 RBAC / 配额：那是调用前的闸，属于 Gateway / Loop 侧，
  本模块只管**回包出门**这一侧。
"""
from __future__ import annotations

import inspect
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

# 允许在任意工作目录下 import（与 plane/tool.py 同样的处理）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import (  # noqa: E402
    ExecCtx,
    GuardVerdict,
    PolicyPack,
    SubagentResult,
    SubagentSpec,
    ToolOutcome,
    ToolResult,
)
# 词汇表随模块再导出：调用方断言「包裹后长什么样」「掩码长什么样」时，
# 不该再去猜字面量。
from plane.governance import (  # noqa: E402
    BLOCKED_URL_LABEL,
    REDACTED,
    UNTRUSTED_TAG,
    InputGuard,
)

__all__ = [
    "GuardedDispatcher",
    "GuardedRunner",
    "GuardFailure",
    "DEFAULT_POLICY_PACK",
    "ENTERPRISE_EXAMPLE",
    "TOOL_SOURCE_PREFIX",
    "sanitize_value",
    # 词汇表（从 governance 再导出）
    "REDACTED", "BLOCKED_URL_LABEL", "UNTRUSTED_TAG",
]


# ══════════════════════════════════════════════════════════════
# 默认 PolicyPack
# ══════════════════════════════════════════════════════════════
#: 空包 = 完全用 governance 的内置清单（协议事实：厂商公开的凭据格式）。
DEFAULT_POLICY_PACK = PolicyPack(name="default")

#: 企业扩展示例：私有 token 前缀**追加**，内置清单仍然生效。
ENTERPRISE_EXAMPLE = PolicyPack(
    name="acme",
    credential_patterns=(("slack", r"xoxb-[A-Za-z0-9-]{10,}"),
                         ("private_jwt", r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
    notes="企业私有 token 前缀追加，内置清单仍然生效",
)

#: 工具回包的 `source` 标签前缀（审计时能追到是哪个工具说的话）
TOOL_SOURCE_PREFIX = "tool"


# ══════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════
class GuardFailure(Exception):
    """护栏判定不放行（或护栏自证不可用）→ 调用方转成 denied。

    单独一个异常类型，是为了把「护栏说不」和「护栏自己炸了」区分开：
    前者带 verdict 原因，后者带异常类型 —— 两种都要 deny，但审计读起来不一样。
    """

    def __init__(self, reason: str, *, hits: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hits = tuple(hits)


def _json_text(obj: Any) -> str:
    """稳定序列化（与 plane/tool.json_text 同语义，但本模块不依赖它）。"""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=False)
    except (TypeError, ValueError):
        return repr(obj)


#: 退化用的包裹实现（governance 的 InputGuard 没有 wrap_untrusted 时）
_FALLBACK_INPUT_GUARD = InputGuard()


def _output_scan(governance: Any, text: str,
                 allow_hosts: Sequence[str] = ()) -> GuardVerdict:
    """调治理面的输出闸。`governance` 可以是 Governance / OutputGuard / 鸭子类型。

    认不出形状 → 抛 TypeError（由调用方 fail-closed），绝不"没有护栏就当干净"。
    """
    if governance is None:
        raise TypeError("governance is None：没有输出护栏，不能放行工具回包")

    hook = getattr(governance, "before_output", None)
    if callable(hook):
        return hook(text, allow_hosts=allow_hosts)

    guard = getattr(governance, "output_guard", None)
    scan = getattr(guard, "scan", None)
    if callable(scan):
        return scan(text, allow_hosts=allow_hosts)

    scan = getattr(governance, "scan", None)
    if callable(scan):
        return scan(text)

    raise TypeError(
        f"governance 形状不可识别（{type(governance).__name__}）："
        "需要 before_output / output_guard.scan / scan 之一")


def _wrap_text(governance: Any, text: str, source: str) -> str:
    """把文本包成 `<external_data_untrusted source="...">`（契约格式，一字不差）。"""
    guard = getattr(governance, "input_guard", None)
    wrap = getattr(guard, "wrap_untrusted", None)
    if not callable(wrap):
        wrap = _FALLBACK_INPUT_GUARD.wrap_untrusted
    return wrap(text, source=source)


def _sanitize_leaf(text: str, governance: Any,
                   allow_hosts: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """单个字符串过输出护栏。不放行 → GuardFailure（fail-closed）。"""
    verdict = _output_scan(governance, text, allow_hosts)
    if not verdict.allowed:
        raise GuardFailure(
            f"输出护栏拒绝放行：{verdict.reason}", hits=verdict.hits)
    payload = verdict.payload
    return (payload if isinstance(payload, str) else text), tuple(verdict.hits)


def sanitize_value(
    node: Any,
    governance: Any,
    *,
    allow_hosts: Sequence[str] = (),
    wrap: Callable[[str], str] | None = None,
) -> tuple[Any, int, tuple[str, ...]]:
    """递归脱敏：只动字符串叶子，**结构（dict/list/tuple）原样保留**。

    返回 `(新节点, 命中规则数, 命中规则名)`。

    - `wrap` 给了就只作用于**整段顶层字符串**（包裹是文本边界，不是结构操作）；
    - 非字符串标量（int/float/bool/None）原样返回 —— 它们带不出凭据
      （凭据是文本形态），也无从注入。
    """
    seen: set[str] = set()
    hit_names: list[str] = []

    def _walk(value: Any, top: bool) -> Any:
        if isinstance(value, str):
            new_text, h = _sanitize_leaf(value, governance, allow_hosts)
            for name in h:
                if name not in seen:
                    seen.add(name)
                    hit_names.append(name)
            if top and wrap is not None:
                return wrap(new_text)
            return new_text
        if isinstance(value, dict):
            return {(_walk(k, False) if isinstance(k, str) else k): _walk(v, False)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v, False) for v in value]
        if isinstance(value, tuple):
            return tuple(_walk(v, False) for v in value)
        return value

    new_node = _walk(node, True)
    return new_node, len(hit_names), tuple(hit_names)


def _allow_hosts_of(governance: Any, extra: Sequence[str] = ()) -> tuple[str, ...]:
    """白名单来源：`PolicyPack.exfil_host_allowlist` + 构造时显式给的。"""
    out: list[str] = [str(h) for h in (extra or ()) if str(h).strip()]
    pack = getattr(governance, "policy_pack", None)
    for h in getattr(pack, "exfil_host_allowlist", ()) or ():
        s = str(h).strip()
        if s:
            out.append(s)
    seen: set[str] = set()
    uniq: list[str] = []
    for h in out:
        k = h.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return tuple(uniq)


# ══════════════════════════════════════════════════════════════
# ① 工具回包闸
# ══════════════════════════════════════════════════════════════
class GuardedDispatcher:
    """包住任意 Dispatcher，在**结果返回前**过输出护栏。

    形状与 `plane.tool.Dispatcher` 一致（invoke / registry / manifests），
    所以 `Loop`、`SubagentRunner._GuardedDispatcher`、demo 都不需要知道自己
    拿到的是真的 Dispatcher 还是一个治理壳。
    """

    def __init__(self, inner: Any, governance: Any, *,
                 wrap_untrusted: bool = True,
                 allow_hosts: Sequence[str] = ()) -> None:
        self.inner = inner
        self.governance = governance
        self.wrap_untrusted = bool(wrap_untrusted)
        self.allow_hosts = _allow_hosts_of(governance, allow_hosts)
        #: 每次治理处置的审计记录（供 Trace / 度量读取）
        self.guard_log: list[dict] = []
        #: 被护栏拒绝的调用（fail-closed 或输出不可放行）
        self.denied_log: list[dict] = []

    # ── 形状透传（Runner 靠它做白名单判定）──────────────────
    @property
    def registry(self) -> Any:
        """**必须透传**：`SubagentRunner._resolve_allowlist` 靠它枚举只读工具。

        返回 None 时下游会 fail-closed 拒绝派发 —— 所以这里如实返回
        `inner.registry`（拿不到就是 None，让下游按不可验证处理）。
        """
        return getattr(self.inner, "registry", None)

    def manifests(self) -> list[Any]:
        """工具清单：优先用 inner 的 manifests()，退化到 registry.manifests()。"""
        manifests = getattr(self.inner, "manifests", None)
        if callable(manifests):
            return list(manifests())
        if manifests is not None:                 # 属性形态（_GuardedDispatcher）
            try:
                return list(manifests)
            except TypeError:
                pass
        registry = self.registry
        getter = getattr(registry, "manifests", None)
        if callable(getter):
            return list(getter())
        return []

    def __getattr__(self, item: str) -> Any:
        """其余属性（stats / artifacts / inner 的日志…）原样转发。

        只在实例自身找不到时触发；`registry` / `manifests` 是显式定义的，
        不会被这里劫持。
        """
        inner = object.__getattribute__(self, "inner")
        try:
            return getattr(inner, item)
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__} 与内层 {type(inner).__name__} 都没有属性 {item!r}"
            ) from None

    # ── 对外入口 ────────────────────────────────────────────
    def invoke(self, name: str, args: dict, e: ExecCtx) -> ToolOutcome:
        # 内层抛异常不在本模块兜：Tool Plane 的契约是"永不向上抛"，真抛了说明
        # 内层违约，由上层（Loop 的 try/except 收口）决定，不在这里假装成治理结论。
        outcome = self.inner.invoke(name, args, e)

        if not isinstance(outcome, ToolOutcome):
            # 非契约返回：内层已经坏了，不要在这里替它编一个结果
            return outcome
        if outcome.kind != "ok":
            # denied / needs_confirmation / failed / timeout：语义由 Tool Plane 定，
            # 护栏不改写（改了就替用户把审批按钮砸了）
            return outcome

        try:
            return self._guard_ok(name, outcome)
        except GuardFailure as exc:
            reason = f"output guardrail: {exc.reason}"
            self.denied_log.append({"tool": name, "reason": reason,
                                    "hits": list(exc.hits),
                                    "turn_id": getattr(e, "turn_id", "")})
            return ToolOutcome.denied(reason)
        except Exception as exc:      # fail-closed：护栏自己炸了 → 不放行
            reason = f"output guardrail: {type(exc).__name__}: {exc}"
            self.denied_log.append({"tool": name, "reason": reason,
                                    "hits": [], "turn_id": getattr(e, "turn_id", "")})
            return ToolOutcome.denied(reason)

    # ── 核心：ok 结果的治理 ──────────────────────────────────
    def _guard_ok(self, name: str, outcome: ToolOutcome) -> ToolOutcome:
        result = outcome.result
        if not isinstance(result, ToolResult):
            # ok 却没有 result：没有内容可治理，也只能按契约透传
            return outcome

        payload, redactions, hits = self._sanitize_payload(name, result.payload)
        new_bytes = len(_json_text(payload).encode("utf-8"))
        new_result = replace(
            result,
            payload=payload,
            bytes_out=new_bytes,
            # 脱敏是累加的：Normalizer 已经遮掉的不抹掉
            redacted=int(getattr(result, "redacted", 0) or 0) + redactions,
        )
        self.guard_log.append({
            "tool": name,
            "hits": list(hits),
            "redacted": redactions,
            "wrapped": bool(self.wrap_untrusted),
            "payload_type": type(payload).__name__,
        })
        return ToolOutcome.ok(new_result)

    def _sanitize_payload(self, name: str, payload: Any) -> tuple[Any, int, tuple[str, ...]]:
        source = f"{TOOL_SOURCE_PREFIX}:{name}"
        if isinstance(payload, str):
            # 文本面：脱敏 → （可选）包裹
            def _wrap(text: str) -> str:
                return _wrap_text(self.governance, text, source)

            return sanitize_value(payload, self.governance,
                                  allow_hosts=self.allow_hosts,
                                  wrap=_wrap if self.wrap_untrusted else None)

        # 结构型 payload：默认收口到文本平面再包裹（注入边界要覆盖 JSON 里的字符串）；
        # wrap_untrusted=False 时保留结构，只做递归脱敏。
        if self.wrap_untrusted:
            text = _json_text(payload)
            new_text, redactions, hits = sanitize_value(
                text, self.governance, allow_hosts=self.allow_hosts)
            wrapped = _wrap_text(self.governance, new_text, source)
            return wrapped, redactions, hits
        return sanitize_value(payload, self.governance,
                              allow_hosts=self.allow_hosts)


# ══════════════════════════════════════════════════════════════
# ② 子 Agent 产出闸
# ══════════════════════════════════════════════════════════════
class GuardedRunner:
    """包住 `SubagentRunner`，对子 Agent 的产出过输出护栏。

    组合而不继承：`SubagentRunner` 内部会把自己的 `_GuardedDispatcher` 套在
    我们传进去的 dispatcher 上，所以「工具回包」和「子 Agent 汇总」两道闸
    天然串成一条链，而 workflow.py 不需要知道 Governance 的存在。
    """

    def __init__(self, inner: Any, governance: Any,
                 allow_hosts: Sequence[str] = ()) -> None:
        self.inner = inner
        self.governance = governance
        self.allow_hosts = _allow_hosts_of(governance, allow_hosts)
        #: 子 Agent 输出的治理记录
        self.guard_log: list[dict] = []

    # ── 透传 ────────────────────────────────────────────────
    def __getattr__(self, item: str) -> Any:
        inner = object.__getattribute__(self, "inner")
        try:
            return getattr(inner, item)
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__} 与内层 {type(inner).__name__} 都没有属性 {item!r}"
            ) from None

    # ── 对外入口 ────────────────────────────────────────────
    def dispatch(self, spec: SubagentSpec, parent_ctx: Any,
                 parent_e: ExecCtx) -> SubagentResult:
        result = self.inner.dispatch(spec, parent_ctx, parent_e)
        return self._guard(result, fallback_name=str(getattr(spec, "name", "") or ""))

    def dispatch_many(self, specs: Sequence[SubagentSpec], parent_ctx: Any,
                      parent_e: ExecCtx, max_workers: int = 1
                      ) -> list[SubagentResult]:
        spec_list = list(specs)          # 允许传生成器：先落成序列，保持输入顺序
        raw = self._call_inner_many(spec_list, parent_ctx, parent_e, max_workers)
        out: list[SubagentResult] = []
        for idx, item in enumerate(raw or []):
            spec = spec_list[idx] if idx < len(spec_list) else None
            out.append(self._guard(item, fallback_name=str(getattr(spec, "name", "") or "")))
        return out

    # ── 内层签名适配 ────────────────────────────────────────
    def _call_inner_many(self, specs, parent_ctx, parent_e, max_workers: int):
        many = getattr(self.inner, "dispatch_many", None)
        if not callable(many):
            # 内层只有 dispatch：逐个派发，保持输入顺序
            return [self.inner.dispatch(s, parent_ctx, parent_e) for s in specs]
        try:
            params = inspect.signature(many).parameters
        except (TypeError, ValueError):
            params = {}
        if "max_workers" in params:
            return many(specs, parent_ctx, parent_e, max_workers=max_workers)
        return many(specs, parent_ctx, parent_e)

    # ── 核心：子 Agent 结果的治理 ───────────────────────────
    def _guard(self, result: Any, fallback_name: str = "") -> SubagentResult:
        try:
            if not isinstance(result, SubagentResult):
                return SubagentResult(
                    name=fallback_name, ok=False,
                    error=("output guardrail: 内层返回了非 SubagentResult"
                           f"（{type(result).__name__}），不可信、不回写"),
                    term_reason="ABORTED")
            if not result.ok:
                # 失败结果原样透传：护栏不追加失败、不改写既有错误。
                # 已知残留：ok=False 的 summary/error 不脱敏（spec 要求"原样透传"）。
                # 子 Agent 失败文本仍可能带密钥 —— 真正要堵这条，需要在
                # `SubagentResult` 里区分「模型产出」和「编排器诊断」，那是契约变更，
                # 不在本 worker 的改动范围内。这里如实记录，不假装已封。
                return result

            summary = result.summary if isinstance(result.summary, str) else ""
            new_summary, s_hits = summary, ()
            if summary:
                new_summary, s_hits = self._scan_text(summary)

            new_artifacts, art_hits = self._sanitize_artifacts(result.artifacts)
            summary_changed = new_summary != summary
            artifacts_changed = new_artifacts is not result.artifacts
            if not summary_changed and not artifacts_changed:
                return result
            self.guard_log.append({
                "agent": result.name,
                "hits": list(dict.fromkeys(list(s_hits) + list(art_hits))),
                "summary_changed": summary_changed,
                "artifacts_changed": artifacts_changed,
            })
            return replace(result, summary=new_summary, artifacts=new_artifacts)
        except GuardFailure as exc:
            return self._abort(result, f"output guardrail: {exc.reason}", fallback_name)
        except Exception as exc:      # fail-closed：护栏炸了，子 Agent 产出不放行
            return self._abort(result, f"output guardrail: {type(exc).__name__}: {exc}",
                               fallback_name)

    def _scan_text(self, text: str) -> tuple[str, tuple[str, ...]]:
        verdict = _output_scan(self.governance, text, self.allow_hosts)
        if not verdict.allowed:
            raise GuardFailure(f"输出护栏拒绝放行：{verdict.reason}", hits=verdict.hits)
        payload = verdict.payload
        return (payload if isinstance(payload, str) else text), tuple(verdict.hits)

    def _sanitize_artifacts(self, artifacts: Any) -> tuple[Any, tuple[str, ...]]:
        """artifacts 也会被回写进父上下文，所以字符串叶子同样要脱敏。"""
        if not artifacts:
            return artifacts, ()
        new_node, _n, hits = sanitize_value(
            artifacts, self.governance, allow_hosts=self.allow_hosts)
        if new_node == artifacts:
            return artifacts, ()
        return new_node, hits

    @staticmethod
    def _abort(result: Any, reason: str, fallback_name: str) -> SubagentResult:
        if isinstance(result, SubagentResult):
            return replace(result, ok=False, error=reason, term_reason="ABORTED")
        return SubagentResult(name=fallback_name, ok=False, error=reason,
                              term_reason="ABORTED")
