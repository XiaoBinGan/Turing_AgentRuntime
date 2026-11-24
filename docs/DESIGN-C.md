# 设计：C（Loop 原生编排）+ B（Workflow 显式入口）

> 目标：让「Leader 拆任务 → 并行子 Agent → 聚合，全程可见、可审批」成立，
> 且**不改 Loop 的终止条件与死锁检测主干**。
>
> 跳过 A（delegate 工具）：A 的能力是 C 的子集，先做 A 再升级 C 等于把
> `delegate` 重写一遍。C 直接对齐终态。

---

## 0. 现状盘点（已存在，不需要重写）

| 能力 | 在哪 | 状态 |
|---|---|---|
| 子 Agent 隔离（fork + 四层降权） | `SubagentRunner` | ✅ |
| 嵌套 Loop（带 6 终止条件） | `_default_runner` | ✅ |
| 并行 fan-out | `dispatch_many(max_workers)` | ✅ |
| 层级预算（切半 + 收紧） | `child()` + `_clamp_budget` | ✅ |
| 看门狗（外部确定性计数） | `max_agents` / `max_total_steps` | ✅ |
| 结果白名单回写 | `MERGE_WHITELIST` | ✅ |
| 补偿 | `Saga` | ✅ |
| 静态 DAG + 并行层 | `DAG` / `DAGRunner` | ✅ |
| 真增量流式 | `StreamingGateway`（tail patch_log） | ✅ |

**缺的三块**：并行工具执行、子 Agent 进度可见、引擎互斥。

---

## 1. C1：Loop 并行工具执行

### 问题

```python
for idx, call in enumerate(resp.tool_calls):        # core/loop.py:708
    outcome = self._dispatcher.invoke(call.name, call.args, e)
```

模型一轮发 5 个互不依赖的工具调用，串行跑 = 5× 延迟。这是真模型实测
「26 次调用 / 278 秒」的直接成因之一。

### 设计

```python
class Loop:
    def __init__(..., parallel_tools: bool = False, max_parallel_tools: int = 4)
```

开关默认 **False**（保守上线，行为不变）。开启后改写 `_execute`：

```
① 预算预扣（串行、原子）：先为每个 call 检查 tool_calls 预算。
   不够就只放行前 N 个，其余记 failed("budget exhausted")。
   —— 预算必须**先定**，不能"跑着跑着没预算了"（那会让并发的
      部分调用白花钱）。
② 线程池并发执行，每个 call 独立 try/except。
③ 结果**按输入顺序**收集（不按完成先后）——
   顺序即语义：`tool_results` 的顺序进 Prompt，重排会让模型推理漂移。
④ 逐条 apply patch（**主线程串行**）：
   Context 不是线程安全的写者友好结构，patch 必须由 Loop 线程统一 apply。
⑤ 审计：每条调用独立记 audit（沿用 `_audit_tool`）。
```

### 关键约束（每条都要测）

| 约束 | 理由 |
|---|---|
| **结果顺序 = 输入顺序** | 并发完成顺序不确定，但 Prompt 内容必须确定性（INV-2） |
| **Context 只在主线程写** | `apply` 非线程安全；并发 apply 会丢 patch |
| **预算预扣后再并发** | 否则并发终态取决于调度，不可复现 |
| **单条失败不影响其他** | `_coerce_outcome` 已兜底，但并发下异常传播要显式隔离 |
| **`cancel_event` 逐条检查** | 超时后未启动的调用直接跳过，不白跑 |
| **`per_turn_limit` 先算** | 并发下 `used >= limit` 的判定必须一次性做完，否则会超发 |
| **审批（T2）在并发前判定** | 需要审批的 call 不进线程池，直接返回 `needs_confirmation` |

### 不做的事

- **不做 tools 之间的依赖分析**：模型声明的是"这一轮我想调这些"，
  默认它们互不依赖。有依赖就该分多轮（模型自己会这么做）。
- **不做跨轮并行**：每轮一次 fan-out，步与步之间仍是串行（保持 FSM 语义）。

---

## 2. C2：子 Agent 进度冒泡（C 的核心价值）

### 问题

子 Agent 跑在工具调用内部，有自己的 Context。`StreamingGateway` 只 tail
**父 Context 的 patch_log** → 子 Agent 跑了 10 步，客户端一个字节都收不到，
只在最后收到聚合结果。

### 为什么不能靠 merge 回写解决

`_merge_back` 在**子 Agent 结束后**才写。那已经错过"实时"了。
而且让子 Agent 往父 Context 写 patch 会破坏 `OWNER_TABLE`（见 §4）。

### 设计：`ExecCtx.progress` —— 侧通道，不进 Context

```python
# contracts.py
class ProgressSink(Protocol):
    """进度侧通道。**只读播报，不改 Context**。

    为什么单独开一条通道而不是写 patch：
      * Context 的每个字段有唯一 owner（OWNER_TABLE）。子 Agent 没有
        父 Context 任何字段的写权，"写进去"必被拒。
      * 进度是**瞬时事件**，不是状态。进 patch_log 会污染轨迹与回放。
    """
    def emit(self, kind: str, data: dict) -> None: ...

@dataclass
class ExecCtx:
    ...
    progress: "ProgressSink | None" = None
```

`SubagentRunner` 在三个点 emit：

```python
e.progress.emit("subagent.start",  {"agent": spec.name, "goal_len": len(spec.goal)})
e.progress.emit("subagent.step",   {"agent": spec.name, "step": n})
e.progress.emit("subagent.finish", {"agent": spec.name, "ok": bool, "steps": n})
```

### StreamingGateway 侧：多源 drain

```python
def _drain_steps(self, ...):
    # 源 ①：patch_log（已有）
    # 源 ②：progress 队列（新）
```

`ProgressSink` 实现为一个无界队列（`queue.Queue`），由 `StreamingGateway`
在每次 tick 里清空并转成 `step` 事件：

```
event: step
data: {"kind": "subagent", "agent": "check-payments", "phase": "start", "bsq": 7}
```

**不占 seq 空间**？——占。所有事件共用一条 seq，客户端靠它检测丢帧。

### 线程安全

`emit` 会被 N 个线程池 worker 同时调用 → 用 `queue.Queue`（线程安全）。
队列无界有风险：子 Agent 暴走会堆事件。故：
- 队列上限 1024
- 满了**丢弃并计数**（`dropped`），在 `subagent.finish` 里带上丢弃数
  —— 宁可少播报，不能因为播报把主链路拖死

### 不做的事

- **不做逐 token 播报**：子 Agent 的 token 级流式需要 adapter 支持 `stream=True`，
  与本次无关。播报粒度是**步**。
- **不把子 Agent 的 tool_results 播出去**：只播"在做什么"，不播"查到了什么"
  （后者可能含敏感数据，且体积不可控）。要看细节走回来的聚合结果。

---

## 3. C3：审批在树内传播

### 问题

子 Agent 遇到 T2 工具（需审批）时，`_GuardedDispatcher` 会越权拒绝 →
子 Agent 白跑一趟。

### 设计

复用现有的 `pending_confirmation` + `WAITING_USER` 相位，但把"挂起"提升到树级：

```
子 Agent 遇 T2
  → 不拒绝，而是 emit("subagent.needs_approval", {tool, args_hash})
  → 子 Loop 转 WAITING_USER 并返回 partial
  → SubagentRunner 把它包成 SubagentResult(ok=False, needs_approval=True)
  → 父 Loop 收到后，**把父级也转 WAITING_USER**，pending_confirmation 带上
     "来自 subagent-x 的 tool-y"
  → 人类批准后，父 resume → 父重新 dispatch 该子 Agent（带 approvals）
```

### 分阶段

**C3a（先做）**：子 Agent 需要审批时，**不白跑**——返回结构化结果告诉父级
"卡在哪、需要批什么"，父级把这条信息放进 `pending_confirmation`。
人批准后重新发起整个 fan-out（幂等子 Agent 不会重复花钱）。

**C3b（后做）**：真续跑（保留子 Agent 的 Context，批准后从断点继续）。
需要子 Agent 的 ctx 落盘 + 引用，成本高；C3a 覆盖 90% 场景。

---

## 4. 引擎互斥：C 与 B 不能同时写同一个 Context

### 冲突点（实测）

```
MERGE_WHITELIST = ("observations", "artifacts")
OWNER_TABLE["observations"] = Owner.LOOP
```

- **C**：父 Loop 以 `Owner.LOOP` 写 observations
- **B**：`DAGRunner` → `dispatch` → `_merge_back` → `parent_ctx.merge(...)`

两者都能写同一个父 Context 的 observations → 顺序与归属不确定。

### 另一处真空

`grep -c "ctx.apply|Patch(" plane/dag.py` = **0**
—— `DAGRunner` 完全不写 Context。它把 `parent_ctx` 透传给 `dispatch`，自己不动手。

**后果**：DAG 跑到第几层、哪个节点被跳过、为什么跳过 —— **没有任何地方记录**。
出事后无法从 Context 复盘。

### 设计

**① 引擎互斥（运行时检查）**

```python
# 一个 Context 只允许挂一个编排引擎
ctx.bind_engine("loop")     # 或 "dag"，重复绑定不同值 → 报错
```

违反 → fail-closed 拒绝，不静默放行。

**② DAGRunner 补自己的轨迹**

DAG 不写业务字段，但**必须记自己的执行事实**。新增专用字段：

```
OWNER_TABLE["workflow_state"] = Owner.WORKFLOW   # 新 owner
```

内容：`{dag_id, layer, node, status, started_at, ended_at, skip_reason}`。

这样 B 可复盘，且与 C 的 observations 互不干扰。

---

## 5. B：Workflow 显式入口

### 端点

```
POST /v1/workflows              提交 DAGSpec → {workflow_id}
GET  /v1/workflows/{id}         查状态（层/节点/进度）
GET  /v1/workflows              列表
```

### 为什么 B 值得独立做

| | C（回合内编排） | B（显式 workflow） |
|---|---|---|
| 谁决定流程 | 模型 | 人（提交 DAGSpec） |
| 适合 | 开放式排查 | 发布/回滚等确定性流程 |
| 可审计 | 事后看 trace | **事前声明，事后比对** |
| 可续跑 | 靠 ctx_id | **一等公民** |

生产里的发布/回滚**不该让模型自由发挥** —— 必须走 B。

### 与 C 的关系

- B 的每个节点内部**可以**是一个 C（节点 = 一个子 Agent，内部自由编排）
- 两者通过 `workflow_state` 与 `observations` 分离，互不覆盖

---

## 6. 实施顺序与验收

| 阶段 | 内容 | 预估 | 状态 |
|---|---|---|---|
| **C1** | Loop 并行工具执行 | 半天 | ✅ **已完成**（见 §10 实测） |
| **C2** | `ExecCtx.progress` + 子 Agent 播报 | 1 天 | ✅ **已完成**（§11） |
| **C3a** | 审批需求冒泡到父级 | 半天 | ✅ **已完成**（§12） |
| **互斥** | `bind_engine` + `workflow_state` owner | 半天 | ✅ **已完成**（§13） |
| **B** | Workflow 端点 | 1 天 | ✅ **已完成**（§14） |

**总计约 3.5 天**，每阶段独立可验收、可回滚。

---

## 7. 不变量（每条都要有测试）

| # | 不变量 | 为什么 |
|---|---|---|
| INV-C1 | 并行工具结果**顺序 = 输入顺序** | Prompt 内容必须确定性，否则同输入两次输出不同 |
| INV-C2 | 预算/限额在并发**之前**预扣 | 否则终态取决于线程调度，不可复现 |
| INV-C3 | Context patch **只在主线程 apply** | `apply` 非线程安全 |
| INV-C4 | 进度事件**不进 patch_log** | 否则污染轨迹与回放 |
| INV-C5 | 进度队列满 → 丢弃 + 计数，**不阻塞** | 播报不能拖死主链路 |
| INV-C6 | 一个 Context 一个编排引擎 | 防两条路径写同一字段 |
| INV-C7 | `workflow_state` 是 DAG 的事实源 | 否则 DAG 无法复盘 |
| INV-C8 | 子 Agent 越权 → 拒绝且**不回写** | 现有语义，不能被 C 放松 |

---

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 并行工具打破现有 1991 个用例 | 默认 `parallel_tools=False`，行为完全不变；开启需显式配置 |
| 线程池耗尽（子 Agent 内部再并行） | 全局信号量：`max_parallel_tools × max_agents × 副本数` 有上限 |
| 进度事件把 SSE 撑爆 | 队列上限 1024 + 丢弃计数；播报粒度是步不是 token |
| C+B 归属冲突 | `bind_engine` fail-closed + `/v1/info` 自述当前引擎 |
| 子 Agent 白跑（需审批时） | C3a 让它返回结构化结果而非失败；幂等保证重发不重复花钱 |
| 真模型不收敛加剧（并行给更多机会乱跑） | 看门狗已有 `max_agents` / `max_total_steps`；且 C1/C2 让"乱跑"可见 |

---

## 9. 明确不做

1. **不做图执行器**：Loop 仍是线性 FSM，"并行"只发生在单步内部。终止条件、
   死锁检测主干一字不改。
2. **不做 A（delegate 工具）**：C 覆盖它的全部能力。
3. **不做 C3b（子 Agent 真续跑）**：C3a 覆盖 90% 场景，C3b 需要子 ctx 落盘。
4. **不做跨轮并行**：步与步之间保持串行。
5. **不做 token 级子 Agent 流式**：粒度是步。


---

## 10. C1 实施记录（已完成）

### 前置必修：Dispatcher 线程安全

`plane/tool.py` 的 `_cache` / `_turn_calls` / `budget.tool_calls_used` **原本无锁**。
并行一开就是静默失效：

| 无锁的后果 | 严重性 |
|---|---|
| `_turn_calls[key] = used + 1` 丢失更新 | `per_turn_limit` 被绕过（限 10 次能调 40 次） |
| 幂等缓存 check-then-act 竞争 | 同一个幂等工具被**真跑两次**（有副作用时是事故） |
| `budget.tool_calls_used += 1` 非原子 | 预算闸被削弱 |

修法：加 `RLock`，**只保护记账**（⑥⑦⑧ 合成一个原子决策），不包住 handler 执行。

### 实现要点

1. `Loop(parallel_tools=False, max_parallel_tools=4)` —— 默认关，行为与历史完全一致
2. `_execute` 分派到 `_execute_serial` / `_execute_parallel`
3. `_can_parallel()` 安全闸：**任一调用 tier ≥ T2 → 整轮退回串行**
4. `_record` / `_record_tool_result` / `_record_observation` / `_record_failure_history`
   抽成共用函数 —— 串行与并行必须写**完全相同**的东西（两份记账代码迟早会漂）

### 为什么 T2 必须退回串行

串行语义是「遇到第一个 `needs_confirmation` 就停下，**后续调用根本不执行**」。
并行会把后面的调用也跑掉 —— 那些可能有副作用。

> 「已经做了不该做的事」比「慢一点」严重得多。

（T3 不受此限：它是**硬拒**，handler 根本不会执行。但 `_can_parallel` 仍把 T3 排除，
保守起见 —— 万一将来 T3 语义变化，这里不会变成事故。）

### 实测

**确定性对照**（4 个工具各睡 0.3s）：

```
parallel=False → 1.22s
parallel=True  → 0.31s     （3.9×）
结果顺序 ['t0','t1','t2','t3'] 与输入一致
```

**真模型 A/B**（deepseek-flash，同一目标）：

```
parallel=0 → 38.0s | 6 步 | DONE
parallel=1 → 18.7s | 3 步 | DONE     （快 51%）
parallel=1 → 24.7s | 4 步 | DONE
```

步数也降了 —— 并行让一轮能做完更多取证，模型不必多轮往返。

### 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_RUNTIME_PARALLEL_TOOLS` | `1`（开） | `0` 回退到逐条串行 |
| `AGENT_RUNTIME_MAX_PARALLEL_TOOLS` | `4` | 单轮并发上限 |

### 测试

`tests/test_parallel_tools.py`（13 例）：顺序保序、确定性、真提速、真用多线程、
并发上限、T2/T3/未知工具的安全退回、单条失败隔离、`per_turn_limit` 并发不绕过、默认关闭。


---

## 11. C2 实施记录：进度侧通道（已完成）

### 设计落地

```python
# contracts.py
@runtime_checkable
class ProgressSink(Protocol):
    def emit(self, kind: str, data: Mapping[str, Any]) -> None: ...

@dataclass
class ExecCtx:
    progress: Any = None      # 侧通道，**不进 Context**
```

```python
# core/observability.py
class QueueProgressSink:      # 有界队列，满了丢弃并计数
    def emit(...)  def drain(limit=64)  @property dropped
```

```
SubagentRunner: subagent.start / finish / rejected / needs_approval
        ↓ (ExecCtx.progress)
StreamingGateway._drain_progress() → step 事件
        ↓
event: step  data: {"kind":"subagent","event":"subagent.start","agent":"check-api","dropped":0,"seq":5}
```

### 实测

```
总帧数 10 | subagent 帧 2
  {"kind":"subagent","event":"subagent.start","agent":"check-api","dropped":0,"seq":5}
  {"kind":"subagent","event":"subagent.finish","agent":"check-api","ok":true,"steps":2,"seq":6}
```

### 一个连带修掉的静默缺陷

`ExecCtx.child()` **不继承 `progress` 与 `cancel_event`**：

| 缺失 | 后果 |
|---|---|
| 不继承 `progress` | 层叠子 Agent（子再派子）的进度永远传不到客户端 |
| 不继承 `cancel_event` | 超时只停父层，子 Agent 继续烧 token（真中止失效） |

两者都是**静默失效**：功能看起来在，实际不生效。已修 + 4 条用例钉住。

---

## 12. C3a 实施记录：权限拒绝的结构化告知（已完成）

### 实测发现的关键事实

子 Agent 被**降权为只读**（INV-18），所以 T2 写工具在**静态白名单**阶段就被过滤，
根本到不了审批点。因此 C3a 的正确形态不是"等人工批准"，而是：

> **告知父级「它想写，但被降权拒了」**

### 实现

```python
# contracts.py
class SubagentResult:
    needs_approval: dict = field(default_factory=dict)
    # {"kind": "privilege_denied", "agent": ..., "reason": ...}
```

`SubagentRunner._resolve_allowlist` 现在会记 `_last_filtered`（被过滤掉的写工具），
拒绝时带上结构化原因；同时 emit `subagent.rejected`（否则客户端只看到子 Agent 凭空消失）。

### 为什么这比"只返回 ok=False"有用

| | 只有 ok=False | 有 needs_approval |
|---|---|---|
| 父级能区分"挂"vs"无权" | ❌ | ✅ |
| 人知道该批什么 | ❌ 翻日志 | ✅ 直接看到工具名 |
| 能否决定换 Agent 重做 | ❌ | ✅ |

---

## 13. 引擎互斥实施记录（已完成）

### 冲突点

```
MERGE_WHITELIST = ("observations", "artifacts")
OWNER_TABLE["observations"] = Owner.LOOP
```

C（回合内编排）与 B（DAG）都会写父 Context 的 `observations`
→ 顺序与归属不确定。

### 两个修法

**① 运行时互斥**

```python
ContextHandle.bind_engine("loop" | "dag")   # 冲突 → ContextError（fail-closed）
```

`Loop.run()` 绑 `loop`，`DAGRunner.run()` 绑 `dag`。`fork()` 出的子 Context 不继承
（它是另一个执行单元）。

**② 给 DAG 补自己的事实源**

```python
Owner.WORKFLOW                    # 新增 owner
OWNER_TABLE["workflow_state"] = Owner.WORKFLOW
```

`DAGRunner._stamp_workflow_state()` 写：层结构、节点顺序、跳过/失败计数、
每个节点的 `ok` / `term_reason` / `error`。

### 踩到的字段形状陷阱

第一次实现按"我以为是"的字段读 `report`，结果全是空串：

| 我以为 | 实际 |
|---|---|
| `report["layers"]` 是数字 | 是**层的列表** `[['a'],['b','c']]` |
| 节点有 `status` / `skipped` / `blocked_by` | 只有 `ok` / `term_reason` / `steps` / `error` |
| `report["failed"]` 是名单 | 是**计数**（数字） |

> **观测面读错字段比不读更坏** —— 它看起来有内容，实际全错。

已全部按实际形状修正，并加用例钉住真实字段名。

---

## 14. B 实施记录：Workflow 端点（已完成）

```
POST /v1/workflows            202 {workflow_id, status:"accepted"}
GET  /v1/workflows/{id}       层 / 节点 / 每个节点状态与错误
GET  /v1/workflows            列表（limit/offset，created_at 倒序）
```

- 请求体**扁平**（不套一层 `dag`）：`{name, nodes, principal}` —— 多包一层只是多一个拼错的机会
- 异步执行（`asyncio.to_thread`），容量有界（默认 200，淘汰最老的**已结束**项）
- 鉴权/配额与 `/v1/turns` 一致

### 实测（真端口）

```
POST → wf_f9b876d7ca53
GET  → status=done | layers=[['check'],['deploy']]
       check → done / deploy → done
GET 列表 → total=1
```

---

## 15. 全部完成状态

| 阶段 | 用例 | 状态 |
|---|---|---|
| C1 并行工具 | 13 | ✅ |
| C2 进度通道 | 15 | ✅ |
| C3a 权限告知 | 9 | ✅ |
| 引擎互斥 + workflow_state | 20 | ✅ |
| B Workflow 端点 | 43 | ✅ |
| **全量** | **2093** | ✅ 全绿 |

（另有 `verify.sh` 的 11 个端到端场景 80 条断言全 PASS）


---

## 16. 收尾：workflow 独立 Context（B 的复盘能力补齐）

B 的实现里 `parent_ctx=None`，导致 `workflow_state` 落不了盘（`DAGRunner` 的
stamping 是 fail-open，静默跳过）。这违背了 §4 的可复盘要求。

**修法**：每条 workflow 造一个**自己的** Context：

```python
ctx = ContextHandle(initial={"goal": f"workflow:{name}", "principal": ...})
ctx.bind_engine("dag")        # 与回合内编排互斥
```

**为什么是独立的而不是复用回合的**：workflow 是独立执行单元。与 C 共享同一个
Context 会撞 `bind_engine` 互斥（两者都写 observations）。

实测：

```
拿到 workflow Context: True
引擎绑定: dag
workflow_state 存在: True
  layers: 2 | nodes: ['a', 'b']
```

> 顺带修正了 B 自己的一条用例断言（它原本断言 `parent_ctx is None`，
> 那是旧行为的固化）。

---

## 17. 最终状态

```
2097 用例 / 11 场景 / 80 断言 / verify.sh 全绿
```

真端口联调：

| 能力 | 实测 |
|---|---|
| 回合 + 并行工具 + SSE | `accepted/started/step×5/completed` |
| Workflow 提交 → 查询 | `done`，两层节点都 `done` |
| `/v1/info` 自述 | `auth.mode=disabled`、`workflow.enabled=true, max_tracked=200` |


---

## 18. Workflow 持久化（补上 §14 的已知缺口）

### 之前的状态

状态只在内存：**进程重启即丢**。对"发布/回滚"这类长流程，等于
"做了一半没人知道"。

### 实现

```python
WorkflowRegistry(store=...)      # core.store 的三个实现都满足
```

| 时机 | 动作 |
|---|---|
| 提交（pending） | **先落盘再进线程池** |
| running / 节点变化 | 落盘 |
| 终态（done/failed） | 落盘 |
| 启动 | `restore()` 读回，未完成的标 `interrupted` |

键格式 `workflow__<id>`（双下划线）：`core.store` 的 `ctx_id` 只允许
`[A-Za-z0-9_.-]`，冒号会让 `save()` 直接抛。

### 关键决定：**不自动续跑，但支持显式续跑**

重启后**自动**重放 DAG 会重复执行已完成的节点 —— 发布流程里那是灾难。
所以：

1. 重启时只做「读回 + 标 `interrupted`」，**不自动重放**
2. 提供 `POST /v1/workflows/{id}/resume`，由**人**决定何时续、续不续

### 续跑语义（§19）

续跑 = **跳过已完成的节点**，只跑剩下的。这是它与"重跑一遍"的唯一区别。

### 实测（真端口 sqlite）

```
提交 wf_95bf31388024 → done
kill + 重启（同一 sqlite）
"event": "workflow.restored", "count": 1
GET → status: done | nodes: [('a','done'), ('b','done')]
```

### 踩到的两个 API 形状错

| 我以为 | 实际 |
|---|---|
| `store.save(key, payload, meta)` | `save(ctx_id, payload)` 只收两个 |
| `state.nodes` 是 list | 是 **dict[name → node]** |

两者都让恢复**静默失败**（`restore()` 里被 `except` 吞掉，`count=0`）。
所以恢复路径也必须有日志 —— 否则"没恢复"和"没有可恢复的"看起来一样。


---

## 19. Workflow 续跑（补完 §18 的"显式续跑"）

### 语义

```
续跑 = 跳过 status=='done' 的节点，只执行剩下的
```

这是它与"重跑一遍"的**唯一区别**，也是它能用在发布流程里的前提。

### 实现

`DAGRunner.run(..., skip=[...], skip_results={...})`：

```python
for name in already:
    status[name] = ST_OK        # 预置 OK → 下游解锁
    results[name] = prev        # 回填上次结果（审计要看"这一步上次做了什么"）
for index, layer in enumerate(layers):
    for name in layer:
        if name in already: continue      # **不重新执行**
```

`WorkflowRegistry.resume()`：

| 状态 | 行为 |
|---|---|
| `interrupted` / `failed` | ✅ 允许续跑 |
| `pending` / `running` | ❌ 409（重复 resume 会**双跑**） |
| `done` | ❌ 409（没有可续的） |
| 不存在 | ❌ 404 |
| spec 不在内存（进程重启后） | ❌ 409 并说清原因 |

### 实测

```
第一次: failed  [('a','done'), ('b','failed')]
resume → 202
续跑后: done    [('a','done'), ('b','done')]
实际执行: ['FAIL']   ← 不含 A，已完成的被跳过
```

真端口：

```
done 状态 resume → 409
未知 id        → 404
重启后 resume   → 409「只有 interrupted / failed 可以续跑」
```

### 一个必要的诚实边界

**spec 不持久化**。DAG 定义是代码产物，持久化它等于把代码复制进数据库
（改代码后旧 spec 会变得不可理解）。所以重启后 `resume` 会返回 409 并说明
"spec 不在内存"，而不是假装能续。

要跨重启续跑，调用方需要**重新提交同样的 DAG**（带上 `skip` 语义，
或直接接受重跑）。这是设计选择，不是遗漏。


---

## 20. Workflow 级 SSE（补完 §14 的第 4 条已知边界）

### 之前的状态

`GET /v1/workflows/{id}` 是同步快照 —— "跑到第几层"只能靠**反复轮询**。
运维盯发布时最想要的是"它现在在做什么"，不是"你再去问一次"。

### 实现：轮询式 SSE

```
GET /v1/workflows/{id}/stream

event: accepted   {"workflow_id":"wf_...","interval_ms":200}
event: progress   {"status":"running","progress":{...},"nodes":[...]}
event: progress   {"status":"done","progress":{"done":2,"total":2},...}
event: completed  {"ok":true,"status":"done","progress":{...},"resumed_count":0}
```

**为什么用轮询而不是事件总线**：

| | 轮询 | 事件总线 |
|---|---|---|
| 实现 | 读一次内存快照 | 每处状态写入点都要埋钩子 |
| 漏事件风险 | 零（下次读一定看到最新） | **漏一处就少一种事件** |
| 重连幂等 | 天然（既成事实一定会读到） | 需要事件回放 |
| 延迟 | = 轮询间隔（默认 200ms） | 即时 |

对"流水线跑到第几层"这个粒度，200ms 完全够用。

### 两条关键设计

1. **只在有变化时发帧**：`(status, [(node, status)...])` 做签名，相同就跳过。
   否则一条跑 5 分钟的流水线会刷 1500 条一模一样的事件。
2. **已完成的也能看**：客户端连上时它可能已经跑完了 —— 直接收到终态，
   而不是挂着等一个永远不会来的进度。

### 实测

```
HTTP 200 | text/event-stream
  ('accepted', None, None)
  ('progress', 'running', 0)
  ('progress', 'running', 1)     ← 节点完成数递增
  ('progress', 'done', 2)
  ('completed', 'done', 2)
```

### 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_RUNTIME_WORKFLOW_STREAM_MS` | `200` | 轮询间隔（ms），clamp 到 [20, 5000] |


---

## 21. 第四轮：B 的两个真实缺口（本轮修复）

### ① 节点之间**没有预算隔离**（单请求放大的又一种形态）

`GatewayNodeRunner.dispatch` 原来调 `gateway.handle({"goal": ..., "principal": ...})`
—— **不传 budget**。于是每个节点都用 Gateway 的默认额度（2 万 token / 20 次工具）。

```
64 个节点 × 2 万 token = 128 万 token
```

而它来自**一次请求**。放大器不是参数值，是节点数。

修法：每节点显式给预算（`min(父预算, 常量上限)`）：

```python
DEFAULT_NODE_TOKEN_TOTAL = 30_000
DEFAULT_NODE_TOOL_CALLS = 12
DEFAULT_NODE_MAX_STEPS = 8
```

可显式注入（`node_budget={...}`），也支持父预算均分。

### ② 下游节点**看不到上游结论** → "先检查再决定"根本跑不通

最典型的静态 DAG 形态：

```
check  →  "检查数据库连接数"
decide →  "根据检查结果决定是否扩容"   depend on check
```

实测：`decide` 收到的 goal 就是 `"根据检查结果决定是否扩容"` ——
**里面没有任何"检查结果"**。下游只能瞎猜。

修法：`DAGRunner` 把上游依赖的结论**追加**到下游 goal：

```
决定是否扩容

[上游节点结论]
- check: ok, 2 步，结论：连接池 20/20 已满
```

只带「成功/失败 + 步骤数 + 摘要（截断 400 字）」三样：

| 带什么 | 为什么 |
|---|---|
| 全量 payload | ❌ 工具回包可能几十 KB，塞进 goal 会撑爆预算 |
| 摘要 | ✅ 够下游判断"要不要继续" |
| 只带**自己声明的依赖** | ✅ 无关节点的结论不该混进来 |

**这是 Runtime 该做的事，不是业务判断** —— Runtime 不解释结果含义，
只负责"把事实传下去"。

### 顺带修的测试脆弱性

三个用例断言 `goal == "我给的字面量"`。注入上游后不再成立，改成
"首行等于字面量" / "包含关键字"。

> 一条更隐蔽的：`assertNotIn("A", goal)` —— 单字母 `"A"` 在 `"FAIL"` 里就有。
> 精确断言语义（`goal.splitlines()[0] == "FAIL"`）比模糊匹配可靠。
