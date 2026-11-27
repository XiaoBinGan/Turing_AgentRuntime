"""plane/quality.py — Quality Plane：Trace（可观测）/ Eval（评测）/ Replay（回放）。

================================================================================
核心洞察：Context 的 patch log 就是执行轨迹
================================================================================

Agent Runtime 里 Context 只有一种变化方式——apply 一条 Patch
（set / append / incr / merge）。所以 **patch_log 就是执行轨迹本身**，
不需要第二套埋点、不需要第二份事件流：

    patch_log ──┬─> Trace      运行时埋点（树状 span + 成本归因）
                │              回答：慢在哪 / 贵在哪 / 谁花的钱
                ├─> Trajectory 把 patch 序列投影成 step 序列
                │              回答：这一趟到底走了哪些步、有没有绕路
                └─> Replay     initial_data + patch 序列逐条重放
                               回答：当时模型/工具到底看到了什么状态

同一个数据结构支撑三件事，因此三者的口径天然一致：
**判「做对了」用的证据，和判「贵了」用的证据，是同一批 patch。**
这也解释了为什么 Evaluator 不需要看「最终文本」——文本匹配只能测「说得像」，
patch 序列才能测「做对」，终态 data 才能测「结果对」。

设计约束：
1. 只依赖 contracts.py（Patch / Usage / Result / now_ms），**不 import core/***。
   Quality Plane 对实现零耦合：谁写 patch_log，我就观测/评测/回放谁。
2. **fail-open（INV-9）**：观测层绝不影响业务层。span 内抛的异常必须原样传播，
   但 Tracer 自己记录失败时静默吞掉——监控挂了不能把 Agent 带挂。
   注意这与 Replayer 相反：重放**必须**严格报错，因为静默重放会造出假等价。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from contracts import Budget, Patch, Result, Usage, now_ms

__all__ = [
    "PRICE_TABLE",
    "Span",
    "Tracer",
    "Trajectory",
    "EvalCase",
    "EvalReport",
    "Evaluator",
    "Replayer",
]


# ══════════════════════════════════════════════════════════════
# 价目表（mock，USD / 1M tokens）
# ══════════════════════════════════════════════════════════════
PRICE_TABLE: dict[str, dict[str, float]] = {
    "mock-1": {"input": 0.5 / 1_000_000, "output": 1.5 / 1_000_000},
    "mock-cheap": {"input": 0.1 / 1_000_000, "output": 0.3 / 1_000_000},
}
# 未知模型不猜价格，按默认档计，保证成本永远算得出来（fail-open）
DEFAULT_PRICE: dict[str, float] = PRICE_TABLE["mock-1"]


def _price_for(model: str | None) -> dict[str, float]:
    return PRICE_TABLE.get(str(model or ""), DEFAULT_PRICE)


def _usd_for(model: str | None, input_tokens: int, output_tokens: int) -> float:
    p = _price_for(model)
    return input_tokens * p["input"] + output_tokens * p["output"]


# ══════════════════════════════════════════════════════════════
# Span / Tracer：树状埋点 + 三维成本归因（INV-17）
# ══════════════════════════════════════════════════════════════
@dataclass
class Span:
    """一个执行区间。children 构成树，duration_ms 自动算。"""

    name: str
    attrs: dict = field(default_factory=dict)
    start_ms: float = 0.0
    end_ms: float = 0.0
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return round(self.end_ms - self.start_ms, 6)

    def walk(self) -> Iterator["Span"]:
        """先序遍历自身与所有后代（finish() 收尾/统计用）。"""
        yield self
        for c in self.children:
            yield from c.walk()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "attrs": dict(self.attrs),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class _UsageEntry:
    """一次 LLM 调用的成本快照，保留原始三维标签便于任意重聚合。"""

    model: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    usd: float
    tenant: str
    user_id: str
    task_id: str

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "usd": round(self.usd, 9),
            "tenant": self.tenant,
            "user_id": self.user_id,
            "task_id": self.task_id,
        }


class Tracer:
    """树状 span 收集器 + 成本归因。

    用法：
        t = Tracer(tenant="acme", user_id="u1", task_id="t1")
        with t.span("turn", {"phase": "EXECUTING"}):
            with t.span("llm"):
                t.record_usage("mock-1", resp.usage)
            with t.span("tool", {"name": "search"}):
                ...
        print(t.tree()); report = t.finish()

    线程模型：单 trace 实例不跨线程共享（栈是实例状态）。多线程各开 Tracer，
    最后按 trace_id 合并即可——成本走的是 usage 列表，不是栈。
    """

    def __init__(self, tenant: str = "", user_id: str = "", task_id: str = "") -> None:
        self.tenant = tenant
        self.user_id = user_id
        self.task_id = task_id
        self.trace_id = uuid.uuid4().hex[:16]
        self._start_ms = now_ms()
        self._roots: list[Span] = []
        self._stack: list[Span] = []
        self._usages: list[_UsageEntry] = []

    # ── span 栈 ──────────────────────────────────────────────
    @contextlib.contextmanager
    def span(self, name: str, attrs: dict | None = None):
        """可嵌套的 span。异常原样传播；记录失败静默（fail-open）。"""
        sp: Span | None = None
        # 入栈本身也可能炸（比如 attrs 的 dict() 拷贝触发自定义 __iter__）
        try:
            sp = Span(name=str(name), attrs=dict(attrs or {}), start_ms=now_ms())
            (self._stack[-1].children if self._stack else self._roots).append(sp)
            self._stack.append(sp)
        except Exception:
            sp = None  # 埋点失败 → 退化成 no-op 的 with，业务继续跑

        try:
            yield sp
        finally:
            # 这里必须吞掉一切异常：finally 里抛错会顶掉业务异常
            try:
                if sp is not None:
                    sp.end_ms = now_ms()
                    if self._stack and self._stack[-1] is sp:
                        self._stack.pop()
                    else:  # 栈被异常打乱：兜底移除，别让后续 span 挂错父节点
                        for i in range(len(self._stack) - 1, -1, -1):
                            if self._stack[i] is sp:
                                del self._stack[i]
                                break
            except Exception:
                pass

    @contextlib.contextmanager
    def scope(self, tenant: str | None = None, user_id: str | None = None,
              task_id: str | None = None):
        """临时切换成本归因维度（子 Agent / 多租户扇出时用）。"""
        saved = (self.tenant, self.user_id, self.task_id)
        try:
            if tenant is not None:
                self.tenant = tenant
            if user_id is not None:
                self.user_id = user_id
            if task_id is not None:
                self.task_id = task_id
        except Exception:
            pass
        try:
            yield self
        finally:
            try:
                self.tenant, self.user_id, self.task_id = saved
            except Exception:
                pass

    # ── 成本归因 ─────────────────────────────────────────────
    def record_usage(self, model: str, usage: Usage, *,
                     tenant: str | None = None, user_id: str | None = None,
                     task_id: str | None = None) -> dict:
        """记一次 LLM 消耗并结算 USD。

        三维标签（tenant / user / task）默认取 Tracer 当前上下文，
        也可显式传参或包在 `with tracer.scope(...)` 里——三种都行，
        这样同一棵 span 树能按三个维度分别聚合（INV-17 验收点）。
        """
        try:
            entry = _UsageEntry(
                model=str(model or ""),
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_hit_tokens=int(getattr(usage, "cache_hit_tokens", 0) or 0),
                usd=0.0,
                tenant=tenant if tenant is not None else self.tenant,
                user_id=user_id if user_id is not None else self.user_id,
                task_id=task_id if task_id is not None else self.task_id,
            )
            entry.usd = _usd_for(entry.model, entry.input_tokens, entry.output_tokens)
            self._usages.append(entry)
            return entry.to_dict()
        except Exception:
            # 归因失败不得中断执行；返回零值占位
            return {"model": str(model or ""), "input_tokens": 0, "output_tokens": 0,
                    "cache_hit_tokens": 0, "usd": 0.0, "tenant": "", "user_id": "",
                    "task_id": ""}

    def _totals(self, end_ms: float) -> dict:
        by_tenant: dict[str, float] = {}
        by_user: dict[str, float] = {}
        by_task: dict[str, float] = {}
        by_model: dict[str, dict] = {}
        t_in = t_out = t_cache = 0
        usd = 0.0
        for e in self._usages:
            usd += e.usd
            t_in += e.input_tokens
            t_out += e.output_tokens
            t_cache += e.cache_hit_tokens
            by_tenant[e.tenant] = by_tenant.get(e.tenant, 0.0) + e.usd
            by_user[e.user_id] = by_user.get(e.user_id, 0.0) + e.usd
            by_task[e.task_id] = by_task.get(e.task_id, 0.0) + e.usd
            m = by_model.setdefault(e.model, {"usd": 0.0, "input": 0, "output": 0})
            m["usd"] += e.usd
            m["input"] += e.input_tokens
            m["output"] += e.output_tokens
        return {
            "duration_ms": round(end_ms - self._start_ms, 6),
            "cost": {
                "usd": round(usd, 9),
                "by_tenant": {k: round(v, 9) for k, v in by_tenant.items()},
                "by_user": {k: round(v, 9) for k, v in by_user.items()},
                "by_task": {k: round(v, 9) for k, v in by_task.items()},
                "by_model": {k: {"usd": round(v["usd"], 9), "input": v["input"],
                                 "output": v["output"]} for k, v in by_model.items()},
            },
            "tokens": {"input": t_in, "output": t_out, "cache_hit": t_cache},
            "llm_calls": len(self._usages),
            "span_count": sum(1 for r in self._roots for _ in r.walk()),
        }

    def finish(self) -> dict:
        """收尾快照：树 + totals。绝不抛错（INV-9）。"""
        try:
            end = now_ms()
            roots = list(self._roots)
            stack = list(self._stack)
        except Exception:
            return {"trace_id": "", "spans": [], "totals": {}, "meta": {}}
        # 没收尾的 span（正常不会发生）就地闭环，避免 duration 为 0
        for sp in stack:
            try:
                if not sp.end_ms:
                    sp.end_ms = end
            except Exception:
                pass
        try:
            return {
                "trace_id": self.trace_id,
                "spans": [r.to_dict() for r in roots],
                "totals": self._totals(end),
                "meta": {"tenant": self.tenant, "user_id": self.user_id,
                         "task_id": self.task_id},
            }
        except Exception:
            return {"trace_id": self.trace_id, "spans": [], "totals": {}, "meta": {}}

    # ── 人读视图（demo 打印用）───────────────────────────────
    def tree(self) -> str:
        roots = self._roots
        if not roots:
            return "(empty trace)"
        lines: list[str] = []

        def label(sp: Span) -> str:
            txt = f"{sp.name} [{_fmt_ms(sp.duration_ms)}]"
            if sp.attrs:
                kv = " ".join(f"{k}={_short(v)}" for k, v in sp.attrs.items())
                txt += f" {{{kv}}}"
            return txt

        def walk(sp: Span, prefix: str, is_last: bool, top: bool) -> None:
            lines.append(prefix + ("" if top else ("└── " if is_last else "├── "))
                         + label(sp))
            child_prefix = prefix if top else prefix + ("    " if is_last else "│   ")
            for i, c in enumerate(sp.children):
                walk(c, child_prefix, i == len(sp.children) - 1, False)

        for i, r in enumerate(roots):
            walk(r, "", i == len(roots) - 1, True)
            if i != len(roots) - 1:
                lines.append("")
        return "\n".join(lines)


def _fmt_ms(ms: float) -> str:
    """亚毫秒用 µs 显示，避免 demo 里全是 0.00ms。"""
    return f"{ms:.2f}ms" if ms >= 1.0 else f"{ms * 1000:.1f}µs"


def _short(v: Any, limit: int = 32) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ══════════════════════════════════════════════════════════════
# Trajectory：patch log → 轨迹
# ══════════════════════════════════════════════════════════════
def _as_items(value: Any) -> list:
    """把一条 patch 的 value 规整成 item 列表（单独 dict / 列表都接受）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _parse_tool_item(item: Any) -> tuple[str, dict] | None:
    """从 tool_results 的一个元素里抠出 (tool_name, args)。

    兼容几种真实写法：
      {"tool": "search", "payload": {...}}       ← ToolResult.to_dict()
      {"tool": "search", "args": {...}}          ← loop 层自记
      {"name": "search", "arguments": {...}}     ← 原始 tool_call
      ToolResult(tool="search", ...)             ← dataclass 直存
      "search"                                   ← 极简记法
    """
    if item is None:
        return None
    if isinstance(item, str):
        return (item, {}) if item else None
    if isinstance(item, dict):
        name = item.get("tool") or item.get("name") or item.get("tool_name") or ""
        args = item.get("args")
        if args is None:
            args = item.get("arguments")
        if args is None:
            args = item.get("payload")
        return (str(name), args if isinstance(args, dict) else {}) if name else None
    # 鸭子类型：ToolResult / NormalizedToolCall
    name = getattr(item, "tool", None) or getattr(item, "name", None)
    if not name:
        return None
    args = getattr(item, "args", None)
    if args is None:
        args = getattr(item, "arguments", None)
    if not isinstance(args, dict):
        payload = getattr(item, "payload", None)
        args = payload if isinstance(payload, dict) else {}
    return (str(name), args)


def _tool_records(patches: Sequence[Patch]) -> list[tuple[str, dict]]:
    """抽出 patch log 里的全部工具调用记录（顺序保持）。"""
    out: list[tuple[str, dict]] = []
    for p in patches:
        try:
            if getattr(p, "path", None) != "tool_results":
                continue
            for item in _as_items(getattr(p, "value", None)):
                rec = _parse_tool_item(item)
                if rec and rec[0]:
                    out.append(rec)
        except Exception:
            continue
    return out


class Trajectory:
    """一条 patch log 的只读投影。

    注意 steps 的 index 是 patch 在 log 中的**序号**（0 基），不是工具调用序号：
    一个 step 可能是 plan 变更、usage 累加，不只是工具调用。这样评测时
    「绕了几步」和「patch 了多少次」可以分开算。
    """

    def __init__(self, patches: Sequence[Patch]) -> None:
        self._patches: tuple[Patch, ...] = tuple(patches)
        self._steps: list[dict] = []
        for i, p in enumerate(self._patches):
            try:
                self._steps.append({
                    "index": i,
                    "op": str(getattr(p, "op", "")),
                    "path": str(getattr(p, "path", "")),
                    "by": getattr(getattr(p, "by", None), "value", str(getattr(p, "by", ""))),
                    "value": getattr(p, "value", None),
                })
            except Exception:
                continue
        self._fingerprint: str | None = None

    @staticmethod
    def from_patch_log(patches: Sequence[Patch]) -> "Trajectory":
        return Trajectory(patches)

    @property
    def patches(self) -> tuple[Patch, ...]:
        return self._patches

    @property
    def steps(self) -> list[dict]:
        return [dict(s) for s in self._steps]  # 防外部改内部状态

    def tool_sequence(self) -> list[str]:
        """path == "tool_results" 的记录里出现的工具名序列。"""
        return [name for name, _ in _tool_records(self._patches)]

    def tool_calls(self) -> list[dict]:
        """带参数的版本，供 arg_precision 校验用。"""
        return [{"tool": n, "args": a} for n, a in _tool_records(self._patches)]

    def loop_count(self) -> int:
        """从 patch log 还原循环次数：incr 累加，set 取历史最大值。"""
        total = 0
        for p in self._patches:
            try:
                if getattr(p, "path", None) != "loop_count":
                    continue
                op = str(getattr(p, "op", ""))
                raw = getattr(p, "value", None)
                if op == "incr":
                    total += int(raw or 1)
                elif op == "set":
                    total = max(total, int(raw or 0))
            except Exception:
                continue
        return total

    @property
    def fingerprint(self) -> str:
        """轨迹指纹：对 (op, path, by, value) 序列做规范化哈希。

        用于跨 run 比对：同一输入跑两遍，patch 序列一致 → 指纹一致
        （说明行为确定性）；指纹不同即「这次走了另一条路」，值得 diff。
        只取行为字段，不含时间戳/span，避免噪声污染比对。
        """
        if self._fingerprint is not None:
            return self._fingerprint
        canon = []
        for s in self._steps:
            canon.append([s["op"], s["path"], s["by"],
                          json.dumps(s["value"], sort_keys=True, ensure_ascii=False,
                                     default=str)])
        blob = json.dumps(canon, ensure_ascii=False, separators=(",", ":"))
        self._fingerprint = hashlib.sha256(blob.encode()).hexdigest()[:16]
        return self._fingerprint

    def to_dict(self) -> dict:
        return {"fingerprint": self.fingerprint, "steps": len(self._steps),
                "tools": self.tool_sequence(), "loop_count": self.loop_count()}


# ══════════════════════════════════════════════════════════════
# Evaluator：状态轨迹匹配（而不是文本匹配）
# ══════════════════════════════════════════════════════════════
@dataclass
class EvalCase:
    name: str
    expect_term_reason: str
    expect_tools: list[str] = field(default_factory=list)
    terminal_assert: Callable[[dict], bool] = lambda _d: True
    max_steps: int = 0  # 0 表示不限


@dataclass
class EvalReport:
    passed: bool
    goal_grounding: bool
    trajectory_match: float
    arg_precision: float
    efficiency: float
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "goal_grounding": self.goal_grounding,
                "trajectory_match": round(self.trajectory_match, 4),
                "arg_precision": round(self.arg_precision, 4),
                "efficiency": round(self.efficiency, 4), "detail": self.detail}


def _lcs_len(a: Sequence[str], b: Sequence[str]) -> int:
    """最长公共子序列长度（经典 DP，O(n*m)，轨迹长度都是个位数到几十，够用）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def _default_arg_checker(tool: str, args: dict) -> bool:
    """默认校验器：工具名非空 + args 是 dict（替换成真 manifest 校验即可）。"""
    return bool(tool) and isinstance(args, dict)


class Evaluator:
    """把「文本像不像」升级成「状态轨迹对不对」。

    四个维度：
      goal_grounding   终态断言（看最终 Context data，不看自然语言）
      trajectory_match 工具序列 LCS / 期望长度（顺序对、允许夹带噪声）
      arg_precision    工具参数能否被 manifest 接受（fake 校验器注入）
      efficiency       1 - 冗余步比例（走 9 步干 3 步的活 = 低效）

    「碰对了」vs「真做对了」：
      终态对（goal_grounding=True）但路径不对/绕路/参数瞎填 → passed=False，
      并在 detail["lucky_pass"]=True 打标。这样 CI 里不会因为“瞎试也中”而放行。
    """

    # 阈值放在类属性上，方便按团队口径调
    MIN_TRAJECTORY = 0.6
    MIN_EFFICIENCY = 0.6
    MIN_ARG_PRECISION = 0.99
    MAX_STEP_RATIO = 1.5

    def __init__(self, arg_checker: Callable[[str, dict], bool] | None = None) -> None:
        self.arg_checker = arg_checker or _default_arg_checker

    # ── 各维度 ───────────────────────────────────────────────
    def _term_ok(self, case: EvalCase, result: Result) -> tuple[bool, str]:
        try:
            actual = getattr(getattr(result, "term_reason", None), "value",
                             str(getattr(result, "term_reason", "")))
            return (str(actual) == str(case.expect_term_reason)), str(actual)
        except Exception as exc:  # 观测层兜底
            return False, f"<error: {exc}>"

    def _grounding(self, case: EvalCase, final_ctx_data: dict) -> tuple[bool, str]:
        assert_fn = case.terminal_assert
        if assert_fn is None:
            return True, ""
        try:
            return bool(assert_fn(final_ctx_data if isinstance(final_ctx_data, dict) else {})), ""
        except Exception as exc:
            # 断言自己炸了 = 没通过，但把原因留在 detail 里（不吞证据）
            return False, f"terminal_assert raised: {type(exc).__name__}: {exc}"

    def _arg_precision(self, traj: Trajectory) -> tuple[float, list[dict]]:
        calls = traj.tool_calls()
        if not calls:
            return 1.0, []
        rejected: list[dict] = []
        ok = 0
        for c in calls:
            try:
                good = bool(self.arg_checker(c["tool"], c["args"]))
            except Exception:
                good = False
            if good:
                ok += 1
            else:
                rejected.append({"tool": c["tool"], "args": c["args"]})
        return ok / len(calls), rejected

    def _efficiency(self, expected_steps: int, actual_steps: int) -> tuple[float, float, int]:
        """返回 (efficiency, step_ratio, redundant_steps)。"""
        if actual_steps <= 0:
            return 1.0, 0.0, 0
        redundant = max(0, actual_steps - max(expected_steps, 1))
        eff = 1.0 - redundant / actual_steps
        ratio = actual_steps / max(expected_steps, 1)
        return max(0.0, min(1.0, eff)), ratio, redundant

    # ── 主入口 ───────────────────────────────────────────────
    def evaluate(self, case: EvalCase, result: Result,
                 final_ctx_data: dict, traj: Trajectory) -> EvalReport:
        term_ok, actual_term = self._term_ok(case, result)
        grounding, grounding_err = self._grounding(case, final_ctx_data)

        actual_tools = traj.tool_sequence()
        expect_tools = list(case.expect_tools or [])
        if expect_tools:
            hit = _lcs_len(actual_tools, expect_tools)
            trajectory_match = min(1.0, hit / len(expect_tools))
            missing = _missing_subsequence(actual_tools, expect_tools)
        else:
            trajectory_match, missing = 1.0, []

        arg_precision, rejected = self._arg_precision(traj)

        # 实际步数：优先用轨迹里的工具记录；没有工具记录时退回 Result.steps
        actual_steps = len(actual_tools) or int(getattr(result, "steps", 0) or 0)
        expected_steps = len(expect_tools) or int(case.max_steps or 0) or actual_steps
        efficiency, step_ratio, redundant = self._efficiency(expected_steps, actual_steps)

        over_max_steps = bool(case.max_steps) and actual_steps > int(case.max_steps)

        # ── 「碰对了」判定：终态对了，但过程证据不足以支撑 ──
        sloppy_reasons: list[str] = []
        if efficiency < self.MIN_EFFICIENCY:
            sloppy_reasons.append(
                f"冗余步过多: efficiency={efficiency:.3f} < {self.MIN_EFFICIENCY}")
        if step_ratio > self.MAX_STEP_RATIO:
            sloppy_reasons.append(
                f"步数放大: {actual_steps} 步做 {expected_steps} 步的事 (ratio={step_ratio:.2f})")
        if trajectory_match < self.MIN_TRAJECTORY:
            sloppy_reasons.append(
                f"工具路径不符: trajectory_match={trajectory_match:.3f} < {self.MIN_TRAJECTORY}")
        if arg_precision < self.MIN_ARG_PRECISION:
            sloppy_reasons.append(
                f"参数被拒: arg_precision={arg_precision:.3f} < {self.MIN_ARG_PRECISION}")
        if over_max_steps:
            sloppy_reasons.append(f"超出 max_steps: {actual_steps} > {case.max_steps}")

        # lucky_pass 只在「目标达成了」时才成立：
        # 没达成就是普通失败，不算「碰对」。
        lucky_pass = bool(grounding and term_ok and sloppy_reasons)

        passed = bool(
            term_ok and grounding and not sloppy_reasons and not lucky_pass
        )

        detail = {
            "case": case.name,
            "term_reason": {"expected": str(case.expect_term_reason),
                            "actual": actual_term, "ok": term_ok},
            "goal_grounding": grounding,
            "terminal_error": grounding_err,
            "expected_tools": expect_tools,
            "actual_tools": actual_tools,
            "missing_from_expected_path": missing,
            "actual_steps": actual_steps,
            "expected_steps": expected_steps,
            "redundant_steps": redundant,
            "step_ratio": round(step_ratio, 4),
            "rejected_calls": rejected,
            "lucky_pass": lucky_pass,
            "sloppy_reasons": sloppy_reasons,
            "max_steps_exceeded": over_max_steps,
            "loop_count": traj.loop_count(),
            "fingerprint": traj.fingerprint,
            "result_steps": int(getattr(result, "steps", 0) or 0),
            "partial": bool(getattr(result, "partial", False)),
        }
        return EvalReport(
            passed=passed,
            goal_grounding=grounding,
            trajectory_match=trajectory_match,
            arg_precision=arg_precision,
            efficiency=efficiency,
            detail=detail,
        )


def _missing_subsequence(actual: Sequence[str], expected: Sequence[str]) -> list[str]:
    """期望路径里没被覆盖到的工具（按出现顺序，允许重复）。"""
    j = 0
    missing: list[str] = []
    for want in expected:
        k = j
        while k < len(actual) and actual[k] != want:
            k += 1
        if k < len(actual):
            j = k + 1
        else:
            missing.append(want)
    return missing


# ══════════════════════════════════════════════════════════════
# Replayer：patch log 自带重放能力
# ══════════════════════════════════════════════════════════════
class Replayer:
    """initial_data + patches → 等价终态。

    语义必须与 Context.apply 完全一致，否则「重放等价」就是假的：
      set   覆盖（含自动建中间 dict）
      append 追加（缺失则建 list）
      incr  数值累加（缺失视为 0）
      merge 浅合并（缺失则建 dict）

    与 Tracer 相反，这里**不** fail-open：未知 op / 类型不符一律 raise，
    静默重放会造出「看起来等价」的假象，比直接失败更危险。
    """

    OPS = ("set", "append", "incr", "merge")

    @staticmethod
    def replay(initial_data: dict, patches: Sequence[Patch]) -> dict:
        data = copy.deepcopy(initial_data) if initial_data else {}
        if not isinstance(data, dict):
            raise TypeError("initial_data 必须是 dict")
        for i, p in enumerate(patches):
            try:
                _apply_one(data, p)
            except Exception as exc:
                raise ValueError(
                    f"replay 失败于 patch[{i}] op={getattr(p, 'op', '?')} "
                    f"path={getattr(p, 'path', '?')}: {exc}"
                ) from exc
        return data

    @staticmethod
    def replay_steps(initial_data: dict, patches: Sequence[Patch]) -> list[dict]:
        """逐步快照，用于 diff「第几步开始偏了」。"""
        data = copy.deepcopy(initial_data) if initial_data else {}
        snaps: list[dict] = [copy.deepcopy(data)]
        for i, p in enumerate(patches):
            try:
                _apply_one(data, p)
            except Exception as exc:
                raise ValueError(f"replay 失败于 patch[{i}]: {exc}") from exc
            snaps.append(copy.deepcopy(data))
        return snaps

    @staticmethod
    def equivalent(initial_data: dict, patches: Sequence[Patch],
                   expected: dict) -> bool:
        """重放后状态是否与 expected 等价（demo / 测试断言用）。"""
        return Replayer.replay(initial_data, patches) == expected


def _segments(path: str) -> list[str]:
    segs = [s for s in str(path).split(".") if s != ""]
    if not segs:
        raise ValueError("path 为空")
    return segs


def _parent_of(data: dict, path: str, create: bool = True) -> tuple[dict, str]:
    segs = _segments(path)
    cur = data
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            if nxt is None and create:
                nxt = {}
                cur[seg] = nxt
            else:
                raise TypeError(f"path '{path}' 中间节点 '{seg}' 不是 dict")
        cur = nxt
    return cur, segs[-1]


#: 预算的扁平字段（与 `core/context.py::_BUDGET_FIELDS` 同名同序）
_BUDGET_FIELDS: tuple[str, ...] = (
    "token_total", "token_used", "cost_total_usd", "cost_used_usd",
    "tool_calls_total", "tool_calls_used", "max_steps",
)

#: 预算的嵌套键 → Context 落盘时的扁平键（与 core/context.py 的 _budget_flat 对齐）。
_BUDGET_NESTED = {
    "token": ("token_total", "token_used"),
    "cost_usd": ("cost_total_usd", "cost_used_usd"),
    "tool_calls": ("tool_calls_total", "tool_calls_used"),
}


def _canon_budget_write(data: dict, path: str, value: Any) -> Any:
    """把**整块 budget 的嵌套写法**规范成 Context 的扁平形状。

    为什么需要：`ContextHandle` 落盘时用扁平字段（`budget.token_used`），而 patch log 里
    可能记着调用方传入的嵌套对象（`{"token": {"total": …, "used": …}}`）。
    `ContextHandle.replay()` 走 `_commit` 会重新扁平化，所以它等价；
    而 `Replayer` 是纯重实现，若不在这里规范化，重放出的终态与 Context 不等价。

    **不只是改键名**：`ContextHandle._commit_budget` 会把整块 budget 还原成
    `Budget` 对象再落盘，因此**缺省字段会被默认值补齐**（`max_steps=8`、
    `token_total=20000`…），且"当前状态"作为 fallback 参与。只做嵌套→扁平
    的改写仍会与 Context 不等价 —— 少掉那些字段正是"看起来等价"的假象。
    这里按同一套语义产出**完整扁平预算**：默认值 → 当前状态 → 扁平字段 → 嵌套字段
    （顺序与 `core/context.py::_budget_from_mapping` 一致；`plane/` 不许 import
    `core/`，所以只能照抄语义，不能复用实现）。
    """
    if path != "budget" or not isinstance(value, dict):
        return value
    flat = {f: getattr(Budget(), f) for f in _BUDGET_FIELDS}
    current = data.get("budget")
    if isinstance(current, dict):
        flat.update({k: v for k, v in current.items() if k in flat})
    for field in _BUDGET_FIELDS:                  # 1) 扁平写法
        if field in value:
            flat[field] = value[field]
    for nested_key, (total_key, used_key) in _BUDGET_NESTED.items():   # 2) 嵌套写法
        nested = value.get(nested_key)
        if not isinstance(nested, dict):
            continue
        if "total" in nested:
            flat[total_key] = nested["total"]
        if "used" in nested:
            flat[used_key] = nested["used"]
    return flat


def _apply_one(data: dict, p: Patch) -> None:
    op = str(getattr(p, "op", ""))
    path = str(getattr(p, "path", ""))
    value = getattr(p, "value", None)
    if op not in Replayer.OPS:
        raise ValueError(f"未知 op: {op!r}")

    if op == "set":
        parent, leaf = _parent_of(data, path)
        parent[leaf] = copy.deepcopy(_canon_budget_write(data, path, value))
        return

    parent, leaf = _parent_of(data, path)
    if op == "append":
        cur = parent.get(leaf)
        if cur is None:
            parent[leaf] = [copy.deepcopy(value)]
        elif isinstance(cur, list):
            cur.append(copy.deepcopy(value))
        elif isinstance(cur, tuple):
            parent[leaf] = list(cur) + [copy.deepcopy(value)]
        else:
            raise TypeError(f"append 目标 '{path}' 不是 list: {type(cur).__name__}")
        return

    if op == "incr":
        cur = parent.get(leaf, 0) or 0
        if not isinstance(cur, (int, float)):
            raise TypeError(f"incr 目标 '{path}' 不是数值: {type(cur).__name__}")
        if not isinstance(value, (int, float)):
            raise TypeError(f"incr 的 value 不是数值: {value!r}")
        parent[leaf] = cur + value
        return

    # merge：浅合并
    cur = parent.get(leaf)
    if cur is None:
        cur = {}
        parent[leaf] = cur
    if not isinstance(cur, dict):
        raise TypeError(f"merge 目标 '{path}' 不是 dict: {type(cur).__name__}")
    if not isinstance(value, dict):
        raise TypeError(f"merge 的 value 不是 dict: {value!r}")
    canon = _canon_budget_write(data, path, value)
    for k, v in canon.items():
        cur[k] = copy.deepcopy(v)


# ══════════════════════════════════════════════════════════════
# OpenTelemetry 导出（把手写的 Span 树接到 OTel 语义约定上）
# ══════════════════════════════════════════════════════════════
# 为什么**不**直接用 OTel SDK 替换上面的 Tracer：
#   * `Tracer.finish()` 返回的 dict 形状被 24 个用例、`Evaluator`、README、
#     以及 `demo/*` 的打印依赖；换成 OTel 原生对象是破坏性变更。
#   * `tree()` 的 ASCII 输出是给人看的，OTel 没有等价物。
# 所以分工是：**span 的产生与树结构仍是本模块（它同时是业务逻辑与可测面），
# 而"发到哪里"交给 OTel**。属性名用 `gen_ai.*` 语义约定，
# 这样接 Jaeger / Tempo / Langfuse 时不用自建字段映射。

#: OTel `gen_ai.*` 语义约定（2026 版）里我们实际会发的键
GENAI_ATTRS = (
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.agent.name",
)


def configure_otel(endpoint: str = "", *, service_name: str = "agent-runtime",
                   headers: dict | None = None, sampler_ratio: float = 1.0,
                   install: bool = True):
    """尝试配置 OTLP/HTTP 导出。**没装 exporter 包时返回 None，不抛错。**

    返回一个 `dict` 说明是否真的生效（"配置了没生效"是本项目反复踩的坑）：

        {"enabled": bool, "endpoint": str, "reason": str}

    `install=False` 只做**能力探测**，不接管全局 provider —— 测试与"先看看能不能配"
    的场景必须用它：OTel 的 `set_tracer_provider` 是首次生效的进程级副作用，
    一旦装上一个指向不可达端点的 provider，退出时会刷
    "Failed to export spans ..." 噪声，而且**没法撤销**。

    用法::

        from plane.quality import configure_otel, tracer_to_otel
        if configure_otel(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))["enabled"]:
            ...  # 环境就绪，tracer_to_otel() 会真的把 span 发出去
    """
    info = {"enabled": False, "endpoint": endpoint, "reason": "", "provider": None}
    try:
        from opentelemetry import trace as _otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except Exception as exc:                                   # noqa: BLE001
        info["reason"] = f"opentelemetry-sdk 未安装: {type(exc).__name__}"
        return info

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=TraceIdRatioBased(max(0.0, min(1.0, sampler_ratio))),
    )
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except Exception as exc:                               # noqa: BLE001
            info["reason"] = (f"exporter 包未安装（pip install "
                              f"opentelemetry-exporter-otlp-proto-http）: "
                              f"{type(exc).__name__}")
            return info
        try:
            kwargs = {"endpoint": endpoint}
            if headers:
                kwargs["headers"] = headers
            provider.add_span_processor(
                __import__("opentelemetry.sdk.trace.export", fromlist=["x"])
                .BatchSpanProcessor(OTLPSpanExporter(**kwargs)))
        except Exception as exc:                               # noqa: BLE001
            info["reason"] = f"exporter 初始化失败: {type(exc).__name__}: {exc}"
            return info
    else:
        info["reason"] = "未配置 endpoint（只装了 SDK，不导出）"
    # **只在真的要导出时才替换全局 provider**。
    # OTel 的 set_tracer_provider 是「首次生效、之后忽略」的进程级副作用：
    # 一旦设过，后续 configure 都不会生效，而且残留的 BatchSpanProcessor
    # 会在退出时打 "Failed to export spans ..." 噪声（测试里尤其明显）。
    # 没 endpoint 就只返回一个"SHIM 可见但不接管全局"的说明，不设全局。
    # 只有「有 endpoint 且调用方允许」才接管全局 provider
    if endpoint and install:
        _otel_trace.set_tracer_provider(provider)
        info["installed"] = True
    else:
        info["installed"] = False
        if endpoint and not install:
            info["reason"] = "install=False：仅探测，未接管全局 provider"
    info["enabled"] = True
    # 把 provider 也返回：调用方（测试 / 多目标导出）可以显式用它，
    # 不必依赖进程级 set_tracer_provider（那个只能设一次，设完撤不掉）
    info["provider"] = provider
    return info


def tracer_to_otel(tracer, *, agent_name: str = "agent-runtime",
                   provider: Any = None, flush: bool = True,
                   flush_timeout_ms: int = 5000) -> dict:
    """把已完成的一次 `Tracer` 会话发给已配置的 OTel provider。

    返回 `{"exported": n, "skipped": n, "reason": str}` —— **总是返回统计而不是抛错**：
    观测面 fail-open（INV-9），上报失败不许影响业务结果。
    """
    out = {"exported": 0, "skipped": 0, "reason": ""}
    try:
        from opentelemetry import trace as _otel_trace
    except Exception as exc:                                   # noqa: BLE001
        out["reason"] = f"opentelemetry 未安装: {type(exc).__name__}"
        return out

    # 显式 provider 优先：避免依赖进程级全局（OTel 的 set_tracer_provider
    # 只能生效一次且不可撤销，测试之间会互相污染，多目标导出也无法共存）
    if provider is not None:
        otel = provider.get_tracer(agent_name)
    else:
        otel = _otel_trace.get_tracer(agent_name)
    try:
        report = tracer.finish()
    except Exception as exc:                                   # noqa: BLE001
        # 观测面 fail-open：连 finish() 炸了也不许影响调用方
        out["reason"] = f"tracer.finish() 失败: {type(exc).__name__}: {exc}"
        return out
    spans = report.get("spans") or []

    def emit(node: dict, parent=None) -> None:
        name = str(node.get("name") or "span")
        attrs = dict(node.get("attrs") or {})
        # 统一到 gen_ai.* 语义约定（保留原属性，补标准键）
        if "model" in attrs and "gen_ai.request.model" not in attrs:
            attrs["gen_ai.request.model"] = attrs["model"]
        attrs.setdefault("gen_ai.operation.name", name.split(".")[0])
        attrs.setdefault("gen_ai.agent.name", agent_name)
        dur_ms = float(node.get("duration_ms") or 0.0)
        try:
            ctx = (_otel_trace.set_span_in_context(parent)
                   if parent is not None else None)
            span = otel.start_span(name, context=ctx, attributes=attrs)
            if dur_ms > 0:
                span.end(end_time=int((node.get("start_ms") or 0) * 1e6 + dur_ms * 1e6))
            else:
                span.end()
            out["exported"] += 1
        except Exception:                                      # noqa: BLE001
            out["skipped"] += 1
            return
        for child in node.get("children") or ():
            emit(child, span)

    for root in spans:
        emit(root)
    # **必须 force_flush**：BatchSpanProcessor 默认每 5s 才调度一次，
    # 不刷的话进程退出时 span 会丢（生产里这是最常见的"trace 缺失"原因），
    # 测试里则表现为每个用例白等 5 秒。flush 失败只记原因，不抛。
    if flush and provider is not None:
        try:
            ok = provider.force_flush(timeout_millis=flush_timeout_ms)
            if not ok:
                out["reason"] = "force_flush 超时（span 可能未送达）"
        except Exception as exc:                               # noqa: BLE001
            out["reason"] = f"force_flush 失败: {type(exc).__name__}"
    if not spans:
        out["reason"] = "没有 span 可导出"
    return out


def otel_available() -> bool:
    """SDK 是否可用（决定 UI/文档里是否宣称支持导出）。"""
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        return True
    except Exception:                                          # noqa: BLE001
        return False
