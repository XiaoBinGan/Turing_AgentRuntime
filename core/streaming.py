"""core/streaming.py — 真增量流式：把"跑完再发"变成"边跑边发"。

================================================================================
问题
================================================================================
`Gateway.handle_stream` 的事件序列是 `accepted → started → completed`，但
`started` 与 `completed` 之间**跑完了整个回合**。客户端拿到 `started` 之后要
干等几十秒，屏幕上一个字都不动 —— 那不是流式，只是"一次很慢的响应分三次发"。

================================================================================
做法：尾随 patch log
================================================================================
本项目已经有一个现成的事实源：**Context 的 patch log**（架构文档 §6：
patch log = 轨迹 = WAL）。Loop 每推进一步都会 `apply` 若干 patch，所以
"尾随 patch log"就等于"实时看到 Agent 在想什么、调了什么"。

于是不需要改 Loop，只需要：

    1. 用 `Gateway.on_context` 钩子拿到正在跑的那个 Context
    2. 主线程 tail `ctx.patch_log`（线程安全：Context 只追加，读快照即可）
    3. 每出现新 patch → 翻译成一条 `step` 事件吐给客户端
    4. 回合结束 → 吐 `completed`

**这比"让 Loop 支持回调"更好**：不侵入执行链，且事件内容与轨迹天然一致
（回放时看到的序列与当时推给客户端的是同一份）。

================================================================================
事件契约（在 Gateway 原三事件之上追加，不替换）
================================================================================
    accepted(0)   →  准入通过
    step(n)       →  一步进展（patch 翻译而来，见 `_translate`）
    started(1)    →  进入执行（与 Gateway 语义一致，兼容既有客户端）
    completed(2)  →  终态

**为什么保留 started 而不是删掉**：已经对接的客户端按 `accepted → started →
completed` 写状态机，删掉会让它们的迁移成本变成"必须同时改两端"。
增量事件是**追加**，不是替代。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Iterator, Mapping

__all__ = ["StreamingGateway", "translate_patch", "TAIL_INTERVAL_S", "MAX_STEP_EVENTS"]

#: 尾随轮询间隔（秒）。Loop 一步通常几十毫秒到几秒，50ms 足够实时。
TAIL_INTERVAL_S = 0.05

#: 单回合最多推多少条 step 事件（防止一次长回合把客户端淹没）。
#: 超过后只推"里程碑"（工具调用、阶段变化），略过记账类 patch。
MAX_STEP_EVENTS = 200

#: 记账类 patch：不推给客户端（它们对"进展"没有信息量，只会刷屏）
_NOISE_PATHS = frozenset({
    "budget", "usage", "loop_count", "phase", "deadline", "error",
})

#: 里程碑类：无论是否超限都推
_MILESTONE_PATHS = frozenset({
    "tool_results", "observations", "pending_confirmation", "plan",
    "skill_state", "memory_hits", "knowledge_hits",
})


def translate_patch(patch: Any) -> dict | None:
    """把一条 patch 翻成事件载荷；不值得推的返回 None。

    翻译规则刻意**保守**：只推"有语义"的字段，且只带摘要（不带完整 payload）——
    流式事件会立刻到客户端，把 80KB 工具回包塞进去等于把 Context 爆炸搬到网络上。
    """
    path = getattr(patch, "path", "") or ""
    root = path.split(".", 1)[0]

    if root in _NOISE_PATHS:
        # `phase` 变化是有意义的（EXECUTING → WAITING_USER 等），其余是记账
        if root == "phase":
            return {"kind": "phase", "phase": str(getattr(patch, "value", "") or "")}
        return None

    value = getattr(patch, "value", None)
    op = getattr(patch, "op", "") or ""

    # 容器初始化（op=set 直接把整个列表塞进去）是种子，不是进展 —— 不推。
    # 只有 append 才代表"又发生了一件事"。
    if op == "set" and root in ("history", "tool_results", "observations",
                                "memory_hits", "knowledge_hits"):
        return None

    if root == "tool_results" and isinstance(value, Mapping):
        tool = str(value.get("tool") or "?")
        kind = str(value.get("kind") or "ok")
        payload = value.get("payload")
        preview = ""
        if isinstance(payload, Mapping):
            # 只取前两个键做预览，绝不整包透传
            items = list(payload.items())[:2]
            preview = ", ".join(f"{k}={v}" for k, v in items)[:120]
        elif payload is not None:
            preview = str(payload)[:120]
        return {"kind": "tool", "tool": tool, "outcome": kind,
                "preview": preview, "seq_path": path}

    if root == "observations" and isinstance(value, Mapping):
        return {"kind": "observation", "tool": str(value.get("tool") or ""),
                "ok": bool(value.get("ok")), "summary": str(value.get("summary") or "")[:160]}

    if root == "history" and isinstance(value, Mapping):
        role = str(value.get("role") or "")
        text = str(value.get("text") or "")[:200]
        return {"kind": "message", "role": role, "text": text}

    if root == "plan":
        return {"kind": "plan", "op": op}

    if root == "skill_state" and isinstance(value, Mapping):
        loaded = [x.get("name") for x in (value.get("loaded") or [])
                  if isinstance(x, Mapping)]
        return {"kind": "skills", "loaded": loaded}

    if root in ("memory_hits", "knowledge_hits"):
        return {"kind": root, "op": op}

    if root == "pending_confirmation":
        return {"kind": "confirmation_required",
                "tool": (value or {}).get("tool") if isinstance(value, Mapping) else ""}

    # 认不出来的字段不推：流式事件是给用户看的进展，不是 patch 转储。
    return None


class StreamingGateway:
    """给任意 `Gateway` 加上**真增量**流式。组合而非继承。

    用法::

        gw = Gateway(..., on_context=streamer.capture)   # 或让本类自己装
        streamer = StreamingGateway(gw)                  # 自动装钩子
        for ev in streamer.handle_stream(request): ...
    """

    def __init__(self, gateway: Any, *, interval_s: float = TAIL_INTERVAL_S,
                 max_steps: int = MAX_STEP_EVENTS) -> None:
        self._gateway = gateway
        self.interval_s = max(0.005, float(interval_s))
        self.max_steps = max(1, int(max_steps))
        #: 当前回合的 Context（由 Gateway 的 on_context 钩子写入）
        self._ctx: Any = None
        #: 当前回合的 ExecCtx（取 `progress` 侧通道用）
        self._exec: Any = None
        self._ctx_ready = threading.Event()
        #: 本轮已经发过的 seq（内层事件的 seq 与我们的 step seq 要拼在一起单调）
        self._seen_seqs: list[int] = []
        self._install_hook()

    # ── 装钩子（不破坏调用方已有的钩子）─────────────────────
    def _install_hook(self) -> None:
        gw = self._gateway
        if not hasattr(gw, "_on_context"):
            return                       # 假 Gateway / 第三方：退化成"跑完再发"
        inner = getattr(gw, "_on_context", None)

        def hook(ctx: Any, e: Any = None) -> None:
            self._ctx = ctx
            # 记下 ExecCtx：进度侧通道挂在它上面（`e.progress`）。
            # 用 `getattr` 是因为接入层可能传别的形状（假 Gateway / 第三方）。
            self._exec = e
            self._ctx_ready.set()
            if callable(inner):
                try:
                    inner(ctx, e)
                except Exception:        # noqa: BLE001 - 观测面 fail-open
                    pass

        try:
            gw._on_context = hook
        except Exception:                # noqa: BLE001 - 只读/冻结的替身
            pass

    # ── 属性透传 ────────────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        """没定义的属性一律转发给内层 gateway。

        为什么必须转发：本类是**装饰器**，调用方仍会读内层的属性
        （`gw.store`、`gw.audit_log`、`gw.rate_limit_per_minute`…）。
        不转发就等于"包一层之后那些东西凭空消失了" —— 而且只在运行时暴露。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._gateway, name)

    # ── 对外 ────────────────────────────────────────────────
    def handle(self, request: dict) -> Any:
        return self._gateway.handle(request)

    def handle_stream(self, request: dict) -> Iterator[dict]:
        """增量事件流：**消费内层事件流**并在事件之间插入 step。

        为什么不调 `gateway.handle()`：内层的 `handle_stream` 已经定义了
        `accepted/rejected → started → completed` 这套语义（含"拒绝只发一条"）。
        自己重跑一遍等于复制那套判断，而且假 Gateway / 第三方接入点可能
        只实现了 `handle_stream`。所以这里只做**插入**，不重做。
        """
        self._ctx = None
        self._ctx_ready.clear()
        # **每条流都从 seq=0 开始**。
        # 早期只在 __init__ 里初始化 `_seen_seqs`，同一个进程里第二条流的
        # 首个事件 seq 从 8 起 —— 客户端按"首条恒为 0"写的状态机会错判。
        # seq 的语义是"这一条流内的顺序"，不是"这个进程累计了多少条"。
        self._seen_seqs = []
        seen = 0
        emitted = 0
        q: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        def _consume() -> None:
            try:
                inner = getattr(self._gateway, "handle_stream", None)
                if not callable(inner):
                    # 只实现了 handle() 的接入点：合成标准三事件，不让流式崩。
                    # （不是"优雅降级"，是契约兜底：文档说 Gateway 两件都有，
                    #   但假件/第三方可能只有一件。）
                    r = self._gateway.handle(request)
                    q.put(("event", {"type": "accepted", "seq": 0,
                                     "data": {"task_id": getattr(r, "task_id", "")}}))
                    q.put(("event", {"type": "started", "seq": 1,
                                     "data": {"phase": getattr(r, "phase", "")}}))
                    q.put(("event", {"type": "completed", "seq": 2,
                                     "data": {"ok": getattr(r, "ok", False),
                                              "text": getattr(r, "text", ""),
                                              "steps": getattr(r, "steps", 0),
                                              "phase": getattr(r, "phase", ""),
                                              "term_reason": getattr(r, "term_reason", ""),
                                              "error": getattr(r, "error", ""),
                                              "audit_id": getattr(r, "audit_id", "")}}))
                    return
                for ev in inner(request):
                    q.put(("event", ev))
            except Exception as exc:                           # noqa: BLE001
                q.put(("error", exc))
            finally:
                q.put(("done", None))

        worker = threading.Thread(target=_consume, name="stream-consume",
                                  daemon=True)
        worker.start()

        # 等 Context 就绪（有界）。
        #
        # 为什么要等：`on_context` 在 Context 建好时触发，之后 patch 才开始累积。
        # 为什么不能等太久：**拒绝路径永远不会产生 Context**（准入就被挡了），
        # 无 Context 的假 Gateway 也不会 —— 早期写死 5s，四个用例各白等 5 秒，
        # 整个模块从 1s 变成 21s。
        #
        # 0.2s 是权衡：真 Gateway 建 Context 是纯内存操作（微秒级），
        # 0.2s 足够覆盖冷启动与线程调度；等不到就退化成"不推 step"，
        # 但内层事件照常转发 —— 功能不丢，只是没有增量。
        self._ctx_ready.wait(timeout=0.2)

        accepted_seen = False
        while True:
            try:
                tag, payload = q.get(timeout=self.interval_s)
            except queue.Empty:
                # **在收到 accepted 之前不推 step**：内层的 accepted 是"准入已过"
                # 的信号，先吐 step 会让客户端看到"先干活、后宣布开始"。
                # 这段极短（通常 <1ms），不是性能问题，是顺序正确性。
                if not accepted_seen:
                    continue
                # 内层还没吐事件：这时正是尾随 patch log + 取进度事件的时机
                for frame in self._drain_progress(emitted):
                    emitted += 1
                    frame["seq"] = len(self._seen_seqs)
                    self._seen_seqs.append(frame["seq"])
                    yield frame
                for frame in self._drain_steps(seen, emitted):
                    if frame is None:
                        seen += 1
                        continue
                    seen += 1
                    emitted += 1
                    frame["seq"] = len(self._seen_seqs)
                    self._seen_seqs.append(frame["seq"])
                    yield frame
                continue

            if tag == "done":
                break
            if tag == "error":
                exc = payload
                yield {"type": "completed",
                       "seq": len(self._seen_seqs),
                       "data": {"ok": False, "text": "", "steps": 0, "phase": "",
                                "term_reason": "",
                                "error": f"internal_error: {type(exc).__name__}: {exc}",
                                "audit_id": ""}}
                break

            # ── 事件与 step 的先后，按事件语义分两类 ────────────────
            #
            # 「开头类」（accepted / started）：**先吐事件再补 patch**。
            #   反过来会让 accepted 落在 step 后面 —— 客户端看到"先干活、后宣布开始"。
            #
            # 「收口类」（其余，主要是 completed / rejected）：**先补 patch 再吐事件**。
            #   patch 是 completed 之前发生的事，排在它后面就是时间倒流；
            #   快速回合里尤其明显（completed 先出队，step 全堆到后面）。
            #
            # seq 统一重写：增量 step 插在内层事件之间，不统一计数客户端会看到
            # 0,1,2 与 0,1,2 两套编号交替。语义不变（seq 唯一含义是顺序）。
            is_opening = str(payload.get("type")) in ("accepted", "started")
            payload = dict(payload)

            if is_opening:
                payload["seq"] = len(self._seen_seqs)
                self._seen_seqs.append(payload["seq"])
                if payload["type"] == "accepted":
                    accepted_seen = True
                yield payload

            # 只有**收口事件之前**才补 patch；开头事件后不补 ——
            # 否则 accepted 之后立刻 drain，会把队列里已经排着的 started
            # 挤到 step 后面（快速回合里必现）。
            # 中间的进展由"队列空"分支持续吐出。
            if accepted_seen and not is_opening:
                for frame in self._drain_progress(emitted):
                    emitted += 1
                    frame["seq"] = len(self._seen_seqs)
                    self._seen_seqs.append(frame["seq"])
                    yield frame
                for frame in self._drain_steps(seen, emitted):
                    if frame is None:
                        seen += 1
                        continue
                    seen += 1
                    emitted += 1
                    frame["seq"] = len(self._seen_seqs)
                    self._seen_seqs.append(frame["seq"])
                    yield frame

            if not is_opening:
                payload["seq"] = len(self._seen_seqs)
                self._seen_seqs.append(payload["seq"])
                yield payload

        worker.join(timeout=1.0)

    # ── 内部 ────────────────────────────────────────────────
    def _drain_progress(self, emitted: int) -> list[dict]:
        """取子 Agent 的进度事件，转成 `step`。

        这是 C 的核心价值：子 Agent 有自己的 Context，父层的 `patch_log` 看不见它。
        没有这条通道，子 Agent 跑 10 步客户端一个字节都收不到，只在最后收到聚合结果。

        **不进 Context**（那会污染 OWNER_TABLE 与回放），所以必须单独读。
        """
        e = self._exec
        sink = getattr(e, "progress", None) if e is not None else None
        drain = getattr(sink, "drain", None)
        if not callable(drain):
            return []
        out: list[dict] = []
        try:
            items = drain(64)
        except Exception:                                      # noqa: BLE001
            return []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            out.append({"type": "step", "data": {
                "kind": "subagent",
                "event": str(item.get("kind") or ""),
                **(item.get("data") if isinstance(item.get("data"), Mapping) else {}),
                "dropped": int(getattr(sink, "dropped", 0) or 0),
            }})
        return out

    def _drain_steps(self, seen: int, emitted: int) -> list[dict | None]:
        """取出 `seen` 之后的新 patch → 事件列表（None = 这条不推）。

        返回的列表长度就是"消费了多少条 patch"，调用方据此推进 `seen`。
        """
        ctx = self._ctx
        if ctx is None:
            return []
        try:
            log = list(ctx.patch_log)     # Context 只追加，读快照安全
        except Exception:                 # noqa: BLE001
            return []
        out: list[dict | None] = []
        taken = 0
        for patch in log[seen:]:
            taken += 1
            # 超限后只推里程碑，避免长回合把客户端刷屏
            if emitted + len(out) >= self.max_steps:
                root = str(getattr(patch, "path", "")).split(".", 1)[0]
                if root not in _MILESTONE_PATHS:
                    out.append(None)
                    continue
            payload = translate_patch(patch)
            if payload is None:
                out.append(None)
                continue
            out.append({"type": "step",
                        "data": {**payload, "patch_index": seen + taken - 1}})
        return out
