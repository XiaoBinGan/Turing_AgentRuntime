# 健壮性与热插拔评估

> 实测日期：2026-11。所有结论都有对应测试或实测命令，不靠推断。
> 对应测试：`tests/test_robustness.py`（30 例）、`tests/test_sse.py`（22 例）、
> `tests/test_wiring_extensions.py`（24 例）、`tests/test_architecture.py`（10 例）。

## 一、简装程度：分层可裁

**核心 Runtime 零第三方依赖可用** —— 17 个模块在纯 stdlib venv 里全部可导入并跑通完整回合。

```
纯 stdlib（0 个第三方包）
├── contracts / builder / core.{context,loop,gateway,store,streaming,summary}
├── plane.{tool,memory,knowledge,governance,quality,router,skill,workflow,dag}
└── 实测：Gateway 全链路 OK，Memory 读写 OK，FTS5 检索 OK
```

| 层 | 第三方依赖 | 不装会怎样 |
|---|---|---|
| **核心 Runtime** | **0** | — |
| 精确 token 计量 | `tiktoken` | 降级到启发式（已标定，中文误差 3%） |
| Prometheus 指标 | `prometheus_client` | `/metrics` 不可用 |
| 强 JSON Schema 校验 | `jsonschema` | 降级到内置子集校验器 |
| 分布式 KV | `redis` | 只能单进程（配额×副本数） |
| HTTP 服务 | `fastapi` | 只能当库用，不能起进程 |
| JWT 鉴权 | `pyjwt` | 只能用 API Key |
| YAML 工具配置 | `pyyaml` | 工具只能代码注册 |
| MCP 接入 | `mcp` | 接不上生态 |
| OTel 导出 | `opentelemetry-sdk` | 只有进程内 Tracer |
| Langfuse | `langfuse` | 无 LLM 可观测 |
| Qdrant / Milvus | `qdrant-client` / `pymilvus` | 只剩内存向量后端 |
| LlamaIndex 切分 | `llama-index-core` | 只剩 heading 切分器（+40 个传递依赖，值得犹豫） |

**结论：简装程度是够的，而且分层清晰。** 但有一处需要改：
`requirements.txt` 把 5 个写着"必需"，实测其中 `fastapi` 只在 HTTP 层需要、
`pyyaml` 只在 YAML 配置时需要。真正的"必需"只有 0 个 —— 应该按层重写那份文件。

## 二、健壮性：故障注入结果

### 2.1 LLM 侧

| 故障 | 行为 | 判定 |
|---|---|---|
| 抛异常（上游 503） | `UNRECOVERABLE` + 文本说明，不崩 | ✅ |
| 响应超过 deadline | **`DEADLINE` + `partial`**，保留已产出内容 | ✅（本轮修） |
| 返回空文本 | `NO_PROGRESS`（识别为无进展，不死循环） | ✅ |
| 返回错误类型（`object()`） | `TypeError` → `UNRECOVERABLE`，**不再当成答案** | ✅（本轮修） |

### 2.2 工具侧

| 故障 | 行为 | 判定 |
|---|---|---|
| handler 抛异常 | 记为 failed，回合继续 | ✅ |
| 工具超时 | `failed(retryable)` | ✅ |
| 返回 200KB 超大结果 | 按 `max_result_bytes` 截断 + 标记 | ✅ |
| T3（不可逆）被调用 | **硬拒**，带审批也拒 | ✅ |
| 重复调用幂等工具 | 返回缓存 + `deduped` 标记进 Prompt | ✅ |

### 2.3 存储侧

| 故障 | 行为 | 判定 |
|---|---|---|
| Memory Provider 不可用 | `RuntimeWiring` **fail-open**（答案照常返回） | ✅ |
| Context Store 不可用 | **fail-closed**（拒绝续跑，不假装成功） | ✅ |
| 未知后端名 | 报错，**不静默退化** | ✅ |

### 2.4 HTTP 侧

| 情况 | 状态码 | 判定 |
|---|---|---|
| 依赖故障（LLM 挂） | **502** | ✅（本轮修，之前是 200） |
| 任务没干完（预算/步数/无进展） | 200 + `phase=FAILED` | ✅ 刻意区分 |
| 缺字段 / 越狱 | 422 | ✅ |
| 未授权 / 凭据错 | 401 / 403 | ✅ |
| 请求体超限 | 413（读 header 就拒） | ✅ |
| 配额超限 | 429 + `Retry-After` | ✅ |
| 流式端点同一故障 | SSE `completed.ok=false` | ✅（本轮修） |

### 2.5 并发与资源

| 项 | 实测 |
|---|---|
| 100 线程并发同 Gateway | 全部完成，0 异常 |
| 配额在并发下 | 40 并发 + limit=10 → **恰好 10 个通过**，不被绕过 |
| 线程泄漏 | 50 回合后线程数不变 |
| 内存增长 | **`audit_log` 曾无界**（1000 回合 1000 条）→ 已改 `deque(maxlen=10000)` |

## 三、热插拔：12 个 SPI 全部可换

| 组件 | 实现 | 实测 |
|---|---|---|
| KV | `InMemoryKV` / `RESPKV` | ✅ 两实现接口一致 |
| ContextStore | memory / file / sqlite / redis | ✅ |
| LLM | mock / openai_compat（任意 OpenAI 兼容网关） | ✅ |
| Tokenizer | tiktoken / heuristic / count-only / HF | ✅ 缺依赖时降级 |
| MemoryProvider | InMemory / Vector / 自定义 | ✅ |
| VectorBackend | memory / qdrant / milvus | ✅ 含真集群 |
| DocumentSplitter | heading / llama-index | ✅ |
| SandboxBackend | process / docker / nsjail | ✅ 强度自报 |
| FtsIndex | SQLite FTS5 | ✅ |
| Extractor | **Agent 层**（OpsExtractor） | ✅ |
| Router | AgentRouter / KeywordRouter / 自定义 | ✅ |
| Planner | StaticDAG / LLM / 自定义 | ✅ |

**热插拔的关键不是"有接口"，是"换完之后行为一致"** —— 每个 SPI 都有契约测试
（`tests/test_spi_contracts.py`），且**都自报能力与降级原因**：

```
vector:qdrant    False  qdrant-client 未安装
splitter:llama-index False  llama-index-core 未安装
sandbox:nsjail   False  not found in PATH
```

> 「配了没生效且没人知道」是本项目反复踩的坑。所以每个可选组件都必须能回答
> 「我可用吗、为什么不可用」。

## 四、本轮修掉的 5 个真实缺陷

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 1 | `audit_log` 无界增长 | 长跑服务内存泄漏（1000 回合 +438KB 且不回收） | `deque(maxlen=10000)`，持久审计走 `audit_sink` |
| 2 | `_coerce_response` 接受任意对象 | 适配器 bug → 乱码被当成答案，`DONE` | 只接受 `LLMResponse/Mapping/str`，其余抛 `TypeError` |
| 3 | deadline 只在**每轮开始前**检查 | 响应迟到仍返回 `DONE`，硬截止被静默绕过 | LLM 返回后复查；超时 → `DEADLINE` + 保留内容 |
| 4 | 依赖故障返回 HTTP 200 | 模型挂了监控看不到（全是 200） | `term_reason=UNRECOVERABLE` → 502 |
| 5 | SSE 与 HTTP 对同一故障结论不一致 | JSON 客户端见 502，流式客户端见"成功" | `completed.ok=false` + `upstream_unavailable` |

## 四之二、本轮又修掉 5 个（第二轮）

| # | 缺陷 | 修法 | 实测 |
|---|---|---|---|
| 6 | **HTTP 超时只停止等待，线程继续跑完整回合** | ① `ExecCtx.cancel_event`（跨线程 Event）② HTTP 超时转**隐式 deadline**（×0.9，让 Gateway 能干净收口）③ adapter 的 socket 读超时按剩余 deadline 收紧 | 超时 0.5s + LLM 每步 0.2s×10 → **只调 1 次就停**（原会跑满 10 次） |
| 7 | **限流是每进程计数**（N 副本 ≈ 阈值×N） | `KvRateWindow`：用 KV 的原子 `incr` + TTL 做跨副本窗口 | 两"副本"共享同一 KV → 合计只放行 3 次（旧行为 6 次） |
| 8 | **`X-Forwarded-For` 可伪造** | `AGENT_RUNTIME_TRUSTED_PROXIES`：只有当 socket 对端在可信网段内才采信 XFF | 无可信配置→忽略 XFF；配了→按 XFF 分桶；对端不可信→仍用 socket 地址 |
| 9 | **工具结果窗口会丢早期证据 + 重复调用白占名额** | 按 payload 指纹**去重合并**（保留最新 + 标注重复次数），上限 40→80 | 96 次调用（含 48 次重复）→ 唯一证据可见率 58% → **100%** |
| 10 | **模型不收敛**（同一目标 4~8 步、26 次调用耗尽预算） | 提示词加收敛指引（别重复调、够了就下结论、最多 6 步）+ 每个工具 description 补"何时该停" + `query_logs`/`get_config` 支持批量 | 真模型实测：**10 步 22-27 次 → 4-6 步 5-6 次 DONE** |

### 关于「真中止」的真实边界（不要被"已修复"误导）

**能做到的**：超时后回合在**下一个检查点**立刻退出 —— adapter 每次重试前、每轮循环开头
都会看 `aborted`。实测"LLM 完全不检查 abort"的最坏情况也只多跑 1 步。

**仍做不到的**：**强杀一个卡在 `socket.read` 里的线程**。CPython 没有安全手段
（`PyThreadState_SetAsyncExc` 会在持锁/半写状态中断，比慢更危险）。
所以真正的兜底是 **socket 读超时**：`adapter._timeout_s()` 把 `min(timeout_ms,
剩余 deadline)` 交给 `urlopen(timeout=)` —— 读阻塞会在 deadline 处被 OS 打断。

> 三者的关系：`HTTP 超时 → 隐式 deadline → socket 读超时`。
> 链条上任何一环缺失，线程都可能活过超时。

## 四之三、第三轮：破坏性测试找出的 4 个真缺陷

用极端输入打 Runtime（超长 goal、畸形 principal、异常工具返回、并发提交、容量边界）。

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 11 | **`rate_limit_per_minute=0` = 全禁**（不是"不限流"） | 运营想关配额，结果**关掉整个服务**，日志只有 `rate_limited` | `limit<=0` 跳过限流判定；`remaining=-1` 表示无上限 |
| 12 | **workflow 节点数无上限** | 1000 节点的 DAG 被接受（实测 202），排队烧光线程/预算 | `max_length=64`（`DEFAULT_MAX_WORKFLOW_NODES`） |
| 13 | **budget 请求体无上限** | `token_total=10^12` 被接受 → 一次请求架空所有闸门 | `BudgetBody` 每项卡上限（200 万 token / 50 USD / 200 次 / 40 步） |
| 14 | **`budget` 以 pydantic 模型透传** | Gateway 吃 dict → "看起来有预算、实际一项没生效" | `to_gateway_request` 转回纯 dict，只带显式给了的键 |

### 共同特征：**单请求放大**

不是持续攻击，**一个请求就够**。而且没有上限时完全静默 ——
服务照常返回 202/200，只是后台开始烧预算/线程/内存。

`tests/test_request_amplification.py`（14 例）把三个入口都钉住。

### 内存确认有界

```
audit_log_maxlen=100 | 100→1000 回合：134KB → 137KB（增 3KB，约 4 B/回合）
```

封顶后是噪声级增长，不是线性。

## 四之四、第四轮：一个真实 flaky（已修复）

全套件连跑时**偶发**失败（6 次挂 1 次），单独跑 15 次全过 ——
典型的时间窗口型 flaky。

定位到 `test_12_server_crash_then_call_errors`：

```python
conn = self.started(env={"MCP_FIXTURE_DIE_AFTER_S": "0.4"})
conn.call("get_note", ...)      # "崩之前是好的"
```

**`0.4s` 是时间窗口**。全套件跑时 CPU 争抢，首次调用的 RPC 往返可能超过 0.4s，
于是"崩之前"那次调用正好撞上 server 已经死了 → 随机失败。

修法：给 fixture 加**按调用次数触发**崩溃的模式：

```
MCP_FIXTURE_DIE_AFTER_CALLS=2      # 第 2 次工具调用后硬崩
```

第 1 次调用一定成功、第 2 次一定把它弄崩 —— **没有时间窗口**。

> 一般规律：**测试里凡是用 sleep/超时来"制造时序"，就是 flaky 的种子。**
> 能改成"按事件计数"的，一律改。

修后连跑 15 次（模块级）+ 5 次（全套件）全过。

## 四之五、副作用落盘：从「只覆盖审批卡点」到「覆盖所有写操作」

### 原来的缺口

`checkpoint` 只在 `WAITING_USER`（等人工批准）时落盘。长回合中途崩溃就全丢：

```
回合: phase=FAILED | 工具抛异常 + LLM 挂了
落盘了吗: []        ← 空
```

真模型实测一个回合 4-6 步 / 26 次工具 / 40 秒。崩在第 5 步，前 4 步的工具调用全没 ——
**其中可能有"已经改了生产"的操作，而你不知道它到底做没做。**

### 修法：只在「非只读工具成功之后」落

```python
# contracts.py
@dataclass
class ExecCtx:
    checkpoint: Any = None      # (ctx, e, info) -> None

# core/loop.py —— 记账之后、失败回写之前
if outcome.kind == "ok" and manifest.side_effect != READ_ONLY:
    e.checkpoint(ctx, e, {"tool": ..., "args_hash": ..., "step": ...})
```

**为什么不"每步都落"**：

| | 只读 | 写 |
|---|---|---|
| 丢了 | 重跑就行（幂等且便宜） | **不知道它到底做没做** |
| 值得付写入成本 | ❌ | ✅ |

### 实测

**故障恢复的价值**（LLM 在第 2 步崩）：

```
崩溃回合: ok=False
崩溃后落盘: True
  恢复后看到工具结果: [('deploy', True)]
  观测: [('deploy', 'ok')]
  → 能知道 deploy 已执行: True
```

**开销**（每回合 5 次写操作，20 回合均值）：

```
无 store（不落盘）      1.1 ms/回合
MemoryStore          1.7 ms/回合      ← +0.6ms
```

### 三个关键决定

1. **`child()` 不继承 `checkpoint`**：子 Context 是 fork 出来的另一个 Context，
   用父的 `ctx_id` 落盘会把两份状态搅在一起（恢复时读到"父骨架 + 子局部"，两边都不对）。
2. **失败要上报，不能吞**：落盘失败写进 `vertex.checkpoint_errors` + 一条 warning。
   吞掉它，崩溃后就会看到"工具做了、状态里没有"——那是最难查的一类错。
   但**不把回合改成失败**：回合确实成功了，把成功改成失败是撒谎。
3. **判不出副作用也要落**：manifest 查不到时宁多写一次，不可漏掉一次写操作。

### 顺带修的一个静默 BUG

`Loop._manifest()` 回退路径把 `ToolRegistry.get()` 的返回值当 manifest 用 ——
但它返回 **`(manifest, fn)` 元组**。于是 `manifest.side_effect` 永远是 `None`，
"判不出副作用"→ 连只读工具都被落盘（多付写入成本，且掩盖了真实意图）。

## 五、仍然不足的地方（诚实）

1. **`audit_log` 默认 10000 条**仍会占内存（约 10~20MB）。要更省请配 `audit_log_maxlen`
   并把持久审计接到 `audit_sink`。
2. **仍不能强杀卡在 socket 上的线程**（CPython 限制）。但已有三道兜底：
   `cancel_event`（协作退出）、隐式 deadline、socket 读超时。实测最坏情况只多跑 1 步。
3. **分布式限流需显式开启**（`AGENT_RUNTIME_RATE_LIMIT_DISTRIBUTED=1` + `AGENT_RUNTIME_KV=redis`）。
   开了却拿到进程内 KV → **启动报错**；没开时 `/v1/info` 自述 `rate_limit_distributed=false`，
   让"多副本忘了开"这个错配可见。注意 KV 版是**固定窗口**（边界允许 2 倍突发），
   内存版才是滑动窗口 —— 取舍是"分布式原子性 > 窗口精度"。
4. **`X-Forwarded-For` 默认完全忽略**（安全默认）：只有配了
   `AGENT_RUNTIME_TRUSTED_PROXIES` 且对端在网段内才采信。配错的后果是"限流比预期更严"
   （所有人共享一个桶），方向是安全的。限流仍只是资源保护，不是鉴权。
5. **工具结果仍有两档压缩**：尾部 4 条完整 600 字符、更早 140 字符、总 80 条（去重后）。
   96 次调用的极端回合能覆盖 100% 唯一证据，但**超过 80 条唯一证据**仍会截断。
6. **收敛性已大幅改善但非保证**：真模型实测（deepseek-flash × 3）现为 **4/5/6 步全部 DONE**，
   此前是 4~8 步、26 次调用耗尽预算。但"模型策略本身在原地打转"这类仍只能靠提示词引导，
   不能机械保证。
