"""core/loop.py — Agent Runtime 的唯一驱动者：**有界**状态机（W2）。

设计约束（来自 BRIEF + contracts.py）：

1. 单一 while 循环，每轮开头按**固定顺序**做 6 类终止判定，先命中先返回：
   ① 用户取消 ② 硬截止 ③ 预算 ④ 步数上限 ⑤ 无进展（死锁） ⑥ 任务完成
   其中 ①~⑤ 在循环开头统一检查，⑥ 在 LLM 返回后检查（它需要响应本身）。
2. 任何输入都必须在**有限步**内终止：deadline / budget / max_steps / 死锁 四道闸，
   而且每条出口都给出明确的 ``TermReason``；非正常退出必须 ``partial=True`` 且 text
   里带「未完成，原因：…」。
3. 三级死锁判定（本模块的核心价值）：
   - L1 硬迭代计数：连续 window 轮动作**字面完全相同**
   - L2 语义指纹：连续 window 轮**语义指纹**相同但字面不同（吃掉"客套乒乓"）
   - L3 仲裁接管：命中后先强制一次 replan（有 planner 时），仍无进展才 NO_PROGRESS；
     累计命中 >= hard_limit（默认 window*3）→ 直接 NO_PROGRESS 并标记需要人工仲裁。
4. Context 只通过 ``Patch`` 写；本模块只写 ``OWNER_TABLE`` 里属于 ``Owner.LOOP`` 的字段
   （phase / loop_count / deadline / error / pending_confirmation / history / usage /
   budget / tool_results）。绝不 import ``core.context``——ctx 只要实现
   ``snapshot()`` / ``apply(Patch)`` 即可，测试里用 fake 顶上。

记账顺序（每轮迭代，顺序不可乱）：
    ① incr loop_count
    ② append history（assistant 消息）
    ③ e.budget.token_used += resp.usage.total，并把 usage 序列化写进 Context
    ④ 每次 tool 调用后 append tool_results（``outcome.result.to_dict()``；
       result 为 None 的拒绝/失败/超时，退化为带 kind/reason 的记录）
"""
from __future__ import annotations

import concurrent.futures as _futures
import contextlib
import hashlib
import json
import os
import re
import threading
import sys
from typing import Any, Mapping, Sequence

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:                      # 允许 `python3 core/loop.py` 直接跑
    sys.path.insert(0, _ROOT)

from contracts import (                        # noqa: E402  (sys.path 补丁之后再 import)
    PlannerProtocol,
    RiskTier,
    Approval,
    ContextSnapshot,
    ExecCtx,
    LLMResponse,
    NormalizedToolCall,
    Owner,
    Patch,
    Phase,
    Result,
    TermReason,
    ToolManifest,
    ToolOutcome,
    ToolResult,
    Usage,
    args_hash,
    dump,
    now_ms,
)

__all__ = [
    "Loop",
    "DeadlockDetector",
    "action_repr",
    "semantic_fingerprint",
    "class_of",
]


# ══════════════════════════════════════════════════════════════
# 一、语义指纹：动作 → 归一化串 → hash
# ══════════════════════════════════════════════════════════════
# 易变部分（数字 / 路径 / 时间戳 / UUID / 长 hex）先替换为占位符，
# 再对「纯自然语言的自由文本」按长度分桶折叠，这样：
#   "感谢建议" / "不客气，还有什么需要"   →  <TEXT:S>（同一语义槽位）
#   "读取第 3 行" / "读取第 88 行"        →  保留骨架 "读取第 <NUM> 行"
# 两级指纹配合，才能在"字面各不相同"的客套乒乓里认出同一个动作。

_RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_RE_TS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{2}:\d{2}:\d{2}(\.\d+)?\b"
)
_RE_PATH = re.compile(r"(?:/[\w.\-]+){2,}/?")
_RE_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_RE_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_RE_WS = re.compile(r"\s+")
_RE_WORD = re.compile(r"[A-Za-z\u4e00-\u9fff]")

# 标点/空白集合（用于把"自由文本"归一：感谢建议 vs 感谢建议！）
_PUNCT = set(
    " \t\r\n\u3000"
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "，。！？、；：‘’“”（）《》【】〈〉…—–·～　"
)
# 骨架里的占位符尖括号要保留
_KEEP = set("<>")


def _mask_dynamic(text: str) -> str:
    """把易变 token 替换为占位符。"""
    s = _RE_UUID.sub("<UUID>", text)
    s = _RE_TS.sub("<TS>", s)
    s = _RE_PATH.sub("<PATH>", s)
    s = _RE_HEX.sub("<HEX>", s)
    s = _RE_NUM.sub("<NUM>", s)
    return _RE_WS.sub(" ", s).strip()


def _strip_punct(text: str) -> str:
    return "".join(ch for ch in text if ch in _KEEP or ch not in _PUNCT)


def _has_word(text: str) -> bool:
    return _RE_WORD.search(text) is not None


def _text_bucket(text: str) -> str:
    n = len(text)
    if n <= 16:
        return "S"
    if n <= 64:
        return "M"
    return "L"


def class_of(value: Any) -> str:
    """把单个参数值折叠成"语义类别"字符串。

    对标量：bool / 数字 / null / 路径 / 时间戳 / 自由文本分桶；
    对容器：递归折叠成结构串，保留**键名**（键名本身有语义，不折叠）。
    """
    if isinstance(value, bool):
        return "<BOOL>"
    if isinstance(value, (int, float)):
        return "<NUM>"
    if value is None:
        return "<NULL>"
    if isinstance(value, str):
        masked = _mask_dynamic(value)
        skeleton = _strip_punct(masked)
        if not skeleton:
            return "<EMPTY>"
        if "<" in skeleton and ">" in skeleton:
            # 含占位符 → 保留骨架（"读取第 <NUM> 行"），数字差异被吸收
            return skeleton
        if _has_word(skeleton):
            # 纯自由文本 → 长度分桶（"感谢建议" 与 "不客气，还有什么需要" 同桶）
            return f"<TEXT:{_text_bucket(skeleton)}>"
        return "<SYM>"
    if isinstance(value, Mapping):
        return "{" + ",".join(
            f"{k}:{class_of(v)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        ) + "}"
    if isinstance(value, (list, tuple, set)):
        return "[" + ",".join(class_of(v) for v in value) + "]"
    return f"<{type(value).__name__}>"


def action_repr(name: str, args: Mapping[str, Any] | None) -> str:
    """一次动作的字面表示：``<tool>|<json args>``（多条动作用换行连接）。"""
    try:
        payload = json.dumps(dict(args or {}), sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = repr(args)
    return f"{name}|{payload}"


def _semantic_segment(segment: str) -> str:
    name, sep, payload = segment.partition("|")
    if sep:
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = None
        if isinstance(parsed, Mapping):
            body = "{" + ",".join(
                f"{k}:{class_of(v)}"
                for k, v in sorted(parsed.items(), key=lambda kv: str(kv[0]))
            ) + "}"
            return f"{_mask_dynamic(name).lower()}|{body}"
    # 解析不出结构（例如空动作标记）→ 整串掩码后当骨架用
    return _mask_dynamic(segment)


def semantic_fingerprint(action: str) -> str:
    """对（可能多段的）动作字面串做语义归一后取 sha256 前 16 位。"""
    segments = [seg for seg in str(action).split("\n") if seg.strip()]
    blob = "\n".join(_semantic_segment(seg) for seg in segments) or "<EMPTY-ACTION>"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _short(text: str, limit: int = 96) -> str:
    text = str(text).replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ══════════════════════════════════════════════════════════════
# 二、三级死锁判定
# ══════════════════════════════════════════════════════════════
class DeadlockDetector:
    """滑动窗口死锁判定。

    ``observe`` 返回 ``(是否死锁, 级别说明)``，级别说明里带 ``L1``/``L2``/``L3`` 前缀。

    - L1 硬迭代计数：窗口内**字面**指纹全等
    - L2 语义指纹：窗口内**语义**指纹全等但字面不全等（"客套乒乓"）
    - L3 仲裁接管：累计命中 >= ``hard_limit``（默认 ``window * 3``）

    ``reset_window()`` 在"强制 replan"之后调用：给一次重新证明进展的机会，
    但累计 ``hits`` 保留——所以一直救不回来仍会在 L3 收口。
    """

    LEVEL_HARD = "L1"
    LEVEL_SEMANTIC = "L2"
    LEVEL_ARBITRATE = "L3"

    def __init__(self, window: int = 4, hard_limit: int | None = None):
        self.window = max(2, int(window))
        self.hard_limit = int(hard_limit) if hard_limit else self.window * 3
        self.hits = 0
        self.last_level = ""
        self.last_detail = ""
        self._literal: list[str] = []
        self._semantic: list[str] = []

    def observe(self, action_fingerprint: str) -> tuple[bool, str]:
        literal = str(action_fingerprint)
        semantic = semantic_fingerprint(literal)
        self._literal.append(literal)
        self._semantic.append(semantic)
        while len(self._literal) > self.window:
            self._literal.pop(0)
            self._semantic.pop(0)

        full = len(self._literal) >= self.window
        if full and len(set(self._literal)) == 1:
            level = self.LEVEL_HARD
            detail = (f"{level}|硬迭代计数：连续 {self.window} 轮动作完全相同"
                      f"（{_short(literal)}）")
        elif full and len(set(self._semantic)) == 1 and len(set(self._literal)) > 1:
            level = self.LEVEL_SEMANTIC
            detail = (f"{level}|语义指纹：连续 {self.window} 轮语义指纹相同但字面不同"
                      f"（fp={semantic}, 例：{_short(self._literal[0])} ≈ {_short(literal)}）")
        else:
            return False, ""

        self.hits += 1
        if self.hits >= self.hard_limit:
            self.last_level = self.LEVEL_ARBITRATE
            self.last_detail = (
                f"{self.LEVEL_ARBITRATE}|仲裁接管：死锁累计命中 {self.hits} 次"
                f" >= hard_limit={self.hard_limit}，replan 已无法恢复，需要人工仲裁"
                f"（最近一次：{detail}）"
            )
            return True, self.last_detail

        self.last_level = level
        self.last_detail = detail
        return True, detail

    def reset_window(self) -> None:
        """强制 replan 后调用：清窗口，保留累计 hits。"""
        self._literal.clear()
        self._semantic.clear()

    def snapshot(self) -> dict:
        return {
            "window": self.window,
            "hard_limit": self.hard_limit,
            "hits": self.hits,
            "last_level": self.last_level,
            "last_detail": self.last_detail,
        }


# ══════════════════════════════════════════════════════════════
# 三、Loop：有界状态机
# ══════════════════════════════════════════════════════════════
_KIND_CN = {
    "ok": "执行成功",
    "denied": "被拒绝",
    "needs_confirmation": "需要确认",
    "failed": "执行失败",
    "timeout": "超时",
}
_BUDGET_CN = {"token": "token", "cost": "成本", "tool_calls": "工具调用次数"}


def _coerce_response(resp: Any) -> LLMResponse:
    """容忍 fake/适配器返回 dict 之类的东西，保证循环本身不会因形状炸掉。"""
    if isinstance(resp, LLMResponse):
        return resp
    if isinstance(resp, Mapping):
        calls = tuple(
            NormalizedToolCall(
                id=str(c.get("id") or f"call_{i}"),
                name=str(c.get("name") or ""),
                args=dict(c.get("args") or {}),
            )
            for i, c in enumerate(resp.get("tool_calls") or [])
            if c.get("name")
        )
        usage = resp.get("usage")
        if isinstance(usage, Usage):
            pass
        elif isinstance(usage, Mapping):
            usage = Usage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_hit_tokens=int(usage.get("cache_hit_tokens", 0) or 0),
                model=str(usage.get("model", "mock-1")),
            )
        else:
            usage = Usage()
        return LLMResponse(text=str(resp.get("text") or ""), tool_calls=calls,
                           usage=usage, finish_reason=str(resp.get("finish_reason") or "stop"))
    # 允许裸字符串（适配器可以只回文本，这是有意留的简易契约）
    if isinstance(resp, str):
        return LLMResponse(text=resp)
    # **其余类型一律拒绝**，不做 `str(resp)` 兜底。
    #
    # 踩过的坑：早期这里 `return LLMResponse(text=str(resp or ""))`，
    # 于是适配器返回任意对象都会被转成 `"<object object at 0x...>"` 当成答案，
    # 回合以 `DONE` 结束 —— **垃圾被静默当成正常结果**，客户端无从察觉。
    # 「形状不对」应该是一次可诊断的失败，不是一句假答案。
    raise TypeError(
        f"LLM.complete 必须返回 LLMResponse / Mapping / str，"
        f"收到 {type(resp).__name__}（适配器契约见 contracts.LLMProtocol）")


def _coerce_outcome(outcome: Any, tool: str) -> ToolOutcome:
    """dispatcher 返回非 ToolOutcome 时归一；异常一律降级为 failed（不许炸循环）。"""
    if isinstance(outcome, ToolOutcome):
        return outcome
    if isinstance(outcome, Mapping):
        kind = str(outcome.get("kind") or "ok")
        result = outcome.get("result")
        if isinstance(result, Mapping):
            result = ToolResult(tool=str(result.get("tool") or tool),
                                payload=result.get("payload"))
        elif not isinstance(result, ToolResult):
            result = None
        if kind == "ok" and result is None:
            result = ToolResult(tool=tool, payload=outcome.get("payload"))
        return ToolOutcome(kind=kind, result=result,
                           reason=str(outcome.get("reason") or ""),
                           retryable=bool(outcome.get("retryable")))
    if isinstance(outcome, ToolResult):
        return ToolOutcome.ok(outcome)
    return ToolOutcome.failed(f"dispatcher 返回了无法识别的类型 {type(outcome).__name__}")


def _resolve_tools(dispatcher: Any) -> list[ToolManifest]:
    """从 dispatcher 解析工具清单。

    这里踩过一个真坑：最初只写 `getattr(dispatcher, "manifests", None)` 且只接受
    list/tuple。而 `Dispatcher` 暴露的是 `.registry`，`manifests` 是 registry 上的**方法**——
    于是工具清单永远解析成空，模型根本看不到任何工具。
    更糟的是它**静默失败**：mock LLM 不读 prompt，493 个测试全绿，接真实模型才发现。

    所以这里做三件事：多路径尝试、`callable` 也认、**解析不到就立刻告警**。
    """
    for holder_name in ("manifests", "registry"):
        holder = getattr(dispatcher, holder_name, None)
        if holder is None:
            continue
        cand = holder if holder_name == "manifests" else getattr(holder, "manifests", None)
        if callable(cand):
            try:
                cand = cand()
            except Exception:                                   # noqa: BLE001
                cand = None
        if isinstance(cand, (list, tuple)) and cand:
            return list(cand)
    return []


class Loop:
    """唯一驱动者。所有出口都是有界的、带 ``TermReason`` 的。

    构造参数（前四个是契约要求的形状，其余给编排者留口子）：

    - ``builder``    ``build(snapshot, tools) -> str``
    - ``llm``        ``complete(prompt, tools, e) -> LLMResponse``
    - ``dispatcher`` ``invoke(name, args, e) -> ToolOutcome``
    - ``tracer``     可选，``span(name, attrs)`` 上下文管理器
    - ``planner``    可选，死锁时强制 replan 用。**契约见 `contracts.PlannerProtocol`**：
                     返回 `Patch | None`，Runtime 校验 owner 后应用；抛异常只记 warning。
                     这是 **Agent 层的扩展点**，Runtime 不提供默认实现。
    - ``tools``      可选，``Sequence[ToolManifest]``；不给就从 dispatcher.manifests 取
    - ``window``     死锁窗口，默认 4
    """

    def __init__(self, builder, llm, dispatcher, tracer=None,
                 planner: PlannerProtocol | None = None,
                 tools: Sequence[ToolManifest] | None = None,
                 parallel_tools: bool = False,
                 max_parallel_tools: int = 4,
                 window: int = 4, hard_limit: int | None = None):
        self._builder = builder
        self._llm = llm
        self._dispatcher = dispatcher
        self._tracer = tracer
        # 启动就校验，不留到死锁那一刻才发现 —— 死锁是"已经在补救"的路径，
        # 补救工具本身是坏的，故障会被误判成"再规划也没用"（推理方向完全反了）。
        if planner is not None and not callable(getattr(planner, "replan", None)):
            raise TypeError(
                "planner 必须实现 contracts.PlannerProtocol.replan(snapshot, reason)；"
                f"收到 {type(planner).__name__}（缺少可调用的 replan 方法）")
        self._planner = planner
        #: 单轮内**并行执行**互不依赖的工具调用。
        #:
        #: 默认 False（行为与历史完全一致）。开启后一轮里的多个 tool_call 并发跑，
        #: 但有几条硬约束（见 `_execute`）——尤其"结果顺序必须等于输入顺序"，
        #: 否则同一输入两次构建出的 Prompt 不同（INV-2 破坏，回放失效）。
        self.parallel_tools = bool(parallel_tools)
        self.max_parallel_tools = max(1, int(max_parallel_tools))
        #: 并行路径下兜底记账的锁（dispatcher 没记时 Loop 自己加，必须原子）
        self._budget_lock = threading.Lock()
        #: 副作用落盘的失败记录（接入层要把它报出去 —— 吞掉等于"没落盘还谎称落了"）
        self.checkpoint_errors: list[str] = []
        #: 工具清单索引：并行前要判 risk_tier（能不能并）。
        self._tool_index: dict[str, ToolManifest] = {
            m.name: m for m in (tools or ())}

        # 告警容器必须先于任何可能告警的代码初始化（踩过一次顺序坑）
        self.warnings: list[str] = []
        self.spans: list[str] = []
        if tools is not None:
            self._tools: list[ToolManifest] = list(tools)
        else:
            self._tools = _resolve_tools(dispatcher)
            if not self._tools:
                # 工具丢失是「静默故障」：模型会一本正经地说"没有可用工具"，
                # 而调用方以为工具已经传下去了。必须出声。
                self.warnings.append(
                    "工具清单为空：dispatcher 既无 manifests 也无 registry.manifests，"
                    "模型将看不到任何工具（若这是预期，请显式传 tools=[]）")
        self.detector = DeadlockDetector(window=window, hard_limit=hard_limit)
        self._deadlock: tuple[bool, str] | None = None
        self._replans = 0

    def _manifest(self, name: str) -> ToolManifest | None:
        """查工具的 manifest。并行判定要用，查不到就算"判不出来"。"""
        found = self._tool_index.get(name)
        if found is not None:
            return found
        for attr in ("manifests", "registry"):
            src = getattr(self._dispatcher, attr, None)
            if src is None:
                continue
            try:
                if isinstance(src, Mapping):
                    return src.get(name)
                got = getattr(src, "get", None)
                if callable(got):
                    found = got(name)
                    # `ToolRegistry.get()` 返回 **`(manifest, fn)` 元组**，不是 manifest。
                    # 直接返回元组会让 `manifest.side_effect` 永远是 None，
                    # 于是"判不出副作用" → 连只读工具都被落盘（多付写入成本）。
                    if isinstance(found, tuple) and found:
                        return found[0]
                    return found
                for m in src:
                    if getattr(m, "name", "") == name:
                        return m
            except Exception:                                  # noqa: BLE001
                return None
        return None

    # ── 对外接口 ────────────────────────────────────────────
    def run(self, goal: str, ctx, e: ExecCtx) -> Result:
        """从零开始跑一个目标，直到 6 类终止条件之一命中。"""
        with self._span("loop.run", {"goal_len": len(goal or ""), "task_id": e.task_id}):
            self._seed(goal, ctx, e)
            block = getattr(ctx, "bind_engine", None)
            if callable(block):
                try:
                    block("loop")     # 与 DAG 互斥（两者都写 observations）
                except Exception:      # noqa: BLE001 - 假 ctx 可能不支持
                    pass
            return self._drive(ctx, e)

    def resume(self, ctx, e: ExecCtx, decision: bool, reason: str = "") -> Result:
        """从 ``WAITING_USER`` 恢复。

        - ``decision=True``  → 补一条人类批准记录（写进 ``e.approvals``）并**重新执行**
          被挂起的动作（以及当时还没轮到的后续调用），然后继续循环。
        - ``decision=False`` → 把拒绝原因写进 history，让 LLM 换方案，继续循环。
        """
        with self._span("loop.resume", {"decision": bool(decision)}):
            snap = ctx.snapshot()
            pending = snap.get("pending_confirmation")
            steps = self._steps(snap)
            if not isinstance(pending, Mapping):
                ctx.apply(Patch("append", "history", Owner.LOOP, {
                    "role": "system", "step": steps,
                    "text": "resume 被调用但没有 pending_confirmation，按普通续跑处理",
                }))
                return self._drive(ctx, e)

            tool = str(pending.get("tool") or "")
            ahash = str(pending.get("args_hash") or args_hash(pending.get("args") or {}))
            if decision:
                if not e.approved(tool, ahash):
                    e.approvals = tuple(e.approvals) + (
                        Approval(tool=tool, args_hash=ahash, approved=True, approver="human"),
                    )
                note = f"用户批准执行 {tool}（args_hash={ahash}）"
                if reason:
                    note = f"{note}；理由：{reason}"
                ctx.apply(Patch("append", "history", Owner.LOOP,
                                {"role": "user", "step": steps, "text": note}))
                ctx.apply(Patch("set", "pending_confirmation", Owner.LOOP, None))
                ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.EXECUTING))
                replay = self._replay_pending(ctx, e, pending)
                if replay is not None:
                    return replay
            else:
                note = (f"用户拒绝执行 {tool}：{reason or '未说明'}。"
                        f"请换一个不依赖该高风险动作的方案。")
                ctx.apply(Patch("append", "history", Owner.LOOP,
                                {"role": "user", "step": steps, "text": note}))
                ctx.apply(Patch("set", "pending_confirmation", Owner.LOOP, None))
                ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.REPLANNING))
            return self._drive(ctx, e)

    # ── 初始化 ──────────────────────────────────────────────
    def _seed(self, goal: str, ctx, e: ExecCtx) -> None:
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.PLANNING))
        ctx.apply(Patch("set", "budget", Owner.LOOP, e.budget.to_dict()))
        if e.deadline > 0:
            ctx.apply(Patch("set", "deadline", Owner.LOOP, e.deadline))
        snap = ctx.snapshot()
        if not isinstance(snap.get("loop_count"), int):
            ctx.apply(Patch("set", "loop_count", Owner.LOOP, 0))
        if not isinstance(snap.get("history"), list):
            ctx.apply(Patch("set", "history", Owner.LOOP, []))
        if not isinstance(snap.get("tool_results"), list):
            ctx.apply(Patch("set", "tool_results", Owner.LOOP, []))
        ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "user", "text": goal}))

    # ── 主循环 ──────────────────────────────────────────────
    def _drive(self, ctx, e: ExecCtx) -> Result:
        while True:
            # ① ~ ⑤：每轮开头统一检查，顺序固定
            term = self._pre_check(ctx, e)
            if term is not None:
                return term

            snap = ctx.snapshot()
            steps = self._steps(snap)
            with self._span("loop.iteration", {"step": steps + 1}):
                # 记账①：步数 +1（先记账，再让 LLM 干活）
                ctx.apply(Patch("incr", "loop_count", Owner.LOOP, 1))
                ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.EXECUTING))

                prompt = self._build_prompt(ctx, e)
                try:
                    resp = _coerce_response(self._llm.complete(prompt, self._tools, e))
                except Exception as exc:                     # LLM 炸了也是有界退出
                    self.warnings.append(f"llm.complete 异常：{exc}")
                    return self._finish(
                        ctx, Phase.FAILED, TermReason.UNRECOVERABLE,
                        f"未完成，原因：LLM 调用抛异常（{type(exc).__name__}: {exc}），"
                        f"已完成 {self._steps(ctx.snapshot())} 步。",
                        partial=True, steps=self._steps(ctx.snapshot()))

                # **LLM 返回后立刻复查 deadline**。
                #
                # 为什么必须复查：adapter 的 `_guard` 只在**发请求之前**检查，
                # 拦不住"请求发出去了、响应超时才回来"。实测：deadline 100ms、
                # LLM 睡 400ms，回合以 `DONE` 返回 —— 硬截止被静默绕过，
                # 调用方以为"按时完成"。
                #
                # 处理方式：**保留已产出的文本**（token 已经花掉，丢掉纯浪费），
                # 但如实标 `DEADLINE` + `partial=True`。诚实报"超时"比假装
                # "按时完成"更重要：客户端要据此决定渲染还是重试。
                self._accounting(ctx, e, snap, resp)
                if e.deadline_passed() and not resp.tool_calls:
                    steps_now = self._steps(ctx.snapshot())
                    late = (resp.text or "").strip()
                    return self._finish(
                        ctx, Phase.FAILED, TermReason.DEADLINE,
                        f"未完成，原因：超过硬截止时间（deadline={int(e.deadline)}ms），"
                        f"已完成 {steps_now} 步。" + (f"\n（超时后仍返回的内容：{late}）" if late else ""),
                        partial=True, steps=steps_now)

                # ⑥ 任务完成：无 tool_calls 且有终答
                if not resp.tool_calls:
                    if resp.text.strip():
                        return self._done(ctx, e, resp)
                    # 空响应：算一次"无进展"观察，靠步数上限/死锁收口
                    self._observe("<EMPTY-ACTION|{}>", ctx)
                    continue

                suspended = self._execute(ctx, e, resp)
                if suspended is not None:
                    return suspended

    # ── ① ~ ⑤ ──────────────────────────────────────────────
    def _pre_check(self, ctx, e: ExecCtx) -> Result | None:
        snap = ctx.snapshot()
        steps = self._steps(snap)

        # ① 用户取消
        if e.aborted:
            return self._finish(
                ctx, Phase.ABORTED, TermReason.ABORTED,
                f"已中止（用户取消），停在第 {steps} 步。",
                partial=False, steps=steps)

        # ② 硬截止
        if e.deadline_passed():
            return self._finish(
                ctx, Phase.FAILED, TermReason.DEADLINE,
                f"未完成，原因：超过硬截止时间（deadline={int(e.deadline)}ms），"
                f"已完成 {steps} 步。",
                partial=True, steps=steps)

        # ③ 预算耗尽
        which = e.budget.exhausted()
        if which is not None:
            return self._finish(
                ctx, Phase.FAILED, TermReason.BUDGET,
                f"未完成，原因：预算耗尽（{_BUDGET_CN.get(which, which)}："
                f"token {e.budget.token_used}/{e.budget.token_total}，"
                f"成本 {e.budget.cost_used_usd}/{e.budget.cost_total_usd} USD，"
                f"工具 {e.budget.tool_calls_used}/{e.budget.tool_calls_total}），"
                f"已完成 {steps} 步。",
                partial=True, steps=steps)

        # ④ 步数上限
        max_steps = int(e.budget.max_steps)
        if steps >= max_steps:
            return self._finish(
                ctx, Phase.FAILED, TermReason.MAX_STEPS,
                f"未完成，原因：达到步数上限 max_steps={max_steps}，已完成 {steps} 步。",
                partial=True, steps=steps)

        # ⑤ 无进展（三级死锁 / 仲裁接管）
        return self._progress_check(ctx, e, steps)

    def _progress_check(self, ctx, e: ExecCtx, steps: int) -> Result | None:
        if self._deadlock is None:
            return None
        _locked, detail = self._deadlock

        # L3：仲裁接管硬上限
        if self.detector.hits >= self.detector.hard_limit:
            return self._finish(
                ctx, Phase.FAILED, TermReason.NO_PROGRESS,
                f"未完成，原因：无进展（{self.detector.last_detail or detail}），"
                f"已完成 {steps} 步；已触发硬上限，**需要人工仲裁**。",
                partial=True, steps=steps)

        # 命中后先强制**一次** replan（有 planner 时），仍无进展就退出
        if self._planner is not None and self._replans < 1:
            self._replans += 1
            self._force_replan(ctx, e, detail, steps)
            self._deadlock = None
            self.detector.reset_window()
            return None

        return self._finish(
            ctx, Phase.FAILED, TermReason.NO_PROGRESS,
            f"未完成，原因：检测到无进展死锁（{detail}），已完成 {steps} 步。",
            partial=True, steps=steps)

    def _force_replan(self, ctx, e: ExecCtx, detail: str, steps: int) -> None:
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.REPLANNING))
        ctx.apply(Patch("append", "history", Owner.LOOP, {
            "role": "system", "step": steps,
            "text": f"检测到死锁（{detail}），强制 replan 一次，换方案重来。",
        }))
        replan = getattr(self._planner, "replan", None)
        if not callable(replan):
            return
        try:
            produced = replan(ctx.snapshot(), detail)
        except Exception as exc:
            self.warnings.append(f"planner.replan 异常：{exc}")
            ctx.apply(Patch("append", "history", Owner.LOOP, {
                "role": "system", "step": steps,
                "text": f"replan 失败：{type(exc).__name__}: {exc}",
            }))
            return
        if isinstance(produced, Patch):
            try:
                ctx.apply(produced)              # planner 写自己的字段（plan/observations）
            except Exception as exc:
                self.warnings.append(f"planner patch 被拒绝：{exc}")
        elif produced is not None:
            ctx.apply(Patch("append", "history", Owner.LOOP, {
                "role": "system", "step": steps,
                "text": f"replan 结论：{_short(str(produced), 400)}",
            }))

    # ── 记账 ────────────────────────────────────────────────
    def _accounting(self, ctx, e: ExecCtx, snap_before: ContextSnapshot,
                    resp: LLMResponse) -> None:
        # ② assistant 消息进 history
        message: dict[str, Any] = {
            "role": "assistant",
            "step": self._steps(snap_before) + 1,
            "text": resp.text,
            "finish_reason": resp.finish_reason,
        }
        if resp.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "name": c.name, "args": c.args} for c in resp.tool_calls
            ]
        ctx.apply(Patch("append", "history", Owner.LOOP, message))

        # ③ token 记账 + usage 序列化
        e.budget.token_used += resp.usage.total
        prev = snap_before.get("usage") or {}
        if not isinstance(prev, Mapping):
            prev = {}
        usage = {
            "input_tokens": int(prev.get("input_tokens", 0) or 0) + resp.usage.input_tokens,
            "output_tokens": int(prev.get("output_tokens", 0) or 0) + resp.usage.output_tokens,
            "cache_hit_tokens": int(prev.get("cache_hit_tokens", 0) or 0)
            + resp.usage.cache_hit_tokens,
            "total_tokens": int(prev.get("total_tokens", 0) or 0) + resp.usage.total,
            "calls": int(prev.get("calls", 0) or 0) + 1,
            "model": resp.usage.model,
            "last": {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_hit_tokens": resp.usage.cache_hit_tokens,
                "total": resp.usage.total,
            },
        }
        ctx.apply(Patch("set", "usage", Owner.LOOP, usage))
        ctx.apply(Patch("set", "budget", Owner.LOOP, e.budget.to_dict()))

        # 上报计量给 Tracer —— 成本归因不能靠事后重建（P3 缺口回填）
        #    INV-9：观测 fail-open，这里无论如何都不能影响主链路
        if self._tracer is not None:
            try:
                record = getattr(self._tracer, "record_usage", None)
                if callable(record):
                    record(resp.usage.model, resp.usage,
                           tenant=e.principal.tenant,
                           user_id=e.principal.user_id,
                           task_id=e.task_id)
            except Exception:                                   # noqa: BLE001
                pass

    # ── 工具执行 ────────────────────────────────────────────
    def _execute(self, ctx, e: ExecCtx, resp: LLMResponse) -> Result | None:
        """执行一轮里的全部工具调用。串行 / 并行两条路径，记账逻辑共用。"""
        fingerprint = "\n".join(action_repr(c.name, c.args) for c in resp.tool_calls)
        if self.parallel_tools and len(resp.tool_calls) > 1 and self._can_parallel(resp):
            return self._execute_parallel(ctx, e, resp, fingerprint)
        return self._execute_serial(ctx, e, resp, fingerprint)

    def _can_parallel(self, resp: LLMResponse) -> bool:
        """这轮能不能并行？**只要有一个调用可能要人工确认，就整轮退回串行。**

        为什么必须这样退：串行语义是"遇到第一个 needs_confirmation 就停下，
        **后续调用根本不执行**"。并行会把后面的调用也跑掉 —— 那些可能有副作用。
        "已经做了不该做的事" 比 "慢一点" 严重得多。

        判定只看 manifest 的 risk_tier：T2 且未获批 → 一定会 needs_confirmation；
        T3 → denied（也不该并进去添乱）。判不出来（工具不在表里）同样退回串行。
        """
        for call in resp.tool_calls:
            manifest = self._manifest(call.name)
            if manifest is None:
                return False
            tier = int(getattr(manifest, "risk_tier", 0))
            if tier >= int(RiskTier.T2_HIGH):
                return False
        return True

    def _execute_parallel(self, ctx, e: ExecCtx, resp: LLMResponse,
                          fingerprint: str) -> Result | None:
        """单轮内并行执行工具调用。

        四条硬约束（`tests/test_parallel_tools.py` 逐条守着）：

          1. **结果按输入顺序收集**，不按完成先后 —— 顺序即语义，
             `tool_results` 的顺序会进 Prompt。
          2. **Context 只在主线程 apply** —— `ContextHandle` 没有锁，
             并发 apply 会丢 patch（实测确认过）。
          3. **取消信号逐条检查** —— 超时后已排队的调用直接跳过，不白烧预算。
          4. **单条异常不影响其他** —— 并发下异常传播必须显式隔离。
        """
        calls = list(resp.tool_calls)
        outcomes: list[Any] = [None] * len(calls)

        def run_one(idx: int) -> None:
            call = calls[idx]
            if e.aborted or e.deadline_passed():
                outcomes[idx] = self._skipped(call, "aborted/deadline")
                return
            before = e.budget.tool_calls_used
            try:
                outcomes[idx] = _coerce_outcome(
                    self._dispatcher.invoke(call.name, call.args, e), call.name)
            except Exception as exc:                        # noqa: BLE001
                outcomes[idx] = ToolOutcome.failed(
                    f"{type(exc).__name__}: {exc}")
            if e.budget.tool_calls_used == before:
                # dispatcher 没记账时由 Loop 兜底（与串行路径一致）
                with self._budget_lock:
                    e.budget.tool_calls_used += 1

        workers = max(1, min(self.max_parallel_tools, len(calls)))
        with _futures.ThreadPoolExecutor(max_workers=workers,
                                         thread_name_prefix="loop-tool") as pool:
            list(pool.map(run_one, range(len(calls))))

        # 主线程串行记账（约束 2）：顺序 = 输入顺序（约束 1）
        for idx, call in enumerate(calls):
            outcome = outcomes[idx]
            if outcome is None:                              # pragma: no cover
                outcome = ToolOutcome.failed("parallel: no outcome")
            suspended = self._record(ctx, e, resp, idx, call, outcome)
            if suspended is not None:
                # 并行路径不该出现（`_can_parallel` 已排除 T2+），但兜住
                return suspended
        self._observe(fingerprint, ctx)
        return None

    @staticmethod
    def _skipped(call: NormalizedToolCall, reason: str) -> Any:
        """取消/超时导致**未执行**的调用：要留痕，不能假装它成功了。"""
        return ToolOutcome.failed(f"{reason}：{call.name} 未执行")

    def _record(self, ctx, e: ExecCtx, resp: LLMResponse, idx: int,
                call: NormalizedToolCall, outcome: ToolOutcome) -> Result | None:
        """一条调用的记账（tool_results / observations / 挂起 / 失败回写）。

        抽出来是因为串行与并行必须写**完全相同**的东西 —— 两份记账代码
        迟早会漂，而漂的那份不会报错，只会让回放对不上。
        """
        if outcome.kind == "needs_confirmation":
            return self._suspend(ctx, e, resp, idx, call, outcome)
        self._record_tool_result(ctx, call, outcome)
        self._record_observation(ctx, call, outcome)
        self._record_failure_history(ctx, call, outcome)
        self._checkpoint_side_effect(ctx, e, call, outcome)
        return None

    def _checkpoint_side_effect(self, ctx, e: ExecCtx, call: NormalizedToolCall,
                                outcome: ToolOutcome) -> None:
        """**有副作用成功之后**落一次盘（崩溃恢复的着力点）。

        为什么不是"每步都落"：只读取证丢了重跑就行（幂等且便宜）；
        而"已经改过生产"的操作丢了是灾难 —— **你不知道它到底做没做**。
        所以写入成本只付在真正需要的地方。

        为什么失败要**收集起来上报**而不是静默：落不下盘意味着"这次写操作
        没有可恢复的记录"。吞掉它，崩溃后就会看到"工具做了、状态里没有"。
        """
        cb = getattr(e, "checkpoint", None)
        if not callable(cb):
            return
        if outcome.kind != "ok":
            return
        manifest = self._manifest(call.name)
        effect = getattr(manifest, "side_effect", None) if manifest is not None else None
        # `READ_ONLY` 不落盘；其余（MUTATING / IRREVERSIBLE / EXTERNAL…）都落。
        # 判不出来的（manifest 缺失）**也要落** —— 宁可多写一次，不可漏掉一次写操作。
        if effect is not None and str(getattr(effect, "value", effect)) == "read_only":
            return
        try:
            cb(ctx, e, {"tool": call.name, "args_hash": args_hash(call.args),
                        "step": self._steps(ctx.snapshot()),
                        "side_effect": str(getattr(effect, "value", effect) or "")})
        except Exception as exc:                              # noqa: BLE001
            self.checkpoint_errors.append(
                f"{call.name}: {type(exc).__name__}: {exc}")

    def _execute_serial(self, ctx, e: ExecCtx, resp: LLMResponse,
                        fingerprint: str) -> Result | None:
        for idx, call in enumerate(resp.tool_calls):
            before = e.budget.tool_calls_used
            try:
                outcome = _coerce_outcome(
                    self._dispatcher.invoke(call.name, call.args, e), call.name)
            except Exception as exc:                        # 工具层抛异常 → 记失败不炸循环
                self.warnings.append(f"dispatcher.invoke 异常：{exc}")
                outcome = ToolOutcome.failed(f"{type(exc).__name__}: {exc}")
            if e.budget.tool_calls_used == before:
                # dispatcher 没记账时由 Loop 兜底，保证 tool_calls 预算这道闸有效
                e.budget.tool_calls_used += 1

            suspended = self._record(ctx, e, resp, idx, call, outcome)
            if suspended is not None:
                return suspended

        self._observe(fingerprint, ctx)
        return None

    # ── 记账（串行/并行共用，避免两份代码漂移）─────────────
    def _record_tool_result(self, ctx, call: NormalizedToolCall,
                            outcome: ToolOutcome) -> None:
        """记账④：tool_results。"""
        if outcome.result is not None:
            record = outcome.result.to_dict()
        else:
            record = {"tool": call.name, "payload": None,
                      "kind": outcome.kind, "reason": outcome.reason}
        record.setdefault("call_id", call.id)
        record.setdefault("step", self._steps(ctx.snapshot()))
        ctx.apply(Patch("append", "tool_results", Owner.LOOP, record))

    def _record_observation(self, ctx, call: NormalizedToolCall,
                            outcome: ToolOutcome) -> None:
        """记账⑤：observations —— 「我观测到了什么」。

        这是别人的输入（Planner 的 replan、子 Agent 的 merge 回写），
        owner 必须是 LOOP：观测事实的权威源在 Loop，不在 Planner。
        """
        payload = outcome.result.payload if outcome.result is not None else None
        summary = "" if payload is None else _short(str(payload), 200)
        ctx.apply(Patch("append", "observations", Owner.LOOP, {
            "step": self._steps(ctx.snapshot()),
            "tool": call.name,
            "ok": outcome.kind == "ok",
            "kind": outcome.kind,
            "summary": summary or outcome.reason,
        }))

    def _record_failure_history(self, ctx, call: NormalizedToolCall,
                                outcome: ToolOutcome) -> None:
        if outcome.kind == "ok":
            return
        ctx.apply(Patch("append", "history", Owner.LOOP, {
            "role": "tool",
            "step": self._steps(ctx.snapshot()),
            "tool": call.name,
            "kind": outcome.kind,
            "retryable": outcome.retryable,
            "text": f"工具 {call.name} {_KIND_CN.get(outcome.kind, outcome.kind)}："
                    f"{outcome.reason}",
        }))

    # ── WAITING_USER 挂起 ───────────────────────────────────
    def _suspend(self, ctx, e: ExecCtx, resp: LLMResponse, idx: int,
                 call: NormalizedToolCall, outcome: ToolOutcome) -> Result:
        steps = self._steps(ctx.snapshot())
        pending = {
            "tool": call.name,
            "call_id": call.id,
            "args": call.args,
            "args_hash": args_hash(call.args),
            "reason": outcome.reason,
            "step": steps,
            "asked_at": now_ms(),
            "remaining_calls": [
                {"id": c.id, "name": c.name, "args": c.args}
                for c in resp.tool_calls[idx + 1:]
            ],
        }
        ctx.apply(Patch("set", "pending_confirmation", Owner.LOOP, pending))
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.WAITING_USER))
        ctx.apply(Patch("append", "history", Owner.LOOP, {
            "role": "system", "step": steps,
            "text": f"需要用户确认：{call.name}（{outcome.reason}）",
        }))
        return Result(
            phase=Phase.WAITING_USER,
            term_reason=TermReason.NEEDS_CONFIRMATION,
            text=f"已暂停，等待用户确认：{call.name}（{outcome.reason}）。"
                 f"已执行 {steps} 步，待确认动作 args_hash={pending['args_hash']}。",
            partial=False,
            steps=steps,
            artifacts=({"pending_confirmation": pending},),
        )

    def _replay_pending(self, ctx, e: ExecCtx, pending: Mapping[str, Any]) -> Result | None:
        calls = [NormalizedToolCall(
            id=str(pending.get("call_id") or "replay_0"),
            name=str(pending.get("tool") or ""),
            args=dict(pending.get("args") or {}),
        )]
        for i, extra in enumerate(pending.get("remaining_calls") or []):
            calls.append(NormalizedToolCall(
                id=str(extra.get("id") or f"replay_{i + 1}"),
                name=str(extra.get("name") or ""),
                args=dict(extra.get("args") or {}),
            ))
        resp = LLMResponse(text="", tool_calls=tuple(calls), finish_reason="resume")
        return self._execute(ctx, e, resp)

    # ── 完成 / 终止 ─────────────────────────────────────────
    def _done(self, ctx, e: ExecCtx, resp: LLMResponse) -> Result:
        snap = ctx.snapshot()
        steps = self._steps(snap)
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.DONE))
        return Result(
            phase=Phase.DONE, term_reason=TermReason.DONE, text=resp.text,
            partial=False, steps=steps,
            artifacts=({"steps": steps, "usage": snap.get("usage") or {},
                        "finish_reason": resp.finish_reason},),
        )

    def _finish(self, ctx, phase: Phase, term: TermReason, text: str,
                partial: bool, steps: int) -> Result:
        ctx.apply(Patch("set", "phase", Owner.LOOP, phase))
        ctx.apply(Patch("set", "error", Owner.LOOP, {
            "term_reason": term.value,
            "text": text,
            "step": steps,
            "needs_human_arbitration": self.detector.hits >= self.detector.hard_limit,
        }))
        return Result(
            phase=phase, term_reason=term, text=text, partial=partial, steps=steps,
            artifacts=({"terminated_by": term.value, "steps": steps,
                        "deadlock": self.detector.snapshot()},),
        )

    # ── 小工具 ──────────────────────────────────────────────
    @staticmethod
    def _steps(snap: ContextSnapshot) -> int:
        """从快照读 loop_count（= 已跑的 LLM 轮数 = Result.steps）。"""
        value = snap.get("loop_count", 0) if isinstance(snap, ContextSnapshot) else 0
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _build_prompt(self, ctx, e: ExecCtx) -> str:
        try:
            prompt = self._builder.build(ctx.snapshot(), self._tools)
            return prompt if isinstance(prompt, str) else str(prompt)
        except Exception as exc:
            self.warnings.append(f"builder.build 异常：{exc}")
            return f"[builder 失败：{type(exc).__name__}: {exc}] 请基于 history 继续。"

    def _observe(self, raw: str, ctx) -> None:
        locked, detail = self.detector.observe(raw)
        self._deadlock = (locked, detail) if locked else None

    @contextlib.contextmanager
    def _span(self, name: str, attrs: dict | None = None):
        if self._tracer is None:
            yield
            return
        try:
            cm = self._tracer.span(name, attrs or {})
        except Exception as exc:
            self.warnings.append(f"tracer.span 异常：{exc}")
            yield
            return
        with cm:
            self.spans.append(name)
            yield


if __name__ == "__main__":  # 手跑自检：无 planner、无 tracer 的最小闭环
    from dataclasses import dataclass, field

    @dataclass
    class MiniCtx:
        data: dict = field(default_factory=dict)
        version: int = 0

        def apply(self, patch: Patch) -> int:
            segs = patch.path.split(".")
            cur = self.data
            for seg in segs[:-1]:
                cur = cur.setdefault(seg, {})
            leaf = segs[-1]
            if patch.op == "set":
                cur[leaf] = patch.value
            elif patch.op == "incr":
                cur[leaf] = (cur.get(leaf) or 0) + patch.value
            elif patch.op == "append":
                cur.setdefault(leaf, []).append(patch.value)
            self.version += 1
            return self.version

        def snapshot(self) -> ContextSnapshot:
            return ContextSnapshot(self.version, json.loads(json.dumps(self.data, default=str)))

    class OneShotLLM:
        def __init__(self):
            self.n = 0

        def complete(self, prompt, tools, e):
            self.n += 1
            if self.n < 3:
                return LLMResponse(text="", usage=Usage(input_tokens=10, output_tokens=5),
                                   tool_calls=(NormalizedToolCall(
                                       id=f"c{self.n}", name="echo", args={"n": self.n}),))
            return LLMResponse(text="完成：已 echo 两次。", usage=Usage(10, 5))

    class EchoDispatcher:
        manifests = [ToolManifest(name="echo", side_effect="read_only")]

        def invoke(self, name, args, e):
            return ToolOutcome.ok(ToolResult(tool=name, payload=args))

    class PlainBuilder:
        def build(self, snap, tools):
            return f"历史 {len(snap.get('history') or [])} 条"

    ctx, e = MiniCtx(), ExecCtx()
    loop = Loop(PlainBuilder(), OneShotLLM(), EchoDispatcher())
    res = loop.run("自检", ctx, e)
    print("phase =", res.phase.value, "| term =", res.term_reason.value,
          "| steps =", res.steps, "| tokens =", e.budget.token_used)
    print("text  =", res.text)
    print("tool_results =", len(ctx.data.get("tool_results") or []))
    print(dump(ctx.data.get("usage")))
