# 架构落地清单（完工判据）

对照 `../agent-runtime-core-arch.md` 逐项核对。**这份清单就是「架构全部内容」的定义**：
每一项都必须有 ①模块 ②测试 ③端到端可见，缺一不算完成。

图例：✅ 已实现并有测试 ／ ⚠️ 部分实现 ／ 🔴 未实现

**当前状态：0 个 🔴，0 个 ⚠️。**

最后更新：全部完成（1602 用例 / 11 场景 / 80 断言 / 真 Milvus 集群 + 真 Langfuse 已验证）

**生产化加固（第二轮）**：并发安全 · SQLite 持久化 · 真实语义召回 · 隔离后端 SPI —— 见文末「生产化加固」。

---

## §0 核心等式

### 一条主线

| 组件 | 状态 | 模块 | 测试 |
|---|---|---|---|
| **Gateway**（Identity/Tenant/Quota/Audit/流式/输入护栏） | ✅ | `core/gateway.py` | `test_gateway.py` 59 例 |
| **Router**（Agent 选择 + confidence + 澄清回退） | ✅ | `plane/router.py` | `test_router.py` 37 例 |
| Planner（plan / replan） | ✅ | `core/loop.py`（`planner=` 注入） | `test_loop.py` 13/13b |
| Agent Loop（有界 FSM · 6 类终止条件） | ✅ | `core/loop.py` | `test_loop.py` 24 例 |
| Runtime Context（patch + owner + 可序列化） | ✅ | `core/context.py` | `test_context.py` 39 例 |
| Context Builder（纯函数 + 分源预算） | ✅ | `builder.py` | `test_builder.py` 15 例 |
| LLM Adapter（多模型归一） | ✅ | `adapters/openai_compat.py` | `test_openai_adapter.py` 38 例 + 真模型 |

### 四个资源面

| 面 | 状态 | 模块 | 测试 |
|---|---|---|---|
| **Tool Plane** | ✅ | `plane/tool.py` `plane/sandbox_tool.py` | 35 + 32 例 |
| **Memory Plane** | ✅ | `plane/memory.py` | `test_memory.py` 45 例 |
| **Knowledge Plane** | ✅ | `plane/knowledge.py` | `test_knowledge.py` 23 例 |
| **Workflow Plane** | ✅ | `plane/workflow.py` `plane/blackboard.py` `plane/dag.py` | 36 + 35 + 40 例 |

### 两个保障面

| 面 | 状态 | 模块 | 测试 |
|---|---|---|---|
| **Governance（拦）** | ✅ | `plane/governance.py` `plane/gate.py` | 42 + 34 例 |
| **Quality（看）** | ✅ | `plane/quality.py` | `test_quality.py` 24 例 |

---

## §2 三类状态（按生命周期切分）

| 项 | 状态 | 说明 |
|---|---|---|
| Context / Memory / Knowledge 判据表 | ✅ | 生命周期、淘汰、合规语义三者分离 |
| 跨租户零泄漏（INV-12） | ✅ | `test_memory.py` + `test_integration.py` |

---

## §3 七层记忆归位裁决

| 原层 | 归位 | 状态 |
|---|---|---|
| Working Memory | Context（执行栈） | ✅ `core/loop.py` |
| Conversation Memory | `Context.history` | ✅ |
| **Summary Memory** | `Context.summary`（滚动压缩） | ✅ `core/summary.py`（36 例）+ Builder 渲染 SUMMARY 段 |
| Episodic Memory | Memory Plane | ✅ |
| Semantic Memory | Memory Plane | ✅ |
| Profile & Preference | Memory Plane（最高注入优先级） | ✅ |
| **Procedural Memory** | **Skill 库**（不归 Memory） | ✅ `plane/skill.py`（43 例，含 consolidate→库 闭环） |

---

## §4.1 Tool Plane

| 能力 | 状态 | 模块 |
|---|---|---|
| MCP **Tools** 支柱（有副作用） | ✅ | `ToolManifest` + Dispatcher |
| **MCP Resources 支柱**（只读、URI 寻址） | ✅ | `plane/resources.py`（36 例，含授权+缓存+护栏） |
| **MCP Prompts 支柱**（工程化模板） | ✅ | `plane/prompts.py`（同上测试文件） |
| Dispatcher（Policy/幂等/限流/超时/Secret/入参校验） | ✅ | `plane/tool.py` |
| Normalizer（schema/截断/去重/脱敏） | ✅ | `plane/tool.py` |
| Sandbox（subprocess + 白名单 + 出站） | ✅ | `plane/sandbox_tool.py` |
| HITL 风险分级（T0~T3） | ✅ | `plane/tool.py` + `plane/governance.py` |
| **input_schema 全量铺开** | ✅ | 全部工具（含 sandbox_run / query_logs / 资源工具） |

---

## §4.2 Memory Plane

| 能力 | 状态 |
|---|---|
| Memory Gateway（多租户/RBAC/PII） | ✅ |
| Memory Runtime（Recall/Rerank/Reflect/Consolidate/Forget/Budget） | ✅ |
| Provider SPI | ✅ |
| 五阶段代谢管道 | ✅ |
| 确定性保留矩阵 | ✅ |
| 五维评估 | ✅ |
| Extractor SPI（领域语义外置） | ✅ |

---

## §4.3 Knowledge Plane

| 能力 | 状态 |
|---|---|
| 混合检索（向量 + BM25） | ✅ |
| 引用回填（doc_id/version/span） | ✅ |
| 版本驱动覆盖 | ✅ |
| 只读门面（INV-11） | ✅ |
| 无时间衰减（与 Episodic 对照） | ✅ |

---

## §4.4 Workflow Plane

| 能力 | 状态 |
|---|---|
| 层次主从（Hierarchical） | ✅ `plane/workflow.py` |
| 共享黑板（三区 + CAS + 订阅） | ✅ `plane/blackboard.py` |
| 强类型 Envelope | ✅ |
| Saga 逆序补偿 | ✅ |
| **静态 DAG 编排** | ✅ `plane/dag.py`（40 例：拓扑/分层/短路/并行确定性） |
| 动态 FSM（单 Agent） | ✅ Loop 状态机 |
| 三级死锁判定 | ✅ |
| 看门狗（外部确定性） | ✅ |
| Subagent 降权（INV-18） | ✅ |

---

## §5.1 Governance

| 能力 | 状态 |
|---|---|
| Identity / Tenant / RBAC | ✅ |
| Budget / Quota 熔断 | ✅ |
| Policy + HITL 审批 | ✅ |
| Guardrail 双层（输入沙箱化 / 输出 DLP） | ✅ |
| Secrets（执行期注入） | ✅ |
| PolicyPack（可覆盖，内置不可关） | ✅ |

---

## §5.2 Quality

| 能力 | 状态 |
|---|---|
| Trace 树状 Span + 成本三维归因 | ✅ |
| 轨迹评测（轨迹匹配 + 终态断言 + lazy pass 检测） | ✅ |
| Replay（patch log 重放） | ✅ |

---

## §6 patch log = 轨迹 = WAL

| 用途 | 状态 |
|---|---|
| Eval 轨迹 | ✅ |
| Trace 状态变迁 | ✅ |
| Replay 重放 | ✅ |
| 崩溃恢复 WAL | ✅ `core/store.py` |

---

## §10 心智模型 ↔ 组件映射

| 心智模型 | 组件 | 状态 |
|---|---|---|
| 大脑 | LLM Adapter | ✅ |
| 手 | Tool Plane | ✅ |
| **眼** | Knowledge Plane + **MCP Resources** | ✅ |
| 规划员 | **Router** + Planner | ✅ |
| 记事本 | Context + Memory Plane | ✅ |
| **说明书** | **Skill 库** | ✅ |
| 通用插座 | MCP | ✅ |

---

## 补齐清单（8 项，已全部完成）

| # | 项 | 模块 | 测试 |
|---|---|---|---|
| 1 | Gateway 接入层 | `core/gateway.py` | `test_gateway.py` 59 例 |
| 2 | Router | `plane/router.py` | `test_router.py` 37 例 |
| 3 | Skill Runtime | `plane/skill.py` | `test_skill.py` 43 例 |
| 4 | MCP Resources | `plane/resources.py` | `test_resources_prompts.py` 36 例 |
| 5 | MCP Prompts | `plane/prompts.py` | 同上 |
| 6 | Static DAG | `plane/dag.py` | `test_dag.py` 40 例 |
| 7 | Summary Memory | `core/summary.py` | `test_summary.py` 36 例 |
| 8 | input_schema 铺开 | 全部工具 | `test_input_schema.py` 37 例 |

**接线层**：`plane/wiring.py` — 把 Skill/Resource/Router/Summary 接进主线（`test_integration.py` 7 例守着）。

## 生产化加固（第二轮，打破单进程假设）

| 项 | 状态 | 模块 | 测试 |
|---|---|---|---|
| 并发安全（gateway / resources / blackboard 加锁） | ✅ | 同上 | `test_concurrency.py` 17 例（含 4 条负向对照） |
| 真实持久化（WAL + 事务 + 按租户查询） | ✅ | `core/sqlite_store.py` | `test_sqlite_store.py` 70 例 |
| 真实语义召回（Embedding SPI + 降级记账） | ✅ | `adapters/embeddings.py` `plane/semantic.py` | `test_embeddings.py` 55 例；真实网关 hit@1 1/5→4/5 |
| 隔离后端 SPI（kernel/container/process + 强度自报） | ✅ | `plane/sandbox_backends.py` | `test_sandbox_backends.py` 46 例；本机真跑 docker |
| 多模型 token 标定 | ✅ | `builder.TokenProfile.tool_overhead_fixed` | 4 模型实测：中文高估 1.44~1.57x、tools 开销 51~274 |

## 完工判据

`./verify.sh` 全绿 **且** 本清单不再有 🔴 / ⚠️ **且** README 与两份架构文档同步。

当前：**2163 用例 / 11 场景 / 80 断言，0 个 🔴，0 个 ⚠️。**


---

## 组件选型（第三轮：借库替换手写）

| 手写 → 库 | 状态 | 换来什么 |
|---|---|---|
| 手写 JSON Schema 子集 → **jsonschema** | ✅ | `oneOf/anyOf/allOf/not/pattern/patternProperties/dependentRequired/$ref/format`；37 个冻结用例零文案改动 |
| 手写 Prometheus 导出 → **prometheus_client** | ✅ | 转义/直方图桶/浮点格式/一致性检查不再自负；私有 registry 隔离 |
| 手写 RESP 客户端 → **redis** 官方 | ✅ | 连接池、协议版本、WatchError 语义；CAS 走 `pipeline(transaction=True)` |
| 字符启发式 → **tiktoken + 标定启发式 SPI** | ✅ | OpenAI 系精确；非 OpenAI 系用标定档（实测中文比 tiktoken 准一倍） |

**依赖清单**：`requirements.txt`（tiktoken / prometheus_client / jsonschema / redis）。
`verify.sh` 自动优先用 `.venv/bin/python`；早期"零第三方依赖"的约束已作废。

**刻意保留手写的**：`JsonLogger`（80 行，无等价库）、`ShutdownCoordinator`（薄）、`Tracer`（待换 OTel）。


---

## 组件替换（第四轮：剩余三项）

| 手写 → 库 | 状态 | 换来什么 | 测试 |
|---|---|---|---|
| 手写 BM25 → **SQLite FTS5** | ✅ | 倒排索引、短语/前缀/NEAR、df/avgdl 不再全量重算；顺带修了单字与数字检索 | `test_fts_index.py` 24 例 |
| 手写 span 树 → **OpenTelemetry 导出** | ✅ | `gen_ai.*` 语义约定；Jaeger/Tempo/Langfuse 直接消费 | `test_otel.py` 10 例 |
| 逐条算相似度 → **Qdrant 向量库** | ✅ | ANN 索引 + payload filter 下推（跨租户隔离在库内完成） | `test_vector_store.py` 29 例 |

**保留手写的**：`Tracer` 的树结构与 `finish()` 形状（公开契约）、`JsonLogger`、`ShutdownCoordinator`。
**依赖清单**：tiktoken / prometheus_client / jsonschema / redis / opentelemetry-sdk / qdrant-client。


---

## 第五轮：向量库与切分器可配置化

| 能力 | 实现 | 测试 |
|---|---|---|
| `VectorBackend` SPI（memory / qdrant / milvus） | `plane/vector_backends.py` | `test_vector_backends.py` 43 例 |
| 文档切分 SPI（heading / llama-index 可选） | `plane/splitters.py` | `test_splitters.py` 20 例 |
| 向量后端接入 Memory（换库不改业务） | `plane/vector_store.py` | `test_vector_store.py` 29 例 |

**按需降级矩阵**：不装 qdrant → memory 后端；不装 milvus 服务 → 报错说明原因（不静默降级）；
不装 llama-index → heading 切分器。三者都有"能力自报"接口，避免"配置了没生效"。


---

## 第六轮：Milvus 真机验证

用 **milvus-lite**（真 Milvus 引擎、进程内、无需 etcd/minio）跑通了此前被 skip 的契约用例：

| 项 | 结果 |
|---|---|
| milvus 契约测试 | 10/10 通过（此前全 skip） |
| 跨租户隔离 | payload filter 在库内生效 |
| Memory 端到端 | 写入/召回/重启持久化均通 |
| 分数边界 | 修好 1.32 → ≤1.0（公式上限） |

**真跑才暴露的三个问题**：`create_index` 需 `IndexParams` 对象（pymilvus 3.x）、
`uri` 必须是 `.db` 文件路径、打分公式倒推非语义项会污染上限。

**未验证**：远程 Milvus 集群（官方 compose 拉 minio 镜像失败，改用 lite 同引擎验证）。


---

## 第七轮：对外边界（能通过网络访问了）

| 能力 | 实现 | 测试 | 实测 |
|---|---|---|---|
| HTTP 服务层（6 端点 + SSE + 探针 + SIGTERM） | `serve.py` | 55 例 | 真端口 18080：200 / 422 / 503 分流正确，SIGTERM drain→flush→退出 |
| MCP 官方 SDK 接入 | `adapters/mcp_client.py` | 32 例 | 进程内真 server + 真 `mcp.Client` 冒烟 |
| Langfuse 三档降级 | `plane/langfuse_export.py` | 32 例 | sdk/otlp/none 自报，密钥零泄漏 |

**本轮修的三个真缺陷**：
1. `ok=False` + 空 error 曾被映射成 HTTP **200**（失败回合返回成功，客户端告警全失效）
2. `/readyz` 曾探 gateway 没在用的 store 实例（sqlite/redis 下是**假绿**）
3. 装了 langfuse 后 `configure_otel` 开始**真连 4318**，测试退出刷噪声且污染全局 provider
   → 加 `install` 开关（OTel 的 `set_tracer_provider` 装上就撤不掉）


---

## 第八轮：鉴权 · 会话 · 真子进程 · 真投递

| 能力 | 模块 | 测试 | 实测 |
|---|---|---|---|
| HTTP 鉴权（API Key + JWT） | `auth.py` | 75 | 401/403 分流、`/metrics` 保护、密钥零泄漏 |
| 会话多轮连续性 | `core/session.py` `core/session_gateway.py` | 38 | 三轮对话历史承接 |
| MCP stdio 真子进程 | `adapters/mcp_transport.py` | 25 | 真起 server，崩溃/超时/线程卫生全覆盖 |
| OTLP 真投递 | `plane/quality.py` | 7 | 本地接收器收到 POST + protobuf 可解 |
| Gateway 正式注入口 | `core/gateway.py`（`context_seed`） | 4 | 会话承接不再依赖私有属性 |

**未验证**：远程 Milvus 集群（Docker Hub 全面限流，本机连 alpine 都拉不到）。


---

## 第九轮：接入层收口（会话进主链路）

| 能力 | 位置 | 实测 |
|---|---|---|
| 会话承接可配置 | `serve.build_gateway_from_env`（`AGENT_RUNTIME_SESSION`） | 开启后 `SessionGateway` 包裹，`injection_channel="context_seed"` |
| 请求体带 `session_id` | `serve.TurnRequest` | 之前被 `extra="forbid"` 拒成 422（字段缺失不报错，但服务端永远看不到） |
| SQLite 路径别名 | `serve._make_store` | `AGENT_RUNTIME_DB` 之前被忽略→静默退化为内存库 |
| 三轮真实 HTTP | 端口 18086 + sqlite | `turn_index=1`、`session_turns=2`、落盘 history 只含 `{role,text}` |

**唯一未验证项**：远程 Milvus 集群 —— Docker Hub 本机全面限流（`alpine` 都拒），
已用 milvus-lite（同引擎）覆盖协议/schema/filter/索引。


---

## 第十轮：真增量流式（objective ④）

| 项 | 结果 |
|---|---|
| `core/streaming.py`（尾随 patch log） | 22 用例，含**时间断言**（首个 step 必须在工具完成前到达） |
| Gateway `on_context` 钩子 | Context 就绪即回调，流式层据此拿到 patch log |
| Gateway `started` 顺序修正 | 从"跑完才发"改为"跑前就发" |
| SSE 帧补 `seq` | 客户端可检测丢帧 |
| serve 默认启用（`AGENT_RUNTIME_STREAMING=1`） | 只在**自建** gateway 上包装，注入对象不碰 |

**objective 逐项状态**：
① 鉴权 ✅ ② MCP stdio ✅ ③ 会话 ✅ ④ 真增量流式 ✅
⑤ 远程 Milvus 集群 ✅ **已真跑验证**（3 容器集群 etcd+minio+milvus v2.5.4，10 契约全过）
   - Docker Hub 被限流 → 改镜像源（daocloud / rat.dev）+ quay + ghcr
   - 真集群暴露两个 lite 没有的问题：`load_collection` 必需、`delete` 软删影响 count
   - 可复现脚本：`scripts/verify_milvus_cluster.sh`
⑥ Langfuse 真投递 ✅ **已真跑**：v2 服务端 + ingestion API，HTTP 207、4 条全成功、读回 3 个 observation
   - v3 自托管需 5 件套（含 clickhouse keeper），单机起不来 → 固定用 v2
   - 我们的 SDK 是 5.x（v3 协议），对 v2 会 404 → 改走 ingestion REST
   - 可复现脚本：`scripts/verify_langfuse.sh`


---

## 第十一轮：验收门禁固化

`./verify.sh` 从 5 道扩到 7 道，后两道默认跳过但可一键开启：

| # | 门禁 | 开关 | 耗时 |
|---|---|---|---|
| ⑥ | 真 Milvus 集群（46 契约） | `VERIFY_CLUSTER=1` | ~3 min |
| ⑦ | 真 Langfuse 投递 + 读回 | `VERIFY_LANGFUSE=1` | ~1 min |

**本轮修的真缺陷**：两个脚本里 `cd "$(dirname "$0")/.."` 在切到 compose 目录后失效
（相对路径基于已改变的工作目录）→ 改成顶部固化 `ROOT="$(cd ... && pwd)"`。
表现为"单独跑脚本成功、被 verify.sh 调用时失败"——典型的**依赖调用方 cwd**。

**全七道实测**：`VERIFY_CLUSTER=1 VERIFY_LANGFUSE=1 ./verify.sh` → ✅ 全部通过。


---

## 第十二轮：生产可用（工具配置化 · 鉴权默认拒绝 · 部署文档）

三路并行施工，共 +132 用例（1602 → 1734）。

| 能力 | 模块 | 用例 | 说明 |
|---|---|---|---|
| **工具配置化** | `toolconfig.py` + `tools.example.yaml` | 59 | YAML 声明式注册（含 MCP server）；配错 reject 启动 |
| **鉴权默认拒绝** | `serve.py`（`StartupRefused`） | 55 | 没凭据就起不来；`/v1/info auth.mode` 可自查 |
| **HTTP 三层边界** | `serve.py`（体量/限流/超时） | 同上 | 413/429/504；限流只对 `/v1/*` |
| **部署文档** | `docs/DEPLOY.md` `docs/TOOLS.md` | — | 59 个变量全表 + 20 条排障 |
| **一键冒烟** | `scripts/smoke.sh` | — | 8/9 项覆盖探针/回合/SSE seq/鉴权/护栏/关停 |

### 编排者补的三处装配缺口（P3 报告）

1. **SSE `seq` 不按流重置** —— `_seen_seqs` 只在 `__init__` 清，同进程第二条流从 8 起。
   客户端按「首条恒为 0」写状态机会错判。已改为每条流从 0 开始（+3 用例）。
2. **`serve.py` 没有 OTel 装配** —— `OTEL_EXPORTER_OTLP_ENDPOINT` 写了也不发。
   已接 `configure_otel(install=True)`，并支持 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` → Basic 头。
   实测：`otel.installed` + 接收器收到 883 字节 POST。
3. **模型前缀硬编码 `OPENAI`** —— 用 `BABEL_*` 凭据恒失败，与 `real_model_smoke.py` 口径不一致。
   已加 `AGENT_RUNTIME_LLM_PREFIX`（默认 `OPENAI`），错误信息里带当前前缀。

另外修了一个自己引入的 UX 缺陷：**启用鉴权后请求体仍强制要 `principal.user_id/tenant`**。
身份明明已由凭据给出，却逼客户端再抄一遍，抄错还判 403。已改为可选（+15 用例，
`tests/test_serve_infra.py`）。


---

## 第十三轮：SSE 完整性（+22 用例）

用户问"SSE 这块做好了吗"，实测查出**一个会让流在生产里莫名断掉的缺口**。

### 缺的是什么

| 项 | 状态 | 后果 |
|---|---|---|
| 事件序列 / seq 单调 | ✅ 已有 | — |
| `Cache-Control` / `X-Accel-Buffering` | ✅ 已有 | 没有后者 nginx 会攒包 |
| 空行分隔 / 断开收口 | ✅ 已有 | — |
| **静默期心跳** | ❌ **缺** | 模型思考 30s 期间零字节；nginx `proxy_read_timeout 60s` 掐断 → "流跑到一半莫名结束" |
| **心跳可配置** | ❌ 缺 | 硬编码无法按部署环境调整 |

### 实测证据

回合里让 LLM 思考 5 秒，抓到的帧时间戳：

```
0.00s accepted  0.00s started  0.06s step ×3
5.01s step ...            ← 中间 4.95 秒一个字节都没有
```

修复后（心跳 3s）：

```
0.1s step ×3
3.1s ♥ : keepalive
6.1s ♥ : keepalive
9.1s ♥ : keepalive
12.1s ♥ : keepalive
15.1s ♥ : keepalive
18.1s ♥ : keepalive
20.0s step ... completed
```

### 修的过程中又踩一个静默故障

第一版把签名写成 `heartbeat_s: float = SSE_HEARTBEAT_S` —— **import 期绑定**，
之后设环境变量、改模块常量全都不生效，而且不报错。表现是"改了配置心跳间隔没变"。
改成 `None` 默认 + 调用时读环境，并加用例
`test_read_at_call_time_not_import_time` 钉住。

### 刻意不做的事

**不发 `retry:` 字段**。这个端点是 POST + 请求体，浏览器 EventSource 无法重放同一回合，
发 `retry:` 等于告诉客户端"重连能续上"——续不上。断点续传走 `ctx_id`（Gateway 本来就支持）。
做不到的事不该在协议里承诺。


---

## 第十四轮：上层扩展点冻结为 SPI（+14 用例）

**定位澄清**：本项目的职责是 **Runtime 架构**；Agent 层内容（Planner 实现、
业务工具、Prompt 模板、技能编排）由上层负责。Runtime 的义务是
**把扩展点冻结成可编译的契约**，让上层不必读 Runtime 源码。

### 之前缺什么

Planner / Router / Skill 执行三处只有**鸭子类型**：
文档写"有 `replan(...)` 即可"，或某个具体类恰好有同名方法。
上层要独立实现时**没有任何契约可依**。这不叫 SPI，叫隐式约定。

### 补的三份契约（`contracts.py` EXTENSIONS v7）

| Protocol | 方法 | 关键约定 |
|---|---|---|
| `PlannerProtocol` | `replan(snapshot, reason) -> Patch \| None` | 返回 Patch 而非直接写 Context；None = 放弃；抛异常只记 warning |
| `RouterProtocol` | `route` / `explain` | 回澄清用 `__clarify__` 哨兵（不是空串）；explain 给分项得分 |
| `SkillHandlerProtocol` | `__call__(args, ctx)` | Skill 是多 Tool 编排，**Runtime 不拆步骤** |

### 顺带修的一个真问题

`Loop(planner=...)` 之前是**用到才报错**（`getattr` 拿不到就当没有）。
现在**构造期校验**：没有可调用的 `replan` → 立刻 `TypeError`。

理由：死锁本来就是补救路径，补救工具本身坏了会被误判成
"再规划也没用"——推理方向完全反了。

### 边界测试（AST 级）

- SPI 必须是纯 Protocol：每个方法体只能有 `...`（**用 AST 判，不靠字符串匹配**，
  后者会把 docstring 续行当成代码——第一版就是这么错的）
- SPI 代码行不许出现业务词汇

### 文档

`docs/SPI.md` —— 给上层实现者看的扩展点清单：谁是 Runtime 谁是对端、
怎么做 Judge、内置实现可换的注意点、以及"你只需实现 Extractor + Planner"。


---

## 第十五轮：上层（Agent 层）照 SPI 实现（+120 用例）

Runtime 侧只做了一件事：**把扩展点冻结成契约**（第十四轮）。本轮由上层照契约实现内容。

| 交付 | 模块 | 用例 | 实现要点 |
|---|---|---|---|
| 记忆抽取 | `agents/extractor.py` | 42 | 九节规则（时间锚点→事件、变更动作+顺序词→偏好、数字+量纲→事实），带 `explain()` 说理由 |
| Prompt 模板 | `agents/prompts.py` | 含上 | 把 builder 里那句业务人格搬到 `agents/`（ADR-8 的落地） |
| 规划器 | `agents/planner.py` | 29 | `StaticDAGPlanner` + `LLMPlanner`，只写 `plan` |
| 业务工具 | `agents/tools.py` + `tools.yaml` | 45 | 8 个工具（4×T0 / 2×T2 / 2×T3），`get_config` 自建脱敏 |
| 技能 | `agents/skills.py` | 含上 | 3 个多 Tool 编排 |
| Agent 注册表 | `agents/registry.py` | 含上 | 3 个 Agent，路由实测 0.489 / 0.470 / 0.483 |

### 我（Runtime 侧）在这一轮修的一个错

**我给上层的契约文档写错了字段权限**：`PlannerProtocol` 的 docstring 写"写 `plan` / `observations` 等"，
而 `OWNER_TABLE["observations"] is Owner.LOOP` —— Planner 写它会被 `apply()` 拒，
**且只记一条 warning**。表现是"规划器跑了但什么也没生效"，极难排查。

上层施工者发现了并只写 `plan`。我把文档改对，**并把这条从"文档约定"升级成可执行断言**：

- `test_plan_is_planner_owned`
- `test_observations_is_loop_owned`
- `test_planner_writing_observations_is_rejected`（真跑一遍看它被拒）
- `test_documented_writable_set_matches_owner_table`（文档提到 observations 就必须说明归 LOOP）

> 教训：**文档可以写错，测试不会。** 凡是"上层照着做会静默失败"的约定，都该有可执行断言。

### 新增的边界守卫

`TestAgentLayerImplementsSPI`：
- Runtime 目录（`core/` `plane/` `adapters/`）**不许 import `agents/`**
- `builder.py` / `contracts.py` / `serve.py` / `toolconfig.py` 同上
- `agents/` 里**必须真有业务词汇**（否则说明分层只是空壳）
- 声称实现 SPI 的类必须真有那个方法

### 全链路实测

```
注册 8 个业务工具
耗时 0.01s | ok=True | steps=3
回答: 根因是连接池耗尽，扩容副本数可缓解
rollback_deployment    -> denied      ← T3 硬拒
delete_namespace       -> denied      ← T3 硬拒
```

### 分层结果

```
Runtime  111 模块 / 1775 用例 / 无业务词汇
Agent 层    11 文件 / 3426 行 / 120 用例 / 全是业务
```


---

## 第十六轮：上层能力接入主链路（+24 用例）

之前的状态：`build_gateway_from_env` 只装了 PromptBuilder + LLM + 工具表，
**Router / Skills / Memory / Planner 全部没接** —— 配了也是白配。

### 接了什么

| 环境变量 | 作用 |
|---|---|
| `AGENT_RUNTIME_EXTRACTOR` | 装 Memory（没有它 Memory 根本不存在） |
| `AGENT_RUNTIME_MEMORY_PROVIDER` | `memory` / `vector` / `module:attr` |
| `AGENT_RUNTIME_ROUTER` | 路由决策写进 `ctx.agent_state` |
| `AGENT_RUNTIME_PLANNER` | 接到 `Loop(planner=)` |
| `AGENT_RUNTIME_PROMPT_PROFILE/TEMPLATE` | 业务人格与 token 标定 |
| `AGENT_RUNTIME_TOOLS` | 工具表（原有） |

### 关键设计：Runtime 不静态 import agents

用 `load_extension("module:attr")` 在运行时解析。所以边界测试
（`test_builder_root_does_not_import_agents`）保持绿，而部署方照样能插自己的实现。

### 新加的两个 Gateway 钩子

`on_turn_start(ctx, goal, principal)` / `on_turn_end(ctx, result)`：
Memory 召回必须在**第一次调模型前**完成（否则模型看不到历史），
写回必须在**回合结束后**（失败回合也要——失败本身是有信息量的事实）。

### 本轮踩的三个坑

1. **抽象层级搞混**：`MemoryRuntime(store=...)` → TypeError。
   它吃的是 `Provider`（put/get/scan/delete），不是 Context 那种 `Store`。
2. **Planner 构造时机**：需要参数的类（`StaticDAGPlanner(dags)`）如果拖到回合里实例化，
   表现是"每一轮都 loop_error"，而根因在启动配置。改成**装配期实例化**并提示写工厂函数。
3. **改了 after_turn 的返回类型**：从 `SummaryResult` 变成 dict，直接把
   `demo/scenario_fullchain.py` 场景 10 打挂（11 → 10）。**加参数可以，改既有返回类型不行**。
   已恢复后向兼容并加用例钉住。


---

## 第十七轮：真模型验证（+5 用例）

用真实网关（babelark，257 个模型）跑通了全链路。**mock 永远暴露不了的问题，这一轮全出来了。**

### 验证了什么

| 项 | 结果 |
|---|---|
| 连通与计量 | deepseek-flash 3.99s，usage 准确（36 in / 91 out） |
| 工具调用 | 一次能发 5 个并行调用，参数正确（service/metric/window 全对） |
| 简单目标收敛 | 「p99 多少」「有发布吗」「连接池上限」→ **全部 2 步 DONE** |
| 复杂排查 | 4~10 步、11~26 次工具调用，产出**完整根因分析**（证据链闭合、排除项、T3 拒绝执行、不确定项） |
| T3 硬拒 | 模型主动提出「回滚 v42」，Runtime 拒绝执行，模型转而交人确认 |

### 真模型暴露的三个 Runtime 缺陷（都已修）

**① 工具结果只渲染最后 6 条 → 模型失忆 → 重复调用**

`_render_tool_results` 写死 `results[-6:]`。真模型一轮发 20+ 次调用时，
前面的证据在它视野里**消失**，于是同一批查询重复发 4 次以上，最后耗尽步数没结论。

改成「尾部 4 条完整 + 更早压到 140 字符 + 总条数封顶 40」。
Runtime 的职责是**让模型看得见自己做过什么**，不是省 token 省到失忆。

**② 去重标记不可见**

幂等工具重复调用返回缓存（`deduped=True`），但 Prompt 里没有标记 ——
模型分不清"新数据"和"我刚查过的同一份"。现在通过 `template.deduped_label` 渲染
（builder 保持中性，措辞由模板给）。

**③ 工具粒度太细，逼模型做 N 次往返**

`query_metrics` 原来一次只查一个服务的一个指标。要做 3 服务 × 5 指标对比，
模型得发 15 次调用（实测发了 18 次）。改成支持 `services=[...]` + `metrics=[...]`
批量后，同类调用从 18 → 10。

顺带：`anyOf` 的报错原来只说「不满足任何一种形态」，模型不知道缺什么。
改进 `validate_against_schema`，现在报「需要以下之一：metric 或 metrics」。

### 已知边界（诚实）

**复杂排查任务不一定在 max_steps 内收敛**。deepseek-flash 实测：
同一句目标有时 4 步 DONE（产出极高质量分析），有时 26 次调用耗尽预算。

这不是 Runtime 崩溃——**有界收口是对的**（给出已完成步数，不假装完成）。
是提示词与工具设计需要继续调优，属于 Agent 层。可复现脚本：
`scripts/real_model_e2e.py`（密钥只经环境变量，不落盘）。


---

## 第十八轮：健壮性与热插拔评估（+30 用例）

按"全方位测试"要求做了故障注入与热插拔验证。**查出并修掉 5 个真实缺陷。**

### 简装：核心零第三方依赖

实测：17 个核心模块在**纯 stdlib venv** 里全部可导入并跑通完整回合。
`requirements.txt` 原来把 5 个标成"必需"，实际只有 HTTP 层要 fastapi、
YAML 配置要 pyyaml —— 已按层重写。

### 修的 5 个缺陷

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | `audit_log` 无界增长 | 长跑服务内存泄漏（1000 回合 +438KB 不回收） |
| 2 | `_coerce_response` 接受任意对象 | 适配器 bug → 乱码当答案，`DONE` |
| 3 | deadline 只在每轮开始前检查 | 响应迟到仍 `DONE`，硬截止被绕过 |
| 4 | 依赖故障返回 HTTP 200 | 模型挂了监控看不到 |
| 5 | SSE 与 HTTP 对同一故障结论不一致 | JSON 见 502、流式见"成功" |

### 热插拔：12/12 SPI 可换

KV / ContextStore / LLM / Tokenizer / MemoryProvider / VectorBackend /
DocumentSplitter / SandboxBackend / FtsIndex / Extractor / Router / Planner ——
全部实测可替换，且**每个都自报可用性与降级原因**。

### 文档

`docs/ROBUSTNESS.md` —— 分层依赖表 + 故障注入结果 + 热插拔矩阵 + 诚实边界清单。


---

## 第十九轮：全部修复剩余 5 项（+37 用例）

按"全部修复"要求，把上一轮列的 5 条不足全部处理掉。

| # | 原不足 | 修法 | 实测 |
|---|---|---|---|
| 6 | HTTP 超时只停等待 | `cancel_event` + 隐式 deadline + socket 读超时（三道） | 超时 0.5s、LLM 10 步 → **只调 1 次** |
| 7 | 限流每进程计数 | `KvRateWindow`（KV 原子 incr） | 两副本共享 → 合计放行 3 次（旧 6 次） |
| 8 | XFF 可伪造 | `AGENT_RUNTIME_TRUSTED_PROXIES`（默认忽略 XFF） | 无可信配置→忽略；配了→按 XFF 分桶 |
| 9 | 工具结果窗口丢证据 | payload 指纹去重合并 + 上限 40→80 | 96 次调用唯一证据可见率 58%→**100%** |
| 10 | 模型不收敛 | 收敛指引 + 停止判据 + 批量工具 | 真模型 **10 步 26 次 → 4-6 步 5-6 次 DONE** |

### 真中止的真实边界（写清楚，不假装）

**能**：超时后在下一个检查点退出（adapter 每次重试前、每轮循环开头都看 `aborted`）。
**不能**：强杀卡在 `socket.read` 的线程（CPython 无安全手段）。
**兜底**：`adapter._timeout_s()` 把 `min(timeout_ms, 剩余 deadline)` 交给 `urlopen(timeout=)`。

> 链条：`HTTP 超时 → 隐式 deadline → socket 读超时`。缺任何一环，线程都可能活过超时。

### 真模型收敛性实测（deepseek-flash × 3）

```
修复前：10 步 / 22-27 次调用 / MAX_STEPS 或 BUDGET / 65-278s / 无结论
修复后：4-6 步 / 5-6 次调用 / DONE / 23-45s / 完整根因分析
```

答案质量同步提升：正确指出"扩容副本会成倍放大 DB 连接数"这种反直觉结论。


---

## 第二十轮：C1 并行工具执行（+13 用例）

按 C+B 设计施工，第一阶段完成。

### 前置必修：Dispatcher 原本无锁

并行一开就静默失效的三处记账（`_cache` / `_turn_calls` / `budget`）已加 `RLock`。
锁只包记账，不包 handler —— 否则并行退化成串行。

### 实测

| | 结果 |
|---|---|
| 确定性对照（4×0.3s） | 1.22s → **0.31s**（3.9×），顺序不变 |
| 真模型 A/B | 38.0s/6 步 → **18.7s/3 步**（快 51%） |

### 安全闸

`_can_parallel()`：任一调用 `tier ≥ T2` → 整轮退回串行。
理由：串行语义是"遇审批点即停，后续不执行"；并行会把它们也跑掉。

### 配置

`AGENT_RUNTIME_PARALLEL_TOOLS`（默认 1）/ `AGENT_RUNTIME_MAX_PARALLEL_TOOLS`（默认 4）。


---

## 第二十一轮：C+B 全部完成（+93 用例）

按 `docs/DESIGN-C.md` 施工完毕。

| 阶段 | 用例 | 关键实现 |
|---|---|---|
| **C1** 并行工具 | 13 | Dispatcher 加记账锁；`_can_parallel` 安全闸（T2+ 退回串行） |
| **C2** 进度通道 | 15 | `ExecCtx.progress` 侧通道 + `QueueProgressSink`（有界、丢弃计数） |
| **C3a** 权限告知 | 9 | `SubagentResult.needs_approval`（结构化，非裸 ok=False） |
| **互斥 + 复盘** | 24 | `Owner.WORKFLOW` + `workflow_state` + `bind_engine` |
| **B** Workflow 端点 | 45 | 三端点 + 有界状态 + 独立 Context |

### 连带修掉的三个静默缺陷

1. **`ExecCtx.child()` 不继承 `progress` / `cancel_event`**
   → 层叠子 Agent 进度传不出、超时只停父层。两者都是"看起来在、实际不生效"。
2. **`DAGRunner` 的 `workflow_state` 不落盘**
   → 跑到第几层、谁被跳过不留痕，无法复盘。修法：每条 workflow 独立 Context。
3. **`Dispatcher` 记账无锁**
   → 并行下 `per_turn_limit` 被绕过、幂等工具可能真跑两次。

### 踩到的三个"我以为"陷阱

| 我以为 | 实际 |
|---|---|
| `report["layers"]` 是数字 | 是**层的列表** |
| 节点有 `status`/`skipped`/`blocked_by` | 只有 `ok`/`term_reason`/`steps`/`error` |
| `report["failed"]` 是名单 | 是**计数** |

> **观测面读错字段比不读更坏** —— 它看起来有内容，实际全错。

另有一次 **stale pyc** 让运行时默认值与源码不符（`max_depth` 显示 0 而非 2），
清缓存后恢复。已记入排查清单。


---

## 第二十二轮：破坏性测试 + 4 个安全缺口（+19 用例）

用极端输入打 Runtime，找出 4 个「单请求放大」缺陷。

| # | 缺陷 | 实测后果 |
|---|---|---|
| 11 | `rate_limit_per_minute=0` = **全禁** | 想关配额 → 关掉整个服务（20 个请求全拒） |
| 12 | workflow 节点数无上限 | 1000 节点 DAG 返回 **202**，排队烧资源 |
| 13 | budget 请求体无上限 | `token_total=10^12` 被接受 |
| 14 | budget 以 pydantic 模型透传 | Gateway 收不到 → 预算"看起来设了、实际没生效" |

### 共同特征

**一个请求就够**，不是持续攻击。没有上限时**完全静默**：照常返回 202/200，
后台开始烧预算/线程/内存。

### 同时确认的两件好事

- **内存有界**：`audit_log_maxlen=100` 时 900 回合只涨 3KB（约 4 B/回合，噪声级）
- **并发稳定**：20 轮×4 并行工具 20/20 ok；8 线程×5 回合 0 异常；workflow 容量精确（5/5, 7×429）

### 修正后语义

`AGENT_RUNTIME_RATE_LIMIT=0` → **不限流**（与 `AGENT_RUNTIME_HTTP_RATE=0` 统一），
`remaining=-1` 自述无上限。负数是配置错误，构造期就拒。


---

## 第二十三轮：Workflow 持久化（+9 用例）

补上 B 的最高价值缺口：进程重启不丢状态。

| 能力 | 实现 |
|---|---|
| 提交即落盘 | 先 `save` 再进线程池（有窗口期，反过来会丢） |
| running/节点/终态 | 每次变化落盘 |
| 重启恢复 | `restore()` 读回；未完成标 `interrupted` |
| 已完成也恢复 | **审计轨迹要能事后查** |

### 关键决定：不自动续跑

重放会**重复执行已完成节点** —— 发布流程里那是灾难（重复删库/重复发版）。
只做到「可查 + 明确告知在哪断的」，续跑交给人决定。

### 实测

```
提交 → done → kill → 重启 → "workflow.restored count=1" → GET: done
```

### 踩到两个 API 形状错（都导致静默失败）

`store.save` 只收两参（我传了三个）；`state.nodes` 是 dict 不是 list。
两者都被 `restore()` 的 `except` 吞掉 → `count=0`。
**所以恢复路径必须有日志**，否则"没恢复"和"没东西可恢复"看起来一样。

### 顺带确认的好消息

- **指标基数有界**：500 个随机 URL 打进来，label 里出现 0 个 path
- **SSE 断开无残留**：10 次立即断开，线程数不变
- **workflow 容量精确**：50 次提交到容量 5 → tracked=5, evicted=45


---

## 第二十四轮：Workflow 续跑（+9 用例）

目标③要求"持久化**与续跑**"，上一轮只做了持久化。本轮补完续跑。

| 能力 | 实现 |
|---|---|
| `DAGRunner.run(skip=[...])` | 预置已完成节点为 OK → 下游解锁，**不重新执行** |
| `POST /v1/workflows/{id}/resume` | 只允许 `interrupted`/`failed` |
| 状态闸门 | `pending`/`running`→409（防双跑）；`done`→409；未知→404 |
| `resumed_count` | 自述续跑过几次（判断"是否反复断"） |

### 实测

```
第一次: failed  [('a','done'), ('b','failed')]
resume → 202 → done [('a','done'),('b','done')]
实际执行: ['FAIL']   ← A 被跳过，没重跑
```

### 诚实边界：spec 不持久化

DAG 定义是代码产物，持久化等于把代码复制进库（改代码后旧 spec 不可理解）。
所以重启后 `resume` 明确报 409「spec 不在内存」，而不是假装能续。


---

## 第二十五轮：Workflow 级 SSE（+9 用例）

补完 B 的最后一条已知边界：流水线进度实时可见。

`GET /v1/workflows/{id}/stream`

| 设计点 | 理由 |
|---|---|
| **轮询式**（非事件总线） | 状态在内存，读一次很便宜；漏事件风险为零；重连天然幂等 |
| **只在有变化时发帧** | 否则跑 5 分钟的流水线会刷 1500 条相同事件 |
| **已完成也能看** | 连上时它可能已跑完 → 直接给终态，不挂着等 |

实测：`accepted → progress(running,0) → progress(running,1) → progress(done,2) → completed`

配置：`AGENT_RUNTIME_WORKFLOW_STREAM_MS`（默认 200，clamp [20, 5000]）。

### B 的四条已知边界现状

| # | 边界 | 状态 |
|---|---|---|
| 1 | 崩溃恢复 | ✅ 持久化 + 重启读回（标 interrupted） |
| 2 | 续跑 | ✅ `POST /resume`（跳过已完成节点） |
| 3 | workflow 级 SSE | ✅ 本轮完成 |
| 4 | 内置 runner 无降权 | ⚠️ 保留（B 的节点跑的是提交者回合，INV-18 只在 C 里成立） |


---

## 第二十六轮：B 的两个真实缺口（+9 用例）

第 5 轮缺陷猎捕发现 B（workflow）有两处不成立的地方。

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 15 | **节点间无预算隔离** | 每节点用 Gateway 默认额度 → 64 节点 = 128 万 token 放大 | 每节点显式预算（2 万/12 次/8 步，可注入） |
| 16 | **下游看不到上游结论** | "根据检查结果决定是否扩容"里没有检查结果 → 只能瞎猜 | 上游摘要追加到下游 goal（只带依赖、截断 400 字） |

### 第 16 条为什么重要

静态 DAG 最常见的形态就是「先检查、再决定」。没有上游结论传递，
这个形态**根本跑不通** —— 而它能跑通之前，B 只能做"互不相关的并行任务"。

### 顺带修的测试脆弱性

3 个用例断言 `goal == 字面量`（注入后不成立）；
1 个用 `assertNotIn("A", goal)`（单字母 "A" 在 "FAIL" 里就有）。
全部改成精确断言语义。


---

## 第二十七轮：消灭一个真实 flaky

全套件连跑 6 次挂 1 次，单独跑 15 次全过 —— 典型时间窗口型 flaky。

**根因**：`MCP_FIXTURE_DIE_AFTER_S=0.4` 是时间触发。全套件 CPU 争抢时，
"崩之前的那次调用"可能赶不上 0.4s 窗口。

**修法**：fixture 加 `MCP_FIXTURE_DIE_AFTER_CALLS=N`（第 N 次调用后硬崩）。
"第 1 次成功、第 2 次弄崩它" —— 无时间窗口。

> 一般规律：**测试里用 sleep 制造时序 = flaky 的种子。**
> 能改成"按事件计数"的一律改。

修后：模块级连跑 15 次 + 全套件连跑 5 次，全过。


---

## 第二十八轮：副作用落盘（+11 用例）

把崩溃恢复从「只覆盖审批卡点」补到「覆盖所有写操作」。

### 缺口

`checkpoint` 原来只在 `WAITING_USER` 落盘。长回合中途崩 = 前几步全丢，
而其中可能有"已经改了生产"的操作。

### 修法

`ExecCtx.checkpoint` 回调 + Loop 在**非只读工具成功之后**触发。

只在写操作后落，不是每步：只读丢了重跑就行（幂等），写丢了**不知道做没做**。

### 实测

```
LLM 第 2 步崩溃 → 恢复后看到 [('deploy', True)] / observations [('deploy','ok')]
开销：1.1ms → 1.7ms/回合（5 次写操作，+0.6ms）
```

### 三个决定

1. `child()` **不继承** checkpoint（子 Context 用父 ctx_id 会搅乱状态）
2. 落盘失败**上报不吞**（vertex + warning），但不把回合改成失败
3. 判不出副作用也落（宁多写，不漏写）

### 顺带修的静默 BUG

`_manifest()` 把 `ToolRegistry.get()` 的 `(manifest, fn)` 元组当 manifest 用
→ `side_effect` 永远 None → 只读工具也被落盘。
