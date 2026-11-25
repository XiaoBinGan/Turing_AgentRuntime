# 部署与运维

> 这份文档面向**"我要把它跑起来"**的人：命令、配置、探针、排障、容量。
> 架构取舍、"为什么这么设计"、踩坑复盘在 [`../README.md`](../README.md)；
> 全景图与门禁现状在 [`STATUS.md`](STATUS.md)。本文**不重复**它们，只链接。
>
> 所有环境变量的权威来源是代码本身：`serve.py` 的 `main()` / `create_app()` /
> `build_gateway_from_env()` / `resolve_auth_mode()` / `require_auth_config()` /
> `build_authenticator_from_env()` 文档串（鉴权细节在 `auth.py`），
> 以及 `plane/vector_backends.py`、`plane/splitters.py`、
> `adapters/openai_compat.py`、`toolconfig.py`。本文与代码冲突时**以代码为准**。
>
> 覆盖范围：`AGENT_RUNTIME_*` 全部 37 个 + 向量/切分 12 个 + 模型后缀 8 个
> + OTLP/Langfuse 若干 —— §2 的配置全表逐条给了「配错的后果」。

---

## 0. 五分钟跑起来

```bash
cd agent-runtime

# ① 建 venv 并装必需依赖（7 个包，约 20 个传递依赖）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# ② 起服务（本机调试：显式关掉鉴权 + scripted LLM + 内存存储 + 127.0.0.1:8000）
#    注意 AGENT_RUNTIME_AUTH=off：serve.py 现在**默认 fail-closed**，
#    什么都不配会直接拒绝启动（见下方"启动失败"说明）
AGENT_RUNTIME_AUTH=off AGENT_RUNTIME_PORT=8000 .venv/bin/python serve.py

# ③ 另一终端：确认活着
curl -s localhost:8000/healthz; echo
curl -s localhost:8000/readyz;  echo

# ④ 真的跑一个回合
curl -s -X POST localhost:8000/v1/turns \
  -H 'Content-Type: application/json' \
  -d '{"goal":"排查连接池超时","principal":{"user_id":"u1","tenant":"acme","scopes":["runtime:run"]}}'

# ⑤ 一键冒烟（8 项 + 可选的优雅关闭检查 = 9 项；任一 FAIL → 退出码非 0）
./scripts/smoke.sh --port 8000 --check-shutdown
```

第 ④ 步应该看到（实测，scripted LLM）：

```json
{"ok":true,"text":"运行时已就绪（scripted LLM）。本轮收到 7 字符的目标，未执行任何工具。接真实模型请设 AGENT_RUNTIME_LLM=openai。","phase":"DONE","term_reason":"DONE","task_id":"task_196138c8","steps":1,"partial":false,"error":"","audit_id":"aud_0345edd725e4","latency_ms":1.32,"vertex":{"tenant":"acme","user_id":"u1","quota":{"used":1,"limit":60,"remaining":59,"window_s":60.0},"budget":{"token":{"total":20000,"used":0},...}}}
```

第 ⑤ 步应该看到：

```
  检查项               结果    详情
  ────────────────────────────────────────────────────────────────
  1 /healthz 存活      PASS  status=alive version=0.1.0 uptime=2.871s pid=71703 …
  2 /readyz 就绪       PASS  ready=true store=MemoryStore kv=in-memory vector=memory
  3 /v1/info 自述      PASS  version=0.1.0 py=3.14.3 store=MemoryStore kv=in-memory …
  4 /v1/turns 一轮     PASS  latency_ms=0.69 phase=DONE steps=1 audit=aud_1f9c3c3b0074 …
  5 /v1/turns/stream   PASS  HTTP 200 8 条事件 seq 0..7 单调递增｜accepted,started,step,…
  6 /metrics 指标      PASS  含 agent_runtime_up，2455 字节 / 24 个样本行
  7 未授权被拒         SKIP  服务未装鉴权（/v1/info auth.enabled=false）…
  8 护栏拦截越狱       PASS  HTTP 422 guard_hits=['ignore_previous_instructions']｜…
  9 SIGTERM 优雅关闭   PASS  draining=1 metrics.flushed=1 drained=1 → 进程退出
  ────────────────────────────────────────────────────────────────
  9 项 · PASS 8 / FAIL 0 / SKIP 1
  结论：通过（有 SKIP 项）—— 本机开发可接受；上线前必须补齐 SKIP 对应的配置
```

> 第 7 项 SKIP 是**预期**的：`AGENT_RUNTIME_AUTH=off` 是本机显式裸奔，
> 没有鉴权可验。换成"配了 API key"的形态，它会变成 PASS（见 §3.2）。

**如果 ② 直接报错退出**（`StartupRefused: 拒绝启动：未配置任何凭据方式。`）——
那不是 bug，是**默认 fail-closed** 在起作用：一个裸奔的进程（principal 由请求体给出）
比一个起不来的进程危险得多。三条合法出路（任选其一）：

```bash
AGENT_RUNTIME_API_KEYS='sk-local-1:dev:runtime:run tool.read#u_dev' .venv/bin/python serve.py
AGENT_RUNTIME_JWT_SECRET="$(openssl rand -base64 48)" .venv/bin/python serve.py
AGENT_RUNTIME_AUTH=off .venv/bin/python serve.py     # 显式裸奔，仅限本机/内网
```

---

## 1. 前置条件

### 1.1 运行时

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **3.14**（实测 3.14.3） | 本仓只在 3.14 上跑过 1602 用例。3.10~3.13 大概率可用（没有 3.14 独有语法），但**没有实测证据**，别把"应该能跑"当"能跑" |
| 操作系统 | macOS / Linux | `plane/sandbox_tool.py` 在 macOS 上内核拒绝 `RLIMIT_AS`，内存上限实际不生效（`enforceable_rlimits()` 可探测） |
| 端口 | 默认 8000 | 只监听 `127.0.0.1`；容器/跨机访问必须设 `AGENT_RUNTIME_HOST=0.0.0.0` |

### 1.2 必需依赖（缺了服务起不来或能力失真）

| 包 | 启用什么 | 不装会怎样 |
|---|---|---|
| `fastapi` / `uvicorn` | HTTP 服务层与进程入口 | `serve.py` 根本起不来（模块级 import）。**这条是"库 → 进程"的分界线** |
| `prometheus_client` | `/metrics` 导出 | 指标渲染失败，`/metrics` 500 |
| `tiktoken` | 精确 token 计量（OpenAI 系） | 退化成标定启发式（中文实测误差 2~6%，见 §5.1） |
| `jsonschema` | 工具入参校验（`input_schema`） | 模型把 `lines` 写成 `limit` 这类幻觉不再被拦，工具拿到空参数静默返回空结果 |
| `redis` | `AGENT_RUNTIME_KV=redis` 的客户端 | 只能用进程内 KV；多副本各自计数 |
| `pyjwt` | JWT 鉴权 | JWT 方式自报 `auth_not_configured`（**不会跳过校验**），API key 不受影响 |

### 1.3 可选依赖（装了才启用，不装自动降级并自报）

| 包 | 启用什么 | 不装时降级到 |
|---|---|---|
| `opentelemetry-sdk` | span 树导出（`gen_ai.*`） | 只有进程内 Tracer |
| `opentelemetry-exporter-otlp-proto-http` | 真发 OTLP | `configure_otel()` 返回 `enabled=false` + 原因 |
| `qdrant-client` | `VECTOR_BACKEND=qdrant` | 该后端不可用 → `/readyz` 报 vector 不可用 |
| `pymilvus` + `milvus-lite` | `VECTOR_BACKEND=milvus`（远程 / 本地文件） | 同上 |
| `llama-index-core` | `SPLITTER=llama-index`（句子边界、重叠窗口） | 用 `heading` 切分器（零依赖、确定性） |
| `pyyaml` | `toolconfig.load_tools()` 读 YAML 工具配置 | 进程内注册工具仍可用；`load_tools()` 报错并给出安装命令（它不是本仓的硬依赖） |
| `mcp` | MCP 工具接入（`adapters/mcp_client.py`） | 只能注册进程内工具 |
| `langfuse` | LLM 可观测 SDK 模式 | 降级 OTLP 模式，两样都没有就 `mode=none` |

装齐 optional 的代价是实打实的：只装必需 ≈20 个包，加 llama-index ≈86，加 mcp ≈120。
装之前先问"这个能力这次真的要吗"。

### 1.4 系统工具

| 用途 | 需要 |
|---|---|
| 一键冒烟 `scripts/smoke.sh` | `curl` + `python3`（不引入任何新包） |
| 一键验收 `verify.sh` 的 ⑥⑦ 门禁 | `docker`（起 Milvus 三容器 / Langfuse 服务端）；本机 Docker Hub 曾全面限流，见 `README.md` 已知边界 |

---

## 2. 配置全表

> 读法：**"配错的后果"是这张表的重点**。这个项目反复踩的坑都是同一类 ——
> 配置看起来对、实际不生效、而且不报错。

### 2.1 服务

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_HOST` | 否 | `127.0.0.1` | 监听地址 | 留在默认值时**只监听回环**：容器里/K8s Pod 外/k8s probe 之外都连不上，表现为 `connection refused`，而进程 `/healthz` 自测是 200（假绿） |
| `AGENT_RUNTIME_PORT` | 否 | `8000` | 监听端口 | 与占用端口冲突 → 启动即 `Address already in use`；被 `PORT` 之类的同名变量干扰时，你会 curl 到一个"别的服务"上 |
| `AGENT_RUNTIME_LOG_LEVEL` | 否 | `INFO` | 结构化日志级别 | 设成 `ERROR` 会把 `auth.disabled` / `streaming.disabled` 这类**必须被说出来的降级**一起吞掉，事故变哑 |
| `AGENT_RUNTIME_DRAIN_TIMEOUT_S` | 否 | `10` | 优雅关闭等待上限（秒） | 设太小 → 飞行中的回合被硬切，`metrics.flushed` 可能来不及；设太大 → 编排器 `terminationGracePeriodSeconds` 先到，进程被 SIGKILL，反而丢了审计 |
| `AGENT_RUNTIME_METRIC_PREFIX` | 否 | `agent_runtime` | Prometheus 指标前缀 | 改了前缀但看板/告警规则没改 → **指标"消失"**（其实是换了名字）；冒烟第 6 项会失败并提示这一点 |
| `AGENT_RUNTIME_STREAMING` | 否 | `1` | 流式回合是否追加 `step` 事件 | 设 `0` 后 SSE 只剩 `accepted/started/completed` 三帧 —— 客户端会以为"模型一次吐完"，不是故障但会让人误判延迟来源 |
| `AGENT_RUNTIME_SSE_HEARTBEAT_S` | 否 | `15` | SSE 心跳间隔（秒）：静默期每 N 秒发一条 `: keepalive` 注释帧 | 设太大（或换成 0/非法值）→ 前者让 nginx `proxy_read_timeout`(默认 60s) 掐连接，表现为"流跑到一半莫名结束"；后者直接报错（0 会被拒，要关心跳请设很大而不是 0） |
| `AGENT_RUNTIME_MAX_BODY_BYTES` | 否 | `1048576`（1 MiB） | 请求体上限；超限 → **413**（按 `Content-Length` 先拒，不把 body 读进内存） | 设 `0` = 关闭（没有上限：一个坏客户端就能吃掉进程内存）；设太小（如 `1024`）→ 正常回合被 413，而错误看起来像客户端的问题；写错成 `lO24m`（字母 O）→ 启动 `ValueError`（刻意不静默回落默认值：那会把「我们配了但没生效」写进事故报告） |
| `AGENT_RUNTIME_HTTP_RATE` | 否 | `0`（关） | **每 IP 每分钟**请求数；超限 → **429**，只作用于 `/v1/*` | 设太大等于没限；设太小会打死正常客户端；`/healthz`、`/metrics` **不受它影响**——限流若把探针打成 429，编排器会判定实例已死并重启它，而实例其实是被自己的探针限死的 |
| `AGENT_RUNTIME_REQUEST_TIMEOUT_S` | 否 | `0`（关） | 请求超时；超时 → **504** | **504 之后那一回合仍在后台跑完**（继续烧 token、调工具、写 Context）：CPython 没有安全的强制终止线程手段，超时只能「停止等待」，日志里有一条 `http.timeout_detached` 如实记下。把它当资源上限用会算错账 |

### 2.2 鉴权

> 权威文档串：`serve.py::resolve_auth_mode` / `require_auth_config` /
> `build_authenticator_from_env`（`auth.py`）。
>
> **默认 fail-closed**：什么都没配 → **拒绝启动**（`StartupRefused`，带可复制的修复命令）。
> 这是本服务唯一一处"主动让进程起不来"的地方，而它正是最该存在的一处 ——
> 一个裸奔的进程（principal 由请求体给出 = 任何客户端都能自称任意租户）
> 比一个起不来的进程危险得多。

**模式判定顺序**（先看显式意图，再看配置，最后落到默认拒绝）：

| 顺序 | 输入 | 结果 |
|---|---|---|
| 1 | 调用方显式传 `explicit=True/False`（代码装配时） | 直接决定 |
| 2 | `AGENT_RUNTIME_AUTH=off/0/false/no` | **disabled**（硬开关，压过一切配置） |
| 2 | `AGENT_RUNTIME_AUTH=on/1/true/enforced` | enforced |
| 3 | 配了 `API_KEYS` 或 `JWT_SECRET` | enforced |
| 4 | `AGENT_RUNTIME_ALLOW_ANONYMOUS=1` | disabled |
| 5 | 都没配 | **enforced → 没有凭据 → 拒绝启动** |

第 3 条压过第 4 条是刻意的：`ALLOW_ANONYMOUS=1` 常是本机调试的残留，而
「已经配好 key、却仍在裸奔」是最难被发现的事故（它看起来一切正常）。
要让进程真的放开，用第 2 条那个**不会在"只是试一下"时被顺手留下**的显式开关。

> ⚠️ `AGENT_RUNTIME_ALLOW_ANONYMOUS=0` 这种写法**不满足** fail-closed 的逃生门：
> 它只表示"不允许匿名"，而"没有凭据"依然会拒绝启动。比"=" 更常见的坑是把它写成
> `false`/`no` 却以为"我关掉了匿名所以能启动"—— 不能。`_startup_hint()` 会专门点出这一条。

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_API_KEYS` | 生产二选一 | 空 | API key 表：`key:tenant:scopes[#user_id]`，条目用 `;` 或换行分隔 | ① 只切**前两个冒号**，按 `:` 全切会把 `runtime:run` 剁碎 → 启动 `ValueError`；② scopes 为空 → 启动即拒（"至少给 `runtime:run`"）；③ key 重复 → 启动即拒；④ **写错一个字符 = 全线 403 `invalid_api_key`**，而错误信息刻意不回显条目（一行坏配置里就有半个密钥） |
| `AGENT_RUNTIME_JWT_SECRET` | 生产二选一 | 空 | HS* 对称密钥（≥32 字节） | 密钥太短/用默认值 → 任何人可自签 token，**鉴权等于没有**；与 IdP 不同步 → 全线 403 `invalid_signature` |
| `AGENT_RUNTIME_JWT_ALGORITHMS` | 否 | `HS256` | 允许的算法（逗号分隔） | 加进 `none`/`RS256` 而密钥是 HS 家族 → 验签必然失败（401/403 全线），或者更糟：算法白名单失守 |
| `AGENT_RUNTIME_JWT_ISSUER` | 否 | 空 | 校验 `iss` | 留空 = **不校验 issuer**：任何用同一密钥签出的 token 都收（多 IdP 环境下等于放宽到一个 IdP 的密钥面） |
| `AGENT_RUNTIME_JWT_AUDIENCE` | 否 | 空 | 校验 `aud` | 留空 = 不校验受众：**给别的服务签的 token 也能用**，跨服务凭据混用 |
| `AGENT_RUNTIME_JWT_LEEWAY_S` | 否 | `0` | 时钟偏移容忍（秒） | 非数字 → 启动 `ValueError`；设 0 且机器时钟偏 >0 → `jwt_expired` 潮，客户端表现为"刚拿到 token 就过期" |
| `AGENT_RUNTIME_AUTH` | 否 | 空（按上面 5 步判定） | 硬开关：`off` = 连鉴权中间件都不装；`on` = 强制要求凭据 | 设成 `off` 等于裸奔（principal 由请求体给出）；设成 `on` 但没配凭据 → **拒绝启动**；写错值（`disable`/`0ff`）→ 落回按配置判定，你以为关了其实还开着（启动日志会打 `auth.enforced`，`/v1/info` 的 `auth.mode` 也写着） |
| `AGENT_RUNTIME_ALLOW_ANONYMOUS` | 否 | `0` | 安装鉴权器但**允许匿名**：无凭据请求拿到**空 scope** 的匿名身份 | 设 `1` 后进程能起、无凭据也能进，但进不了 `runtime:run`（Gateway 仍 fail-closed 403）—— 于是「服务起来了、回合全 403」这种最难查的组合就出现了；它**不**等于裸奔，日志会有 `auth.anonymous_allowed`；这条最容易被「先跑通再说」留在生产 |
| `AGENT_RUNTIME_AUTH_EXEMPT` | 否 | `/healthz` | 豁免路径（逗号分隔，fnmatch） | 把 `/readyz` 加进豁免 = 就绪探针可被未授权调用（可接受）；把 `/v1/*` 加进去 = **鉴权形同虚设**；写成空串 = 连 `/healthz` 都要凭据 → liveness 失败 → 编排器**无限重启**，而重启会砍掉最后的 drain/flush |
| `AGENT_RUNTIME_AUTH_PROTECT` | 否 | 空（=全保护） | 只保护命中的路径 | 设成 `/v1/*` 而漏了 `/metrics` → 业务量对外裸奔（`/metrics` 默认受保护正是这个原因） |
| `AGENT_RUNTIME_TRUSTED_PROXIES` | 否 | 空 | 可信反代网段（逗号分隔，如 `10.0.0.0/8`）。只有 socket 对端在此网段内才采信 `X-Forwarded-For` | 空 = **完全忽略 XFF**（安全默认，防伪造）。配错的后果是"所有请求共享一个限流桶"（比预期更严），方向安全 |
| `AGENT_RUNTIME_RATE_LIMIT_DISTRIBUTED` | 否 | `0` | `1` = 用 KV 做**跨副本**限流计数 | 多副本却不开 → 实际阈值 × 副本数。开了但 KV 是进程内的 → **启动报错**。`/v1/info` 的 `rate_limit_distributed` 可自查 |
| `AGENT_RUNTIME_METRICS_AUTH` | 否 | `1` | `/metrics` 是否要鉴权 | 设 `0` → 抓取端方便了，但租户名/回合数/错误率对外可读 |
| `AGENT_RUNTIME_METRICS_TOKEN` | 否 | 空 | `/metrics` 专用 Bearer 令牌 | 不配而 `METRICS_AUTH=1` → 抓取端 401，看板全空；配了但抓取端没带 → 同样 401（**这是最常见的"指标没了"原因**） |

**401 / 403 / 500 的边界**（验收口径，按 `reason` 冒号前的原因码判定）：

| 状态 | 含义 | 典型原因码 | 带 `WWW-Authenticate` |
|---|---|---|---|
| 401 | 你没给 | `missing_credentials` | 有 |
| 403 | 你给了但不认 | `invalid_api_key` / `jwt_expired` / `scheme_not_configured` / `principal_conflict` | 无 |
| 500 | 我没配好 | `auth_not_configured` / `auth_internal_error` | 无 |

> `auth_not_configured` 在**进程入口**已经看不到了：`main()` 走 fail-closed，没凭据就直接 `StartupRefused`（进程根本不起）。它只会在**代码装配**的应用里出现（例如你自己 `create_app(authenticator=None, auth_required=True)`）。

### 2.3 工具（Tool Plane）

工具**不是**靠环境变量决定的 —— 一个进程能干什么由装配代码决定。
声明式入口是 `toolconfig.py` + 一个 YAML 文件，用法见 [`TOOLS.md`](TOOLS.md)。

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_TOOLS` | 否 | 空 | 工具配置 YAML 的路径（`toolconfig.load_tools_from_env()` 只认这一个名字） | 空/未设置 = **显式空工具表**（合法，模型看到"没有工具"）；设了但文件不存在 / YAML 语法错 / 工具重名 / schema 非法 / 写了 `irreversible` 而 `allow_irreversible=false` → **`RuntimeError` 拒绝启动**（`工具配置加载失败：文件: 位置: 原因`），不静默少装一半；路径写错成相对路径时按**进程 cwd** 解析，systemd/容器里常常对不上 |

> 启动日志里会有 `tools.loaded count=N names=[...]`（N=0 表示空表）和每条 `tools.config_warning`；
> 关停时 `tools.closed`（MCP 子进程在 drain 之后才收，正在跑的工具调用需要连接还活着）。
> 一个工具能干什么、`side_effect`/`risk_tier` 怎么填：[`TOOLS.md`](TOOLS.md)。
>
> 相关但**不是**工具的变量：`AGENT_RUNTIME_PROMPT_TOKENS`（要留给工具清单的预算）、
> `AGENT_RUNTIME_RATE_LIMIT`（回合级配额）。工具自己的限制（`timeout_ms` /
> `max_result_bytes` / `per_turn_limit` / `required_scope`）写在 YAML 里，不在这里。

### 2.4 模型（LLM）

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_EXTRACTOR` | 否 | 空 | `module:attr`，指向 `contracts.Extractor` 实现（**Agent 层**）。配了才会装 Memory | 拼错 → **拒绝启动**（不静默）；空 → Memory 不启用，模型看起来"没记忆" |
| `AGENT_RUNTIME_MEMORY_PROVIDER` | 否 | `memory` | Memory 的 Provider：`memory` / `vector` / `module:attr` | 配 `vector` 但后端不可用 → 拒绝启动（不放行空 Provider） |
| `AGENT_RUNTIME_ROUTER` | 否 | 空 | `module:attr`，指向 `contracts.RouterProtocol` 实现；空则不做路由 | 空 → Context 里没有 `agent_state`，多 Agent 场景退化 |
| `AGENT_RUNTIME_PLANNER` | 否 | 空 | `module:attr`，指向 `contracts.PlannerProtocol` 实现。**需要参数的要写成工厂函数** | 直接给需要参数的类 → 启动报错并提示写工厂函数（不留到每轮 loop_error） |
| `AGENT_RUNTIME_SKILLS` | 否 | 空 | `module:attr`，返回技能清单 | 空 → `skill_state` 为空，Builder 的技能段消失 |
| `AGENT_RUNTIME_PROMPT_PROFILE` | 否 | 空 | `module:attr`，返回 `builder.TokenProfile` | 空 → 用 Runtime 的保守默认（中文会低估） |
| `AGENT_RUNTIME_PROMPT_TEMPLATE` | 否 | 空 | `module:attr`，返回 Prompt 模板（含 system 人格） | 空 → 用 Runtime 的空模板（没有业务人格） |
| `AGENT_RUNTIME_RECALL_K` | 否 | `5` | Memory 每轮召回条数 | 太大 → Prompt 被记忆挤占；0 → 不召回 |
| `AGENT_RUNTIME_LLM_PREFIX` | 否 | `OPENAI` | 模型适配器读的**前缀**：`{PREFIX}_API_KEY` / `{PREFIX}_BASE_URL` / `{PREFIX}_MODEL`。用 babelark 那套凭据时设成 `BABEL` | 不设而只用 `BABEL_*` → 报 `OPENAI_API_KEY / OPENAI_BASE_URL 不完整`（错误信息里带当前前缀） |
| `AGENT_RUNTIME_PROMPT_TOKENS` | 否 | `8000` | Prompt 预算（Builder 的 total_tokens） | 设太小 → 工具清单/历史被裁掉，模型说"没有可用工具"；非数字 → 启动 `ValueError` |
| `AGENT_RUNTIME_RATE_LIMIT` | 否 | `60` | 每租户每 60s 的回合上限 | 设 0/负数 → 所有请求 429（配额窗口判超限）；多副本时窗口各自独立，**实际放行 = N 倍** |
| `OPENAI_BASE_URL` | `LLM=openai` 时必填 | — | 兼容 OpenAI 协议的网关地址 | 缺失 → `AGENT_RUNTIME_LLM=openai` 但 adapter 拿不到配置 → 回合 500 `OpenAI_API_KEY / OPENAI_BASE_URL 不完整` |
| `OPENAI_API_KEY` | 否 | 空 | 凭据 | **允许为空**（本地 Ollama/vLLM 常不校验）；填错 → 上游 401，回合 500 |
| `OPENAI_MODEL` | `LLM=openai` 时必填 | — | 模型名 | 缺失同 `BASE_URL`；名字错 → 上游 400/404 |
| `OPENAI_TIMEOUT_MS` | 否 | adapter 默认 | 单次调用超时 | 小于模型首 token 延迟 → 全部超时；reasoning 模型尤其明显 |
| `OPENAI_FALLBACK_MODELS` | 否 | 空 | 逗号分隔的兜底模型 | 配了不存在的名字 → 兜底也失败，错误信息指向**兜底模型**，让人查错方向 |
| `OPENAI_TEMPERATURE` | 否 | adapter 默认 | 采样温度 | 非数字 → 被静默忽略（不报错），你以为设了 |
| `OPENAI_PRICE_IN_PER_M` / `OPENAI_PRICE_OUT_PER_M` | 否 | 0 | 成本核算单价（每百万 token，入/出分开） | 不配 → `budget.cost_total_usd` 恒为 0，**成本预算永不触发**，超支无告警；配成「每千 token」的价 → 成本放大 1000 倍，预算瞬间打满 |

> **前缀规则**：不是固定的 `OPENAI_*`。`adapters/openai_compat.from_env(prefix)` 读
> `{PREFIX}_BASE_URL` / `{PREFIX}_API_KEY` / `{PREFIX}_MODEL` 等。
> 但 `serve.py` 的 `build_gateway_from_env` **硬编码了 `from_env("OPENAI")`** ——
> 服务端想换前缀目前得改代码（见 §8 已知限制）。
> `scripts/real_model_smoke.py` 走的是另一套：`BABEL_KEY` / `BABEL_BASE` / `BABEL_MODEL`。

### 2.5 存储（Context / KV）

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_STORE` | 否 | `memory` | `memory` \| `sqlite` \| `file` \| **`redis`** | 拼错（`sqlite3`/`SQLite`）→ **不报错**，`/readyz` 报 store 不可用（`未知 AGENT_RUNTIME_STORE=...`），而回合仍能跑（因为 Gateway 拿到的是 `_Unavailable`）→ 续跑全部失效。**多副本必须用 `redis`**：`sqlite`/`file` 是单机文件，A 写的会话 B 读不到（实测 `turn_index` 各自从 1 开始） |
| `AGENT_RUNTIME_STORE_TTL_S` | 否 | `86400` | `AGENT_RUNTIME_STORE=redis` 时的 key TTL（秒） | 设太小 → 会话在用户还在聊的时候过期；设太大 → Redis 里的旧 Context 不清，内存持续涨 |
| **`AGENT_RUNTIME_WORKERS`** | 否 | `0`（单进程） | uvicorn 多 worker 数 | **不是免费加速**：`uvicorn --workers N` 只接受导入字符串，传 app 对象会被**静默忽略**（已加 `app_factory()` 修掉）。更要紧的是语义：多 worker ≈ 多副本，限流/配额/`audit_log`/workflow 状态表**各进程一份**，必须同时开 `RATE_LIMIT_DISTRIBUTED=1` + 共享 Redis，否则阈值 × N |
| **`AGENT_RUNTIME_TOKEN_PROFILE`** | 否 | `calibrated` | `calibrated` \| `conservative` | **默认用实测标定档（CJK 0.68 tok/字）**。设 `conservative` 退回 1.0 tok/字——纯中文会**高估 1.57×**，`budget=20000` 只能用 12760。换到未标定的模型时才该用 conservative |
| **`AGENT_RUNTIME_GOVERNANCE_QUOTA`** | 否 | `60` | 治理面每 `kind` 每分钟配额 | 与 `AGENT_RUNTIME_RATE_LIMIT` 是**两道独立的闸**：那个管回合数，这个管 `runtime_turn`/`tool_call`/`llm_call` 各自计数。设 `0` = 关闭。压测时必须关，否则会被它的默认 60 拦住而报错只说 `quota exceeded` |
| `AGENT_RUNTIME_STORE`（**副作用落盘也用它**） | — | `memory` | 非只读工具成功后落一份 checkpoint | **`memory` → 崩溃后无法知道"哪一步真做了"**。生产必须 `sqlite`/`file` |
| `AGENT_RUNTIME_STORE`（workflow 也用它） | — | 同上 | workflow 状态与 Context 共用这个 store | **`memory` → 重启后发布流水线状态全丢**（含"跑到第几层、哪个节点被跳过"）。生产必须 `sqlite`/`file` |
| `AGENT_RUNTIME_STORE_PATH` | `sqlite` 时建议 | `:memory:` | sqlite 文件路径 | **不配 = 内存库**：重启后 `ctx_id` 全部丢失（表现为"续跑/审计没了"）；目录不可写 → 构造失败 → `/readyz` 503 |
| `AGENT_RUNTIME_DB` | 否 | — | **`STORE_PATH` 的别名**（后加，两个都认，冲突时 `STORE_PATH` 优先） | 只在老版本认 `STORE_PATH` 时它才被忽略。本版本两者都认；混用时**以 `STORE_PATH` 为准**，只配 `_DB` 的老部署升级后行为可能变 |
| `AGENT_RUNTIME_STORE_ROOT` | `file` 时建议 | 空 | FileStore 根目录 | 留空 → 用默认目录；填了但目录不存在/不可写 → store 不可用，产物写入失败 |
| `AGENT_RUNTIME_KV` | 否 | `memory` | `memory` \| `redis` | 拼错 → kv 不可用（`/readyz` 503）；`redis` 但连不上 → 同上，且**每次探针耗时 2s**（`READY_TIMEOUT_S`） |
| `AGENT_RUNTIME_REDIS_HOST` | 否 | `127.0.0.1` | Redis 地址 | 指错 → kv 探针失败 → `/readyz` 503，`/v1/turns` 仍 200（**探针红、业务绿**，容易被忽略） |
| `AGENT_RUNTIME_REDIS_PORT` | 否 | `6379` | 端口 | 非数字 → 构造异常 → kv 不可用 |
| `AGENT_RUNTIME_REDIS_DB` | 否 | `0` | 库号 | 与别的服务共用 DB 且前缀没隔开 → key 互相覆盖 |
| `AGENT_RUNTIME_REDIS_PASSWORD` | 否 | 空 | 口令 | 漏配 → `NOAUTH` → kv 不可用；口令进 `/v1/info`？不会（白名单 scrub） |
| `AGENT_RUNTIME_KEY_PREFIX` | 否 | `rt:` | KV key 前缀 | 多环境共用同一 Redis 且前缀相同 → **测试环境覆盖生产 key** |

### 2.6 向量与切分

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `VECTOR_BACKEND` | 否 | `memory` | `memory` \| `qdrant` \| `milvus` \| `auto` | `serve.py` 刻意**不用 `auto`**（auto 会挑机器上第一个装了库的后端，不可复现）。配 `milvus` 但没装 `pymilvus` → **报错说明原因**，不静默退化；配未知值 → `_Unavailable` + `/readyz` 失败 |
| `VECTOR_DIM` | milvus 必填 / qdrant 首次必填 | `0` | 向量维度 | 建集合时错维度 → 后续 `search` 抛维度不匹配；改维度需**重建集合**（已有数据不可迁移） |
| `QDRANT_URL` | qdrant 远程时 | 空 | 服务地址 | 空 = 走 `QDRANT_LOCATION` 本地模式；远程地址写错 → 后端不可用 |
| `QDRANT_LOCATION` | 否 | `:memory:` | 本地模式路径 / `:memory:` | 指到不可写目录 → 后端不可用；`:memory:` 重启即丢 |
| `QDRANT_API_KEY` | 否 | 空 | 云端凭据 | 漏配 → 401 → 后端不可用 |
| `QDRANT_COLLECTION` | 否 | `vectors` | 集合名 | 与别的应用同名 → **写入成功但查不到**（payload 结构不同、filter 打不中） |
| `MILVUS_URI` | milvus 时 | `http://127.0.0.1:19530` | **决定形态**：以 `.db` 结尾 = milvus-lite（进程内、单机）；`http(s)://host:19530` = 远程集群 | 传目录 → `needs start with [unix, http, https, tcp] or a local file ends`；以为在连集群实际在跑 lite（或反过来）→ **单机 vs 跨副本共享的语义完全不同**，扩容后发现数据不在 |
| `MILVUS_TOKEN` | 否 | 空 | 集群凭据 | 漏配 → 连接被拒 → 后端不可用 |
| `MILVUS_COLLECTION` | 否 | `vectors` | 集合名 | 同 Qdrant：同名不同 schema → 查不到 |
| `SPLITTER` | 否 | `heading` | `heading` \| `llama-index` | 配 `llama-index` 但没装包 → 装配失败（**不是静默退化**） |
| `SPLITTER_MAX_CHARS` | 否 | `600` | 单块最大字符 | 过大 → 单块超预算被 Builder 裁掉；过小 → 块数爆炸、检索噪声上升 |
| `SPLITTER_OVERLAP` | 否 | `60` | 块间重叠 | ≥ `MAX_CHARS` → 切分不收敛（块永远"还差一点"），CPU 拉满 |

### 2.7 会话（多轮）

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_SESSION` | 否 | `0` | `1` = 启用会话多轮承接 | 不设 → 每轮都是新会话，客户端传 `session_id` 也不会承接（**表现为"多轮对话不工作"**）；设 `1` 但会话层装配失败 → **启动即 `RuntimeError`**（刻意不静默退化成"没有会话"） |
| `AGENT_RUNTIME_SESSION_MAX_HISTORY` | 否 | `20` | 保留的最大轮数 | 过大 → 老内容挤占 `PROMPT_TOKENS`，当轮工具清单被裁；过小 → 早期决议丢失 |
| `AGENT_RUNTIME_SESSION_TTL_S` | 否 | `604800`（7 天） | 会话过期秒数 | 非数字 → 启动异常；过短 → 用户隔天回来"会话没了"；过长 + 落盘 store → 表无限增长 |

> 会话落盘用的是**同一个 store**（`AGENT_RUNTIME_STORE`）：`memory` 时重启即丢，
> `sqlite` 时跟着 `STORE_PATH`。只承接问答骨架（`role`/`text`），
> **不承接 `tool_results`** —— 理由见 `README.md` 的「会话多轮承接」。

### 2.8 可观测

| 变量 | 必填 | 默认 | 作用 | 配错的后果 |
|---|---|---|---|---|
| `AGENT_RUNTIME_METRIC_PREFIX` | 否 | `agent_runtime` | 见 §2.1 | 看板/告警规则不改 → 指标"消失" |
| `AGENT_RUNTIME_METRICS_AUTH` / `_TOKEN` | 否 | `1` / 空 | 见 §2.2 | 抓取端 401 → 看板全空 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 否 | 空 | span 导出的 OTLP/HTTP 地址（由 `plane/quality.configure_otel()` 读取） | 空 = 只保留进程内 Tracer（**不发任何 trace**）；地址不可达 → 退出时刷 `Failed to export spans` 噪声，且 `set_tracer_provider` 是"首次生效、没法撤销"的进程级副作用 |
| `LANGFUSE_*` | 否 | 空 | Langfuse 导出（`plane/langfuse_export.configure_langfuse(public_key=..., host=...)`） | 这些是**参数**不是 serve.py 读取的环境变量：本版本的 `serve.py` **没有接 Langfuse/OTel 装配**（见 §8 已知限制） |

**当前只有一条观测链路是开箱即用的**：`/metrics`（Prometheus 抓取）+
结构化 JSON 日志到 stdout + 关停时的 `metrics.flushed(bytes=...)`。
span 导出要用代码装配（`configure_otel` / `configure_langfuse`），`serve.py` 里没有接线。

`/metrics` 暴露的指标（`AGENT_RUNTIME_METRIC_PREFIX=agent_runtime` 时）：

```
agent_runtime_up                        gauge     进程存活
agent_runtime_turns_total               counter   按 ok/tenant 分标签
agent_runtime_turn_duration_seconds     histogram 回合耗时
agent_runtime_turn_tokens_total         counter   token 消耗
agent_runtime_turn_errors_total         counter   按错误码分标签
agent_runtime_http_requests_total       counter   按 path/status 分标签（含 401/403）
```

`/v1/info` 的 `config` 字段只回显**白名单**变量（`VECTOR_BACKEND` / `VECTOR_DIM` /
`AGENT_RUNTIME_STORE` / `_KV` / `_LLM` / `_PROMPT_TOKENS` / `_METRIC_PREFIX` /
`_ALLOW_ANONYMOUS` / `_AUTH` / `_MAX_BODY_BYTES` / `_HTTP_RATE` / `_REQUEST_TIMEOUT_S`），
且值过 scrub：它常被放在不需要鉴权的内网端口上，dump 整个 `os.environ` 等于一次全量凭证泄漏。
**"我在 `/v1/info` 里看不到 `AGENT_RUNTIME_API_KEYS`"是设计，不是 bug。**

---

## 3. 三种典型部署形态

### 3.1 本机开发（scripted LLM + 显式裸奔 + 内存存储）

用于：改代码、跑用例、看事件流。重启即丢数据是**特性**（每次都是干净状态）。

```dotenv
# ── 本机开发：最小可跑，零外部依赖 ──
AGENT_RUNTIME_HOST=127.0.0.1
AGENT_RUNTIME_PORT=8000
AGENT_RUNTIME_LOG_LEVEL=DEBUG
AGENT_RUNTIME_LLM=scripted

# 存储：全在进程内
AGENT_RUNTIME_STORE=memory
AGENT_RUNTIME_KV=memory
VECTOR_BACKEND=memory

# 鉴权：显式关掉（不加这一行 → fail-closed 会拒绝启动）
AGENT_RUNTIME_AUTH=off

# 下面两个刻意留空：ALLOW_ANONYMOUS=1 也能起来，但那是"装了鉴权器却允许匿名"，
# 无凭据请求进不了 runtime:run（回合全 403，比裸奔更难查）。本机就用 AUTH=off。
# AGENT_RUNTIME_API_KEYS=
# AGENT_RUNTIME_JWT_SECRET=

# 会话：关（单轮语义最省心）
AGENT_RUNTIME_SESSION=0
```

```bash
# 上面那块存成 .env 后：
set -a; . ./.env; set +a
.venv/bin/python serve.py
./scripts/smoke.sh --port 8000 --check-shutdown   # 第 7 项会是 SKIP，这是预期
```

`/v1/info` 会显示 `backends: {store: MemoryStore, kv: in-memory, vector: memory}`、
`auth.enabled: false` / `auth.mode: "disabled"`。
**启动日志里必有一行 `auth.disabled`**（带一句「任何客户端都能自称任意租户」）——
那不是错误，是提醒。 `auth.disabled` 这个词在日志里出现，就说明这个进程在上线前
必须被划掉重来。

### 3.2 内网服务（真模型 + API Key + sqlite + qdrant）

用于：一个团队内部用，有租户隔离要求，允许单机/单副本。

```dotenv
# ── 内网服务：真模型 + API key + 持久化 ──
AGENT_RUNTIME_HOST=0.0.0.0              # 容器/跨机必须改，否则只监听回环
AGENT_RUNTIME_PORT=8000
AGENT_RUNTIME_LOG_LEVEL=INFO
AGENT_RUNTIME_DRAIN_TIMEOUT_S=15

# 模型（serve.py 固定读 OPENAI 前缀）
AGENT_RUNTIME_LLM=openai
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=deepseek-flash
OPENAI_TIMEOUT_MS=60000
OPENAI_PRICE_IN_PER_M=0.27
OPENAI_PRICE_OUT_PER_M=1.10
AGENT_RUNTIME_PROMPT_TOKENS=8000
AGENT_RUNTIME_RATE_LIMIT=60

# 存储：sqlite 落盘（注意是 STORE_PATH，不是别的名字）
AGENT_RUNTIME_STORE=sqlite
AGENT_RUNTIME_STORE_PATH=/var/lib/agent-runtime/rt.db
AGENT_RUNTIME_KV=memory                # 单副本可接受；多副本见 §5.2

# 向量：qdrant 远程
VECTOR_BACKEND=qdrant
QDRANT_URL=http://qdrant.internal:6333
QDRANT_COLLECTION=agent_runtime_vectors
VECTOR_DIM=1024
SPLITTER=heading

# 鉴权：至少配一种
AGENT_RUNTIME_API_KEYS=sk-acme-1:acme:runtime:run tool.read#u_alben;sk-beta-1:beta:runtime:run
AGENT_RUNTIME_ALLOW_ANONYMOUS=0
AGENT_RUNTIME_AUTH_EXEMPT=/healthz
AGENT_RUNTIME_METRICS_AUTH=1
AGENT_RUNTIME_METRICS_TOKEN=<openssl rand -hex 24>

# HTTP 层资源边界（默认全关；内网服务至少给个体量上限）
AGENT_RUNTIME_MAX_BODY_BYTES=1048576
AGENT_RUNTIME_HTTP_RATE=0
AGENT_RUNTIME_REQUEST_TIMEOUT_S=0

# 会话：开（sqlite 落盘能跨重启）
AGENT_RUNTIME_SESSION=1
AGENT_RUNTIME_SESSION_MAX_HISTORY=20
AGENT_RUNTIME_SESSION_TTL_S=604800
```

```bash
mkdir -p /var/lib/agent-runtime
set -a; . ./.env; set +a
.venv/bin/python serve.py
./scripts/smoke.sh --port 8000 --key sk-acme-1 --check-shutdown
```

这一形态下冒烟应该 **9 项全 PASS**：第 7 项（未授权被拒）从 SKIP 变成 PASS
—— 那是"鉴权真的生效"的唯一证据。

### 3.3 生产（真模型 + JWT + redis + milvus 集群 + OTLP）

用于：多副本、跨重启、有外部 IdP 与集中观测。

```dotenv
# ── 生产：多副本 + 外部 IdP + 分布式依赖 ──
AGENT_RUNTIME_HOST=0.0.0.0
AGENT_RUNTIME_PORT=8000
AGENT_RUNTIME_LOG_LEVEL=INFO
AGENT_RUNTIME_DRAIN_TIMEOUT_S=25        # 要 > 最长单回合耗时；配合 terminationGracePeriodSeconds
AGENT_RUNTIME_METRIC_PREFIX=agent_runtime
AGENT_RUNTIME_STREAMING=1

# 模型
AGENT_RUNTIME_LLM=openai
OPENAI_BASE_URL=https://gw.internal/v1
OPENAI_API_KEY=${MODEL_GATEWAY_KEY}
OPENAI_MODEL=deepseek-flash
OPENAI_FALLBACK_MODELS=deepseek-chat,qwen-plus
OPENAI_TIMEOUT_MS=120000
OPENAI_PRICE_IN_PER_M=0.27
OPENAI_PRICE_OUT_PER_M=1.10
AGENT_RUNTIME_PROMPT_TOKENS=16000
AGENT_RUNTIME_RATE_LIMIT=60

# 存储：sqlite 单文件不够 → 挂共享卷；跨副本续跑请等 RedisStore（未实现，见 §8）
AGENT_RUNTIME_STORE=sqlite
AGENT_RUNTIME_STORE_PATH=/data/agent-runtime/rt.db
AGENT_RUNTIME_KV=redis
AGENT_RUNTIME_REDIS_HOST=redis.internal
AGENT_RUNTIME_REDIS_PORT=6379
AGENT_RUNTIME_REDIS_DB=3
AGENT_RUNTIME_REDIS_PASSWORD=${REDIS_PASSWORD}
AGENT_RUNTIME_KEY_PREFIX=rt:prod:

# 向量：远程 milvus 集群（http:// = 集群；.db 结尾 = lite）
VECTOR_BACKEND=milvus
MILVUS_URI=http://milvus.internal:19530
MILVUS_TOKEN=${MILVUS_TOKEN}
MILVUS_COLLECTION=agent_runtime_vectors
VECTOR_DIM=1024
SPLITTER=llama-index
SPLITTER_MAX_CHARS=600
SPLITTER_OVERLAP=60

# 鉴权：JWT（外部 IdP），API key 只留给内部批处理
AGENT_RUNTIME_JWT_SECRET=${JWT_HS256_SECRET}
AGENT_RUNTIME_JWT_ALGORITHMS=HS256
AGENT_RUNTIME_JWT_ISSUER=https://idp.example.com
AGENT_RUNTIME_JWT_AUDIENCE=agent-runtime
AGENT_RUNTIME_JWT_LEEWAY_S=30
AGENT_RUNTIME_API_KEYS=sk-batch-1:ops:runtime:run#svc_batch
AGENT_RUNTIME_ALLOW_ANONYMOUS=0
AGENT_RUNTIME_AUTH_EXEMPT=/healthz
AGENT_RUNTIME_AUTH_PROTECT=
AGENT_RUNTIME_METRICS_AUTH=1
AGENT_RUNTIME_METRICS_TOKEN=${METRICS_TOKEN}

# 会话
AGENT_RUNTIME_SESSION=1
AGENT_RUNTIME_SESSION_MAX_HISTORY=20
AGENT_RUNTIME_SESSION_TTL_S=604800

# HTTP 层资源边界（生产必须显式给；默认 0=关）
AGENT_RUNTIME_MAX_BODY_BYTES=262144
AGENT_RUNTIME_HTTP_RATE=600          # 每 IP 每分钟；/healthz、/metrics 天然豁免
AGENT_RUNTIME_REQUEST_TIMEOUT_S=120  # 504 之后回合仍在后台跑完，见 §2.1

# 可观测：⚠️ 本版本 serve.py 不读这两个（需代码装配，见 §8）
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
```

> **生产必须先解决的两件事**（都在 §8 标了阻塞）：
> ① `AGENT_RUNTIME_STORE=sqlite` 在**多副本**下是每副本一个文件，跨副本续跑不成立；
> ③ 模型前缀**已可配**：`AGENT_RUNTIME_LLM_PREFIX=BABEL` 即可用 `BABEL_API_KEY/BASE_URL/MODEL`，
>    与 `scripts/real_model_smoke.py` 的口径对齐了（早期硬编码 `OPENAI` 是两处不一致的根源）。

> ② OTLP **已接线**（`main()` 里 `configure_otel(..., install=True)`）：配上 endpoint 就会真发，
>    启动日志出现 `otel.installed` 即生效；未配置时打 `otel.disabled` 并只保留进程内 Tracer。

---

## 4. 探针与优雅关闭

### 4.1 `/healthz` vs `/readyz`

| | `/healthz` | `/readyz` |
|---|---|---|
| 回答什么 | 进程还在吗 | 现在能不能接流量 |
| 探什么 | 什么都不探（连依赖都不碰） | store / kv / vector 三件逐个探，每件有超时 |
| 关停期间 | **仍然 200**（刻意） | 503 + `reason: shutting_down` |
| 鉴权 | 默认豁免（`AGENT_RUNTIME_AUTH_EXEMPT=/healthz`） | **默认不豁免**（要凭据） |
| 拿它做什么 | livenessProbe / 重启判据 | readinessProbe / 摘流判据 |

为什么关停期间 `/healthz` 还返回 200：liveness 失败会触发**重启**，
而一个正在优雅排空的进程被重启，等于把最后的 flush 和审计路径直接砍掉。
摘流是 `/readyz` 的事 —— 两件事混在一起，事故时你会同时失去排空和诊断。

`/readyz` 的响应形状（失败时必须能一眼看出哪件坏了）：

```json
{"ready":false,
 "checks":{"store":{"critical":true,"ok":true,"kind":"SQLiteStore","latency_ms":0.4},
           "kv":{"critical":true,"ok":false,"kind":"redis",
                 "detail":"ping失败: ConnectionRefusedError ...","latency_ms":2.0},
           "vector":{"critical":true,"ok":true,"kind":"milvus","latency_ms":12.1}},
 "reason":"unhealthy_dependencies: kv",
 "shutting_down":false}
```

实测（把 Redis 指到死端口）：`HTTP 503`，`reason=unhealthy_dependencies: kv`，
`冒烟第 2 项`照原样打印 `坏项=kv[critical]: ping...`。

### 4.2 SIGTERM 的 drain 顺序

```
SIGTERM
  → SignalDriver.install(loop) 的回调：shutdown.begin()（停接新请求，/readyz 立刻 503）
  → 后台任务跑 shutdown.wait()（阻塞在 Condition，不占事件循环线程）
  → 等在飞行的请求（含未吐完的 SSE 流）走完，上限 AGENT_RUNTIME_DRAIN_TIMEOUT_S
  → logger: server.draining  (inflight=N, active={turn_stream:1})
  → 关闭钩子：_flush_observability() → 渲染一次指标 → logger: metrics.flushed (bytes=N)
  → logger: server.drained (report={drained:true, remaining:0, elapsed_s})
  → on_drained → server.should_exit = True → uvicorn 退出
  → lifespan 收尾：再跑一次 begin+wait（钩子只执行一次）→ logger: server.stopped
```

设计要点（都已被测试钉住）：

- **信号在 lifespan 里注册**（不是创建 app 时）：那时才有 running loop，且一定**晚于**
  uvicorn 自己装的处理器 —— 后注册生效，所以退出走"先排空再退"，不是"立刻断开"。
- **信号处理函数里只做两件轻活**（`begin()` + 起后台任务）：在里面阻塞等待等于停掉
  事件循环，飞行中的请求再也没机会完成，drain 必然"超时"。
- **飞行中的 SSE 流会被计数**（`shutdown.track("turn_stream")`）：drain 必须知道
  "还有一个流没吐完"，否则长连接被硬切。
- 关停期间 `/v1/turns` 直接 503（`{"error":"shutting_down"}`），
  `/v1/turns/stream` 只发一帧 `rejected`（客户端状态机据此收口，不启动"半条流"）。

实测（9 项冒烟的 `--check-shutdown`）：

```
draining=1 metrics.flushed=1 drained=1 → 进程退出
```

### 4.3 Kubernetes 配置片段

```yaml
spec:
  # 必须 > DRAIN_TIMEOUT_S + 一个健康探针周期，否则 SIGKILL 会把排空切掉
  terminationGracePeriodSeconds: 40
  containers:
    - name: agent-runtime
      image: agent-runtime:0.1.0
      command: [".venv/bin/python", "serve.py"]
      ports: [{name: http, containerPort: 8000}]
      envFrom:
        - secretRef: {name: agent-runtime-secrets}   # API keys / JWT / 上游凭据
        - configMapRef: {name: agent-runtime-config} # 非敏感配置
      env:
        - name: AGENT_RUNTIME_HOST
          value: "0.0.0.0"        # 忘了这条 → 只监听回环，probe 全红
        - name: AGENT_RUNTIME_DRAIN_TIMEOUT_S
          value: "25"
      livenessProbe:              # 只看进程：不要给它加依赖检查
        httpGet: {path: /healthz, port: http}
        initialDelaySeconds: 5
        periodSeconds: 10
        failureThreshold: 3
      readinessProbe:             # 探依赖；/readyz 默认要凭据 → 见下方注释
        httpGet:
          path: /readyz
          port: http
          httpHeaders:
            - name: X-API-Key
              value: $(PROBE_API_KEY)   # 放一个只有 runtime:run 的探针 key
        initialDelaySeconds: 3
        periodSeconds: 5
        failureThreshold: 2
      startupProbe:
        httpGet: {path: /healthz, port: http}
        failureThreshold: 30
        periodSeconds: 2
```

三条容易踩的：

1. **`/readyz` 默认要凭据**：probe 必须带 `X-API-Key`/Bearer，或者把 `/readyz`
   加进 `AGENT_RUNTIME_AUTH_EXEMPT`。忘了这条 → readiness 永远红 → Pod 永远不接流量，
   而 `/healthz` 是绿的，看起来"服务好的、就是没流量"。
2. **liveness 不要探 `/readyz`**：依赖抖动会把健康进程全部重启，故障被放大成雪崩。
3. **`terminationGracePeriodSeconds` 要大于 drain**：否则排空还没做完就被 SIGKILL。

---

## 5. 容量与调优

### 5.1 token 预算

`AGENT_RUNTIME_PROMPT_TOKENS`（默认 8000）是 Builder 的总预算，里面要装：
系统指令 + 工具清单 + 检索内容 + 最近 history + 当轮 goal。

实测数字（`README.md` 的「真实模型实测」章节，deepseek-flash / babelark 网关）：

| 项 | 实测 | 含义 |
|---|---|---|
| 中文 390 字 | 真实 **235~256** tok；标定启发式给 **249**（误差 2~6%）；未标定给 480（高估 1.9x） | 中文按"1 字 ≈ 0.6 tok"估 |
| 英文 | 标定 229 vs 真实 230 | 英文接近 1 word ≈ 1.3 tok |
| 工具 schema 固定开销 | 单个工具的 `tools` 数组带来 **51~274 tok**（跨 4 个模型：gemini 51 / GLM 157 / deepseek 274 / qwen 273） | `builder.py` 取上界：固定项 **240 tok**；186 字符的 schema 另加 **+258 tok** |

调参含义：

- **`AGENT_RUNTIME_PROMPT_TOKENS` 至少留出 `N × 300` 给工具清单**（N = 注册的工具数）。
  预算给小了，Builder 会**先裁工具清单/历史**再裁 goal —— 表现是模型说"没有可用工具"，
  而不是报错。这个项目的 `README.md` 里专门记过一次这个坑（"工具清单从来没进过 Prompt"）。
- 换模型/换网关**必须重新标定 tools 开销**：51 与 274 差 5 倍，写死单值必然偏差。
  `builder.py` 的 `TOOL_OVERHEAD` 已按 profile 标定（`note="measured across 4 models; ..."`）。
- reasoning 模型（`deepseek-reasoner` 一类）会把 `max_tokens` 全花在 reasoning 上，
  `content` 返回 null + `finish_reason=length`。adapter 已透传 `max_tokens`，
  但预算要按 **2~3 倍**留。

### 5.2 并发与限流

- **两层限流，别混**：
  - `AGENT_RUNTIME_HTTP_RATE`（默认 0=关）= **每 IP 每分钟**，HTTP 层，只作用于 `/v1/*`，429 且**没有** `vertex.quota`；
  - `AGENT_RUNTIME_RATE_LIMIT`（默认 60）= **每租户每分钟**的**回合**上限，滑动窗口按 tenant 分桶，429 + `Retry-After`（取自 `vertex.quota.retry_after_s`）。
  前者防单机被打满，后者防单租户吃光配额 —— 生产两个都要给。
- `serve.py` 走**单进程 uvicorn**。同步 Gateway 调用一律丢线程池
  （`asyncio.to_thread`）—— 否则一个慢回合会把事件循环钉住，连 `/healthz` 一起超时。
- **跨副本配额不精确**：每副本各自一个窗口，2 副本 × limit=60 = 实际 120/60s。
  要精确得换集中式计数器（当前未实现）。
- SQLiteStore 并发实测：20 线程同时写 **0 错误 0 卡死**（`tests/test_concurrency.py`）。
  但它是**单文件**，多副本共享 NFS 写同一 `.db` 不是这套测试覆盖的场景。

### 5.3 工具面

- `max_result_bytes` 默认 65536：超大结果走**截断**，原文进 artifacts，回给模型的只是摘要。
  调大它会直接吃掉 prompt 预算。
- `per_turn_limit` 默认 10：单回合内同一工具最多调 10 次（防"工具乒乓"）。
- `timeout_ms` 默认 5000：工具超时是**软超时**（超时杀的是等待，不是远端操作本身）。

### 5.4 Milvus 一致性（**最终一致** —— 这条最容易出事）

```python
# plane/vector_backends.py::MilvusVectorBackend.flush
# "Milvus 是最终一致的：upsert 返回成功不代表马上能被 search 看到。
#  单机 lite 几乎瞬时可见，真集群要等 flush / 段回收 —— 所以：
#    * 测试与演示要显式 flush() 后再断言
#    * 生产不要依赖'写完立刻查到'，跨副本更不可靠"
```

运维含义：

| 现象 | 原因 | 处理 |
|---|---|---|
| 写完马上查不到 | 最终一致 | `flush()` 后再断言；业务侧接受"下一轮可见" |
| `count()` 比实际多 | `get_collection_stats()["row_count"]` 数的是"插入过的实体数"，delete 是打 tombstone | 用 `query(count(*))` 数**当前可见**行（实现已这么做）；等 compaction 清 tombstone |
| 集群上偶发查不到、lite 上复现不了 | lite 清得快，真集群才暴露 | 用 `VERIFY_CLUSTER=1 ./verify.sh` 跑真集群门禁 |
| collection not loaded | 集合未 load 或 load 未完成 | 见 §6 第 5 条 |

### 5.5 内存与进程

- Context 全在进程内（`MemoryStore`）：单个长任务会把 patch_log + 快照堆在内存里。
  `AGENT_RUNTIME_STORE=sqlite/file` 不减少内存占用，只解决"跨重启/跨进程"。
- 黑板 CAS 与配额窗口都是 `threading.RLock` 单进程语义：**多副本即失效**。
- macOS 上 `RLIMIT_AS` 被内核拒绝，沙箱内存上限**实际不生效**；
  要看真实生效项用 `enforceable_rlimits()`。

---

## 5.5 扩展点装配（Agent 层）

Runtime **不静态依赖 `agents/`**（有边界测试守着）。上层能力通过 `module:attr` 配置注入：

```bash
export AGENT_RUNTIME_EXTRACTOR="agents.extractor:OpsExtractor"
export AGENT_RUNTIME_ROUTER="agents.registry:build_router"
export AGENT_RUNTIME_PROMPT_PROFILE="agents.prompts:build_profile"
export AGENT_RUNTIME_PROMPT_TEMPLATE="agents.prompts:build_template"
export AGENT_RUNTIME_TOOLS="$PWD/agents/tools.yaml"
```

启动日志逐条确认（**这是"配了真生效"的唯一证据**）：

```
"event": "extension.loaded", "kind": "prompt_profile", "spec": "agents.prompts:build_profile"
"event": "extension.loaded", "kind": "extractor",      "spec": "agents.extractor:OpsExtractor"
"event": "tools.loaded",      "count": 8
"event": "memory.enabled",    "extractor": "OpsExtractor"
```

**看不到 `memory.enabled` 就是 Memory 没装**——即使 `AGENT_RUNTIME_EXTRACTOR` 写了。
`AGENT_RUNTIME_PLANNER` 指向需要参数的类会**启动即报错**，提示改成工厂函数。

## 6. 故障排查

> 每条都给**可复制的诊断命令**。约定：`$BASE` 是你的基址（例如 `http://127.0.0.1:8000`）。

### ① 配了 milvus，实际跑的是内存

```bash
curl -s $BASE/v1/info | python3 -m json.tool | grep -A4 '"backends"'
curl -s $BASE/readyz  | python3 -m json.tool | grep -A6 '"vector"'
env | grep -E 'VECTOR_BACKEND|MILVUS_URI|QDRANT_URL'
```

- `backends.vector` 显示 `memory`，但你设了 `VECTOR_BACKEND=milvus` →
  **看启动日志**：`_make_vector` 构造失败时把它包成 `_Unavailable`，
  `/readyz` 才会给出 `detail`（例如 `pymilvus 未安装` / `uri 必须是 .db 或 http://`）。
- 典型原因：`MILVUS_URI` 传了目录（报 `needs start with [unix, http, https, tcp] or a local file ends`）；
  `VECTOR_DIM=0`；`pymilvus` 没装但 `milvus-lite` 装了（两者不同包）。
- 注意：`serve.py` 默认 `VECTOR_BACKEND=memory`，**不是 `auto`** —— 这是刻意的，
  `auto` 会让"连了什么"取决于机器上装了哪个库。

### ② 重启后数据没了

```bash
curl -s $BASE/v1/info | python3 -m json.tool | grep -A4 '"deps"'
ls -l "$AGENT_RUNTIME_STORE_PATH" 2>/dev/null || echo "STORE_PATH 未设 → sqlite 用 :memory:"
env | grep -E 'AGENT_RUNTIME_STORE|AGENT_RUNTIME_DB|AGENT_RUNTIME_STORE_PATH'
```

三个不同原因，症状一样：

| 原因 | 判据 | 处理 |
|---|---|---|
| `AGENT_RUNTIME_STORE=memory`（默认） | `/v1/info` 的 `store` 是 `MemoryStore` | 显式设 `sqlite` |
| `STORE=sqlite` 但没给路径 | `STORE_PATH`/`DB` 都没设 → 路径是 `:memory:` | 设 `AGENT_RUNTIME_STORE_PATH=/abs/path/rt.db` |
| 路径给了但只配了别名 | 早期实现只认 `STORE_PATH`；`AGENT_RUNTIME_DB` 是后加的**别名**，两者都认，**冲突时 `STORE_PATH` 优先** | 只留一个，别两个都配 |

同一个坑还有一个变体：**配了 `AGENT_RUNTIME_DB` 却被忽略**（老版本/别的分支），
表现就是"重启后数据没了"，而 `env` 里明明有路径。

### ③ 多轮对话不工作

```bash
curl -s $BASE/v1/info | python3 -m json.tool | grep -E 'SESSION|features'
curl -s -X POST $BASE/v1/turns -H 'Content-Type: application/json' \
  -d '{"goal":"第一轮","principal":{"user_id":"u1","tenant":"acme","scopes":["runtime:run"]}}' \
  | python3 -m json.tool | grep -i session
```

按顺序排除：

1. **`AGENT_RUNTIME_SESSION` 没设**（默认 0）：每轮都是新会话。设 `1`。
2. **第二轮没带 `session_id`**：第一轮响应里 `vertex.session_id` 就是它，
   第二轮必须原样放进请求体（请求模型里这个字段曾经漏过 —— 漏了会被 `extra="forbid"` 直接 422）。
3. **`session_id` 带了但报 422**：那是 `TurnRequest` 的字段名拼错（`sessionId`），
   本项目 `extra="forbid"`，拼错就是 422 而不是静默忽略。
4. **store 是 memory**：`AGENT_RUNTIME_SESSION=1` + `STORE=memory` 只能在同一进程内承接，
   重启即断。要跨重启，`STORE=sqlite`。

### ④ trace 缺失（span 一条都没发出去）

```bash
.venv/bin/python - <<'PY'
from plane.quality import configure_otel
print(configure_otel("", install=False))            # 能力探测：SDK 在不在
print(configure_otel("http://127.0.0.1:4318/v1/traces", install=False))  # exporter 装没装
PY
pip list 2>/dev/null | grep -i opentelemetry
```

- `{"enabled": false, "reason": "opentelemetry-sdk 未安装: ..."}` → 装 `opentelemetry-sdk`。
- `exporter 包未安装（pip install opentelemetry-exporter-otlp-proto-http）` → 装它。
- **两个都在，但还是没有 trace** —— 两个可能：
  ① 进程**退出前没 flush**：`BatchSpanProcessor` 是批量的，没 `force_flush()`/`shutdown()`
  就退出，尾巴全丢。脚本里必须显式 flush；
  ② ~~**`serve.py` 根本没有 OTel 装配**~~ → **已修**：`main()` 现在装配 OTel。
  历史记录：`OTEL_EXPORTER_OTLP_ENDPOINT`
  写了也不会发。这是已知限制（§8）。
- `set_tracer_provider` 是"首次生效、之后忽略"的进程级副作用：一旦装过一个指向
  不可达端点的 provider，后续配置都不生效，而且退出时刷 `Failed to export spans` 噪声。

### ⑤ Milvus `collection not loaded`

```
pymilvus.exceptions.MilvusException: <Collection> not loaded
```

```bash
.venv/bin/python - <<'PY'
import os
from pymilvus import MilvusClient
c = MilvusClient(uri=os.environ.get("MILVUS_URI", "http://127.0.0.1:19530"))
print([ (x.get("collection_name"), x.get("loaded")) for x in c.list_collections() ] if False else c.list_collections())
PY
```

- 新建/重建集合后**必须 `load()`** 才能 `search`；`milvus-lite` 会自动 load，
  真集群不一定 —— 这就是"lite 上好好的，集群上报 not loaded"。
- 刚 `flush()` 完立刻 `search` 也可能撞上 `not loaded`（段在回收）。
- 索引参数写法在 pymilvus 3.x 变了：`client.create_index(collection, "vector", {...})`
  直接 `ParamError`，必须用 `prepare_index_params()`（见 `README.md`）。

### ⑥ 401 / 403 / 500 分不清

```bash
curl -s -o /dev/stderr -w '%{http_code}\n' $BASE/v1/info                        # 不带凭据 → 401?
curl -s -o /dev/stderr -w '%{http_code}\n' -H "X-API-Key: $KEY" $BASE/v1/whoami # 我是谁
curl -s $BASE/healthz                                                            # 服务在不在
```

| 状态 | 原因码 | 处理 |
|---|---|---|
| 401 `missing_credentials` | 没带凭据 | 加 `X-API-Key` 或 `Authorization: Bearer` |
| 403 `invalid_api_key` | key 不在表里 | 核对 `AGENT_RUNTIME_API_KEYS`（注意 `;` 分隔、只切前两个冒号） |
| 403 `jwt_expired` | token 过期 | 检查时钟偏移，`AGENT_RUNTIME_JWT_LEEWAY_S` |
| 403 `scheme_not_configured` | 用了没配的方式（如给了 Bearer 但没设 `JWT_SECRET`） | 配对应方式或换凭据类型 |
| 403 `principal_conflict` | 请求体 principal 与凭据身份不一致 | 见第 ⑦ 条 |
| 500 `auth_not_configured` | 装鉴权时一个凭据方式都没配 | 配 `API_KEYS` 或 `JWT_SECRET` |

### ⑦ `403 principal_conflict`

```bash
curl -s -H "X-API-Key: $KEY" $BASE/v1/whoami | python3 -m json.tool
```

规则：**body 不能比凭据更宽**。

- `user_id`/`tenant` 留空 = 不声明（用凭据身份）→ **推荐做法**。
- 写了就必须与凭据一致。API key 不写 `#user_id` 时 user_id 是 `svc_<sha256[:8]>`，
  写 `"user_id":"alben"` 必然冲突 → 要么加 `#alben`，要么 body 留空。
- body 的 `scopes`/`roles` 必须是凭据授予集合的**子集**，多声明就是"自己给自己升权限" → 403。

### ⑧ `/readyz` 503 但服务能正常跑回合

```bash
curl -s $BASE/readyz | python3 -m json.tool
curl -s -m 3 $BASE/v1/turns -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"goal":"x","principal":{"user_id":"u1","tenant":"acme","scopes":["runtime:run"]}}'
```

这是**预期行为**，不是矛盾：探针探的是 `store`/`kv`/`vector` 的**可用性**，
而 `Gateway` 在依赖不可用时仍可能跑完（memory 路径不碰 redis）。
按 `/readyz` 的 `detail` 修那件依赖。**不要**为了让它变绿去改探针逻辑 ——
那正是"探针假绿"的来源（本项目有专门用例钉住"探针对象必须就是业务对象"）。

### ⑨ SSE 一次吐出（看起来不像流式）

```bash
curl -sN -m 10 -D- -X POST $BASE/v1/turns/stream -H 'Content-Type: application/json' \
  -d '{"goal":"x","principal":{"user_id":"u1","tenant":"acme","scopes":["runtime:run"]}}' | head -30
```

- 先看响应头有没有 `X-Accel-Buffering: no`（本服务会带）。
- **Nginx 会缓冲 SSE**：必须 `proxy_buffering off;` 且 `proxy_http_version 1.1;`，
  否则事件被攒成一坨（表现：`started` 之后静默 5 秒，然后所有帧一起到）。
- 如果连 `step` 事件都没有，只有三帧：检查 `AGENT_RUNTIME_STREAMING`（默认 1）
  以及启动日志有没有 `streaming.disabled`（流式装不上会**留痕**，不静默）。

### ⑩ `/metrics` 401 或没有 `agent_runtime_up`

```bash
curl -s -o /dev/null -w '%{http_code}\n' $BASE/metrics
curl -s -H "Authorization: Bearer $METRICS_TOKEN" $BASE/metrics | grep agent_runtime_up
curl -s $BASE/metrics | head -3
```

- 401 → `AGENT_RUNTIME_METRICS_AUTH=1`（默认）且抓取端没带令牌；
  用 `AGENT_RUNTIME_METRICS_TOKEN` 配一个，抓取配置里加 `authorization: Bearer ...`。
- 200 但没有 `agent_runtime_up` → **指标前缀被改了**：`AGENT_RUNTIME_METRIC_PREFIX`。
  冒烟第 6 项会直接点出这一条。
- 500 `metrics_render_failed`：**不是空 body**，是渲染真坏了（空 exposition 会被抓取端
  当成"指标没了"）。看响应里的 `detail`。

### ⑪ SSE 的 `seq` 不从 0 开始

```bash
curl -sN -m 10 -X POST $BASE/v1/turns/stream -H 'Content-Type: application/json' \
  -d '{"goal":"x","principal":{"user_id":"u1","tenant":"acme","scopes":["runtime:run"]}}' \
  | grep -o '"seq":[0-9]*' | head -3
```

已知行为：`seq` 计数器挂在 `StreamingGateway` 实例上（每进程一份），**不是每条流重置**，
所以同一进程里第二条流的 `seq` 会从 8、16 这样开始。判据是**单调递增**（客户端靠它检测丢帧），
不是"必须从 0 起"。冒烟第 5 项按"单调"判定，并会打印实际区间。

### ⑫ 启动即 `RuntimeError: AGENT_RUNTIME_SESSION=1 但会话层装配失败`

```bash
AGENT_RUNTIME_SESSION=1 .venv/bin/python -c "
from serve import build_gateway_from_env
print(build_gateway_from_env())"
```

刻意不静默降级：你开了它就该生效。常见原因：`STORE` 指向不可用的 store（sqlite 路径
不可写 / `_Unavailable`），会话层拿不到落盘后端。修 store，别关 `SESSION`。

### ⑬ `AGENT_RUNTIME_LLM=openai` 但回合 500

```bash
env | grep -E '^OPENAI_'
.venv/bin/python -c "
from adapters.openai_compat import from_env
print(from_env('OPENAI'))"      # None = 缺 BASE_URL 或 MODEL
```

报错文案是 `AGENT_RUNTIME_LLM=openai 但 OPENAI_API_KEY / OPENAI_BASE_URL 不完整`。
注意 **`API_KEY` 允许为空**（本地 Ollama/vLLM 不校验），缺的是 `BASE_URL` 或 `MODEL`。
另外：前缀是 `OPENAI`（serve.py 硬编码），不是 `BABEL` —— 拿 `BABEL_*` 起服务会一直拿 `None`。

### ⑭ 容器里 `curl 127.0.0.1:8000` 连不上，日志却写着 `server.listen`

`AGENT_RUNTIME_HOST` 还是默认 `127.0.0.1`：只监听回环，容器外/别的容器进不来。

```bash
curl -s $BASE/v1/info | python3 -c 'import sys,json;print(json.load(sys.stdin)["platform"])'
docker exec <cid> sh -c 'netstat -lntp 2>/dev/null || ss -lntp'   # 看 bind 地址
```

设 `AGENT_RUNTIME_HOST=0.0.0.0`。

### ⑮ 数据文件读不了：`schema_version 不认识`

```
StoreSchemaMismatch: ... 的 schema_version 不认识：期望 1，收到 2（不猜旧版本怎么读）
```

```bash
.venv/bin/python - <<'PY'
import json, glob, sqlite3
for path in glob.glob("/data/**/*.db", recursive=True)[:5]:
    con = sqlite3.connect(path)
    print(path, "user_version=", con.execute("PRAGMA user_version").fetchone()[0])
PY
head -c 200 /path/to/ctx.json     # file store 的信封
```

信封 `schema_version` **不匹配就拒绝猜**（`core/store.py`），表版本存在
`PRAGMA user_version`（当前 3）会**自动向前迁移**。回滚边界见 §7。

### ⑯ 启动就退出：`StartupRefused: 拒绝启动：未配置任何凭据方式`

```bash
# 复现：把你现在的环境喂给判定函数，看它得出什么模式
.venv/bin/python -c "
import os
from serve import resolve_auth_mode, _has_credentials
print('mode =', resolve_auth_mode(env=os.environ))
print('有凭据 =', _has_credentials(os.environ))
print('AUTH 开关 =', repr(os.environ.get('AGENT_RUNTIME_AUTH')))
print('ALLOW_ANONYMOUS =', repr(os.environ.get('AGENT_RUNTIME_ALLOW_ANONYMOUS')))
"
```

这是**设计**，不是故障。三种成因：

| 你的配置 | 判定 | 处理 |
|---|---|---|
| 什么都没配 | enforced → 没凭据 → 拒绝 | 配 `API_KEYS` / `JWT_SECRET`，或 `AUTH=off` |
| `ALLOW_ANONYMOUS=0` / `false` / `no` | 仍然 enforced | 想放开必须 `=1`（真值只有 `1/true/yes/on`）；`_startup_hint()` 会明确点出「这个值不是真值」 |
| `AUTH` 拼错（`disable` / `0ff`） | 落回按配置判定 | 用 `off` 或 `0`；日志里的 `auth.enforced` 会暴露真相 |

### ⑰ 配了 key 却不生效：日志里同时有 `auth.disabled` 和 `auth.credentials_ignored`

```bash
grep -o '"event": "auth[^}]*' <你的启动日志>
curl -s $BASE/v1/info | python3 -c 'import sys,json;print(json.load(sys.stdin)["auth"])'
```

这两行同时出现 = **凭据配了，但被 `AGENT_RUNTIME_AUTH=off` 压过去了**
（硬开关优先级最高）。不是 bug，是提醒：要么去掉那个 `off`，要么承认自己在裸奔。
`/v1/info` 的 `auth.mode` 会写 `disabled`。

### ⑱ 413 / 429 / 504 分别是哪一层拒的

```bash
# 413：超体量（默认 1 MiB）
python3 -c 'print("{\"goal\":\"" + "x"*2000000 + "\",\"principal\":{\"user_id\":\"u\",\"tenant\":\"t\",\"scopes\":[\"runtime:run\"]}}")' > /tmp/big.json
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/v1/turns \
  -H 'Content-Type: application/json' --data-binary @/tmp/big.json

# 429：区分是哪一层的限流
curl -s -D- -o /tmp/r.json -X POST $BASE/v1/turns -H 'Content-Type: application/json' \
  -d '{"goal":"x","principal":{"user_id":"u","tenant":"t","scopes":["runtime:run"]}}' | head -5
grep -o '"vertex".*' /tmp/r.json
```

| 状态 | 触发变量 | 怎么区分 |
|---|---|---|
| **413** | `AGENT_RUNTIME_MAX_BODY_BYTES` | 按 `Content-Length` **先拒**，不把 body 读进内存 |
| **429** | `AGENT_RUNTIME_HTTP_RATE`（每 IP/分钟，HTTP 层）**或** `AGENT_RUNTIME_RATE_LIMIT`（每租户/分钟，Gateway 层） | HTTP 层的 429 没有 `vertex.quota`；Gateway 的 429 带 `vertex.quota.retry_after_s` 且响应头有 `Retry-After` |
| **504** | `AGENT_RUNTIME_REQUEST_TIMEOUT_S` | 日志里找 `http.timeout_detached`：那一回合**仍在后台跑完**（继续烧 token、写 Context），504 只表示"客户端不再等" |

`/healthz`、`/metrics` 不受 HTTP 限流影响，只有 `/v1/*` 受。

### ⑲ 启动就 `Address already in use`

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
ps -o pid,command -p $(lsof -t -nP -iTCP:8000 -sTCP:LISTEN)
```

上一个进程没退（常见于 `kill -9` 后端口还没释放，或者你以为的"另一个终端"其实在跑）。
先按第 4 节的 SIGTERM 走一遍，别直接 `kill -9`：那会丢掉 drain 与 flush。

### ⑳ 配了 `tools.yaml` 但模型说「没有可用工具」

```bash
grep -o '"event": "tools[^}]*' <你的启动日志>     # tools.loaded count=? source=?
curl -s $BASE/v1/info | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("config",{}).get("AGENT_RUNTIME_TOOLS"))'
# 单独验配置（不动服务）
.venv/bin/python -c "
from toolconfig import load_tools_from_env
t = load_tools_from_env()
print('manifest 数:', len(t.manifests), '--', t.names())
print('warnings:', t.warnings)
t.close()"
# 注意：/v1/info 的 config 是**白名单**，AGENT_RUNTIME_TOOLS 不在里面 ——
# 上面那行 curl 大概率打不出东西，用它确认"服务自己怎么读"，而不是当唯一证据。
grep -o '"event": "tools.loaded"[^}]*' <你的启动日志>   # 这才是权威
```

按顺序排除：

| 现象 | 原因 | 处理 |
|---|---|---|
| `tools.loaded count=0 source=""` | `AGENT_RUNTIME_TOOLS` 没进到**进程**的环境里（容器 envFrom 写错 / systemd 里没 `Environment=` / `export` 后没重启） | 进程启动时只读一次环境变量，改完必须重启 |
| `tools.loaded count=0 source=/path/tools.yaml` | 文件在但**解析出 0 个工具**（`tools: []`，或 MCP server 一个工具都没 discover 到） | 看 `tools.config_warning`；单独跑上面的加载器验证 |
| 启动直接失败 `工具配置加载失败：...` | YAML 坏了 / import 失败 / 重名 / schema 非法 / 不可逆工具 | 报错是 `文件: 位置: 原因` 格式，照最后一段改（详见 [`TOOLS.md`](TOOLS.md) §8） |
| `count=N` 但模型仍说没有 | **`AGENT_RUNTIME_PROMPT_TOKENS` 太小**，工具清单被 Builder 裁掉了 | 每个工具按 300 tok 预留预算（§5.1） |
| 工具在，但调用总是 `needs_confirmation` | 该工具是 `T2`，这个 turn 没有 approval | 正常行为：审批要由调用方/人行签，不是配置问题 |

---

## 7. 升级与回滚

### 7.1 三个独立的版本号（别混）

| 版本 | 位置 | 当前值 | 不匹配时 |
|---|---|---|---|
| Store 信封 `schema_version` | 每个落盘的 Context 信封（JSON）/ sqlite 行 | `1`（`core/store.py::STORE_SCHEMA_VERSION`） | `StoreSchemaMismatch`，**拒绝猜旧版本怎么读** |
| sqlite 表结构 `user_version` | `PRAGMA user_version` | `3`（`core/sqlite_store.py::SQLITE_SCHEMA_VERSION`） | **自动向前迁移**（建表 → 补列 → 建索引，幂等） |
| 会话 `schema_version` | `SessionStore` 落盘文档 | `1`（`core/session.py::_SCHEMA_VERSION`） | 该会话读不出来（报"不认识"），不影响其他会话 |

### 7.2 升级步骤（零停机）

```bash
# ① 备份（sqlite 用 .backup，不要直接 cp 正在写的库）
.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/agent-runtime/rt.db")
dst = sqlite3.connect("/data/agent-runtime/rt.db.bak-$(date +%Y%m%d)")
src.backup(dst); dst.close(); src.close()
PY

# ② 新副本用新镜像起来，探针通过后再切流量（readiness 判定走 /readyz）
# ③ 老副本按 SIGTERM 排空退出（观察 server.draining → metrics.flushed → server.drained）
# ④ 确认新副本 /v1/info 的 version 与 backends 符合预期
curl -s $BASE/v1/info | python3 -m json.tool | head -20
```

迁移是**只增不减**的：`_MIGRATIONS` 里每条带版本号，`version <= current` 就跳过，
`_ensure_columns` 用 `ALTER TABLE` 补缺列（`CREATE TABLE IF NOT EXISTS` 不会补列 ——
老库"有表但缺列"时只有 ALTER 能救）。

### 7.3 回滚

| 回滚对象 | 能不能直接回 | 说明 |
|---|---|---|
| 代码（同大版本内） | ✅ | 表版本只增索引/列，老代码忽略新索引即可 |
| 代码（跨 envelope 版本） | ⚠️ 需要数据侧配合 | 新代码若把 `STORE_SCHEMA_VERSION` 提到 2，老代码读到 `2` 会直接 `StoreSchemaMismatch`。要么回滚时用备份库，要么把数据按新版本清理掉 |
| 数据（从备份恢复） | ✅ | 先停写（摘流），再替换 `.db`，再放流量 |
| 配置 | ✅ | 环境变量是启动时读的，改完必须**重启进程**才生效（没有热加载） |

**回滚检查清单**：

```bash
# 1) 进程退干净了吗
lsof -nP -iTCP:8000 -sTCP:LISTEN
# 2) 数据版本对不对
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('/data/agent-runtime/rt.db')
print('user_version', c.execute('PRAGMA user_version').fetchone()[0])
print('rows', c.execute('SELECT count(*) FROM contexts').fetchone()[0])"
# 3) 冒烟（这一步是"回滚成功"的唯一证据，不是"进程起来了"）
./scripts/smoke.sh --port 8000 --key "$KEY" --check-shutdown
```

### 7.4 配置变更的生效方式

除 `AGENT_RUNTIME_METRIC_PREFIX` 之外，**所有环境变量都是进程启动时读一次**。
没有 `/reload`、没有 SIGHUP 重载。改配置 = 滚动重启。
（这也是为什么 §4 的 drain 语义要先调对：每次改配置都在走一遍关停路径。）

---

## 8. 已知边界

> 完整列表在 [`../README.md`](../README.md) 的「已知边界（这是骨架，不是产品）」。
> 这里只做**上线判定**：哪些是"能带着上线"，哪些是"不解决就别上"。

### 8.0 已经不再是问题（别照着旧笔记排查）

| 曾经的问题 | 现状 |
|---|---|
| 鉴权默认不装 —— `authenticator=None` 时 principal 由请求体自报，任何客户端都能自称任意租户 | **已改 fail-closed**：没凭据 → 拒绝启动（`StartupRefused`），裸奔必须显式 `AGENT_RUNTIME_AUTH=off`。冒烟第 7 项（未授权被拒）就是这条的守门人 |
| HTTP 层没有请求体上限 / 限流 / 超时 | **已加三道边界**（`MAX_BODY_BYTES` / `HTTP_RATE` / `REQUEST_TIMEOUT_S`），默认关闭但可配 |
| 工具注册只能改 Python 源码 | **已接线**：`serve.py` 启动时读 `AGENT_RUNTIME_TOOLS` → `toolconfig.load_tools_from_env()` → `register_into(registry)` → `Loop(tools=...)`；不配置就是显式空表，配置坏了拒绝启动 |

### 8.1 阻塞上线（上线前必须解决或明确接受风险）

| 边界 | 后果 | 现在能怎么办 |
|---|---|---|
| **HTTP 层默认不限流、不超时**（`AGENT_RUNTIME_HTTP_RATE=0`、`AGENT_RUNTIME_REQUEST_TIMEOUT_S=0`） | 单个客户端能把进程打满；没有请求级超时兜底 | 生产显式给两个值；注意 504 只是「不再等待」，回合仍在后台跑完（见 §2.1） |
| **跨副本配额不精确**（每副本独立窗口） | N 副本 = N 倍限流额度，租户能打出 N 倍流量 | 单副本部署，或把 `RATE_LIMIT` 按副本数除 |
| **`RedisStore` 未实现，跨实例续跑未验证** | 多副本下 A 副本写的 Context，B 副本续不上（`resume_failed`） | 会话/续跑粘性路由，或单副本 + `sqlite` |
| **黑板 CAS 只在单进程内成立**（`threading.RLock`） | 多副本下并发写同一 key 会丢更新 | 单副本；分布式要换 etcd txn / Redis WATCH |
| **`serve.py` 没接 OTel/Langfuse 装配** | `OTEL_EXPORTER_OTLP_ENDPOINT` 写了也不发 trace，线上"没有 trace 可查" | 需要代码侧接线（`configure_otel` / `configure_langfuse`），或用 `/metrics` + 结构化日志顶住 |
| **`serve.py` 硬编码 `from_env("OPENAI")`** | 换模型前缀要改代码；拿 `BABEL_*` 起服务会一直 `None` → 回合 500 | 用 `OPENAI_*` 命名（指向任何 OpenAI 兼容网关） |
| **多副本共享 sqlite 单文件未验证** | 共享卷/NFS 上写同一 `.db` 可能锁冲突/损坏 | 单副本；或等真正的分布式 store |
| **Subagent 输出未过 Governance 护栏** | 编排层拿到的子 agent 文本未经过 `OutputGuard` | 编排层手动接 `Governance.before_output` |
| **沙箱是 `subprocess` + 白名单，不是内核隔离** | 逃逸面大于 gVisor；macOS 上内存上限不生效 | 别把不可信代码交给 `sandbox_run`；要强隔离用容器后端（`require_backend=kernel` 会显式报 `SandboxStrengthUnmet`） |
| **`plane/quality.py` 的 span 树是自实现** | OTel 只接管导出；`finish()` 的 dict 形状是公开契约，不能换 | 接受它（换成 OTel 原生 span 是破坏性变更） |

### 8.2 可带着上线（有明确降级路径 + 自报）

| 边界 | 影响 | 备注 |
|---|---|---|
| `MemoryStore` 的语义相似度是字符 bigram Jaccard | 同义改写召回有限 | 真向量库 + embedding 才好；`memory` 只适合冒烟 |
| Knowledge 每次全量 rebuild，无增量索引 | 大语料 rebuild 慢 | 与部署形态无关 |
| FTS5 的 `bm25()` 与手写 BM25 分数绝对值不同 | 不保证跨实现可比较 | 排序在小语料上一致 |
| qdrant 本地模式未验证分布式 | 本地 `:memory:` 只适合单机 | 分布式用远程 URL |
| milvus 只用 milvus-lite 验证过 | 集群连接/多副本一致性/对象存储未验证 | 上集群前先跑 `VERIFY_CLUSTER=1 ./verify.sh` |
| `llama-index` 让依赖从 20 涨到 86 包 | 镜像变大、供应链面变大 | 它只是可选切分器 |
| MCP 只验证了进程内真 server + 假 session | `stdio_client` / `streamable_http_client` 起子进程没验 | 接外部 MCP server 前先自己压一遍 |
| Langfuse 未对真实云服务投递过 | 只有 OTLP 链路真验证（本地接收器收到 POST） | 缺 endpoint 与凭据两个变量 |
| 会话并发轮次无序号互斥 | 两个请求同时进同一会话拿到相同 `turn_index`（历史不丢，序号会重） | 会话内串行化，或接受序号重复 |
| 指标 flush 只在关停时渲染一次 | 没有 pushgateway/OTLP 导出，持久化靠 Prometheus 抓取 | 抓取间隔要短于进程寿命 |
| Summary 压缩不省 token | 它买的是"早期决议不丢" | 想省钱压 `max_summary_chars` |
| `resources` 的 cache stampede 未修 | 并发首次读同一 uri 会重复调 reader | 加 singleflight 属改语义，先不动 |
| Docker 超时杀的是宿主 CLI | 容器可能活到 `--rm` 清理 | 要"超时即容器死"需 `--cidfile` + `docker kill` |
| tools 固定开销 240 tok 的标定样本有限 | 换模型/网关偏差可能到 5 倍 | 换模型必须重新标定 |
| `runtime` 族不在 RBAC 家族表里 | 入口准入（`runtime:run`）与能力授权分层 | 明确知道即可 |
| 评估「挂起 4 小时」用假时钟 | 没有真等 4 小时 | 时间相关逻辑靠假时钟覆盖 |

### 8.3 判定口诀

- **单副本 + 内存/`sqlite` + 配了鉴权** → 可以当"内部服务"上线，`/readyz` + 冒烟守门。
- **多副本** → 先解决"配额 / 续跑 / 黑板"三条，否则隔离与限流都是假的。
- **要对外** → 必须补 OTel 接线（否则线上无 trace）+ 网关层的 SSE 不缓冲 + 显式开 `HTTP_RATE`/`REQUEST_TIMEOUT_S` + 令牌轮换方案 + 工具表（`AGENT_RUNTIME_TOOLS`）过一遍 code review。

---

## 附：相关文档

| 文档 | 看什么 |
|---|---|
| [`../README.md`](../README.md) | 组件选型、架构取舍、踩坑复盘、真实模型实测数字、已知边界全文 |
| [`STATUS.md`](STATUS.md) | 全景图、七道门禁现状、模块与用例分布 |
| [`TOOLS.md`](TOOLS.md) | 工具注册（YAML / MCP）与 `side_effect`/`risk_tier` 选择 |
| [`../ARCH-CHECKLIST.md`](../ARCH-CHECKLIST.md) | 架构文档逐项落地清单（每项标了模块与测试文件） |
| [`../verify.sh`](../verify.sh) | 七道门禁（①语法 ②1602 用例 ③契约自洽 ④11 场景 ⑤HTTP 面 ⑥真 Milvus ⑦真 Langfuse） |
| [`../scripts/smoke.sh`](../scripts/smoke.sh) | 部署后一键冒烟（9 项检查） |
