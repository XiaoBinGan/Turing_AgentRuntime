# Agent Runtime 全景状态图

> 数据来源：`./verify.sh`（1602 用例 / 11 场景 / 80 断言全绿）
> 最后更新：施工完成态

## 一、整体架构（一条主线 + 四资源面 + 两保障面）

![架构全景](images/01-架构全景.png)


```mermaid
flowchart TB
    classDef entry fill:#1e3a5f,stroke:#4a9eff,color:#fff
    classDef main fill:#2d4a2d,stroke:#5cb85c,color:#fff
    classDef plane fill:#4a3d1e,stroke:#d4a017,color:#fff
    classDef guard fill:#5f1e1e,stroke:#ff6b6b,color:#fff
    classDef store fill:#3d2d4a,stroke:#9b59b6,color:#fff
    classDef ext fill:#333,stroke:#888,color:#ddd

    CLIENT(["外部客户端<br/>curl / SDK / 浏览器"]):::ext

    subgraph L0["① 接入层 —— 105 模块中的 2 个入口"]
        direction LR
        SERVE["serve.py · FastAPI<br/>55 用例"]:::entry
        AUTH["auth.py<br/>API Key + JWT<br/>81 用例"]:::entry
    end

    subgraph L1["② 请求编排"]
        direction TB
        SGW["SessionGateway<br/>会话承接 · 38 用例"]:::main
        STR["StreamingGateway<br/>真增量流式 · 22 用例"]:::main
        INST["InstrumentedGateway<br/>观测 + 关停 · 44 用例"]:::main
        GW["Gateway<br/>11 步准入 → 执行 → 收口<br/>59 用例"]:::main
    end

    subgraph L2["③ 执行内核"]
        direction LR
        PLAN["Planner"]:::main
        LOOP["Loop FSM<br/>6 终止条件 · 24 用例"]:::main
        CTX["Context<br/>patch log = 轨迹 = WAL<br/>39 用例"]:::main
        LLM["LLM 适配<br/>scripted / openai<br/>38 用例"]:::main
        BUD["PromptBuilder<br/>token 预算<br/>25 用例"]:::main
    end

    subgraph P1["④ 资源面（四个）"]
        direction TB
        TOOL["Tool Plane<br/>Dispatcher · 34 例<br/>MCP client 32 + transport 25"]:::plane
        MEM["Memory Plane<br/>多因子重排 · 45 例"]:::plane
        KNOW["Knowledge Plane<br/>23 例 + 切分器 20"]:::plane
        WF["Workflow Plane<br/>DAG 40 + 黑板 20"]:::plane
    end

    subgraph P2["⑤ 保障面（两个）"]
        direction TB
        GOV["Governance · 拦（fail-closed）<br/>RBAC / 输入护栏 / gate<br/>42 + 34 用例"]:::guard
        QUAL["Quality · 看（fail-open）<br/>Tracer / Eval / Replay<br/>24 + 11 + 7 用例"]:::guard
    end

    subgraph S1["⑥ 状态存储（三类按生命周期切分）"]
        direction LR
        SCTX["Context Store<br/>一次任务<br/>50 例"]:::store
        SSESS["Session Store<br/>一段对话"]:::store
        SMEM["Memory Store<br/>跨任务保留矩阵"]:::store
        SKNOW["Knowledge Store<br/>版本驱动"]:::store
    end

    subgraph S2["⑦ 基础设施（全部可换实现）"]
        direction LR
        KV["KV<br/>InMemory / RESP<br/>37 用例"]:::store
        VEC["VectorBackend<br/>memory / qdrant / milvus<br/>46 用例"]:::store
        FTS["FTS5 索引<br/>24 用例"]:::store
        DIST["分布式配额<br/>38 用例"]:::store
        SB["沙箱后端<br/>process / container<br/>46 + 33 用例"]:::store
    end

    subgraph EXT["⑧ 外部系统（均已真跑验证）"]
        direction LR
        MILVUS[("Milvus 集群<br/>v2.5.4 · 3 容器 ✅")]:::ext
        LANGFUSE[("Langfuse<br/>HTTP 207 + 读回 ✅")]:::ext
        MCPEXT[("MCP 生态<br/>stdio 子进程 ✅")]:::ext
        OTEL[("OTLP<br/>真 POST ✅")]:::ext
    end

    CLIENT --> AUTH --> SERVE
    SERVE --> SGW --> STR --> INST --> GW
    GW --> PLAN --> LOOP
    LOOP <--> CTX
    LOOP --> LLM
    LOOP --> BUD
    GW -.拦截.-> GOV
    LOOP --> TOOL
    LOOP --> MEM
    LOOP --> KNOW
    LOOP --> WF
    GW -.全程观测.-> QUAL
    CTX --> SCTX
    SGW --> SSESS
    MEM --> SMEM
    KNOW --> SKNOW
    MEM --> VEC
    KNOW --> VEC
    KNOW --> FTS
    GW --> KV
    GW --> DIST
    TOOL --> SB
    VEC --> MILVUS
    QUAL --> LANGFUSE
    QUAL --> OTEL
    TOOL --> MCPEXT
```

## 二、一轮请求的完整时序（含所有检查点）

![请求时序](images/02-请求时序.png)


```mermaid
sequenceDiagram
    autonumber
    participant C as 客户端
    participant A as auth.py
    participant S as serve.py
    participant SG as SessionGateway
    participant ST as StreamingGateway
    participant G as Gateway
    participant GV as Governance
    participant L as Loop
    participant X as Context
    participant Q as Quality

    C->>A: POST /v1/turns + 凭据
    A-->>C: 401 没给 / 403 不认 / 500 没配好
    A->>S: Principal
    S->>S: pydantic 校验（422）
    S->>SG: 带 session_id?
    SG->>SG: 取会话历史骨架
    SG->>ST: 注入历史骨架
    ST->>G: 转发（Instrumented 已包外层）
    G->>G: ①~⑦ 准入（形状/身份/RBAC/配额/输入护栏）
    Note over G,GV: 任一不过 → 拒绝，单条 rejected 事件
    G->>L: ⑧ 执行
    L->>X: apply(Patch)
    X-->>ST: 尾随 patch log → step 事件
    ST-->>C: SSE 增量推送（seq 单调）
    L->>GV: 每步工具调用前审批
    GV-->>L: 放行 / 需确认 / 拒绝
    L->>X: 写 tool_results / observations
    L->>L: 6 终止条件判定
    L-->>G: TurnResult
    G->>Q: 审计（不记 goal 原文）
    G-->>S: 
    S-->>C: 状态码按语义分流 + seq 序列
    SG->>SG: 成功才写回会话
```

## 三、三种失败姿态（这是设计核心）

![失败姿态](images/03-失败姿态.png)


```mermaid
flowchart LR
    classDef closed fill:#5f1e1e,stroke:#ff6b6b,color:#fff
    classDef open fill:#1e3d5f,stroke:#4a9eff,color:#fff
    classDef store fill:#3d2d4a,stroke:#9b59b6,color:#fff

    subgraph FAILCLOSED["fail-closed —— 宁可拒绝"]
        direction TB
        F1["Governance<br/>身份/RBAC/配额/输入护栏"]:::closed
        F2["Context 检查点<br/>无 store → 拒绝续跑"]:::closed
        F3["MCP 副作用<br/>判不出 → 保守 T2"]:::closed
        F4["context_seed 抛异常<br/>→ internal_error"]:::closed
    end

    subgraph FAILOPEN["fail-open —— 宁可降级"]
        direction TB
        O1["Quality / Tracer<br/>导出失败不影响回合"]:::open
        O2["Metrics<br/>render 失败不影响业务"]:::open
        O3["Langfuse 导出<br/>永远返回统计不抛"]:::open
        O4["会话写回失败<br/>答案照常返回"]:::open
    end

    subgraph NEVER["绝不静默 —— 必须报错"]
        direction TB
        N1["VECTOR_BACKEND 未知<br/>→ 报错不退化成 memory"]:::store
        N2["milvus 连不上<br/>→ 报错不假装 ready"]:::store
        N3["AGENT_RUNTIME_DB<br/>→ 两个别名都认"]:::store
        N4["TurnRequest.session_id<br/>→ 字段缺失就 422"]:::store
    end
```

## 四、验证覆盖（七道门禁）

![验证门禁](images/04-验证门禁.png)


```mermaid
flowchart TB
    classDef done fill:#2d4a2d,stroke:#5cb85c,color:#fff
    classDef opt fill:#4a3d1e,stroke:#d4a017,color:#fff
    classDef real fill:#1e3a5f,stroke:#4a9eff,color:#fff

    V["./verify.sh"]:::done

    V --> G1["① 语法编译<br/>106 模块"]:::done
    V --> G2["② 单元+集成<br/>1602 用例 / 41s"]:::done
    V --> G3["③ 契约自洽性"]:::done
    V --> G4["④ 端到端场景<br/>11 场景 / 80 断言"]:::done
    V --> G5["⑤ HTTP 服务面<br/>55 用例"]:::done
    V -.VERIFY_CLUSTER=1.-> G6["⑥ 真 Milvus 集群<br/>3 容器 · 46 契约 · 3min"]:::real
    V -.VERIFY_LANGFUSE=1.-> G7["⑦ 真 Langfuse<br/>投递+读回 · 1min"]:::real

    G6 --> R1["暴露 lite 没有的问题：<br/>load_collection 必需<br/>delete 软删影响 count<br/>最终一致需 flush"]:::opt
    G7 --> R2["暴露版本差异：<br/>v3 需 keeper 起不来<br/>SDK 5.x 对 v2 会 404<br/>改走 ingestion REST"]:::opt
```

## 五、真跑验证过的外部系统

| 系统 | 方式 | 证据 | 脚本 |
|---|---|---|---|
| **Milvus 集群** | etcd + minio + milvus v2.5.4 | 46 契约全过 | `scripts/verify_milvus_cluster.sh` |
| **Langfuse** | v2 服务端 + ingestion API | HTTP 207、4 条成功、读回 3 obs | `scripts/verify_langfuse.sh` |
| **OTLP** | 本地 HTTP 接收器 | 真 POST、protobuf 可解、鉴权头透传 | `tests/test_otel_delivery.py` |
| **MCP stdio** | 真起子进程 server | discover / call / 崩溃 / 超时 | `tests/test_mcp_transport.py` |
| **Docker 沙箱** | 真起容器 | kernel 级隔离 | `tests/test_sandbox_backends.py` |
| **HTTP 服务** | 真端口 18080~18093 | 状态码分流、SSE seq、SIGTERM drain | 手工 + `tests/test_serve.py` |
| **多轮会话** | 真端口 + sqlite | `session_turns=2`、落盘只含 `{role,text}` | 手工 |
| **真实 LLM** | babelark 网关 | 中文 235~256 token（vs tiktoken 480） | `scripts/real_model_smoke.py` |

## 六、已知边界（诚实清单）

| 边界 | 影响 | 是否阻塞上线 |
|---|---|---|
| ~~鉴权默认关闭~~ **已修** | 现在不配凭据 → `StartupRefused` 拒绝启动（fail-closed）。`create_app(authenticator=None)` 仍允许（那是给测试注入假件的缝），但 `main()` 挡住 | **否** —— 默认安全 |
| Langfuse 只验证到 ingestion 层 | SDK 5.x ↔ v2 服务端协议不通 | 否 —— 部署选 v3 即可 |
| 会话并发无序号互斥 | 同会话并发请求 `turn_index` 会重 | 否 —— 历史不丢 |
| MCP 只有 stdio | `streamable_http_client` 未验证 | 否 |
| ~~HTTP 层无速率限制中间件~~ **已修** | 现在有 `RateLimitMiddleware`（`AGENT_RUNTIME_HTTP_RATE`，每 IP 每分钟，超限 429）。实测 limit=3 时第 4 次即拒 | 否 |
| ~~租户配额不跨副本~~ **已修** | Gateway 加 `quota_counter`（Redis `INCR`）。实测 limit=4 时 2 副本共拒，不再 ×副本数 | 否 |
| ~~KV 惰性连接掩盖配置错误~~ **已修** | `RATE_LIMIT_DISTRIBUTED=1` 时启动即探活；Redis 不可达则**拒绝启动** | 否 |
| ~~FileStore 只 mkdir 不验可写~~ **已修** | 只读目录现在**构造期**就 `StoreError`，不会拖到首次 checkpoint | 否 |
| ~~schema 不认识就作废~~ **已修** | 加迁移表：逐级升级，缺迁移才拒，比本进程新的版本仍拒 | 否 |
| `tiktoken` 中文高估 1.9x | 非 OpenAI 模型走启发式 Tokenizer | 否 —— 已标定 |

## 七、规模

```
106 模块  /  48 测试文件  /  5 脚本  /  60393 行
1602 用例  /  11 场景  /  80 断言
必需依赖 5  /  可选依赖 5
```

| 层 | 模块数 | 用例数（估） |
|---|---|---|
| 接入（serve/auth） | 2 | 136 |
| 编排（gateway/session/streaming/instrument） | 5 | 163 |
| 内核（loop/context/builder/llm） | 5 | 126 |
| 资源面（tool/memory/knowledge/workflow） | 12 | 297 |
| 保障面（governance/quality） | 3 | 122 |
| 存储（store/kv/vector/fts/dist） | 8 | 307 |
| 适配器（embeddings/mcp/tokenizer） | 4 | 137 |
| 其他（架构/集成/并发） | — | 314 |

---

## 运维面验证状态

七项曾标为"未验证"，现已全部用可复现脚本验证：

| 能力 | 脚本 | 关键结论 |
|---|---|---|
| 多副本一致性 | `scripts/verify_multi_replica.sh` | 配额跨副本精确；修复了"每进程各算各的" |
| 容量基线 | `scripts/loadtest.py` | 单副本 145 RPS 平台化（GIL）；2 副本 1.72× |
| 备份恢复 | `scripts/verify_backup_restore.sh` | sqlite backup API + file；坏备份 fail-closed |
| schema 迁移 | `tests/test_store_migration.py` | 逐级迁移；写错的迁移函数会被抓 |
| SLO / 告警 | `docs/design/SLO.md`、`deploy/prometheus/` | 阈值来自实测；9 条规则 promtool 通过 |
| 值班手册 | `docs/design/RUNBOOK.md` | 每条含"不要做什么" |
| 故障演练 | `scripts/chaos_drill.sh` | 6 项姿态验证 |

### 演练中发现并修掉的四个缺陷

1. **租户配额不跨副本** —— `RATE_LIMIT_DISTRIBUTED` 只作用于 HTTP 层，
   Gateway 的每租户配额一直是进程内存。5 副本 `limit=60` 实际放行 300。
2. **KV 惰性连接掩盖配置错误** —— `RESPKV` 构造不碰网络，`shared` 恒为 True。
   于是"配了分布式 + Redis 连不上"能启动成功，第一个请求才失败。
3. **`FileStore` 只 mkdir 不验可写** —— 只读目录一路启动到首次 checkpoint 才失败，
   而那时可能已执行过有副作用的工具。
4. **`QuotaGuard` 没有"不限"语义** —— 与 `RATE_LIMIT=0` 口径不一致，
   压测时被默认 60/60s 拦住却报 `quota exceeded`，容易误判成 Gateway 配额没配。


---

## 跨项目审计发现的问题（2026-11-26）

对照一份同类实现（TypeScript）的问题清单自查本仓。**四条适用，其中两条是高危默认值。**

| 条目 | 本仓情况 | 处理 |
|---|---|---|
| P0-1 孤儿 tool 协议消息 | **结构上不可能** —— 出站是单条 `role:"user"`，工具结果渲染成文本，不发 `role:"tool"` | 无需修（代价见下） |
| P0-2 压缩盲切 | **存在**。随机 2000 次真实形态，59.2% 切出孤儿 tool 首条 | `_safe_cut` 退到单元边界，孤儿降到 0 |
| P1-1 CJK 高估 | **存在且是默认值**。`serve.py` 用保守档（1.0 tok/字），实测 0.638 → 高估 1.57×，`budget=20000` 只能用 12760 | 生产默认改标定档；保留 `AGENT_RUNTIME_TOKEN_PROFILE=conservative` 开关 |
| P1-2 缓存不算钱 | **存在**。adapter 解析了 `cache_hit_tokens`，`cost_of()` 忽略它，按全价计 | `Usage` 拆成 `total`（窗口）与 `billable()`（成本）；`cost_of` 收 `cache_hit_tokens` |
| P2-1 媒体 | **没做**（content 恒为纯文本） | 未做，如实记录 |
| P3-2 文档漂移 | 已有 `check_readme_facts.py` 门禁 | 本次新增 `check_config_defaults.py` 覆盖"默认值反了"这一类 |

### 一个真实代价：没有用原生 tool 协议

本仓把工具结果渲染成 Prompt 里的**文本块**，不是 OpenAI 的
`assistant.tool_calls` + `role:"tool"` 配对消息。

- **好处**：协议上不可能 400（没有配对要求）
- **代价**：模型看到的工具结果与普通文本同构，**原生 tool 往返的学习信号弱**

这是个明确的取舍，不是疏漏。要换成原生协议，得同时改
`builder.py`（消息化）与 `adapters/openai_compat.py`（出站映射），
并且必须补上 P0-1 那类配对校验。

### 方法论上的收获

`check_readme_facts.py` 查得出「模块数漂了」，查不出「默认值反了」。
后者更危险：文档读者会按文档相信行为，而代码走另一条路。

新增的 `check_config_defaults.py` 补上这一类：**凡是文档承诺的默认值，
都必须有一条断言指向真实装配路径**。
