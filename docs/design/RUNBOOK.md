# 值班手册

按告警名索引。每条写清：**怎么确认、先做什么、不要做什么**。

「不要做什么」比「要做什么」更重要 —— 事故中最常见的二次伤害是
慌乱中的顺手改动（调高配额、重启全部实例、关掉告警）。

---

## 1. 实例不可用

**告警**：`InstanceDown`

### 确认

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://INSTANCE:PORT/healthz
docker logs --tail 100 CONTAINER     # 或 journalctl -u agent-runtime -n 100
```

### 先做什么

1. 看退出码：`StartupRefused`（退出码 2）说明是**配置问题**，重启没用
2. 配置问题看日志里的三行修复提示（`AGENT_RUNTIME_API_KEYS` / `JWT_SECRET` / `ALLOW_ANONYMOUS`）
3. 非配置问题：摘流 → 重启 → 观察 `/readyz` 再放回

### 不要做

- **不要**为了让它起来而设 `AGENT_RUNTIME_ALLOW_ANONYMOUS=1`。那会关掉鉴权，
  日志里只会留一条 `auth.disabled`，事后很难发现。

---

## 2. `/readyz` 失败（依赖不健康）

**告警**：`ReadyzFailing`

### 确认

```bash
curl -s http://INSTANCE:PORT/readyz | jq .
```

看 `checks` 里哪个 `ok=false`：`store` / `kv` / `vector`。

### 先做什么

| 失败项 | 动作 |
|---|---|
| `store` | 查磁盘空间、文件权限、sqlite 文件是否被锁 |
| `kv` | 查 Redis 连通性：`redis-cli -h HOST -p PORT ping` |
| `vector` | 查 Qdrant/Milvus 服务；向量检索挂掉时 Knowledge 面降级但不阻塞回合 |

### 不要做

- **注意这个已知陷阱**：`/readyz` 红的时候 `/v1/turns` 可能仍返回 200
  （Gateway 拿到的是 `_Unavailable` store）。**探针红、业务绿**，很容易被忽略。
  看到这个组合要当成"续跑/审计已失效"，不是"业务正常"。

---

## 3. 延迟升高

**告警**：`TurnLatencyP95High` / `TurnLatencyP99Critical`

### 确认

```bash
# 当前并发与吞吐
.venv/bin/python scripts/loadtest.py --url http://INSTANCE:PORT/v1/turns \
  --concurrency 10 --duration 10
```

对照 `docs/design/CAPACITY.md`：并发 20 → p95 228ms，并发 40 → p95 494ms。

### 先做什么

1. 如果并发超过 40：**加副本**（实测 2 副本 192 RPS，1.72× 扩展率）
2. 如果并发不高但延迟高：查上游 LLM（见 §9）与 store（sqlite 写锁）
3. 如果是工具拖慢：看 `agent_runtime_tool_duration` 分位，确认哪个工具

### 不要做

- **不要**先调高 `AGENT_RUNTIME_RATE_LIMIT`。延迟高说明处理不过来，
  放宽准入只会让队列更长、p99 更差。
- **不要**加 worker 线程。单副本吞吐受 GIL 限制（实测并发 1→40，
  RPS 反而从 145 降到 115），加线程只增加上下文切换开销。

---

## 4. 错误率升高

**告警**：`TurnErrorRateHigh`

### 确认

```bash
curl -s http://INSTANCE:PORT/metrics | grep 'agent_runtime_turns_total{ok="false"'
# 单条错误详情看日志
grep '"event": "turn.failed"' /var/log/agent-runtime.log | tail -20
```

按 `error` 字段分类：

| error 前缀 | 含义 | 动作 |
|---|---|---|
| `rate_limited:` | 配额拒绝 | 见 §5 |
| `bad_request:` | 请求体不合法 | 客户端问题，不是服务故障 |
| `upstream_unavailable:` | LLM 不可达 | 见 §9 |
| `unrecoverable:` | 内部不可恢复 | 保留 trace_id，查代码 |
| `internal_error:` | 未分类 | 同上，优先查 |

### 不要做

- **不要**只看总错误率就扩容。`bad_request` 占多数时扩容无用，
  那是调用方在发坏请求。

---

## 5. 大量 429

**告警**：`QuotaRejectionSpike`

### 确认

```bash
curl -s http://INSTANCE:PORT/v1/info | jq '{rate_limit, rate_limit_distributed}'
curl -s http://INSTANCE:PORT/metrics | grep 'status="429"'
```

### 先做什么

1. 区分**业务增长**与**客户端重试风暴**：看重试间隔是否规律
2. 重试风暴 → 联系调用方加退避；这是客户端问题
3. 真实增长 → 按成本核算调高 `AGENT_RUNTIME_RATE_LIMIT`，并记录为什么调

### 不要做

- **不要**把 `AGENT_RUNTIME_RATE_LIMIT` 设为 `0` 应急。`0` 是"不限流"，
  这会让成本闸完全失效 —— 而且它长得像"关闭"，事后没人会想起来改回去。

---

## 6. 多副本未开分布式限流

**没有自动告警**（`/v1/info` 有字段，但没有指标，规则查不到）。

### 人工核对（多副本部署时必做）

```bash
for p in $PORTS; do
  curl -s http://HOST:$p/v1/info | jq -r '"\(.rate_limit_distributed)"'
done
```

全部为 `true` 才算配好。

### 后果

`false` 时实际阈值 = 配置值 × 副本数。5 副本、`limit=60` → 实际放行 300。
**这类失效不报错**，只在账单上体现。

---

## 7. 落盘失败

**没有自动告警**（只在单次响应的 `vertex.checkpoint_errors` 里）。

### 发现方式

```bash
grep 'checkpoint_errors' /var/log/agent-runtime.log | tail
```

### 后果

崩溃后无法知道"哪些写操作已生效"。这是最危险的一类故障，
因为它破坏的是**事后可解释性**。

### 动作

1. 立刻查磁盘：`df -h`、`du -sh /data`
2. 查 store 连通性：`/readyz` 的 `store` 项
3. 修好之前**停止执行有副作用的 workflow**（发布/回滚）

---

## 8. Workflow 堆积

**没有自动告警**（`running` 数只在 `/v1/info` 里）。

### 确认

```bash
curl -s http://INSTANCE:PORT/v1/info | jq '.workflow'
curl -s 'http://INSTANCE:PORT/v1/workflows?limit=20' | jq '.workflows[] | {id, status, spec_name}'
```

### 动作

1. `workers` 不足 → 调 `AGENT_RUNTIME_WORKFLOW_WORKERS`
2. 单条卡住 → 查它的节点状态；`interrupted` 的用 `POST /v1/workflows/{id}/resume`
3. 容量满（429）→ 已结束的会被自动淘汰，不用手动清

### 不要做

- **不要**直接删状态文件来"清理"。那会丢掉审计轨迹，
  而 workflow 的价值一半在事后可查。

---

## 9. 上游 LLM 故障

**表现**：`TurnErrorRateHigh` + 错误里带 `upstream_unavailable`

### 确认

```bash
curl -s http://INSTANCE:PORT/readyz | jq '.checks'
# 直接探上游
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $KEY" $BASE_URL/models
```

### 按错误类型分流

| 现象 | 原因 | 动作 |
|---|---|---|
| 超时（>30s） | 上游慢或网络 | 降并发，等上游恢复 |
| 401/403 | 密钥过期 | 换密钥，别改其他配置 |
| 429 | 上游配额 | 联系供应商，或切备用模型 |
| 5xx | 上游故障 | 等；同时确认自己的重试有退避 |

### 不要做

- **不要**为了"看起来恢复"而把错误吞掉返回空答案。那会让下游
  基于空结果得出错误结论 —— 比明确报错危险得多。

---

## 10. 多 worker 下限额被放大

**没有自动告警**（`info{rate_limit_distributed}` 只能看出开关状态）。

### 症状

配了 `limit=60`，实际放行量远超 60；账单高于预期；`/metrics` 只有部分请求的数据。

### 确认

```bash
# 1. 确认是不是多 worker
curl -s http://HOST:PORT/v1/info | jq '.workers // "n/a"'
ps -eo args | grep -c "[a]pp_factory"          # worker 进程数

# 2. 确认分布式开关
curl -s http://HOST:PORT/metrics | grep 'agent_runtime_info'
```

### 动作

多 worker ≈ 多副本。必须同时满足：

- `AGENT_RUNTIME_RATE_LIMIT_DISTRIBUTED=1`
- `AGENT_RUNTIME_KV=redis` 且 Redis 可达（不可达会**拒绝启动**，这是故意的）

### 不要做

- **不要**在未开分布式的情况下用多 worker 跑生产。阈值 × N 的失效不报错，
  只在账单上体现。
- **可以用多 worker 提吞吐，而且确实有效**：实测 4 worker = 2.97×（2,247 → 6,681 RPS）。
  **但必须先确认限流与配额是分布式的**，否则限额会放大 N 倍。
- **不要相信单进程 Python 压测器给出的 RPS。** 旧的 `scripts/loadtest.py`
  自身天花板约 110 RPS，只有单 worker 服务端的 1/20 —— 用它比较 worker 数
  会得出"多 worker 无效"的错误结论（我自己就得出过一次）。压测请用 `wrk`。

---

## 11. 内存"看起来在涨"

### 症状

监控上 RSS 从 85MB 涨到 238MB 又回落，反复锯齿。

### 判断

**先看谷值，不要看当前值。** 实测（20 分钟、13.7 万请求）：

```
原始 RSS: 186 → 70 → 79 → 89 → 182 → 85 → 88 → 102 → 223 → 93 → 238 → 83 → 101
谷值序列（剔除预热）: 85.5 → 85.1 → 87.9 → 83.1
稳态斜率: -0.10 MB/min
```

锯齿是 CPython 分配器行为（内存不立刻还给 OS）。**真泄漏的特征是谷值抬高**，
不是峰值波动。

### 决定性验证

停掉负载，看 RSS 是否回落：

```bash
# 实测从 238MB 峰值回落到 6MB
```

**还回去了 = 没有泄漏。**

### 不要做

- **不要**看到峰值就重启。重启会丢掉 `audit_log` 与 workflow 状态。
- **不要**用"全样本线性拟合"判断（我第一次就是这么误判的）。
  锯齿数据做线性拟合，结论取决于末次采样在峰还是谷。

---

## 12. 会话跨副本读不到

### 症状

多副本部署下，用户在 A 副本聊了两轮，请求被负载均衡打到 B 副本时"失忆"。

### 确认

```bash
curl -s http://HOST:PORT/v1/info | jq '.deps.store.kind'
```

如果是 `sqlite` 或 `file` → 这是**单机文件**，多副本各写各的。

### 动作

```
AGENT_RUNTIME_STORE=redis
AGENT_RUNTIME_REDIS_HOST=...
AGENT_RUNTIME_REDIS_PORT=6379
```

### 判据

同一 `session_id` 交替打两个副本，`turn_index` 应当连续：

```
副本 A 第1轮 → 1
副本 B 第2轮 → 2      ← 读到 A 的历史
副本 A 第3轮 → 3
```

修复前各自从 1 开始（已实测确认）。

---

## 13. 模型输出里混进思维链

### 症状

回合 `text` 长这样，而不是答案：

```
用户问"2+2等于几？只回答数字。" 需要只回答数字。答案是4。
```

用户看到的是模型的内心独白。更麻烦的是它被写进 `history`，**下一轮模型
会看到自己上一轮的思维链当前文**，判断开始漂。

### 确认

```bash
# 1. 看是不是推理模型
curl -s $UPSTREAM/v1/models | jq '.data[].id'

# 2. 直接打一次，看 content 结构
curl -s $UPSTREAM/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"MODEL","messages":[{"role":"user","content":"2+2=?"}],"max_tokens":100}' \
  | jq -r '.choices[0].message.content'
```

如果输出里有 `</think>`、`</thinking>` 之类的标记 → 是推理模型。

### 动作

**默认已经处理了**（`OpenAICompatAdapter(strip_reasoning=True)`）。
如果看到思维链，说明有人关掉了它，或者响应里有不认识的标记格式：

```bash
# 检查是否被显式关闭
grep -rn "strip_reasoning" /path/to/your/wiring.py
```

不在已知标记列表里的（自定义模型可能用别的），需要加进
`_REASONING_MARKERS`。

### 不要做

- **不要**把 `strip_reasoning` 关掉来"看完整输出"。要看完整输出用日志，
  不要让它进 `history` —— 一旦进了，后续几轮的判断都会被污染。
- **不要**指望上游的 `message["reasoning"]` 字段。实测 vLLM 0.32 在没配
  `--reasoning-parser` 时该字段是 `null`，思维链**只在 content 里**。

---

## 14. 上游能对话但工具调用永远不触发

### 症状

`/v1/turns` 返回 200，模型回复看起来正常（甚至在说"我要调用 xxx"），
但 `observations` 是空的，工具一次都没执行。

### 确认

直接打上游，看 `finish_reason`：

```bash
curl -s $UPSTREAM/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"MODEL",
  "messages":[{"role":"user","content":"查一下 api 的 p99"}],
  "tools":[{"type":"function","function":{"name":"get_metrics",
    "parameters":{"type":"object","properties":{"service":{"type":"string"}}}}}]
}' | jq '{finish: .choices[0].finish_reason, tc: .choices[0].message.tool_calls}'
```

| 返回 | 含义 |
|---|---|
| `400 requires --enable-auto-tool-choice` | vLLM 缺启动参数 |
| `200` + `finish:"stop"` + `tool_calls:null` | **parser 与模型模板不匹配** |
| `200` + `finish:"tool_calls"` | 上游正常，问题在本层 |

### 动作

vLLM 需要两个参数，**第二个错了不会报错，只是永不产出 tool_calls**：

```bash
--enable-auto-tool-choice --tool-call-parser <parser>
```

parser 按模型的 chat template 选：

| 模型模板里的标记 | parser |
|---|---|
| `<tool_call><function=NAME>` | **`qwen3_xml`** |
| `{"name":...,"arguments":...}` JSON | `hermes` |
| `<|python_tag|>` | `llama3_json` |

查模板：

```bash
docker exec CONTAINER grep -oE "<tool_call>|<\|python_tag\|>|function=" \
  /model/chat_template.jinja | sort -u
```

### 不要做

- **不要**把这当成"模型不支持 function calling"。实测用错 parser 时
  Qwen3.8-27B 会先输出思考（"Let's use the get_metrics function"）然后
  **在 `</think>` 处停止**，`finish_reason` 是 `stop` —— 完全像是模型能力问题。
- **不要**只验证"能对话"就认为接好了。必须验证 `finish_reason == "tool_calls"`。

---

## 15. 回合挂在 WAITING_USER（等审批）

### 症状

`POST /v1/turns` 返回 **200** 但 `phase=WAITING_USER`、`term_reason=NEEDS_CONFIRMATION`。

**这不是故障**：挂起是一个成功的回合结果，checkpoint 已落盘，工具**没有执行**。

### 确认

```bash
curl -s -X POST $HOST/v1/turns -H 'Content-Type: application/json' \
  -d '{"goal":"...","principal":{...}}' | jq '{phase, term_reason, ctx_id}'
```

`ctx_id` 就是续跑的把手。

### 动作：批准或拒绝

```bash
# 批准（工具会真的执行）
curl -s -X POST $HOST/v1/turns/$CTX_ID/resume \
  -H 'Content-Type: application/json' \
  -d '{"decision":true,"principal":{...}}'

# 拒绝（无副作用，模型被告知换方案）
curl -s -X POST $HOST/v1/turns/$CTX_ID/resume \
  -H 'Content-Type: application/json' \
  -d '{"decision":false,"reason":"收件人写错了，先别发","principal":{...}}'
```

| 状态码 | 含义 |
|---|---|
| 200 | 处理完（批准后跑完，或拒绝后模型已换方案） |
| 404 | ctx_id 不存在 / 已过期 / 不属于调用方 |
| 409 | ctx_id 存在但不在等待确认状态 |
| 422 | 请求体不合法（`decision` 必填） |

### 不要做

- **不要**再发一次相同目标的 `/v1/turns`。那会开一个新任务，旧任务继续挂着。
- **不要**不带 `decision` 就带 `ctx_id` 打 `/v1/turns` —— 会得到 409
  `resume_needs_decision`。**没表态不等于同意**：早先这里是硬编码批准，
  等于任何人拿着 ctx_id 发一次请求就自动执行了挂起的危险操作。
- **不要**在批准前删掉 checkpoint（`store.delete`）。删了就没有 `pending_confirmation`，
  也就没有了 `args_hash`，无法再安全地重放。

---

## 16. 工具热加载失败

### 症状

`POST /v1/admin/tools/reload` 返回 **422** `reload_failed`。

**注意这是好事**：旧工具表保持不动，服务继续用上一份能工作的工具集。
需要的是修配置，不是回滚部署。

### 确认

```bash
# 1. 端点是否存在（不配 admin token 时根本不注册）
curl -s -o /dev/null -w '%{http_code}\n' -X POST $HOST/v1/admin/tools/reload
#    404 = 没配 AGENT_RUNTIME_ADMIN_TOKEN；403 = token 不对

# 2. 看服务端日志的 reason（带文件名+行号）
journalctl -u agent-runtime | grep tools.reload_failed
```

### 常见原因

| 日志里的字样 | 含义 |
|---|---|
| `缺少 version 字段` | YAML 根节点必须有 `version: 1`，本模块拒绝猜字段语义 |
| `YAML 语法错误（第 N 行第 M 列）` | 缩进/引号问题 |
| `import` 指向的模块导入失败 | 模块不在 `PYTHONPATH`，或函数名写错 |
| `工具名重复` | 同一个名字注册了两次 |

### 动作

改配置文件 → 再调一次端点。空 diff（`added/removed/changed` 全空）表示
"文件变了但注册结果没变"，通常是你改的字段不影响注册（比如只改了
`description` 之外的元数据，或者改到了被忽略的位置）。

### 不要做

- **不要**因为 422 就重启服务。422 意味着旧表还在正常工作。
- **不要**在热加载返回 200 后就认为"新工具已可用"却忽略 diff。
  看 `added` / `changed` 里有没有你改的那一项 —— 没有就是没生效。
- **不要**指望热加载能改 `AGENT_RUNTIME_TOOLS` 的**路径**。路径是启动期
  读取的，改它需要重启；端点只重读该路径下的内容。

---

## 待补的观测能力

以下三项目前**没有指标**，所以没有对应告警。补 metric 是让它们可运维的前提：

| 能力 | 需要的指标 |
|---|---|
| 分布式开关状态 | `agent_runtime_info{rate_limit_distributed}` |
| 副作用落盘失败 | `agent_runtime_checkpoint_errors_total` |
| Workflow 堆积 | `agent_runtime_workflow_running` |

在那之前，只能靠 §6/§7/§8 的人工核对。**写一条查不到数据的告警规则
比没有更糟** —— 它会让人以为有覆盖。
