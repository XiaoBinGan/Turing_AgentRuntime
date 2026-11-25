# 工具配置指南（`toolconfig.py`）

> **以 `toolconfig.py` 的实际实现为准。** 这份文档跟着代码走，但代码是权威：
> 接口、字段名、错误文案有出入时，信 `toolconfig.py` 的 docstring 与
> `tests/test_toolconfig.py`。文末第 9 节列出「本文与最初设计的预期契约之间的差异」，
> 那些点已经按**实际实现**写过一遍，若后续实现再变，请以代码为准并回来改这份文档。
>
> 部署视角的全局配置在 [`DEPLOY.md`](DEPLOY.md)；工具面在运行时里的位置见
> [`STATUS.md`](STATUS.md) 的「资源面（四个）」。
>
> **接线状态**：`serve.py` 已经接上了 —— 启动时读 `AGENT_RUNTIME_TOOLS`
> （未设置 = 显式空工具表），配置坏了就拒绝启动。所以「配好 YAML 重启进程」就能生效，
> 不需要改 Python。接线细节与生命周期收口见第 7 节。

---

## 1. 它解决什么问题

`serve.py` 的注释说得很对：「服务层不替部署方决定这个进程能干什么」。
问题是它**没留声明入口** —— 想给进程加一个「查 p99」的工具，只能改 Python 源码、
开 PR、重新发版。

`toolconfig.py` 只做一件事：把「这个进程有哪些工具」从代码搬到一个 YAML 文件里。
产出的东西和手写注册**完全等价**（`ToolManifest` + `fn(args, ctx)`），
所以 Dispatcher 侧的权限 / 审批 / 幂等 / 限流 / 脱敏一律照旧，**没有旁路**。

```python
from toolconfig import load_tools

loaded = load_tools("tools.yaml")     # -> LoadedTools(manifests, handlers, connections, warnings)
try:
    loaded.register_into(registry)    # registry 就是 plane.tool.ToolRegistry
    dispatcher = Dispatcher(registry)
    ...
finally:
    loaded.close()                    # MCP 子进程必须在这里收掉
```

`LoadedTools` 的四个字段：

| 字段 | 内容 | 谁用 |
|---|---|---|
| `manifests` | `tuple[ToolManifest, ...]` | Builder 生成工具描述；`ToolRegistry.register` |
| `handlers` | `dict[name, fn(args, ctx)]` | Dispatcher 调用 |
| `connections` | 真起来的 MCP 子进程连接 | **必须 `close()`**，否则留一排孤儿进程 |
| `warnings` | 不致命但该被看见的事（跳过 schema 校验、启发式判不出副作用…） | 启动时打印给人看 |

`warnings` 刻意**不用** `logging.warning` 收工：启动期的一条日志没人看，
而这些正是「工具行为和你以为的不一样」的根源。**把它打进启动摘要。**

---

## 2. 加一个进程内工具

### 2.1 YAML（这份是实测跑通的）

```yaml
version: 1                       # 必填。没有它直接报错（见 §8.1）
defaults:
  allow_irreversible: false      # 默认就是 false；写出来是为了让 review 看见
  max_tools: 64                  # 一个 turn 里模型看得到的工具数上限

tools:
  - name: query_metrics
    description: 查某个指标的当前值（只读）
    import: "mytools:query_metrics"        # module:function
    side_effect: read_only
    input_schema:
      type: object
      properties:
        name: {type: string, maxLength: 64}
      required: [name]
      additionalProperties: false
```

### 2.2 Python 函数

```python
# mytools.py（和 tools.yaml 同目录即可：配置目录会被追加进 sys.path）
def query_metrics(args: dict, e) -> dict:
    """查 p99。"""
    return {"metric": args["name"], "value": 0.42}
```

签名是**自适应**的（`_wrap_handler` / `_positional_arity`），三种都行：

| 写法 | 收到什么 | 什么时候用 |
|---|---|---|
| `def f(args: dict, e: ExecCtx) -> Any` | 参数 dict + 执行上下文（租户、task_id、trace_id、budget、deadline、idempotency_key…） | 需要知道自己是谁 / 要打审计 / 要看预算 |
| `def f(args: dict) -> Any` | 只有参数 dict | 大多数工具 |
| `def f() -> Any` | 什么都不需要 | 无参数工具（YAML 里仍要写 `input_schema: {type: object, properties: {}}`） |

三条约定：

- `args` 一定是 **dict**：模型可能传 `None` / list，加载器会归一成 `{}`，你不会收到脏类型。
- 返回值**原样**交给 `Normalizer`：截断（`max_result_bytes`）、脱敏（`secrets`）都在那里做，
  不要在工具里自己再做一遍（重复实现会两处不一致）。
- 抛异常 = 这次调用失败（`ToolOutcome.failed`，`retryable` 由 Dispatcher 判），
  **不要**把失败包装成"正常返回一段错误文本" —— 那会让模型基于假数据推理。

### 2.3 实测

```bash
PYTHONPATH=/tmp/tooldemo .venv/bin/python -c "
from toolconfig import load_tools
from plane.tool import ToolRegistry
loaded = load_tools('/tmp/tooldemo/tools.yaml')
print(loaded.names(), loaded.warnings)
reg = ToolRegistry(); loaded.register_into(reg)
m, fn = reg.get('query_metrics')
print(m.risk_tier, m.side_effect, m.required_scope, m.idempotent)
print(fn({'name': 'p99'}, None))
loaded.close()"

# → ['query_metrics'] ()
# → RiskTier.T0_SAFE SideEffect.READ_ONLY tool.read True
# → {'metric': 'p99', 'value': 0.42}
```

注意 `side_effect: read_only` 一个人就推出了 `risk_tier=T0` / `required_scope=tool.read` /
`idempotent=True` —— 默认值不是拍脑袋，是**从副作用推出来的**（见 §4）。

---

## 3. 接一个 MCP server

MCP server 的工具清单**由 server 自报**（discover），注册名是 `<server>__<tool>`。

```yaml
version: 1
mcp_servers:
  - name: pg                     # 工具名前缀 → pg__query / pg__list_tables
    command: npx                 # 必须是可执行文件，不经过 shell
    args: ["-y", "@modelcontextprotocol/server-postgres"]
    env:
      DATABASE_URL: "${DEMO_DB_URL}"        # 见 §6：展开不到就报错
      PGOPTIONS: "${PGOPTIONS:-}"          # 允许为空
    cwd: ./servers               # 相对**配置文件所在目录**，不是进程 cwd
    startup_timeout_s: 20
    call_timeout_s: 30
    # 启发式判不出读写时，套这组默认值（保守：写操作要多一道审批）
    default_side_effect: mutating
    default_risk_tier: T2
    # 逐工具显式纠正（优先级最高）
    overrides:
      pg__query:
        side_effect: read_only
```

要注意的几点（都来自实现，不是经验之谈）：

1. **`name` 不能含 `__`**：`__` 是工具名前缀的分隔符，server 名里再带一个，
   「拆掉前缀还原原始名」就变成歧义操作，`overrides` 按原始名匹配时会对不上。
2. **`command` 不经过 shell**：`command: "npx -y xxx"` 是错的（找不到这个可执行文件），
   参数必须放 `args` 列表。
3. **`overrides` 里只写 `risk_tier` 是不够的**：副作用才是决定「要不要审批」的那一项，
   只给 tier 会报错（`请同时写 side_effect`）。
4. **起子进程失败会自己收干净**：`_attach_mcp_server` 在异常路径先 `stop()` 再抛，
   不会留孤儿；但**成功之后**的清理是你的责任（`loaded.close()`）。
5. 生命周期：**先 `conn.stop()`，再丢弃 registry**。注册进 registry 的 handler 持有
   `MCPToolBridge`，而 bridge 持有会话 —— 顺序反了会拿到已断开的会话。

---

## 4. `side_effect` / `risk_tier` 怎么选

### 4.1 两个字段的关系

`side_effect` 是**事实**（这个工具干了什么），`risk_tier` 是**策略**（要不要人批）。
实现里 `side_effect` 有默认映射，你不写 `risk_tier` 就按它推：

| `side_effect` | 推导出的 `risk_tier` | 推导出的 `required_scope` | 推导出的 `idempotent` |
|---|---|---|---|
| `read_only` | `T0_SAFE` | `tool.read` | `true` |
| `mutating` | `T2_HIGH` | `tool.write` | `false` |
| `irreversible` | `T3_CRITICAL` | `tool.write` | `false` |

**不写 `side_effect` 时默认 `mutating`**（和 MCP 的保守默认一致：协议不告诉读写时，
宁可多一道审批）。

### 4.2 Dispatcher 怎么用它们（这决定了你该怎么填）

| `risk_tier` | 运行时行为 | 什么操作该放这一档 |
|---|---|---|
| **T0_SAFE** | 直接执行 | 纯查询、无副作用、可重复：`query_logs`、`query_metrics`、`get_ticket`、`read_file`（只读打开） |
| **T1_LOW** | 直接执行（结果可能很大 / 慢，但不改状态） | 只读但重：`dump_logs`、全量导出、跨库检索、大文件解析 |
| **T2_HIGH** | **无 approval → `needs_confirmation`**（不是 denied，是"问人"） | 改状态但可回滚/可重试：`restart_service`、`write_file`、`create_ticket`、`sandbox_run`、发通知 |
| **T3_CRITICAL** | **硬拒**（`critical tools cannot be confirmed, only refused`） | 不可逆：`delete_*`、`drop_*`、转账、`rm -rf`、发布到生产、发外部邮件/短信 |

三条实操判断：

1. **拿不准就往上放一档。** T0 与 T2 的差别是"要不要打扰人"，T2 与 T3 的差别是
   "会不会造成无法回滚的损失"。放高了的代价是一次多余审批，放低了的代价是一次事故。
2. **`idempotent` 与 `side_effect` 分开填。** `restart_service` 是 mutating 但
   **不**幂等（`idempotent: false`）：同参数第二次调用不该命中幂等缓存，
   否则第二次重启被"缓存"掉，看起来成功其实没重启。
3. **`required_scope` 不要照抄默认值。** 高价值工具应该有自己的 scope
   （`tool.write.payments` 之类），否则一把 `tool.write` 就能做所有写操作。

### 4.3 逐字段速查

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | 必填 | `^[A-Za-z0-9_.-]+$`；不能用空格/中文（过不了 provider 的 function name 校验） |
| `description` | `""` | **给模型看的**。写清"什么时候该用它"，不是"它是什么" |
| `import` | 必填 | `module:function`；`module` 不存在与 `function` 不存在是两种报错（分开的） |
| `side_effect` | `mutating` | `read_only` / `mutating` / `irreversible` |
| `risk_tier` | 由 `side_effect` 推 | `T0` / `T1` / `T2` / `T3`（也接受 `0`~`3`） |
| `idempotent` | `read_only` 时为 `true` | 同参数重复调用是否可安全去重 |
| `required_scope` | 按副作用推 | 进 Dispatcher 前要过的 scope |
| `timeout_ms` | `5000` | 软超时（超时杀的是等待） |
| `max_result_bytes` | `65536` | 超了截断 + 原文进 artifacts |
| `per_turn_limit` | `10` | 单回合内同一工具最多调用次数（防工具乒乓） |
| `secrets` | `()` | 结果里要脱敏的字段名（`***REDACTED***`） |
| `input_schema` | 空 object schema | **强烈建议写**：不写就只能靠模型猜参数名（实测会把 `lines` 写成 `limit`） |

---

## 5. 为什么 `allow_irreversible` 默认 `false`

默认 `false` 时，配置里出现 `side_effect: irreversible` 的工具 → **拒绝加载**（不是忽略、
不是降级）。理由（`_check_irreversible` 的原文，抄在这里因为它值得被每任运维读一遍）：

1. **它在配置里看不出任何异常。** 一个删数据的工具被「顺手」配进来（复制粘贴上一个
   server 的段落、合并别人的 PR、照着示例改），它和其他工具长得一模一样。
2. **忽略条目的语义是"你以为反了"。** 忽略等于"配置声明了 T3 能力但进程没有" ——
   人以为自己关掉了，其实反了；降级成 T2 更糟：审批一下就把数据删了。
3. **报错把"要不要给它这个能力"变成必须显式回答的问题。**
   `defaults: {allow_irreversible: true}` 写在配置里、进 code review，
   这正是这类能力该有的摩擦。
4. **它不减少任何可用能力。** 运行时 T3 本来就被 Dispatcher 硬拒
   （`critical tools cannot be confirmed, only refused`），
   所以默认拒绝加载只是把「配了也用不了」从运行时提前到启动期说清楚。

同一个闸门对 MCP 也生效：`overrides` 里把某个工具显式标成 `irreversible`，
而 `allow_irreversible=false` → 在**起子进程之前**就报错（省得起一个进程只为报一句配置错）。

---

## 6. 环境变量插值 `${VAR}`

只在 `mcp_servers[*].env` 的值、`mcp_servers[*].cwd` 里生效（内置工具的 `import` 不做插值）。

| 写法 | 行为 |
|---|---|
| `${VAR}` | 变量未定义 → **报错**（不退化成空串） |
| `${VAR:-default}` | 变量未设置**或为空串**时用 `default`（与 shell 的 `:-` 一致） |
| `${VAR:-}` | 允许为空（"这个变量可以没有"的显式写法） |

为什么未定义要报错：`env: {DATABASE_URL: "${DATABASE_URL}"}` 在变量没设时会变成
`DATABASE_URL=""`，server 起来后报「连接串非法」—— 排查起点被推到子进程里，
而真正的错因（配置要的东西环境里没有）在加载期就已经确定了。

实测报错原文：

```
...bad4.yaml: mcp_servers[0] (name=pg).env['DATABASE_URL']: 环境变量 ${DEMO_DB_URL} 未定义
（值里引用了它，但没有默认值）；先 export DEMO_DB_URL=...，或写成 ${DEMO_DB_URL:-默认值}
```

`env` 里 `KEY:` 后面**留空**（YAML `null`）= 空串，这是「清掉继承来的同名变量」的合法写法。

---

## 7. 接线：让进程真的用上这些工具

### 7.1 用 `serve.py` 起服务（零改代码，推荐）

```bash
export AGENT_RUNTIME_TOOLS=/etc/agent-runtime/tools.yaml
AGENT_RUNTIME_API_KEYS='sk-...:acme:runtime:run tool.read' .venv/bin/python serve.py
```

`serve.py` 的 `build_gateway_from_env()` 做的事，按顺序：

1. `load_tools_from_env(env)` —— 空/未设置 → `LoadedTools()`（显式空表，不报错）；
   配置坏了 → `RuntimeError: 工具配置加载失败：<文件: 位置: 原因>`（**拒绝启动**）。
2. 每条 `warnings` → 日志事件 `tools.config_warning`（必须看，这是"行为与预期不同"的来源）。
3. `loaded_tools.register_into(registry)` → `Dispatcher(registry)`。
4. `Loop(builder, llm, dispatcher, tools=list(manifests))`。
5. 日志 `tools.loaded count=N names=[...] source=...` —— `count=0` 就是"这个进程没有工具"。
6. `loaded_tools` 挂在 gateway 上（`gateway._loaded_tools`），lifespan 退出时在
   **drain 之后**调 `close()`（`tools.closed`）：正在跑的工具调用需要连接还活着。

**注意解析基准**：`load_tools_from_env()` 用 `Path.cwd()` 解析相对路径。
systemd / 容器 / 不同目录启动的进程 cwd 不一样 —— **生产一律写绝对路径**。

### 7.2 在自己的装配里接（库用法 / 自定义服务）

```python
from toolconfig import load_tools_from_env, load_tools
from plane.tool import Dispatcher, ToolRegistry

loaded = load_tools_from_env()          # 读 AGENT_RUNTIME_TOOLS；未设置 = 空工具集（不报错）
# 或者显式给路径与解析基准（服务进程 cwd 不可靠时用这个）
# loaded = load_tools("/etc/agent-runtime/tools.yaml", cwd="/etc/agent-runtime")
for warning in loaded.warnings:         # 必须打出来：这是"行为和你想的不一样"的来源
    logger.warn("tools.warning", detail=warning)

registry = ToolRegistry()
loaded.register_into(registry)
dispatcher = Dispatcher(registry)

try:
    ...                                  # 跑服务；loaded.connections 里的 MCP 子进程保持存活
finally:
    loaded.close()                       # 幂等；关停钩子里也要调一次
```

`load_tools_from_env()` 的语义边界很清楚：

- **未设置**（或空串）→ 返回空 `LoadedTools()`，**不报错**：这是"这个进程不接工具"的
  合法部署（只做对话的实例），变成启动失败是拿运维当敌人。
- **设了但文件坏了** → 报错。这两种情况必须能区分。

### 7.3 一个能跑的自检

```bash
# 不动服务，先确认配置本身是对的
AGENT_RUNTIME_TOOLS=/etc/agent-runtime/tools.yaml .venv/bin/python -c "
from toolconfig import load_tools_from_env
t = load_tools_from_env()
print('工具数:', len(t.manifests), t.names())
for w in t.warnings: print('WARN:', w)
t.close()"
```

输出 `工具数: 0 []` 而你确实写了工具 → 看 §8；输出正常但服务里没有 →
看启动日志的 `tools.loaded`（多半是环境变量没进到进程，或者 cwd 不对）。

---

## 8. 常见错误怎么排查

**所有 `ToolConfigError` 的格式都是 `文件: 位置: 原因`**，照着改就行。
下面每条都是实测抓到的原文（不是编的）。

### 8.1 缺 `version`

```
<root>: 缺少 version 字段；请显式写 `version: 1`（没有版本号的配置无法判断字段语义，本模块拒绝猜）
```

写 `version: 1`。为什么不给默认值：将来 v2 改了字段语义（比如 `risk_tier` 换成名字），
旧 loader 会把它当 v1 静默解析 —— 语义被静默解释错的配置，比拒绝启动的配置危险得多。

### 8.2 import 失败

```
tools[0] (name=ghost): 模块 'no_such_module_xyz' 导入失败：ModuleNotFoundError: No module named 'no_such_module_xyz'（检查 PYTHONPATH / 拼写 / 该模块的依赖是否装了）
```

```bash
# 先确认"这个模块在这个解释器里能不能导"
PYTHONPATH=/opt/tools .venv/bin/python -c "import mytools; print(mytools.__file__)"
# 再确认"函数在不在"
PYTHONPATH=/opt/tools .venv/bin/python -c "import mytools; print(mytools.query_metrics)"
```

- 报「模块导入失败」= 装依赖或改 `PYTHONPATH`；
- 报「模块 'x' 里没有 'f'」= 改名字 / 改 `import` 路径（这两种错误是**故意分开的**，
  因为修法完全不同）；
- 报「不是 callable」= `import` 指到了一个常量或类实例：
  ```
  tools[0] (name=notcallable): mytools:__doc__ 不是 callable（收到 str）；工具必须是一个函数
  ```

**注意**：配置文件所在目录会被**追加**到 `sys.path` 末尾（不是插入，避免遮蔽已安装模块），
所以同目录放 `mytools.py` 就能直接 `import: "mytools:query_metrics"`。
服务进程的 cwd 与"你敲命令时的 cwd"经常不同 —— 用 `load_tools(path, cwd=...)` 显式指定。

### 8.3 重名

```
tools[1]: 工具名 'dup' 重复；工具名是注册与审计的唯一键，不允许同名
```

MCP 撞名会指向具体 server：`mcp_servers[0] (name=pg): 工具名 'pg__query' 重复…`。
**不做"后者覆盖前者"**：`ToolRegistry.register` 对同名是硬拒绝的（静默覆盖会连权限声明
一起换掉），加载期先拦住才能说清是哪个 server 撞的名。

排查：

```bash
.venv/bin/python -c "
from toolconfig import load_tools
t = load_tools('tools.yaml')
import collections
c = collections.Counter(t.names())
print([n for n, k in c.items() if k > 1])
print(t.names())"
```

### 8.4 `input_schema` 非法

```
tools[0] (name=badschema): input_schema 不是合法 JSON Schema：'objectt' is not valid under any of the given schemas
```

只做**形状**校验（`jsonschema.validators.validator_for` + `check_schema`）：
语义对不对（该 required 的字段漏了没有）机器判不了，人得负责。但不校验形状的代价是
运行期每一次调用都 fail-closed 失败（Dispatcher 对坏 schema 直接 failed），等于工具全废。

提示：`type: objectt` 这种拼写错误由 shape 校验兜住；`required: [name]` 但
`properties` 里没有 `name` 这种**语义**错误它兜不住 —— 拿到真模型上跑一次才看得见。

没写 `input_schema` 不会报错，但会进 `warnings`：

```
tools[0] (name=x): 没有声明 input_schema，已兜底成空 object schema。模型只能靠工具名猜参数名（实测会把 lines 写成 limit），建议补上
```

### 8.5 环境变量未定义

见 §6。修法二选一：`export DEMO_DB_URL=...` 或写成 `${DEMO_DB_URL:-默认值}`。

### 8.6 不可逆工具被拦

```
defaults.allow_irreversible: 配置里有不可逆工具 ['nuke_everything']，但 allow_irreversible=false（默认）。
删数据的工具不该被顺手配进来：确认要开放就在配置里显式写 `defaults: {allow_irreversible: true}`，否则把这些工具摘掉
```

先问"这个工具真的该出现在这个进程里吗"，再决定要不要开闸（§5）。

### 8.7 超过 `max_tools`

```
defaults.max_tools: 内置工具共 3 个，超过上限 max_tools=2；删掉不用的工具，或在 defaults 里调大 max_tools
```

（`phase` 会是「内置工具」或「全部工具」—— 后者包含 MCP discover 回来的工具。）

64 不是拍脑袋：超过这个量级，工具描述本身就吃掉半个 prompt，模型选错工具的概率
比多给它的能力涨得更快。**不做静默截断**（截断会让"配了没生效"变成无声事实）。

### 8.8 YAML 语法错

```
tools.yaml: YAML 语法错误（第 5 行第 4 列）：expected <block end>, but found '<block mapping start>'
```

带行号列号（缩进/冒号/tab 离开行号基本无法定位）。另外：加载用 `yaml.safe_load`，
`!!python/object` 这类标签**不会**被解析成对象 —— 工具配置是运维写的数据，不是可执行代码。

### 8.9 配了 `AGENT_RUNTIME_TOOLS` 但进程里没工具

```bash
grep -o '"event": "tools.loaded"[^}]*' <启动日志>        # count=? source=?
AGENT_RUNTIME_TOOLS=/path/tools.yaml .venv/bin/python -c "
from toolconfig import load_tools_from_env
t = load_tools_from_env(); print(len(t.manifests), t.names()); t.close()"
```

| 日志 | 原因 | 处理 |
|---|---|---|
| `count=0 source=""` | 变量没进到**进程**（容器 envFrom / systemd `Environment=` / export 后没重启） | 重启进程；环境变量只在启动时读一次 |
| `count=0 source=/path/tools.yaml` | 文件解析出 0 个工具（`tools: []`，或 MCP discover 空） | 看 `tools.config_warning`，用上面的自检跑一遍 |
| `count=N` 但模型说没有工具 | `AGENT_RUNTIME_PROMPT_TOKENS` 太小，工具清单被裁 | 每工具按 300 tok 预留（`DEPLOY.md` §5.1） |
| 启动失败 `工具配置加载失败` | 配置本身错 | 报错是 `文件: 位置: 原因`，见 §8.1~§8.8 |

---

## 9. 与「预期契约」的差异（对齐清单）

这份文档最初按预期契约写，`toolconfig.py` 落地后已按**实际实现**校准。差异如下，
后续如再变仍以代码为准：

| 项 | 预期契约 | 实际实现 | 影响 |
|---|---|---|---|
| 注册方式 | 调用方自己 `for m in loaded.manifests: registry.register(m, loaded.handlers[m.name])` | **`loaded.register_into(registry)`**（顺序、缺 handler、部分失败的处理都在一处定死） | 用 `register_into`，别自己循环 |
| `load_tools` 签名 | `load_tools("tools.yaml")` | `load_tools(path, *, cwd=None)` —— 相对路径按 `cwd`（默认进程 cwd）解析 | 服务进程 cwd 与运维 cwd 常不同，显式传 `cwd` |
| 环境变量入口 | `load_tools_from_env()` | 同名，只认 **`AGENT_RUNTIME_TOOLS`**（不猜别名） | 未设置 = 空工具集，不报错 |
| 版本字段 | 未提及 | **必须有 `version: 1`**，否则报错 | 老配置要补这一行 |
| handler 签名 | 固定 `f(args: dict, e: ExecCtx) -> Any` | **自适应**：2 参 / 1 参 / 0 参都行 | 简单工具不用被迫接 `ExecCtx` |
| `allow_irreversible` | 默认 false | 同名同默认；但行为是**拒绝加载**（不只是不执行） | 配了 T3 工具会启动失败，不是静默跳过 |
| `logger` 参数 | 期望 `load_tools_from_env(logger=...)` | **没有**这个参数；诊断信息在 `LoadedTools.warnings` 里 | 接线方自己把 warnings 打到日志（`serve.py` 已打成 `tools.config_warning`） |
| `serve.py` 接线 | 未提及 | 已接：`AGENT_RUNTIME_TOOLS` → `register_into` → `Loop(tools=...)`；关停时 `close()` | 配好 YAML 重启即可，不必改代码 |
| MCP 默认值 | 未提及 | `default_side_effect` / `default_risk_tier` / `overrides` 三层优先级 | 见 §3、§4 |
| `jsonschema` 缺失时 | — | 跳过形状校验 + 记 warning（不报错） | CI 里装齐依赖才能挡住 schema 拼写错 |

---

## 附：相关文件

| 文件 | 看什么 |
|---|---|
| [`../toolconfig.py`](../toolconfig.py) | 实现与全部 docstring（**权威**） |
| [`../tests/test_toolconfig.py`](../tests/test_toolconfig.py) | 每条规则对应的用例 |
| [`../plane/tool.py`](../plane/tool.py) | `ToolRegistry` / `Dispatcher`（T0~T3 与 approval 的真实行为） |
| [`../plane/sandbox_tool.py`](../plane/sandbox_tool.py) | 一个"像样"的 manifest 长什么样（T2 + `input_schema` + 白名单） |
| [`../adapters/mcp_transport.py`](../adapters/mcp_transport.py) | `attach_stdio_server`（`<server>__<tool>` 命名、生命周期） |
| [`../adapters/mcp_client.py`](../adapters/mcp_client.py) | `MCPToolBridge`（discover / 调用 / 副作用启发式） |
| [`DEPLOY.md`](DEPLOY.md) | 部署视角：环境变量全表、探针、故障排查 |
