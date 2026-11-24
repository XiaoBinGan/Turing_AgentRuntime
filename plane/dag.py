"""plane/dag.py —— Workflow Plane 的**静态 DAG 编排**流派。

架构文档 §4.4 把编排锁成两派，边界清晰、不混用：

* **静态 DAG（本文件）**：固定流程，拓扑在代码里固化 → 确定性可审计。
  企业里「A 做完才能做 B，C 和 D 可以并行」的 SOP 流水线就是这一路。
* **动态 FSM + 事件总线**：探索型任务（已实现：`core/loop.py` 的 `Loop`）。

与 `plane/workflow.py` 的分工（**刻意不 import 它**）：

* `HierarchicalOrchestrator`：Leader 动态拆任务 → fan-out / fan-in，拓扑是运行时决定的；
* `DAGRunner`：拓扑是**编译期**（spec）决定的，只负责按依赖调度。

`DAGRunner` 对 runner 只做鸭子类型要求：
`dispatch(spec: SubagentSpec, parent_ctx, parent_e) -> SubagentResult`，
`plane/workflow.py` 的 `SubagentRunner` 天然满足（含 INV-18 的降权与只读白名单）。

三条硬约束（都是「确定性」的物理兑现，不是风格偏好）：

1. `layers()` 与 `topo_order()` **同层按节点名排序**，不依赖 dict / spec 插入顺序；
   且 `flatten(layers()) == topo_order()` —— 两个视图不许打架。
2. 成环必须报出**是哪几个节点**成环（不只是「有环」），且只报环成员，
   不把环下游的受害者也拖进来。
3. 短路只发生在**层边界**：同层已启动的节点一定跑完再收结果，
   **绝不中途 kill 线程**（kill = 资源泄漏 + 半写上下文，比慢一点糟得多）。
"""
from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping, Sequence

from contracts import DAGSpec, SubagentResult, SubagentSpec, TermReason

#: 节点被短路时的 term_reason。契约里的 `TermReason` 没有 SKIPPED 成员（契约冻结），
#: 所以这里是 **DAG 层自有字面量**：不执行 ≠ 执行失败，账本要能分开记。
SKIP_TERM = "SKIPPED"

#: 每个节点一个 span（fail-open：tracer 坏掉不影响流水线）
#: 上游节点结论注入到下游 goal 的格式。
#:
#: **为什么 Runtime 要做这件事**：静态 DAG 最常见的形态就是「先检查、再决定」。
#: 下游节点如果看不到上游结论，它只能按自己那句 goal 瞎猜 ——
#: "根据检查结果决定是否扩容"里根本没有"检查结果"。
#: 这不是业务判断（Runtime 不解释结果含义），只是**把事实传下去**。
UPSTREAM_BLOCK = "\n\n[上游节点结论]\n{items}"

NODE_SPAN = "dag.node"

ST_OK = "ok"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"

__all__ = [
    "DAG",
    "DAGRunner",
    "DAGValidationError",
    "DAGExecutionError",
    "SKIP_TERM",
    "NODE_SPAN",
]


# ══════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════
class DAGValidationError(Exception):
    """静态拓扑不合法：重名 / 依赖不存在 / 自依赖 / 成环。

    **一次报全部**——逐个报会让人修完一个跑一次，流水线校验不该这么折磨人。
    """

    def __init__(self, errors: Iterable[str] | str) -> None:
        if isinstance(errors, str):
            errors = [errors]
        self.errors: list[str] = [str(e) for e in (errors or [])]
        detail = "；".join(self.errors) if self.errors else "（无错误明细）"
        super().__init__(f"DAG 校验失败（{len(self.errors)} 项）：{detail}")


class DAGExecutionError(Exception):
    """执行期配置错误（不是节点失败）。节点失败一律收敛成 `ok=False` 的结果，不抛。"""

    def __init__(self, message: str, node: str = "") -> None:
        self.node = node
        super().__init__(message)


# ══════════════════════════════════════════════════════════════
# 纯函数小工具
# ══════════════════════════════════════════════════════════════
def _field(node: Any, key: str, default: Any = "") -> Any:
    """同时支持 dataclass（DAGNode）与 dict 形状的节点，便于测试与外部 spec 生成。"""
    if isinstance(node, Mapping):
        return node.get(key, default)
    return getattr(node, key, default)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    """把环旋转到「最小名字开头」，同一环从不同入口进来只报一次。"""
    if not cycle:
        return ()
    pivot = min(range(len(cycle)), key=lambda i: cycle[i])
    return tuple(cycle[pivot:] + cycle[:pivot])


# ══════════════════════════════════════════════════════════════
# DAG：静态拓扑的校验 / 分层 / 就绪计算
# ══════════════════════════════════════════════════════════════
def _with_upstream(goal: str, deps: Sequence[str],
                   upstream: Mapping[str, dict]) -> str:
    """把上游依赖的结论拼到 goal 后面（**只传事实，不解释含义**）。

    只带"成功/失败 + 步骤数 + 摘要"三样：
      * 全量 payload 可能很大（工具回包），塞进 goal 会撑爆预算
      * 摘要已够下游做"要不要继续"的判断
    """
    if not deps or not upstream:
        return goal
    lines: list[str] = []
    for dep in deps:
        payload = upstream.get(dep)
        if not isinstance(payload, Mapping):
            continue
        ok = bool(payload.get("ok"))
        steps = int(payload.get("steps") or 0)
        summary = str(payload.get("summary") or payload.get("text") or "").strip()
        err = str(payload.get("error") or "").strip()
        head = f"- {dep}: {'ok' if ok else 'failed'}, {steps} 步"
        if summary:
            head += f"，结论：{summary[:400]}"
        elif err:
            head += f"，错误：{err[:200]}"
        lines.append(head)
    if not lines:
        return goal
    return goal + UPSTREAM_BLOCK.format(items="\n".join(lines))


class DAG:
    """`DAGSpec` 的只读调度视图。

    只负责「拓扑」这件事：合法吗、谁先谁后、谁和谁能并行、现在谁能跑。
    执行是 `DAGRunner` 的事——**拓扑与执行分离**，两边的测试才好互不牵扯。
    """

    def __init__(self, spec: DAGSpec) -> None:
        if spec is None:
            raise ValueError("DAG 需要一个 DAGSpec，收到 None（空图请用 DAGSpec(name=..., nodes=())）")
        if not hasattr(spec, "nodes"):
            raise ValueError(
                f"DAG 需要一个带 nodes 字段的 DAGSpec，收到 {type(spec).__name__}")
        self.spec = spec
        self.name: str = _as_text(_field(spec, "name", ""))
        self._raw_nodes: list[Any] = list(_field(spec, "nodes", ()) or ())

        self._nodes: dict[str, Any] = {}
        for node in self._raw_nodes:
            # 重名后者覆盖前者（只是为了让后续查询有个确定答案）；validate() 会报错
            self._nodes[_as_text(_field(node, "name", ""))] = node

        self._deps: dict[str, tuple[str, ...]] = {
            name: self._deps_of(node) for name, node in self._nodes.items()
        }

    # ── 只读视图 ────────────────────────────────────────────
    @property
    def names(self) -> list[str]:
        """节点名（**按名字排序**，这是本模块所有输出的确定性基准）。"""
        return sorted(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def node(self, name: str) -> Any:
        try:
            return self._nodes[name]
        except KeyError:
            raise KeyError(f"DAG {self.name!r} 里没有节点 {name!r}") from None

    def deps(self, name: str) -> tuple[str, ...]:
        return self._deps.get(name, ())

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"DAG(name={self.name!r}, nodes={len(self._nodes)})"

    # ── 校验 ────────────────────────────────────────────────
    def validate(self) -> list[str]:
        """返回**全部**错误（空列表＝通过）。四类：重名 / 依赖不存在 / 自依赖 / 成环。"""
        errors: list[str] = []

        # ① 重名（保留 spec 顺序，说清是第几个，好定位）
        first_seen: dict[str, int] = {}
        for index, node in enumerate(self._raw_nodes):
            name = _as_text(_field(node, "name", ""))
            if not name.strip():
                errors.append(f"第 {index + 1} 个节点的名字为空（节点名是 DAG 的唯一身份）")
            elif name in first_seen:
                errors.append(
                    f"重名节点: {name!r}（第 {first_seen[name] + 1} 个与第 {index + 1} 个重复）")
            else:
                first_seen[name] = index

        known = set(self._nodes)

        # ② 依赖不存在（自依赖走 ③，别混在「不存在」里误报）
        for name in sorted(self._nodes):
            for dep in self._deps[name]:
                if dep != name and dep not in known:
                    errors.append(f"节点 {name} 依赖不存在的节点 {dep}")

        # ③ 自依赖：哪怕其它都合法也必须拦，否则调度器会永远等自己
        for name in sorted(self._nodes):
            if name in self._deps[name]:
                errors.append(f"自依赖: 节点 {name} 依赖自己（自环无法调度）")

        # ④ 成环：报出具体环路径（环成员，不含环的下游受害者）
        for cycle in self._find_cycles():
            errors.append("成环: " + " -> ".join(list(cycle) + [cycle[0]]))

        return errors

    def _deps_of(self, node: Any) -> tuple[str, ...]:
        raw = _field(node, "depends_on", ()) or ()
        if isinstance(raw, str):                     # 单依赖写成字符串也接住
            raw = (raw,)
        out: list[str] = []
        for dep in raw:
            text = _as_text(dep)
            if text not in out:                      # 去重：重复依赖不该算两次入度
                out.append(text)
        return tuple(out)

    def _find_cycles(self) -> list[list[str]]:
        """找出所有环（每个环报一次，从最小名字进环）。

        **迭代式 DFS**，不用递归：一条 2000 节点流水线不该被递归深度上限打断。
        只报 **回边闭合的那段路径**——环成员精确，不把环下游拖进来。
        """
        known = set(self._nodes)
        color: dict[str, int] = {name: 0 for name in self._nodes}   # 0 未访问 1 在栈上 2 完成
        found: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        def record(cycle: list[str]) -> None:
            key = _canonical_cycle(cycle)
            if key and key not in seen:
                seen.add(key)
                found.append(list(key))

        for start in sorted(self._nodes):
            if color[start] != 0:
                continue
            color[start] = 1
            path: list[str] = [start]
            pos: dict[str, int] = {start: 0}
            stack: list[tuple[str, Any]] = [
                (start, iter(sorted(self._deps.get(start, ()))))
            ]
            while stack:
                name, it = stack[-1]
                advanced = False
                for dep in it:                       # 迭代器本身带状态，break 后可续
                    if dep == name or dep not in known:
                        continue
                    state = color[dep]
                    if state == 0:
                        color[dep] = 1
                        pos[dep] = len(path)
                        path.append(dep)
                        stack.append((dep, iter(sorted(self._deps.get(dep, ())))))
                        advanced = True
                        break
                    if state == 1:                   # 回到栈上的节点 = 回边 = 环
                        record(path[pos[dep]:])
                if advanced:
                    continue
                stack.pop()
                color[name] = 2
                path.pop()
                pos.pop(name, None)
        return found

    # ── 拓扑 / 分层 ─────────────────────────────────────────
    def layers(self) -> list[list[str]]:
        """分层：同层无依赖关系、可并行；层内按名字排序。

        层号 = 到它最长依赖链的长度（`1 + max(level(deps))`），
        这不是随便选的定义：它是**最大并行度**的层划分，同时保证
        「A 做完才能做 B」在层号上严格体现。
        """
        self._require_valid()
        names = sorted(self._nodes)
        if not names:
            return []

        level: dict[str, int] = {name: 0 for name in names}
        pending: dict[str, set[str]] = {name: set(self._deps[name]) for name in names}
        dependents: dict[str, list[str]] = {name: [] for name in names}
        for name in names:
            for dep in self._deps[name]:
                dependents[dep].append(name)

        queue: list[str] = [name for name in names if not pending[name]]
        processed = 0
        while queue:
            name = queue.pop()
            processed += 1
            for child in dependents[name]:
                if level[name] + 1 > level[child]:
                    level[child] = level[name] + 1
                pending[child].discard(name)
                if not pending[child]:
                    queue.append(child)

        if processed != len(names):       # validate() 已拦环；这里是兜底，绝不静默截断
            raise DAGValidationError(["成环: 拓扑排序未能覆盖全部节点"])

        depth = max(level.values()) + 1
        out: list[list[str]] = [[] for _ in range(depth)]
        for name in names:                # names 已排序 → 层内天然按名字排序
            out[level[name]].append(name)
        return out

    def topo_order(self) -> list[str]:
        """拓扑序 = `flatten(layers())`。

        **刻意用 layers 展平来实现**，而不是另写一个 Kahn：
        两个视图必须逐元素相等，否则调用方按哪个都不是。
        （注意：Kahn + 最小堆的「全局最小优先」与分层展平并不等价，
        例如 a→(b,c) 且 d 孤立时前者给 a,b,c,d、后者给 a,d,b,c。这里选后者，
        因为「同层按名字排序」这条约束本来就是按层说的。）
        """
        return [name for layer in self.layers() for name in layer]

    def ready_nodes(self, done: set[str] | frozenset[str] | None) -> list[str]:
        """当前可执行节点：依赖全部 done、自己未 done。按名字排序。"""
        finished = set(done or ())
        return [
            name for name in sorted(self._nodes)
            if name not in finished and all(dep in finished for dep in self._deps[name])
        ]

    # ── 内部 ────────────────────────────────────────────────
    def _require_valid(self) -> None:
        errors = self.validate()
        if errors:
            raise DAGValidationError(errors)


# ══════════════════════════════════════════════════════════════
# DAGRunner：按层调度 + 失败短路
# ══════════════════════════════════════════════════════════════
class DAGRunner:
    """把静态 DAG 跑成一条确定性流水线。

    调度语义（顺序即语义，不允许含糊）：

    1. `validate()` 有错 → `DAGValidationError`，**一个节点都不派发**；
    2. 逐层执行 `layers()`；层号是屏障：第 k 层全部落定，第 k+1 层才开始；
    3. 层内 `max_parallel == 1` 时串行（名字序）；`> 1` 时 `ThreadPoolExecutor`
       并行，但**结果按名字序收**（完成先后不影响账本顺序）；
    4. 节点失败（`ok=False` / dispatch 抛异常）→ 依赖它的后继**短路标记 skipped**，
       但**同层已启动的节点继续跑完**——短路是「别再派发新的」，
       不是「把在跑的线程掐掉」（kill 线程 ＝ 资源泄漏 + 半写上下文）；
    5. 异常隔离：`runner.dispatch` 抛任何异常都收敛成该节点的 `ok=False`，
       带 `UNRECOVERABLE` 与原始异常文本，不炸整个 DAG。
    """

    def __init__(self, runner: Any, *, tracer: Any = None, max_parallel: int = 1) -> None:
        if not callable(getattr(runner, "dispatch", None)):
            raise DAGExecutionError(
                "runner 必须提供可调用的 dispatch(spec, parent_ctx, parent_e)，"
                f"收到 {type(runner).__name__}（可直接传 plane.workflow.SubagentRunner）")
        self._runner = runner
        self._tracer = tracer
        try:
            workers = int(max_parallel)
        except (TypeError, ValueError):
            workers = 1
        self.max_parallel = max(1, workers)
        #: 最近一次运行的报告（外部想看 skipped 明细/状态时用）
        self.last_run: dict = {}
        self.last_status: dict[str, str] = {}

    # ── 对外入口 ────────────────────────────────────────────
    def run(
        self,
        spec: DAGSpec,
        parent_ctx: Any,
        parent_e: Any,
        goals: dict[str, str] | None = None,
        *,
        skip: Sequence[str] = (),
        skip_results: Mapping[str, dict] | None = None,
    ) -> dict:
        """跑一条 DAG。

        `skip`：**已经完成的节点名**（续跑用）。它们不重新执行，直接标 OK，
        其下游节点因此可以继续。这是"续跑"能成立的唯一方式 ——
        重放已完成的节点会重复产生副作用（重复发版、重复删库）。

        `skip_results`：跳过节点的结果（用于回填到 report，让审计看到
        "这一步上次做过，结果是什么"）。不给就只记 `resumed_ok`。
        """
        dag = DAG(spec)
        errors = dag.validate()
        if errors:                                   # 静态失败：不执行任何节点
            raise DAGValidationError(errors)

        overrides: dict[str, str] = dict(goals or {})
        # 与回合内编排互斥：两者都会回写 observations，并发写会让
        # 顺序与归属不确定（见 ContextHandle.bind_engine）。
        bind = getattr(parent_ctx, "bind_engine", None)
        if callable(bind):
            try:
                bind("dag")
            except Exception as exc:                           # noqa: BLE001
                raise DAGExecutionError(
                    f"该 Context 已绑定另一个编排引擎，不能再跑 DAG：{exc}") from exc
        layers = dag.layers()

        status: dict[str, str] = {}
        results: dict[str, dict] = {}
        order: list[str] = []

        # 续跑：把已完成的节点预置成 OK，下游因此解锁，且它们**不会被重新执行**。
        already = {str(x) for x in (skip or ())}
        for name in already:
            status[name] = ST_OK
            prev = (skip_results or {}).get(name)
            results[name] = dict(prev) if isinstance(prev, Mapping) else {
                "name": name, "ok": True, "steps": 0,
                "text": "", "error": "", "term_reason": "RESUMED",
                "resumed_ok": True,
            }

        for index, layer in enumerate(layers):
            runnable: list[str] = []
            for name in layer:
                if name in already:
                    order.append(name)      # 已完成的仍进 order（顺序即审计）
                    continue
                blocked = [dep for dep in dag.deps(name) if status.get(dep) != ST_OK]
                if blocked:
                    # 短路决策在**派发之前**做：依赖没跑赢，就不启动
                    status[name] = ST_SKIPPED
                    results[name] = self._skipped_payload(name, blocked)
                else:
                    runnable.append(name)            # layer 已按名字排序
            if not runnable:
                continue

            payloads = self._execute_layer(
                dag, runnable, index, overrides, parent_ctx, parent_e, results)
            for name in runnable:
                payload = payloads[name]
                results[name] = payload
                status[name] = ST_OK if payload.get("ok") else ST_FAILED
                order.append(name)

        report = {
            "ok": sum(1 for s in status.values() if s == ST_OK),
            "failed": sum(1 for s in status.values() if s == ST_FAILED),
            "skipped": sum(1 for s in status.values() if s == ST_SKIPPED),
            "results": results,
            "order": order,
            "layers": [list(layer) for layer in layers],
            "short_circuited": any(s == ST_SKIPPED for s in status.values()),
        }
        self.last_run = report
        self.last_status = dict(status)
        # **把执行事实写进 Context**（`workflow_state`，owner=WORKFLOW）。
        #
        # 为什么必须写：`DAGRunner` 原先把 `parent_ctx` 只透传给 `dispatch`，
        # 自己一个字都不写（实测 `ctx.apply` 计数 0）。后果是"跑到第几层、
        # 哪个节点被跳过、为什么跳过"没有任何地方记录 —— 出事后无法复盘。
        #
        # 为什么写 `workflow_state` 而不是 `observations`：后者是回合内观测事实
        # （owner=LOOP）。两者混用会让工作流编排（B）与回合内编排（C）互相覆盖。
        self._stamp_workflow_state(parent_ctx, report)
        return report

    def _stamp_workflow_state(self, parent_ctx: Any, report: dict) -> None:
        """落一份可复盘的工作流事实。**失败不影响执行**（观测面 fail-open）。"""
        apply = getattr(parent_ctx, "apply", None)
        if not callable(apply):
            return
        try:
            from contracts import Owner, Patch
        except Exception:                                      # noqa: BLE001
            return
        rep = report if isinstance(report, dict) else {}
        nodes = rep.get("results")
        raw_layers = rep.get("layers")
        # `report["layers"]` 是**层的列表**（`[['a'], ['b','c']]`），不是数量。
        # 早先按 int 处理会直接 TypeError —— 而这是观测面，不该因为形状猜错就炸。
        layers = (len(raw_layers) if isinstance(raw_layers, (list, tuple))
                  else int(raw_layers or 0))
        state = {
            "dag": str(rep.get("name") or ""),
            "layers": layers,
            "layer_nodes": [[str(x) for x in layer]
                            for layer in (raw_layers or ()) if isinstance(layer, (list, tuple))],
            "order": [str(x) for x in (rep.get("order") or ())],
            # `failed` / `skipped` 在 report 里是**计数**（数字），不是名单。
            # 早先按列表迭代会 TypeError —— 观测面不该因为形状猜错就炸。
            "failed_count": int(rep.get("failed") or 0),
            "skipped_count": int(rep.get("skipped") or 0),
            "short_circuited": bool(rep.get("short_circuited")),
            "ok": bool(rep.get("ok")),
            # 节点 payload 的真实形状是 `{ok, steps, text, error, term_reason}`，
            # **跳过的节点靠 `term_reason == "SKIPPED"` 标识**（没有 status/skipped 字段）。
            # 早先按"我以为是"的字段读，结果全是空串 —— 观测面读错字段比不读更坏：
            # 它看起来有内容，实际什么都不对。
            "nodes": [
                {"name": str(k),
                 "ok": bool((v or {}).get("ok")),
                 "skipped": str((v or {}).get("term_reason") or "") == "SKIPPED",
                 "term_reason": str((v or {}).get("term_reason") or ""),
                 "steps": int((v or {}).get("steps") or 0),
                 "error": str((v or {}).get("error") or "")[:200]}
                for k, v in (nodes or {}).items()
            ] if isinstance(nodes, dict) else [],
        }
        try:
            apply(Patch("set", "workflow_state", Owner.WORKFLOW, state))
        except Exception:                                      # noqa: BLE001
            pass

    # ── 层执行（串行 / 并行，唯一区别是调度器）───────────────
    def _execute_layer(
        self,
        dag: DAG,
        runnable: list[str],
        layer_index: int,
        goals: dict[str, str],
        parent_ctx: Any,
        parent_e: Any,
        upstream: Mapping[str, dict] | None = None,
    ) -> dict[str, dict]:
        names = list(runnable)
        workers = min(self.max_parallel, len(names))
        if workers <= 1:                             # 串行：名字序即执行序
            return {
                name: self._run_node(dag, name, layer_index, goals, parent_ctx,
                                     parent_e, upstream)
                for name in names
            }

        out: dict[str, dict] = {}
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                name: pool.submit(
                    self._run_node, dag, name, layer_index, goals, parent_ctx,
                    parent_e, upstream)
                for name in names
            }
            # 关键：即便第一个取回的结果是失败，也照样把同层所有 future 都 `.result()`
            # 收完——不 cancel、不提前 shutdown，已启动的节点一定跑完。
            for name in names:
                try:
                    out[name] = futures[name].result()
                except BaseException as exc:         # noqa: BLE001 - 线程池异常也要收敛
                    out[name] = self._error_payload(name, exc)
        finally:
            pool.shutdown(wait=True)                 # 等线程收尾，绝不留悬空线程
        return out

    # ── 单节点 ──────────────────────────────────────────────
    def _run_node(
        self,
        dag: DAG,
        name: str,
        layer_index: int,
        goals: dict[str, str],
        parent_ctx: Any,
        parent_e: Any,
        upstream: Mapping[str, dict] | None = None,
    ) -> dict:
        node = dag.node(name)
        goal = self._goal_for(dag, node, goals, name)
        goal = _with_upstream(goal, dag.deps(name), upstream or {})
        attrs = {
            "dag": dag.name,
            "node": name,
            "layer": layer_index,
            "agent": _as_text(_field(node, "agent", "")),
            "goal_len": len(goal),
        }
        with self._span(NODE_SPAN, attrs):
            # allowed_tools=() 空 = 只读（INV-18：子 Agent 权限只能更窄）
            sub = SubagentSpec(name=name, goal=goal, allowed_tools=())
            try:
                raw = self._runner.dispatch(sub, parent_ctx, parent_e)
            except Exception as exc:                 # noqa: BLE001 - 单节点崩溃不传染
                return self._error_payload(name, exc)
            return self._payload(name, raw)

    @staticmethod
    def _goal_for(dag: DAG, node: Any, goals: dict[str, str], name: str) -> str:
        """goals 覆盖 > node.goal > `{spec.name}:{node}`。空串算「没给」。"""
        candidates = [goals.get(name), _field(node, "goal", "")]
        for cand in candidates:
            if isinstance(cand, str) and cand.strip():
                return cand
        return f"{dag.name}:{name}"

    # ── 结果塑形（统一成本契约里的 to_dict 形状）─────────────
    @staticmethod
    def _payload(name: str, raw: Any) -> dict:
        data: Any = None
        to_dict = getattr(raw, "to_dict", None)
        if callable(to_dict):
            try:
                data = to_dict()
            except Exception:                        # noqa: BLE001 - 坏 to_dict 不炸流水线
                data = None
        if not isinstance(data, dict):
            if isinstance(raw, Mapping):
                data = dict(raw)
            else:
                data = {
                    "name": name,
                    "ok": bool(getattr(raw, "ok", False)),
                    "summary": _as_text(getattr(raw, "summary", "")),
                    "artifacts": list(getattr(raw, "artifacts", ()) or ()),
                    "steps": int(getattr(raw, "steps", 0) or 0),
                    "term_reason": _as_text(getattr(raw, "term_reason", "")),
                    "error": _as_text(getattr(raw, "error", "")),
                }
        data["name"] = name                          # 账本按 DAG 节点名对齐
        data["ok"] = bool(data.get("ok", False))
        return data

    @staticmethod
    def _error_payload(name: str, exc: BaseException) -> dict:
        return SubagentResult(
            name=name,
            ok=False,
            error=f"dispatch 异常: {type(exc).__name__}: {exc}",
            term_reason=TermReason.UNRECOVERABLE.value,
        ).to_dict()

    @staticmethod
    def _skipped_payload(name: str, blocked: list[str]) -> dict:
        return SubagentResult(
            name=name,
            ok=False,
            error="上游未完成，节点被短路跳过: " + ", ".join(sorted(blocked)),
            term_reason=SKIP_TERM,
        ).to_dict()

    # ── tracer（可缺省、fail-open）────────────────────────────
    def _span(self, name: str, attrs: dict) -> Any:
        tracer = self._tracer
        if tracer is None:
            return contextlib.nullcontext()
        span = getattr(tracer, "span", None)
        if not callable(span):
            return contextlib.nullcontext()
        try:
            return span(name, attrs)
        except Exception:                            # noqa: BLE001 - 观测坏了不许影响执行
            return contextlib.nullcontext()
