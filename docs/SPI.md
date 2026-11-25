# Runtime 提供的扩展点（SPI）清单

> **这份文档是给上层（Agent 层）实现者看的。**
> Runtime 只定义"我会怎么调你"，不实现任何业务内容。照着这里的契约实现，
> 不需要读 Runtime 源码。
>
> 全部定义在 `contracts.py`，由 `tests/test_spi_contracts.py` 守住
> （纯 Protocol、无实现代码、无业务词汇）。

## 一、谁是 Runtime，谁是 Agent 层

判据只有一条：**判断的是「形状」还是「内容」。**

| | Runtime | Agent 层 |
|---|---|---|
| 管什么 | 有几个字段、上限多少、权限够不够、何时终止 | 这句话是什么意思、该拆几步、调哪个工具 |
| 知道业务吗 | **不知道**（`tests/test_architecture.py` 禁止业务词） | 全是业务 |
| 换业务要改吗 | 不改 | 全改 |

举例：Memory 的保留矩阵（几天淘汰、什么类型留多久）是 Runtime；
「这句话算偏好还是事件」是 Agent 层（实现 `Extractor`）。

---

## 二、Agent 层要实现的三个扩展点（v7 新增）

在 v7 之前，这三处只是**鸭子类型**——文档写"有 `replan(...)` 即可"，
上层要独立实现时没有任何可编译的契约，只能去读 Runtime 源码猜签名。
现在冻结成 Protocol。

### 2.1 `PlannerProtocol` — 规划

```python
class PlannerProtocol(Protocol):
    def replan(self, snapshot: Mapping[str, Any], reason: str) -> Patch | None: ...
```

**Runtime 什么时候调你**：Loop 连续多步无进展（死锁）时，给一次机会换方案。

注意这**不是"每步都规划"**——每步做什么由 LLM 在当轮自行决定（ReAct）。
Planner 只在"沿用当前方案已经不收敛"时介入。

**契约要点**：

| 要点 | 为什么 |
|---|---|
| 返回 `Patch`，不是直接操作 Context | Runtime 校验 owner 后 `ctx.apply()`。让 Planner 直接写等于绕开 OWNER_TABLE 与审计 |
| 返回 `None` = "我也想不出" | Loop 据此判 `NO_PROGRESS`，不会无限重试 |
| 抛异常不算致命 | Runtime 记 warning + 写系统消息，继续跑。规划失败不该让整个回合失败 |
| `snapshot` 是只读快照 | 改不到真实状态 |

**启动就校验**：`Loop(planner=obj)` 时若 `obj` 没有可调用的 `replan`，立刻 `TypeError`。
不留到死锁那一刻——死锁本来就是补救路径，补救工具坏了会被误判成"再规划也没用"。

**放在哪**：`agents/`（业务内容）。可参考素材：`plane/dag.py`（静态 DAG 执行）。

### 2.2 `RouterProtocol` — 选 Agent

```python
class RouterProtocol(Protocol):
    def route(self, goal: str, principal: Principal) -> RouteDecision: ...
    def explain(self, goal: str, principal: Principal) -> list[dict]: ...
```

Runtime 已提供默认实现 `plane/router.py.AgentRouter`（goal 匹配 + 描述相似 +
scope 过滤 + tag 命中 + priority，加权四项，score ∈ [0,1]）。

**什么时候自己实现**：需要完全不同的策略（embedding 相似度、A/B 分流、租户白名单硬指定）。

**实现方必须遵守的三条**（Runtime 与客户端都依赖）：

1. **回澄清用哨兵 `RouteDecision.agent == "__clarify__"`**，不是空串。
   哨兵去查 Agent 表必然查不到 → 立刻炸，不存在"忘了处理然后拿它当 Agent 名"。
2. **没有候选时不许抛**，返回 `__clarify__` + `confidence=0.0`。
3. **`explain()` 要给每个候选的分项得分**（`text_match` / `tag_match` /
   `scope_ok` / `priority_norm` / `score` / `eligible`）。线上排查全靠它。

### 2.3 `SkillHandlerProtocol` — 技能执行

```python
class SkillHandlerProtocol(Protocol):
    def __call__(self, args: Mapping[str, Any], ctx: ExecCtx) -> Any: ...
```

**Tool 与 Skill 的区别**（最容易混的一处）：

| | Tool | Skill |
|---|---|---|
| 粒度 | 原子能力（一次 HTTP、一次 SQL） | 多 Tool 编排（"发布一次变更"= 查状态→备份→执行→验证） |
| Runtime 管什么 | 权限、审批、归一化 | 配额、审计、深度限制 |
| 谁拆步骤 | — | **Skill 自己**（Runtime 不拆） |

拿 `ExecCtx` 是为了做多步编排时能看预算与截止时间，但**拿不到 Dispatcher 之外的写权限**。

---

## 三、Runtime 已内置的实现（可换，不必自己写）

| SPI | 内置实现 | 换的时候注意 |
|---|---|---|
| `LLMProtocol` | `adapters/openai_compat`（OpenAI 兼容协议）、`llm_mock` | 前缀可配 `AGENT_RUNTIME_LLM_PREFIX` |
| `BuilderProtocol` | `builder.PromptBuilder` | token 预算在这里 |
| `DispatcherProtocol` | `plane/tool.Dispatcher` | 权限/审批/归一化。**换实现必须申报 `capabilities()`**，否则启动即被拒（见下） |
| `TracerProtocol` | `plane/quality.Tracer` | 可导出 OTel / Langfuse |
| `ContextStore` | memory / file / sqlite / redis | 多副本必须 sqlite 或 redis |
| `KV` | `InMemoryKV` / `RESPKV` | 多副本必须 `RESPKV` |
| `VectorBackend` | memory / qdrant / milvus | milvus 是**最终一致**，写完要 `flush()` |
| `EmbeddingProvider` | `adapters/embeddings` | 降级到 bigram 时有记账 |
| `DocumentSplitter` | heading（默认）/ llama-index | llama-index 带 ~40 个传递依赖 |
| `SandboxBackend` | process / container | container 需 docker，缺镜像会明确报错 |
| `Extractor` | **无默认**（必须 Agent 层提供） | Memory 管道的入口 |

---

## 四、边界纪律（有测试守着）

`tests/test_architecture.py` + `tests/test_spi_contracts.py`：

1. `adapters/` **不许出现业务词汇**（连接池 / 告警 / 运维 / 排查 …）
2. `adapters/` 不许 import `agents/` 或 `demo/`
3. 反方向允许：Agent 层可以依赖 Runtime
4. **SPI 必须是纯 Protocol**：AST 校验每个方法体只有 `...`，没有实现代码
5. **SPI 的代码行不许出现业务词汇**（注释可以讲道理，签名不行）

> 这五条不是洁癖。ADR-8 记录过三处真实渗透：Memory 里的三条中文正则判断
> 「这句话是偏好还是事件」、Builder 里的 `【硬性约束】`、`llm_mock` 里的业务脚本。
> 后果都是"换个业务就要改 Runtime 源码"。

---

## 五、给上层的一句话

**你只需要实现 `Extractor` + `PlannerProtocol` + 你的工具/技能，
Router 用默认的就够。** Runtime 的 1770 个用例不依赖任何业务内容——
你实现了什么，它都不会因此变红。


---

## 六、Dispatcher 的保障自述（新增）

`Dispatcher` 是可替换的，但**换掉它会静默丢掉一整排保障**。

实测过一个"只调 handler"的天真实现：

| 保障 | 真 Dispatcher | 天真实现 |
|---|---|---|
| T2 审批 | `needs_confirmation` | `ok` |
| T3 硬拒 | `denied` | `ok` |
| 幂等缓存 | handler 跑 1 次 | handler 跑 3 次 |
| 预算记账 | `tool_calls_used=1` | `tool_calls_used=0` |
| 工具超时 | `timeout / 0.06s` | `ok / 0.40s` |

**运行期没有任何信号。** 一个只有只读工具的部署，某人把 dispatcher 换成
远程执行适配器，审批就没了，而回合照常返回 `ok`。

### 修法：启动期比对

```python
Gateway(..., required_dispatch=DISPATCH_CAPABILITIES)
gw.check_dispatch(dispatcher)   # 缺失 → DispatcherCapabilityError（启动期）
```

```python
class MyDispatcher:
    def capabilities(self) -> frozenset[str]:
        return frozenset({"scope", "schema", "timeout"})   # 诚实申报
```

**不申报 = 空集 = 全部缺失。** 宁可启动失败，也不要静默降级。

`tests/test_dispatch_capabilities.py` 逐项实测：申报了某项，就有对应的行为测试。

### 幂等三态

`ToolManifest.idempotent` 是 `bool | None`，判定统一走 `manifest.cacheable()`：

| 值 | 只读 | 写 | 有 `idempotency_key` |
|---|---|---|---|
| `None`（未声明） | 缓存 | **不缓存** | 写工具缓存（业务显式要求） |
| `True` | 缓存 | 缓存 | 缓存 |
| `False` | **不缓存** | 不缓存 | **不缓存**（显式压过一切） |

`per_turn_limit` 限**调用次数**（含缓存命中，防模型原地打转）；
`budget.tool_calls_used` 限**真执行**（缓存命中不占预算）。两道闸各管一件事。
