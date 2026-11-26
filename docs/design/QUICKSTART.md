# Quickstart：用这个框架做点自己的事

> 面向"第一次接触这个仓库、想写自己第一个 Agent"的人。
>
> 本文所有输出都是在本机**实测贴回来的**，不是"应该会输出"。
> 环境：macOS，`.venv` 为 Python 3.14.3，仓库 HEAD `f11432b`，实测日期 2026-11-26。

README 告诉你这个框架**是什么**（Runtime 提供形状、应用提供内容）。
这份文档只回答一个问题：**我该怎么把它跑起来，然后加自己的第一个工具。**

---

## 1. 五分钟跑通

假设你已经 clone 下来，并且在仓库根目录：

```bash
cd /Users/supre/Documents/tmp/agent-runtime
python3 -m venv .venv && .venv/bin/pip install -r requirements-core.txt   # 空文件，但这条要跑

.venv/bin/python examples/quickstart/01_minimal.py    # ① 不联网，0.15 秒，先确认骨架是好的
.venv/bin/python examples/quickstart/02_real_llm.py   # ② 要环境变量；没设会给提示，不抛 traceback
.venv/bin/python examples/quickstart/03_approval.py   # ③ 高风险工具的审批与续跑
```

`requirements-core.txt` 是空的，这是设计：**①②③ 全部只用标准库**，不需要 OpenAI SDK、
不需要 tiktoken、不需要 fastapi（`core/`、`plane/` 不许 import 第三方这件事由
`tests/test_architecture.py` 在 AST 层守着）。用系统 `python3` 直接跑 ①③ 也实测通过。

①③ 的真实耗时 `0.15s` / `0.12s`（纯本地）；② 取决于端点延迟。从 clone 到看见第一个
`phase = DONE`，实测 **不到 1 分钟**（不含 `pip` 的网络时间）。

---

## 2. 三个示例各自讲什么

| 文件 | 行数 | 讲什么 | 联网 |
|---|---|---|---|
| `examples/quickstart/01_minimal.py` | 133 | 一个自定义工具 + 一个假 LLM + 一次 `Loop.run`。**没有 API Key 也能跑通骨架** | 否 |
| `examples/quickstart/02_real_llm.py` | 135 | 同一个例子，把假 LLM 换成 OpenAI 兼容端点（`BASE_URL`/`API_KEY`/`MODEL`） | 是 |
| `examples/quickstart/03_approval.py` | 145 | T2 风险工具触发 `needs_confirmation`，以及三种续跑/拒绝方式 | 否 |

三个例子里 **Runtime 侧一个字都没改**：`Loop` / `ContextHandle` / `Dispatcher` /
`PromptBuilder` 的用法完全一致，换的只有「工具定义」和「LLM 实现」。

### ① `01_minimal.py` —— 骨架，零依赖

装配只有四步，代码里用 ① ② ③ ④ 标出来了：

1. `ToolManifest(name, side_effect, risk_tier, input_schema)` + 一个 `fn(args, e)` 函数，
   用 `ToolRegistry.register()` 装起来
2. `Dispatcher(registry)` —— **唯一的副作用出口**，入参校验 / scope / 风控 / 限流 / 缓存 /
   超时全在这里，不在工具函数里
3. 一个实现了 `complete(prompt, tools, e) -> LLMResponse` 的对象（这里 30 行假实现）
4. `Loop(PromptBuilder(), llm, dispatcher).run(goal, ctx, e)`

真实输出：

```
── 结果 ─────────────────────────────────────────────
phase       = DONE
term_reason = DONE
steps       = 2
text        = 这句话有 3 个词。
工具调用     = ['count_words']
patch 条数   = 19（执行轨迹 = 崩溃恢复的 WAL）
loop 警告    = （无）
```

第 2 轮喂给模型的 prompt 长这样（`PromptBuilder` 拼的；示例里的 `FakeLLM` 把最近一次
prompt 存在 `self.seen_prompt`，想看得更细就 `print(llm.seen_prompt)`）：

```
<<<TOOLS>>>
- count_words [read_only/T0_SAFE]: 统计一段文本的字符数与词数

<<<TOOL_RESULTS>>>
- count_words: chars=19, words=3

<<<HISTORY>>>
- user: 数一下 hello runtime world 有几个词
- assistant: 先数一下字数。

<<<STATE>>>
phase=Phase.EXECUTING step=2
```

注意：**工具结果不是靠 messages 数组回填的**，是 `PromptBuilder` 每轮从 Context 重新
拼进 prompt 文本里。所以接 OpenAI 兼容协议时，`messages` 里永远只有一条 `user`（见 ②）。

### ② `02_real_llm.py` —— 换成真模型

```bash
export BASE_URL='https://api.example.com/v1'   # 必填，http(s):// 开头
export API_KEY='sk-...'                        # 本地 vLLM / Ollama 可以留空
export MODEL='your-model-name'                 # 必填
.venv/bin/python examples/quickstart/02_real_llm.py
```

接的是 `POST {BASE_URL}/chat/completions`，所以 OpenAI / DeepSeek / Qwen（DashScope 兼容
模式）/ vLLM / Ollama 都是一份代码。密钥只从环境变量读，adapter 会自己把消息里的密钥
换成 `***`。

**没设环境变量时**（真实输出，退出码 2）：

```
这个例子需要先设置环境变量：BASE_URL、MODEL

    export BASE_URL='https://api.example.com/v1'   # 必填，要 http(s):// 开头
    export API_KEY='sk-...'                        # 本地 vLLM/Ollama 可留空
    export MODEL='your-model-name'                 # 必填

（01_minimal.py 不联网，可以先跑它确认骨架是好的）
```

**没有真密钥时怎么验证这条路**：本文作者拿到的是"不给密钥"，所以起了一个本地 OpenAI
协议桩（脚本放 `/tmp`，不进仓库；第 1 次请求回 `tool_calls`，第 2 次回终答），用
`BASE_URL=http://127.0.0.1:<port>/v1` 跑同一个脚本。**这验证了协议转换与工具回填，
不等于验证了真模型**。真实输出：

```
── 请求 stub-model @ http://127.0.0.1:62007/v1 ─────────────────────
phase       = DONE
term_reason = DONE
steps       = 2
text        = 上海现在 23℃，多云。
工具调用     = [('get_weather', True)]
usage       = in=530 out=26  估算=False
本次成本     = $0.000304
```

桩那头收到的两次请求（证明工具 schema 与鉴权头都发出去了）：

```
[stub] #1 /v1/chat/completions model=stub-model auth=Bearer tools=['get_weather']
[stub] #2 /v1/chat/completions model=stub-model auth=Bearer tools=['get_weather']
[stub] messages roles = ['user']
```

**端点不可达时**（真实输出，退出码 3）—— 注意这里**没有 traceback**：

```
── 请求 nope @ http://127.0.0.1:9/v1 ─────────────────────
模型调用失败（kind=server, status=502）：HTTP 502
  → 检查 BASE_URL='http://127.0.0.1:9/v1' 是否可达（本地服务是否已启动）
```

原因：`Loop` 不允许异常逃逸，它把 adapter 抛的 `LLMError` 收成
`Result(phase=FAILED, term_reason=UNRECOVERABLE)`。**所以 `try: loop.run() except:` 抓不到
模型故障**，必须检查 `term_reason`。

### ③ `03_approval.py` —— 审批（这个框架和"直接调 LLM"最大的区别）

一个 `risk_tier=T2_HIGH` + `side_effect=MUTATING` 的工具，在拿到人工批准之前
`Dispatcher` 会把它拦成 `needs_confirmation`，回合停在 `WAITING_USER`：
**模型想做的事 ≠ 系统允许做的事。**

真实输出（四段）：

```
① 不批准
   phase=WAITING_USER  term_reason=NEEDS_CONFIRMATION  steps=1
   text = 已暂停，等待用户确认：send_email（send_email requires approval (tier T2, side_effect=mutating, args_hash=dce8330cd5f15801)）。已执行 1 步，待确认动作 args_hash=dce8330cd5f15801。
   待确认: tool=send_email  args={'to': 'boss@example.com', 'subject': '本周进展'}
   args_hash=dce8330cd5f15801   ← 批准时要原样带回来
   外部世界 SENT=[]  ← 空，说明工具根本没执行

② resume(decision=True)
   phase=DONE  steps=2
   text = 已处理完毕。
   外部世界 SENT=['boss@example.com']  ← 这次真的发了

③ 新 ExecCtx 里直接带 Approval（模拟重启后续跑）
   phase=DONE  steps=2  ← 不再挂起，直接跑完

④ resume(decision=False)：拒绝
   第一次 phase=WAITING_USER → 拒绝后 phase=DONE
   模型收到的通知（写进 history）: 用户拒绝执行 send_email：收件人写错了，先别发。请换一个不依赖该高风险动作的方案。
   发信次数：2 → 2（拒绝没有产生副作用）
   loop 警告=（无）
```

三种接法对应三种真实场景：

| 写法 | 用在哪 |
|---|---|
| `loop.resume(ctx, e, decision=True, reason=...)` | 同一个进程里，前端点"同意" |
| `ExecCtx(approvals=(Approval(tool, args_hash, True),))` 或 `e.approvals = (...)` | 进程重启了 / HTTP 层审批完重建 `ExecCtx` |
| `loop.resume(ctx, e, decision=False, reason=...)` | 点了"拒绝"，模型会收到"换方案"的通知 |

`args_hash` 是**逐字比对**的：参数改一个字节，之前那份批准就不算数（防止"批 A 做 B"）。
批准后 `Loop` 会**重放**当时被拦下的那次调用，然后继续原循环。

---

## 3. 关键概念速查

按"你会先撞上谁"排序。

### 契约层（`contracts.py`，唯一允许定义共享类型的地方）

| 概念 | 一句话 | 在哪 |
|---|---|---|
| `Phase` | 回合当前在哪个阶段，11 个值；`WAITING_USER` 就是"等你审批" | `contracts.py:26` |
| `TermReason` | 为什么停：`DONE` / `BUDGET` / `MAX_STEPS` / `NO_PROGRESS` / `NEEDS_CONFIRMATION` / `DEADLINE` / `ABORTED` / `UNRECOVERABLE` | `contracts.py:60` |
| `Owner` | **谁有权写这个字段**。每个 Context 字段有且只有一个 owner | `contracts.py:40` |
| `OWNER_TABLE` | 字段 → owner 的映射表（前缀匹配）。往没声明的字段写 = 直接抛 `OwnershipViolation` | `contracts.py:129` |
| `Patch` | Context 的**唯一写入口**：`Patch(op, path, by, value)`。append 出来的 `patch_log` 同时是执行轨迹、回放源、崩溃恢复 WAL | `contracts.py:103` |
| `ContextSnapshot` | 不可变视图，`PromptBuilder` 只吃这个。**注意用 `.get("a.b")`，不能用 `snap["x"]` 下标** | `contracts.py:113` |
| `Budget` | token / 成本 / 工具调用次数 / 步数四道闸，默认 `max_steps=8` | `contracts.py:167` |
| `Principal` | 主体：`user_id` / `roles` / `tenant` / `scopes`。工具能不能跑，先看 `required_scope` 在不在 | `contracts.py:204` |
| `Approval` | 一条人工批准：`(tool, args_hash, approved, approver)` | `contracts.py:219` |
| `ExecCtx` | **主动脉**：主体 + 预算 + 截止 + trace id + 幂等键 + approvals + 取消信号，一个对象带到任何地方 | `contracts.py:227` |
| `ToolManifest` | 工具的形状声明（**不是实现**）：名字、`side_effect`、`risk_tier`、`required_scope`、`input_schema`、超时、结果大小上限 | `contracts.py:331` |
| `SideEffect` | `read_only` / `mutating` / `irreversible`。**决定能否缓存** | `contracts.py:71` |
| `RiskTier` | `T0_SAFE` / `T1_LOW` / `T2_HIGH` / `T3_CRITICAL`。**T2 未获批 → 挂起；T3 → 直接拒，连确认都不给** | `contracts.py:77` |
| `ToolOutcome` | 工具执行结果的信封：`ok` / `denied` / `needs_confirmation` / `failed` / `timeout` | `contracts.py:402` |
| `Usage` | token 计量。`total` 算窗口占用，`billable()` 才用来算钱（缓存命中打折） | `contracts.py:440` |
| `LLMResponse` | LLM 必须返回的东西：`text` + `tool_calls` + `usage` | `contracts.py:480` |
| `Result` | `Loop` 的输出：`phase` + `term_reason` + `text` + `partial` + `steps` + `artifacts` | `contracts.py:490` |
| `LLMProtocol` | LLM adapter 唯一的契约：`complete(prompt, tools, e) -> LLMResponse` | `contracts.py:512` |
| `DISPATCH_CAPABILITIES` | `Dispatcher` 声明自己提供哪些保障（审批/scope/schema/幂等/限流/预算/超时/脱敏…），装配期可校验 | `contracts.py:527` |
| `args_hash()` | 参数指纹，幂等缓存与「批准的是不是同一件事」都靠它 | `contracts.py:962` |

### 运行时四件套

| 概念 | 一句话 | 在哪 |
|---|---|---|
| `ContextHandle` | **唯一可变状态**。你只读 `snapshot()` / `patch_log`，写一律由 `Loop` 内部走 `Patch` | `core/context.py:215` |
| `Loop` | 唯一驱动者。`run(goal, ctx, e)` 从零跑，`resume(ctx, e, decision)` 从 `WAITING_USER` 续跑 | `core/loop.py:386` |
| `DeadlockDetector` | 三级死锁判定（字面计数 / 语义指纹 / 仲裁），`NO_PROGRESS` 由它触发 | `core/loop.py:208` |
| `PromptBuilder` | `ContextSnapshot → prompt 字符串`，**纯函数**。各段有 token 配额，默认总窗口 4000 | `builder.py:294` |

### 工具面（`plane/tool.py`）

| 概念 | 一句话 | 在哪 |
|---|---|---|
| `ToolRegistry` | 名字 → `(manifest, fn)`。**同名重复注册直接抛错**（防止权限被静默换掉） | `plane/tool.py:72` |
| `Dispatcher` | 唯一的副作用出口，按固定顺序过 10 道闸：存在性 → schema → scope → policy → risk_tier → 限流 → 幂等缓存 → 预算 → secret 注入 → 超时执行 | `plane/tool.py:417` |
| `Normalizer` | 结果四道处理：类型收口 → 大小截断（原文落盘成 artifact）→ 去重 → 凭据脱敏 | `plane/tool.py:124` |
| `Policy` | 静态黑名单（`rm -rf`、`curl\|sh`、`sudo`、`DROP TABLE`…）。`check` 返回 str = 拒绝原因 | `plane/tool.py:312` |
| `SubprocessSandbox` | `shell=False` + 可执行白名单的最小沙箱 | `plane/tool.py:351` |

### 适配器与观测

| 概念 | 一句话 | 在哪 |
|---|---|---|
| `ScriptedLLM` / `CallableLLM` | 确定性假 LLM。**有状态**：脚本播完会重复最后一条 | `adapters/llm_mock.py:26` |
| `OpenAICompatAdapter` | 一份实现接 OpenAI / DeepSeek / Qwen / vLLM / Ollama；只对 429/5xx/网络抖动重试，401/400 不重试 | `adapters/openai_compat.py:173` |
| `from_env("OPENAI")` | 读 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`，缺了返回 `None` 而不是抛错 | `adapters/openai_compat.py:690` |
| `Tracer` / `Trajectory` / `Evaluator` | 轨迹即 patch log；评测与回放都从它复原，不需要额外埋点 | `plane/quality.py:134` / `403` / `534` |
| `Gateway` | HTTP 面（`serve.py` 用的就是它） | `core/gateway.py:317` |

---

## 4. 上手障碍（第一次看到这个仓库时实际卡住的地方）

以下每条都是**实测踩到的**，不是推测。按"卡住的严重程度"排。

### 4.1 `pip install agent-runtime` 装到的是别人的包 ⚠️ 最严重

README 第一条命令是 `pip install agent-runtime`。PyPI 上确实存在这个包，但：

```
version:  0.0.1
summary:  Agent Runtime
author:   AutoGen Developers
home_page: https://github.com/microsoft/autogen
upload:   2025-01-06
requires_python: >=3.6
```

也就是说新人照 README 第一条命令执行，装进来的是 **AutoGen 的一个 0.0.1 占位包**。
把它下载下来看内容更清楚（`pip download agent-runtime --no-deps --no-binary :all:`）：

```console
$ tar xzf agent_runtime-0.0.1.tar.gz && cat agent_runtime-0.0.1/agent-runtime/__init__.py
# agent-runtime package
$ cat agent_runtime-0.0.1/agent_runtime.egg-info/top_level.txt
agent-runtime
autogen-agentchat
autogen-agentflow
autogen-core
...
```

`__init__.py` 里只有一行注释，而顶层模块名是 `agent-runtime`（带连字符，**根本 import
不了**）。本仓的 `contracts` / `core` / `plane` 一个都没有，`pip` 也不会报任何错。
本仓 `pyproject.toml` 的版本是 `0.2.0`，从未发布到这个名字上。

建议（不改 README，只记录）：README 的 pip 那段要么标"未发布到 PyPI，请用 clone 方式"，
要么把 clone 提到第一条。

### 4.2 `risk_tier=0` 不报错，而且**静默把工具从 prompt 里删掉**

这是本文档作者写第一版示例时踩的坑。`ToolManifest(risk_tier=0)`（裸 int）完全合法，
`Dispatcher` 也能正常拦/放行，但 `PromptBuilder._render_tools` 读的是
`m.risk_tier.name`：

```
AttributeError: 'int' object has no attribute 'name'
```

而 `Loop._build_prompt` 把这个异常**吞掉**了，返回一行
`[builder 失败：AttributeError: ...] 请基于 history 继续。` 当 prompt 继续跑。表现是：

- `phase = DONE`、`steps = 2`、`text` 看着正常
- **模型压根看不到任何工具**（prompt 里 tools 段没了）
- 没有任何异常冒出来，只有 `loop.warnings` 里留一句

换句话说：这个错误在最坏的位置静默 —— 空手跑的模型会一本正经地编答案，而你看到的
指标全绿。

`demo/fixtures.py` 里全部写 `RiskTier.T0_SAFE`，所以 demo 从来碰不到；新人从零写
manifest 时几乎一定会写 `risk_tier=1` 这种更自然的形式。示例统一用
`RiskTier.T0_SAFE` / `RiskTier.T2_HIGH`，并在 `01_minimal.py` 的 `build_registry()`
docstring 里把这个坑写下来了；`01_minimal.py` 结尾还会打印 `loop.warnings`（**每次跑完
都该看一眼**，Loop 的内部异常只记在那里）。

### 4.3 模型挂了不抛异常，是一个 `Result`

`Loop._drive` 里 `except Exception` 把 LLM 的异常收成
`Result(FAILED, UNRECOVERABLE)`。所以：

```python
try:
    result = loop.run(goal, ctx, e)
except Exception:          # ← 抓不到模型故障
    ...
```

新人按常规直觉写 try/except，模型 401 了却什么也没发生，只看得到 `text` 里一行被脱敏的
错误。必须检查 `term_reason`，并且错误类别要从 `adapter.history[-1]["kind"]` 拿 ——
`Result` 里没有 `kind`。

另一个小摩擦：adapter 脱敏是**字面替换** `message.replace(self.api_key, "***")`（加上两条
key 形状的正则）。所以调试时如果随手 `export API_KEY=x`，所有含 `x` 的词都会被改花 ——
作者实测拿到过 `all models e***hausted after 3 request(s)`。用正常长度的假 key 就没事。

### 4.4 扁平布局的 import 陷阱

`from contracts import ...` 能成立的前提是**仓库根在 `sys.path` 里**。直接
`python examples/quickstart/01_minimal.py` 不加 `sys.path.insert` 时：

```
ModuleNotFoundError: No module named 'builder'
```

报错完全没提"路径不对"，新人第一反应通常是 `pip install -e .`。但看
`pyproject.toml`：

```toml
py-modules = ["contracts", "builder", "auth", "toolconfig"]
packages.find.include = ["core*", "plane*", "adapters*", "agents*", "integrations*"]
```

装到 site-packages 后就是**无命名空间的顶层 `contracts` / `core` / `builder`** ——
任何同名包（很多项目都有 `core/`）会互相覆盖。所以"pip 装"和"clone 用"是两条不一致的
路径，文档里没讲这条。示例统一用 `sys.path.insert(0, <仓库根>)`，三种启动方式
（仓根、examples 目录内、系统 python3）都实测能跑。

### 4.5 概念先于解释，"补丁协议"没人定义

README 首屏直接给七个决定：**"Patch protocol context"**、"ExecCtx as aorta"、
"三层状态切分"。第一次看会卡在这里 —— 它没说 `ContextHandle` 是那个"上下文"、
`Owner` 是什么、为什么写一个字段要先声明归属。

实测 grep 计数（README 全文）：

| 词 | README 出现次数 | 说明 |
|---|---|---|
| `ExecCtx` | 3 | 有，但没说它是 dataclass 还是协议 |
| `Patch` / `补丁` | 3 | 只给了 `apply(Patch(op,path,by,value))` 一行 |
| `Dispatcher` | 1 | 没有说它是唯一副作用出口 |
| `ContextHandle` | **0** | 新人根本不知道往哪写状态 |
| `PromptBuilder` | **0** | 不知道 prompt 从哪来 |
| `ToolManifest` | **0** | 不知道工具怎么声明 |
| `risk_tier` | **0** | 而它决定审批行为 |

本文第 3 节的表就是为了填这个洞。

### 4.6 `ContextSnapshot` 不能用下标

`ctx.snapshot()["history"]` → `TypeError: 'ContextSnapshot' object is not subscriptable`。
它只有 `.get(path, default)`，而且 `path` 支持 `"a.b.c"` 点路径。从 dict 迁过来的人
一定会先写下标。

### 4.7 `ScriptedLLM` 是有状态的，复用会得到假结论

示例 ③ 第一版四段演示共用一个 `ScriptedLLM` 和一个 `Loop`，结果第 ③ 段的 `steps`
掉到 1、第 ④ 段**根本不再挂起**（`phase` 直接 `DONE`，"审批没触发"）。原因不是审批坏了，
而是**脚本已经播完、模型不再请求工具** —— 只有第 ① 段真的撞上了 T2 闸。
`demo/run.py` 每个场景都 `confirmation_llm()` 新建一个，但没有任何一处说明"为什么要新建"。
现在示例里每段都 `new_loop()` 并写了注释。

### 4.8 没有"最小例子"，第一条成功反馈来得很晚

README 的路径是：pip（装错包）→ `demo/run.py`（11 个场景、30 秒、80 条断言、几百行
SRE 业务脚本）→ `serve.py`（4796 行）+ `curl`。中间**没有一步是"我自己写 10 行、看到一个
结果"**。

`demo/fixtures.py` 的 229 行里全是"连接池上限 20 / 重启 api 服务 / P1 告警"这类**业务
内容**，而文件头明确写着"这不是 Runtime 的一部分"。新人很容易把它当模板照抄，把业务
词汇抄进自己的工具里 —— 而 `tests/test_architecture.py` 恰恰禁止 `core/`、`plane/` 出现
业务词。

### 4.9 `docs/design/` 没有阅读顺序

README 那张表的顺序是 STATUS / SPI / TOOLS / DEPLOY / ROBUSTNESS / DESIGN-C / DEVLOG，
看不出逻辑（第一份 STATUS 是模块地图，对新人最没用；最有用的 SPI 排第二）。新人不知道
先看哪个。实际顺序应该是 **SPI（扩展点契约）→ TOOLS（工具与风险分级）→
STATUS（模块地图）→ 其余**。另外 `docs/design/` 里没有任何 `README.md` / `index.md`，
`docs/index.html` 是网页壳子，不提供阅读路径。

### 4.10 小摩擦

- `.venv` 实际是 Python **3.14.3**，README 徽章写 `3.12+`，`requirements-core.txt` 注释
  才提到 3.14 已验证 —— 版本信息散在三处。
- README 说"2215 tests / 145 modules / 76k lines"，实测（本机 HEAD `f11432b`）：
  `def test_` 2184 个；全仓 `.py` **79284 行**，其中 `tests/` 35968 行、其余 43316 行，
  而 `core/` 只有 9219 行 / 13 个文件。数字口径是"含测试"，但没写明 —— 新人按
  "76k 行"去估 `core/` 的规模会高一个量级。
- `demo/run.py` 输出 "11/11 场景跑通"，但源码里 `cases` 是 `4 + PLANE + WF + DUR +
  FULL + CONC` 拼出来的，光看 `run.py` 数不出 11 个。
- 沙箱后端探测在 demo 里会真的去调 `docker info`（本机 0.26s），没装 docker 的机器上
  这段会走失败分支再回退到 subprocess —— 结果是"能跑但多等一拍"，输出里只有一行
  `available=False`。
- `serve.py` 的 `AGENT_RUNTIME_ALLOW_ANONYMOUS=1` 只解决了"能不能进"。README 那个 `curl`
  例子里 `scopes` 是 `["runtime:run","tool.read"]`，而 `_principal_from_dict`（`serve.py:1593`）
  **不给任何默认 scope** —— 于是所有 `required_scope="tool.write"` 的工具一律 `denied`，
  而响应里只有一句 `denied`。新人会以为"模型不会用工具"。

---

## 5. 从示例往下走

1. 想让工具真被模型用起来 → 读 `docs/design/TOOLS.md`（`input_schema` 为什么必填、
   `risk_tier` 怎么选、`tools.yaml` 怎么接线）。
2. 想接自己的模型 → 照 `adapters/openai_compat.py` 实现
   `complete(prompt, tools, e) -> LLMResponse` 即可，两个方法都行。
3. 想知道每个字段谁能写 → `contracts.py:129` 的 `OWNER_TABLE`，配合 `docs/design/SPI.md`。
4. 想做评测/回放 → `plane/quality.py` 的 `Trajectory.from_patch_log(ctx.patch_log)`
   （`demo/run.py` 场景 1 有完整演示）。
5. 想看审批在 HTTP 面怎么走 → `serve.py` + `core/gateway.py`，`WAITING_USER` 会被序列化
   成响应，恢复走 `Loop.resume`。
