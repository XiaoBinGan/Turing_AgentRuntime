"""toolconfig.py — 声明式工具配置：YAML → `ToolManifest` + handler。

## 这个文件补的是什么

`serve.py` 里的 `build_gateway_from_env()` 现在是 `Loop(..., tools=[])`（显式空表），
注释说得对 ——「服务层不替部署方决定这个进程能干什么」。问题是它**没留声明入口**：
想给进程加一个工具，只能改 Python 源码。运维加一个「查 p99」的工具就要开 PR，
这是本模块存在的唯一理由。

本模块只做一件事：把「这个进程有哪些工具」从代码里搬到一个 YAML 文件里，
产出的东西和手写注册**完全等价**（`ToolManifest` + `fn(args, ctx)`），
所以 Dispatcher 侧的权限 / 审批 / 幂等 / 限流 / 脱敏一律照旧，没有旁路。

    config = load_tools("tools.yaml")        # 或 load_tools_from_env()
    try:
        config.register_into(registry)       # registry 就是 plane.tool.ToolRegistry
        dispatcher = Dispatcher(registry)
        ...
    finally:
        config.close()                       # MCP 子进程必须在这里收掉

## 两种工具来源

1. **内置工具**（`tools:`）：`module:function` 的 import path。进程内函数，
   没有子进程，调用成本 = 一次函数调用。
2. **MCP server**（`mcp_servers:`）：真起 stdio 子进程，工具清单由 server 自报
   （`adapters/mcp_transport.attach_stdio_server`）。注册名带 `<server>__` 前缀。

## 三条硬规则（都是「宁可拒绝启动，不许静默降级」）

* **fail-closed**：`defaults.allow_irreversible: false`（默认）时，配置里出现
  `side_effect: irreversible` 直接报错。理由见 `_check_irreversible`。
* **未定义的环境变量报错**：`${VAR}` 展开不到就报错，**不**退化成空串。
  退化成空串的后果是 `DATABASE_URL=""` 这种配置被当成合法值一路带到运行时。
* **错误信息必须可行动**：每条 `ToolConfigError` 都带「文件 + 哪一项 + 错在哪」。
  「工具配置加载失败」这种消息在运维场景里等于没报错。

## 谁能改什么

本模块是**只读消费方**：`contracts.py` / `plane/*` / `adapters/*` 一个字节都不改。
不碰 `serve.py`（那是接线方的事），这里只提供 `load_tools` / `load_tools_from_env`。

跑法：
    .venv/bin/python -m unittest tests.test_toolconfig -v
"""
from __future__ import annotations

import inspect
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from contracts import RiskTier, SideEffect, ToolManifest

# pyyaml 是**别人的传递依赖**（llama-index 带进来的）。别人的依赖不是本模块的保证，
# 所以按可选依赖处理：没装时 `import toolconfig` 仍然成功（纯 `parse_tools_config`
# 用不到 YAML），真去 `load_tools()` 才给出可行动的错误。
try:                                            # pragma: no cover - 取决于环境
    import yaml
except ImportError:                             # pragma: no cover
    yaml = None                                 # type: ignore[assignment]

# jsonschema 同理：装了才校验 input_schema 的**形状**，没装就跳过并记 warning。
# 注意它和 contracts.validate_against_schema 是同一份依赖，但这里只用来做
# 「配置期静态检查」——运行期的入参校验仍然只发生在 Dispatcher 里，不重复实现。
try:                                            # pragma: no cover - 取决于环境
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for
except ImportError:                             # pragma: no cover
    SchemaError = None                          # type: ignore[assignment]
    validator_for = None                        # type: ignore[assignment]

__all__ = [
    "LoadedTools",
    "ToolConfigError",
    "load_tools",
    "load_tools_from_env",
    "parse_tools_config",
    "ENV_VAR",
    "SUPPORTED_VERSION",
    "DEFAULT_MAX_TOOLS",
]

#: 环境变量名：`load_tools_from_env()` 只认这一个（不猜、不拼多个名字的花样）
ENV_VAR = "AGENT_RUNTIME_TOOLS"

#: 支持的配置格式版本。**没有 version 字段 = 报错**（见 `_parse` 里的注释）
SUPPORTED_VERSION = 1

#: `defaults.max_tools` 缺省值：一个 turn 里模型看得到的工具数上限。
#: 64 不是拍脑袋 —— 超过这个量级，工具描述本身就吃掉半个 prompt，
#: 模型选错工具的概率比多给它的能力涨得更快。
DEFAULT_MAX_TOOLS = 64

#: 副作用 → 默认风险档。与 `MCPToolBridge` / `attach_stdio_server` 同一条规则：
#: 只读直接放行（T0）、写要审批（T2）、不可逆只能拒（T3）。
_SIDE_EFFECT_TIER: dict[SideEffect, RiskTier] = {
    SideEffect.READ_ONLY: RiskTier.T0_SAFE,
    SideEffect.MUTATING: RiskTier.T2_HIGH,
    SideEffect.IRREVERSIBLE: RiskTier.T3_CRITICAL,
}

#: 工具名允许的字符集：与 OpenAI function name 的 `^[a-zA-Z0-9_-]+$` 兼容，
#: 额外放行 `.`（便于「一个 server 的工具天然分组」）。**不放行空格/中文** ——
#: 这类名字过不了 LLM provider 的 schema 校验，等到运行时才 400 就太晚了。
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: `${VAR}` / `${VAR:-default}` 的单次匹配
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: `defaults` 段允许的键（多一个就报错：拼错 `allow_irreversable` 会静默放行删库）
_DEFAULT_KEYS = frozenset({"allow_irreversible", "max_tools"})

#: 单个 import 工具允许的键
_TOOL_KEYS = frozenset({
    "name", "description", "import", "side_effect", "risk_tier", "idempotent",
    "required_scope", "timeout_ms", "max_result_bytes", "per_turn_limit",
    "secrets", "input_schema",
})

#: 单个 MCP server 允许的键
_MCP_KEYS = frozenset({
    "name", "command", "args", "env", "cwd", "startup_timeout_s", "call_timeout_s",
    "default_side_effect", "default_risk_tier", "overrides",
})

#: 顶层允许的键
_TOP_KEYS = frozenset({"version", "tools", "mcp_servers", "defaults"})

#: `input_schema` 缺失时的兜底：合法的空 object schema。
#: 给 `{}` 不是合法 schema（根上没有 type），而 `None` 会让 Dispatcher 完全跳过校验 ——
#: 两者都会让模型开始瞎猜参数名，兜底成这个最小合法形状是最不容易出错的。
_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class ToolConfigError(RuntimeError):
    """配置错误。**必须说清是哪个文件、哪一项、错在哪**。

    继承 `RuntimeError`：接线方（serve.py / demo）按 `RuntimeError` 兜底捕获时不会漏掉；
    同时它是**启动期**错误 —— 配置坏了就别启动，而不是启动后少了一半工具慢慢排查。
    """


def _err(source: str, item: str, detail: str) -> ToolConfigError:
    """拼一条「文件 + 项 + 原因」的错误。所有报错都走这里，保证格式一致。

    `source` 是配置文件路径（`parse_tools_config` 直接从内存 dict 解析时是 `<config>`），
    `item` 是配置里的定位串（`tools[2] (name=query_metrics)` 这种），
    `detail` 才是人真正要看的那句。
    """
    return ToolConfigError(f"{source}: {item}: {detail}")


# ══════════════════════════════════════════════════════════════
# 产物
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class LoadedTools:
    """一次配置加载的全部产物。

    四个字段的分工：
      * `manifests`：给 Builder 生成工具描述 / 给 `ToolRegistry.register` 用；
      * `handlers`：`name -> fn(args, ctx)`，签名与 `plane.tool.ToolFn` 一致；
      * `connections`：真起来的 MCP 子进程连接。**必须 `close()`**，
        否则设备上会留一排 `mcp_fixture_server` 孤儿进程；
      * `warnings`：不致命但该被人看见的事（跳过 jsonschema 校验、启发式判不出副作用…）。
        刻意不用 `logging.warning` 收工：启动期的一条日志没人看，
        而这些东西正是「工具行为和你以为的不一样」的根源。

    `close()` 幂等：MCP 连接的 `stop()` 本身幂等，重复调用无害（接线方可能在
    lifespan 退出和异常分支各调一次，不该为此抛异常）。
    """

    manifests: tuple[ToolManifest, ...] = ()
    handlers: dict[str, Callable[[dict, Any], Any]] = field(default_factory=dict)
    connections: tuple[Any, ...] = ()
    warnings: tuple[str, ...] = ()

    # ── 生命周期 ────────────────────────────────────────────────
    def close(self) -> None:
        """收掉所有 MCP 子进程。幂等；单个连接出错不打断其余连接的清理。"""
        for conn in self.connections:
            # 先 stop（关 session → SDK terminate 子进程 → 停后台 loop 线程），
            # 再 close_stderr（删临时文件）。顺序不能反：stderr 文件是 stop 期间
            # 唯一的诊断来源，先删了就只能看到「进程没了」而不知道它说了什么。
            for method in ("stop", "close_stderr"):
                fn = getattr(conn, method, None)
                if not callable(fn):
                    continue
                try:
                    fn()
                except Exception:                # noqa: BLE001 — 清理期绝不向上抛
                    pass

    def __enter__(self) -> "LoadedTools":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False

    # ── 便捷视图 ────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self.manifests)

    def names(self) -> list[str]:
        return [m.name for m in self.manifests]

    def manifest(self, name: str) -> ToolManifest | None:
        for m in self.manifests:
            if m.name == name:
                return m
        return None

    def register_into(self, registry: Any) -> list[ToolManifest]:
        """把加载结果注册进 `ToolRegistry`（或任何有 `register(manifest, fn)` 的对象）。

        单独开这个方法而不是让调用方自己 for 循环：注册顺序、handler 缺失时的报错、
        部分注册失败的处理都该在一处定下来，不该每个接线点各写一遍。
        """
        registered: list[ToolManifest] = []
        for manifest in self.manifests:
            handler = self.handlers.get(manifest.name)
            if handler is None:
                raise ToolConfigError(
                    f"工具 {manifest.name!r} 有 manifest 但没有 handler（加载器 bug）")
            registry.register(manifest, handler)
            registered.append(manifest)
        return registered


# ══════════════════════════════════════════════════════════════
# ① 基础解析：枚举 / 标量
# ══════════════════════════════════════════════════════════════
def _parse_side_effect(raw: Any, source: str, item: str) -> SideEffect:
    if isinstance(raw, SideEffect):
        return raw
    if isinstance(raw, str):
        try:
            return SideEffect(raw.strip().lower())
        except ValueError:
            pass
    valid = ", ".join(s.value for s in SideEffect)
    raise _err(source, item, f"side_effect={raw!r} 非法；可选值：{valid}")


def _parse_risk_tier(raw: Any, source: str, item: str) -> RiskTier:
    if isinstance(raw, RiskTier):
        return raw
    # bool 是 int 的子类，`risk_tier: true` 会静默变成 T1 —— 先挡掉
    if isinstance(raw, bool):
        raise _err(source, item, f"risk_tier={raw!r} 非法；要的是 T0/T1/T2/T3")
    if isinstance(raw, int):
        try:
            return RiskTier(raw)
        except ValueError:
            raise _err(source, item,
                       f"risk_tier={raw!r} 超出范围；可选值：T0, T1, T2, T3") from None
    if isinstance(raw, str):
        text = raw.strip().upper()
        try:
            return RiskTier(int(text.lstrip("T")))
        except (ValueError, TypeError):
            pass
    raise _err(source, item, f"risk_tier={raw!r} 非法；可选值：T0, T1, T2, T3")


def _parse_int(raw: Any, source: str, item: str, *,
               minimum: int | None = None, what: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _err(source, item, f"{what} 必须是整数，收到 {type(raw).__name__}（{raw!r}）")
    if minimum is not None and raw < minimum:
        raise _err(source, item, f"{what}={raw} 太小；必须 >= {minimum}")
    return raw


def _parse_float(raw: Any, source: str, item: str, *, what: str,
                 minimum: float = 0.0) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _err(source, item, f"{what} 必须是数字，收到 {type(raw).__name__}（{raw!r}）")
    if float(raw) <= minimum:
        raise _err(source, item, f"{what}={raw} 必须 > {minimum}")
    return float(raw)


def _parse_bool(raw: Any, source: str, item: str, *, what: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise _err(source, item, f"{what} 必须是 true/false，收到 {type(raw).__name__}（{raw!r}）")


def _parse_str(raw: Any, source: str, item: str, *, what: str) -> str:
    if isinstance(raw, str):
        return raw
    raise _err(source, item, f"{what} 必须是字符串，收到 {type(raw).__name__}（{raw!r}）")


def _check_keys(raw: Mapping[str, Any], allowed: frozenset[str],
                source: str, item: str) -> None:
    """未知键 → 报错。

    为什么不容忍未知键：YAML 里拼错一个键（`mcp_server:` / `allow_irreversable:`）
    在「忽略未知键」的策略下表现是「这段配置**静默消失**」—— 工具没了、
    不可逆闸门松了，且没有任何提示。启动期报错三秒就能改好，静默反而是事故。
    """
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise _err(source, item,
                   f"未知字段 {unknown}；允许的字段：{sorted(allowed)}")


# ══════════════════════════════════════════════════════════════
# ② 环境变量插值
# ══════════════════════════════════════════════════════════════
def _expand_env_value(value: str, source: str, item: str,
                      env: Mapping[str, str]) -> str:
    """展开 `${VAR}` 与 `${VAR:-default}`；**未定义且没有默认值 → 报错**。

    为什么不退化成空串：`env: {DATABASE_URL: "${DATABASE_URL}"}` 在变量没设时
    会变成 `DATABASE_URL=""`，server 起来后报的是「连接串非法」—— 排查起点被推到了
    子进程里，而真正的错因（配置要求的东西环境里没有）在这一步就已经确定了。

    `:-` 的语义按 shell：变量**未设置或为空**时用默认值（空串在 shell 里也算 unset，
    这里保持同一语义，免得 `${A:-x}` 在 `A=` 时得到空串这种反直觉结果）。
    """
    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        current = env.get(name)
        if current is None or (current == "" and default is not None):
            if default is not None:
                return default
            raise _err(
                source, item,
                f"环境变量 ${{{name}}} 未定义（值里引用了它，但没有默认值）；"
                f"先 export {name}=...，或写成 ${{{name}:-默认值}}")
        return current

    return _ENV_REF_RE.sub(_sub, value)


def _expand_env_map(raw: Any, source: str, item: str,
                    env: Mapping[str, str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _err(source, item, f"env 必须是 mapping，收到 {type(raw).__name__}")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise _err(source, item, f"env 的键必须是非空字符串，收到 {key!r}")
        if value is None:
            # YAML 里 `KEY:` 后面留空 = 空串。这是「清掉继承来的同名变量」的合法写法，
            # 所以允许，但值必须显式成字符串（不能把 None 传给子进程）。
            out[key] = ""
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise _err(source, item,
                       f"env[{key!r}] 必须是标量，收到 {type(value).__name__}")
        out[key] = _expand_env_value(str(value), source, f"{item}.env[{key!r}]", env)
    return out


# ══════════════════════════════════════════════════════════════
# ③ import 加载
# ══════════════════════════════════════════════════════════════
def _resolve_import(spec: Any, source: str, item: str) -> Callable[..., Any]:
    """`module:function` → 真函数。任何一步失败都给「哪个 module / 哪个函数」。

    为什么要自己解析而不是 `importlib.import_module` 后一次 getattr：
    「module 不存在」和「函数不存在」是两种完全不同的修法（装依赖 vs 改名字），
    合并成一句 `ImportError` 会让人去查错方向。
    """
    text = _parse_str(spec, source, item, what="import")
    if ":" not in text:
        raise _err(source, item,
                   f"import={text!r} 形状不对；要的是 'module:function'（例如 myapp.tools:query_metrics）")
    module_name, _, attr_path = text.partition(":")
    module_name, attr_path = module_name.strip(), attr_path.strip()
    if not module_name or not attr_path:
        raise _err(source, item,
                   f"import={text!r} 形状不对；module 和 function 都不能为空")

    try:
        module = __import__(module_name, fromlist=["__all__"])
    except ImportError as exc:
        raise _err(source, item,
                   f"模块 {module_name!r} 导入失败：{type(exc).__name__}: {exc}"
                   f"（检查 PYTHONPATH / 拼写 / 该模块的依赖是否装了）") from exc
    except Exception as exc:                     # noqa: BLE001 — 模块顶层代码炸了
        raise _err(source, item,
                   f"模块 {module_name!r} 导入时抛错：{type(exc).__name__}: {exc}") from exc

    target: Any = module
    for part in attr_path.split("."):
        if not hasattr(target, part):
            raise _err(source, item,
                       f"模块 {module_name!r} 里没有 {attr_path!r}"
                       f"（在 {getattr(target, '__name__', target)!r} 上找不到属性 {part!r}）")
        target = getattr(target, part)
    if not callable(target):
        raise _err(source, item,
                   f"{module_name}:{attr_path} 不是 callable（收到 {type(target).__name__}）；"
                   "工具必须是一个函数")
    return target


def _positional_arity(fn: Callable[..., Any]) -> int:
    """数一下这个函数能接几个位置参数，用来决定要不要把 `ExecCtx` 递进去。

    为什么要自适应而不是定死 `(args, ctx)`：内置工具多半只关心参数
    （`def query_metrics(args)`），逼它们多写一个用不到的 ctx 是把接口负担转嫁给运维 ——
    而运维正是本模块要服务的人。两参数版本 `(args, ctx)` 仍然支持（要读
    principal / budget 的工具用得上）。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):              # 内建函数 / C 扩展：按单参数处理
        return 1
    count = 0
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return 2                             # (*args) 能接两个，就给两个
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD):
            count += 1
    return min(count, 2)


def _wrap_handler(name: str, fn: Callable[..., Any]) -> Callable[[dict, Any], Any]:
    """把用户函数包成 `Dispatcher` 要求的 `fn(args, ctx)` 同步签名。

    顺带做两件收口：`args` 一定是 dict（模型可能传 None / list），
    返回值原样给出（截断 / 脱敏是 `Normalizer` 的职责，这里不重复实现）。
    """
    arity = _positional_arity(fn)

    def handler(args: dict, ctx: Any = None) -> Any:
        payload = dict(args or {}) if isinstance(args, Mapping) else {}
        if arity >= 2:
            return fn(payload, ctx)
        if arity == 1:
            return fn(payload)
        return fn()

    handler.__name__ = f"toolconfig_handler_{name}"
    handler.__doc__ = getattr(fn, "__doc__", None)
    return handler


# ══════════════════════════════════════════════════════════════
# ④ input_schema
# ══════════════════════════════════════════════════════════════
def _parse_input_schema(raw: Any, source: str, item: str,
                        warnings: list[str]) -> dict:
    """校验 `input_schema` 的形状；缺失时补合法兜底。

    只有**形状**校验（`check_schema`）：语义对不对（该 required 的字段有没有漏）
    机器判不了，人得自己负责。但不校验形状的代价是运行期每一次调用都
    fail-closed 失败（Dispatcher 对坏 schema 直接 failed），等于工具全废 —— 所以这一步是必须的。
    """
    if raw is None:
        warnings.append(
            f"{item}: 没有声明 input_schema，已兜底成空 object schema。"
            f"模型只能靠工具名猜参数名（实测会把 lines 写成 limit），建议补上")
        return dict(_EMPTY_SCHEMA)
    if not isinstance(raw, Mapping):
        raise _err(source, item,
                   f"input_schema 必须是 mapping（JSON Schema 对象），收到 {type(raw).__name__}")
    schema = dict(raw)
    if not schema:
        warnings.append(f"{item}: input_schema 是空 mapping，已替换成空 object schema")
        return dict(_EMPTY_SCHEMA)
    if validator_for is None or SchemaError is None:   # pragma: no cover - 取决于环境
        warnings.append(
            f"{item}: 未安装 jsonschema，跳过 input_schema 形状校验（坏 schema 会在运行期被拒）")
        return schema
    try:
        validator_cls = validator_for(schema)   # 按 $schema 选 draft，缺省用最新（2020-12）
        validator_cls.check_schema(schema)
    except SchemaError as exc:
        detail = getattr(exc, "message", None) or str(exc)
        raise _err(source, item, f"input_schema 不是合法 JSON Schema：{detail}") from exc
    return schema


# ══════════════════════════════════════════════════════════════
# ⑤ fail-closed：不可逆闸门
# ══════════════════════════════════════════════════════════════
def _check_irreversible(manifests: Sequence[ToolManifest], *, allow: bool,
                        source: str) -> None:
    """`allow_irreversible=False` 时，出现 irreversible 工具就**拒绝加载**。

    为什么默认是 false 且是报错而不是忽略/降级：
      * 一个删数据的工具被「顺手」配进来（复制粘贴上一个 server 的段落、
        合并别人的 PR、运维照着示例改），在配置里看不出任何异常 —— 它和其他
        工具长得一模一样。忽略掉条目等于「配置声明了 T3 能力但进程没有」，
        人以为自己关掉了，其实是**以为反了**；降级成 T2 更糟：审批一下就把数据删了。
      * 报错把「要不要给它这个能力」变成一个必须显式回答的问题
        （`allow_irreversible: true` 写在配置里、进 code review），
        这正是这类能力该有的摩擦。
      * 运行时 T3 本来就被 Dispatcher 硬拒（`critical tools cannot be confirmed,
        only refused`），所以默认拒绝加载不减少任何**可用**能力 —— 它只是把
        「配了也用不了」提前到启动期说清楚。
    """
    if allow:
        return
    hits = [m.name for m in manifests if m.side_effect is SideEffect.IRREVERSIBLE]
    if hits:
        raise _err(
            source, "defaults.allow_irreversible",
            f"配置里有不可逆工具 {hits}，但 allow_irreversible=false（默认）。"
            f"删数据的工具不该被顺手配进来：确认要开放就在配置里显式写 "
            f"`defaults: {{allow_irreversible: true}}`，否则把这些工具摘掉")


# ══════════════════════════════════════════════════════════════
# ⑥ 内置工具
# ══════════════════════════════════════════════════════════════
def _parse_tool_item(raw: Any, index: int, *, source: str,
                     ) -> tuple[ToolManifest, Callable[[dict, Any], Any], list[str]]:
    """解析 `tools[i]` → `(manifest, handler, warnings)`。"""
    where = f"tools[{index}]"
    if not isinstance(raw, Mapping):
        raise _err(source, where, f"每一项必须是 mapping，收到 {type(raw).__name__}")
    _check_keys(raw, _TOOL_KEYS, source, where)

    name = _parse_str(raw.get("name"), source, where, what="name").strip()
    if not name:
        raise _err(source, where, "name 不能为空")
    where = f"tools[{index}] (name={name})"
    if not _NAME_RE.match(name):
        raise _err(source, where,
                   f"name={name!r} 含非法字符；只允许字母/数字/下划线/连字符/点")
    if "import" not in raw:
        raise _err(source, where, "缺少 import 字段（形如 'module:function'）")

    warnings: list[str] = []
    fn = _resolve_import(raw.get("import"), source, where)

    # 副作用 → 默认风险档 → 默认 scope / 幂等。缺 side_effect 时取 MUTATING：
    # 和 MCP 的保守默认一致（协议不告诉读写时宁可多一道审批）。
    side = (_parse_side_effect(raw["side_effect"], source, where)
            if "side_effect" in raw else SideEffect.MUTATING)
    tier = (_parse_risk_tier(raw["risk_tier"], source, where)
            if "risk_tier" in raw else _SIDE_EFFECT_TIER[side])
    idempotent = (_parse_bool(raw["idempotent"], source, where, what="idempotent")
                  if "idempotent" in raw else side is SideEffect.READ_ONLY)
    scope = (_parse_str(raw["required_scope"], source, where, what="required_scope")
             if "required_scope" in raw
             else ("tool.read" if side is SideEffect.READ_ONLY else "tool.write"))
    timeout_ms = (_parse_int(raw["timeout_ms"], source, where, minimum=1,
                             what="timeout_ms")
                  if "timeout_ms" in raw else 5000)
    max_bytes = (_parse_int(raw["max_result_bytes"], source, where, minimum=1,
                            what="max_result_bytes")
                 if "max_result_bytes" in raw else 65536)
    per_turn = (_parse_int(raw["per_turn_limit"], source, where, minimum=1,
                           what="per_turn_limit")
                if "per_turn_limit" in raw else 10)
    description = (_parse_str(raw["description"], source, where, what="description")
                   if "description" in raw else "")
    secrets: tuple[str, ...] = ()
    if "secrets" in raw:
        raw_secrets = raw["secrets"]
        if not isinstance(raw_secrets, (list, tuple)):
            raise _err(source, where, "secrets 必须是字符串列表")
        secrets = tuple(_parse_str(s, source, where, what="secrets[]")
                        for s in raw_secrets)

    schema = _parse_input_schema(raw.get("input_schema"), source, where, warnings)

    manifest = ToolManifest(
        name=name,
        side_effect=side,
        risk_tier=tier,
        idempotent=idempotent,
        timeout_ms=timeout_ms,
        max_result_bytes=max_bytes,
        required_scope=scope,
        secrets=secrets,
        per_turn_limit=per_turn,
        description=description,
        input_schema=schema,
    )
    return manifest, _wrap_handler(name, fn), warnings


# ══════════════════════════════════════════════════════════════
# ⑦ MCP server
# ══════════════════════════════════════════════════════════════
def _heuristic_undecided(remote: str, schema: dict | None) -> bool:
    """启发式是不是**放弃了**（而不是判出了「写」）？

    必须这么绕一下：`classify_side_effect()` 在「名字是 create_/delete_」
    和「名字毫无动词信号」两种情况下返回的都是 MUTATING/T2，光看返回值区分不了。
    而 `default_side_effect` 的语义是「**判不出来时**用它」——
    对 `create_note` 也套默认值，等于允许把写操作配成只读（那就是拆审批闸门）。

    判定用的是 `adapters/mcp_client` 里那两个模块级词元函数（只 import，不修改
    BRIEF 划给 S2 的文件）。拿不到就在 `_apply_mcp_defaults` 里退化成「不套默认值」，
    方向是保守的（保持启发式结论），不会因为 SDK 改名而放宽。
    """
    try:
        from adapters.mcp_client import _tokens, _verbs_in
    except ImportError:                          # pragma: no cover - 依赖被人删了
        return False
    tokens = _tokens(remote)
    if isinstance(schema, Mapping):
        props = schema.get("properties")
        if isinstance(props, Mapping):
            for key in props:
                tokens.extend(_tokens(key))
        required = schema.get("required")
        if isinstance(required, (list, tuple)):
            for key in required:
                if isinstance(key, str):
                    tokens.extend(_tokens(key))
    irreversible, write, read = _verbs_in(tokens)
    return not (irreversible or write or read)


def _apply_mcp_defaults(manifest: ToolManifest, *, default_side_effect: SideEffect | None,
                        default_risk_tier: RiskTier | None,
                        warn: Callable[[str], None]) -> ToolManifest:
    """对「启发式判不出副作用」的工具套用 server 级默认值。

    优先级（本函数只负责中间那一层）：
    `overrides`（逐工具显式） > `default_*`（判不出时的默认） > 启发式。
    """
    if default_side_effect is None:
        return manifest
    remote = manifest.name.split("__", 1)[-1]
    if not _heuristic_undecided(remote, manifest.input_schema):
        return manifest
    side = default_side_effect
    tier = default_risk_tier or _SIDE_EFFECT_TIER[side]
    warn(f"工具 {manifest.name!r} 的副作用启发式判不出来，按 server 默认值取 "
         f"{side.value}/{tier.name}（要改就写 overrides）")
    return replace(
        manifest,
        side_effect=side,
        risk_tier=tier,
        idempotent=(side is SideEffect.READ_ONLY),
        required_scope=("tool.read" if side is SideEffect.READ_ONLY else "tool.write"),
    )


def _parse_overrides(raw: Any, source: str, where: str) -> dict[str, tuple[SideEffect, RiskTier]]:
    """`overrides: {tool_name: {side_effect: ..., risk_tier: ...}}` → 给 attach 用的映射。

    值统一翻成 `(SideEffect, RiskTier)` 二元组（`attach_stdio_server` 的
    `policy_overrides` 两种形状都收，这里只用一种，免得下游两种分支都走一遍）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _err(source, where, f"overrides 必须是 mapping，收到 {type(raw).__name__}")
    out: dict[str, tuple[SideEffect, RiskTier]] = {}
    for tool_name, spec in raw.items():
        item = f"{where}.overrides[{tool_name!r}]"
        if not isinstance(tool_name, str) or not tool_name:
            raise _err(source, where, f"overrides 的键必须是非空字符串，收到 {tool_name!r}")
        if not isinstance(spec, Mapping):
            raise _err(source, item,
                       f"覆盖项必须是 mapping（如 {{side_effect: irreversible}}），"
                       f"收到 {type(spec).__name__}")
        _check_keys(spec, frozenset({"side_effect", "risk_tier"}), source, item)
        if "side_effect" not in spec and "risk_tier" not in spec:
            raise _err(source, item, "覆盖项里 side_effect / risk_tier 至少要有一个")
        side = (_parse_side_effect(spec["side_effect"], source, item)
                if "side_effect" in spec else None)
        tier = (_parse_risk_tier(spec["risk_tier"], source, item)
                if "risk_tier" in spec else None)
        if side is None and tier is not None:
            # 只给 tier 时副作用保持启发式结论 —— 和 MCPToolBridge 的分支语义一致
            raise _err(source, item,
                       "只写 risk_tier 不够：副作用才是决定「要不要审批」的那一项，"
                       "请同时写 side_effect")
        assert side is not None
        out[tool_name] = (side, tier if tier is not None else _SIDE_EFFECT_TIER[side])
    return out


def _parse_mcp_server(raw: Any, index: int, *, source: str, base_dir: Path | None,
                      env: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    """解析 `mcp_servers[i]` → 交给 `attach_stdio_server` 的关键字参数 + warnings。"""
    where = f"mcp_servers[{index}]"
    if not isinstance(raw, Mapping):
        raise _err(source, where, f"每一项必须是 mapping，收到 {type(raw).__name__}")
    _check_keys(raw, _MCP_KEYS, source, where)

    name = _parse_str(raw.get("name"), source, where, what="name").strip()
    if not name:
        raise _err(source, where, "name 不能为空")
    where = f"mcp_servers[{index}] (name={name})"
    if not _NAME_RE.match(name):
        raise _err(source, where,
                   f"name={name!r} 含非法字符；只允许字母/数字/下划线/连字符/点")
    if "__" in name:
        # 工具名是 `<server>__<tool>`，server 名里再带 `__` 会让「拆掉前缀还原原始名」
        # 变成歧义操作（overrides 按原始名匹配时就对不上了）
        raise _err(source, where, f"name={name!r} 不能包含 '__'（它是工具名前缀的分隔符）")

    command = _parse_str(raw.get("command"), source, where, what="command").strip()
    if not command:
        raise _err(source, where, "command 不能为空")

    args_raw = raw.get("args") or []
    if not isinstance(args_raw, (list, tuple)):
        raise _err(source, where, f"args 必须是字符串列表，收到 {type(args_raw).__name__}")
    args = [_parse_str(a, source, where, what="args[]") for a in args_raw]

    server_env = _expand_env_map(raw.get("env"), source, where, env)

    cwd: str | None = None
    if raw.get("cwd") is not None:
        cwd_text = _parse_str(raw["cwd"], source, where, what="cwd")
        cwd_text = _expand_env_value(cwd_text, source, f"{where}.cwd", env)
        candidate = Path(cwd_text)
        if not candidate.is_absolute():
            # 相对 cwd 相对**配置文件所在目录**，不是相对启动进程的 cwd ——
            # 否则同一份配置在 systemd 和手动启动下跑出两种行为
            candidate = (base_dir or Path.cwd()) / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise _err(source, where,
                       f"cwd={cwd_text!r} 解析成 {candidate} 后不是目录")
        cwd = str(candidate)

    warnings: list[str] = []
    startup = 20.0
    if raw.get("startup_timeout_s") is not None:
        startup = _parse_float(raw["startup_timeout_s"], source, where,
                               what="startup_timeout_s")
    call_timeout = 30.0
    if raw.get("call_timeout_s") is not None:
        call_timeout = _parse_float(raw["call_timeout_s"], source, where,
                                    what="call_timeout_s")

    default_side: SideEffect | None = None
    if raw.get("default_side_effect") is not None:
        default_side = _parse_side_effect(raw["default_side_effect"], source, where)
    default_tier: RiskTier | None = None
    if raw.get("default_risk_tier") is not None:
        default_tier = _parse_risk_tier(raw["default_risk_tier"], source, where)

    overrides = _parse_overrides(raw.get("overrides"), source, where)

    spec = {
        "server_name": name,
        "command": command,
        "args": args,
        "env": server_env,
        "cwd": cwd,
        "startup_timeout_s": startup,
        "call_timeout_s": call_timeout,
        "policy_overrides": overrides,
        "default_side_effect": default_side,
        "default_risk_tier": default_tier,
    }
    return spec, warnings


def _attach_mcp_server(spec: dict[str, Any], *, source: str, where: str,
                       allow_irreversible: bool,
                       warnings: list[str]) -> tuple[Any, list[ToolManifest],
                                                      dict[str, Callable[[dict, Any], Any]]]:
    """起子进程 → discover → 拿 manifest + handler。失败时自己收掉进程。"""
    from adapters.mcp_transport import MCPTransportError, attach_stdio_server

    # 先在配置层把「声明了 irreversible 但没开闸」挡掉：这时候起子进程纯属浪费，
    # 而且进程起来后才发现策略冲突，错误信息里会混进一堆传输层日志。
    forced = spec.pop("policy_overrides", {})
    if forced and not allow_irreversible:
        bad = [k for k, (side, _tier) in forced.items() if side is SideEffect.IRREVERSIBLE]
        if bad:
            raise _err(
                source, f"{where}.overrides",
                f"把 {bad} 显式声明为 irreversible，但 defaults.allow_irreversible=false（默认）；"
                f"确认要开放就在配置里写 `defaults: {{allow_irreversible: true}}`")

    default_side = spec.pop("default_side_effect", None)
    default_tier = spec.pop("default_risk_tier", None)

    # 用真的 ToolRegistry 承接 attach 的结果，再从里面把 handler 取出来 ——
    # 这样 handler 走的是 `MCPToolBridge.make_handler`（前缀剥离、结果收口、错误包装
    # 都已实现），本模块不重复实现一遍 MCP 调用语义。
    from plane.tool import ToolRegistry

    registry = ToolRegistry()
    try:
        conn, registered = attach_stdio_server(
            registry,
            command=spec["command"],
            args=spec["args"],
            env=spec["env"] or None,
            server_name=spec["server_name"],
            policy_overrides=forced or None,
            cwd=spec["cwd"],
            startup_timeout_s=spec["startup_timeout_s"],
            call_timeout_s=spec["call_timeout_s"],
        )
    except MCPTransportError as exc:
        raise _err(source, where,
                   f"MCP server 起不来或 discover 失败：{exc}") from exc
    except Exception as exc:                     # noqa: BLE001 — 任何失败都要带配置定位
        raise _err(source, where,
                   f"MCP server 启动失败：{type(exc).__name__}: {exc}") from exc

    handlers: dict[str, Callable[[dict, Any], Any]] = {}
    manifests: list[ToolManifest] = []
    override_names = set(forced)
    for manifest in registered:
        entry = registry.get(manifest.name)
        if entry is None:                        # pragma: no cover - attach 的契约
            continue
        handler = entry[1]
        remote = manifest.name.split("__", 1)[-1]
        if remote not in override_names:
            manifest = _apply_mcp_defaults(
                manifest,
                default_side_effect=default_side,
                default_risk_tier=default_tier,
                warn=warnings.append,
            )
        handlers[manifest.name] = handler
        manifests.append(manifest)
    for name in override_names:
        if all(m.name.split("__", 1)[-1] != name for m in manifests):
            # 覆盖项写了一个 server 根本没有的工具：不致命（server 版本升级/降级都会
            # 出现），但必须让人知道这条覆盖**没有生效** —— 以为删数据被标成 T3、
            # 实际还在 T2，这种错觉比报错危险。
            warnings.append(
                f"{where}.overrides[{name!r}]: server 没有这个工具，覆盖未生效")
    return conn, manifests, handlers


# ══════════════════════════════════════════════════════════════
# ⑧ 主解析
# ══════════════════════════════════════════════════════════════
def parse_tools_config(data: dict, *, base_dir: Path | None = None) -> LoadedTools:
    """把**已经解析好的**配置 dict 变成 `LoadedTools`。

    单独暴露这个入口的意义：调用方可能从数据库 / K8s ConfigMap / 测试内存里拿到
    配置，没必要先落盘成 YAML 再读一遍（落盘版本 `load_tools` 内部就是它）。
    `base_dir` 用于解析 MCP server 的相对 `cwd` 与 import 的 sys.path 追加。
    """
    return _parse(data, base_dir=base_dir, source="<config>",
                  env=os.environ)


def _parse(data: Any, *, base_dir: Path | None, source: str,
           env: Mapping[str, str]) -> LoadedTools:
    if not isinstance(data, Mapping):
        raise _err(source, "<root>",
                   f"配置根必须是 mapping（YAML 顶层是字典），收到 {type(data).__name__}")

    _check_keys(data, _TOP_KEYS, source, "<root>")

    # ── version ────────────────────────────────────────────────
    # 缺 version = **报错**（不是取默认值 1）。理由：没有版本号，未来 v2 改了字段
    # 语义（比如 risk_tier 换成名字）时，旧 loader 会把它当 v1 静默解析 —— 一个
    # 语义被静默解释错的配置，比一个拒绝启动的配置危险得多。要求显式写死版本，
    # 等于给「这份配置按哪套规则读」留下不可误读的凭据。
    if "version" not in data:
        raise _err(source, "<root>",
                   "缺少 version 字段；请显式写 `version: 1`"
                   "（没有版本号的配置无法判断字段语义，本模块拒绝猜）")
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SUPPORTED_VERSION:
        raise _err(source, "version",
                   f"不支持的配置版本 {version!r}；本模块只支持 version {SUPPORTED_VERSION}")

    # ── defaults ───────────────────────────────────────────────
    defaults_raw = data.get("defaults") or {}
    if not isinstance(defaults_raw, Mapping):
        raise _err(source, "defaults",
                   f"defaults 必须是 mapping，收到 {type(defaults_raw).__name__}")
    _check_keys(defaults_raw, _DEFAULT_KEYS, source, "defaults")
    # **默认 false**：见 `_check_irreversible` 的三条理由。一句话：不可逆能力
    # 必须是运维显式签过字的东西，不能是「没特别写就是开」。
    allow_irreversible = False
    if "allow_irreversible" in defaults_raw:
        allow_irreversible = _parse_bool(defaults_raw["allow_irreversible"], source,
                                         "defaults", what="allow_irreversible")
    max_tools = DEFAULT_MAX_TOOLS
    if "max_tools" in defaults_raw:
        max_tools = _parse_int(defaults_raw["max_tools"], source, "defaults",
                              minimum=0, what="max_tools")

    warnings: list[str] = []
    manifests: list[ToolManifest] = []
    handlers: dict[str, Callable[[dict, Any], Any]] = {}
    connections: list[Any] = []

    tools_raw = data.get("tools") or []
    if not isinstance(tools_raw, (list, tuple)):
        raise _err(source, "tools", f"tools 必须是列表，收到 {type(tools_raw).__name__}")

    # import 前先把配置目录追加进 sys.path（**追加**，不是插入）：
    # 运维把 tools.yaml 和 myapp/ 放同一个目录时，`import: myapp.tools:f` 就能直接用，
    # 不必再去配 PYTHONPATH。追加到末尾是为了不遮蔽已安装的模块（插入会改变
    # 已有 import 的解析结果，那是比便利重要得多的风险）。
    if base_dir is not None:
        _ensure_sys_path(base_dir)

    try:
        for index, raw in enumerate(tools_raw):
            manifest, handler, item_warnings = _parse_tool_item(
                raw, index, source=source)
            warnings.extend(item_warnings)
            _check_duplicate(manifest.name, manifests, source, f"tools[{index}]")
            manifests.append(manifest)
            handlers[manifest.name] = handler

        # 早退检查：内置工具已经超限时不必再花几秒起 MCP 子进程
        _check_max_tools(len(manifests), max_tools, source, phase="内置工具")

        mcp_raw = data.get("mcp_servers") or []
        if not isinstance(mcp_raw, (list, tuple)):
            raise _err(source, "mcp_servers",
                       f"mcp_servers 必须是列表，收到 {type(mcp_raw).__name__}")

        for index, raw in enumerate(mcp_raw):
            spec, item_warnings = _parse_mcp_server(
                raw, index, source=source, base_dir=base_dir, env=env)
            warnings.extend(item_warnings)
            where = f"mcp_servers[{index}] (name={spec['server_name']})"
            conn, mcp_manifests, mcp_handlers = _attach_mcp_server(
                spec, source=source, where=where,
                allow_irreversible=allow_irreversible, warnings=warnings)
            # 进程已经起来了：从这里开始的任何错误都必须先收掉它，否则配置写错
            # 一次就在机器上留一个孤儿 server（CI 里越跑越多，排查成本极高）
            connections.append(conn)
            for manifest in mcp_manifests:
                _check_duplicate(manifest.name, manifests, source, where)
                manifests.append(manifest)
                handlers[manifest.name] = mcp_handlers[manifest.name]

        _check_max_tools(len(manifests), max_tools, source, phase="全部工具")
        _check_irreversible(manifests, allow=allow_irreversible, source=source)
    except BaseException:
        # 任何失败的路径都要把已起的子进程收干净再抛（`StdioMCPConnection.stop()`
        # 幂等，重复收拾无害）
        for conn in connections:
            for method in ("stop", "close_stderr"):
                fn = getattr(conn, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:            # noqa: BLE001
                        pass
        raise

    return LoadedTools(
        manifests=tuple(manifests),
        handlers=handlers,
        connections=tuple(connections),
        warnings=tuple(warnings),
    )


def _ensure_sys_path(base_dir: Path) -> None:
    text = str(base_dir)
    if text not in sys.path:
        sys.path.append(text)


def _check_duplicate(name: str, existing: Sequence[ToolManifest],
                     source: str, where: str) -> None:
    """同名工具 → 报错（含名字）。

    为什么不是「后者覆盖前者」：`ToolRegistry.register` 对同名是硬拒绝的
    （静默覆盖会连权限声明一起换掉）。加载期先拦住，报错里能说清是哪个
    server / 哪一项撞了名；等到注册期才炸，就只剩一句「tool 已注册」。
    """
    if any(m.name == name for m in existing):
        raise _err(source, where,
                   f"工具名 {name!r} 重复；工具名是注册与审计的唯一键，不允许同名")


def _check_max_tools(count: int, max_tools: int, source: str, *, phase: str) -> None:
    """工具数超限 → 报错。

    为什么是报错而不是截断：截断会让「配了但没生效」变成一个无声的事实
    （`registry.manifests()` 少几个，模型就是看不见它们）。上限是**容量判断**，
    应由人调整配置或调 `max_tools`，不该由加载器替人猜要丢哪一个。
    """
    if count > max_tools:
        raise _err(source, "defaults.max_tools",
                   f"{phase}共 {count} 个，超过上限 max_tools={max_tools}；"
                   f"删掉不用的工具，或在 defaults 里调大 max_tools")


# ══════════════════════════════════════════════════════════════
# ⑨ 文件 / 环境变量入口
# ══════════════════════════════════════════════════════════════
def load_tools(path: str | Path, *, cwd: str | Path | None = None) -> LoadedTools:
    """读一个 YAML 文件并加载其中的工具。

    `path` 可以是相对路径，相对 `cwd`（默认当前工作目录）解析 —— 服务进程的
    cwd 和「运维敲命令时的 cwd」经常不是同一个，所以 `cwd` 必须能显式传。
    配置里的相对 `cwd`（MCP server 的）相对**配置文件所在目录**。
    """
    if yaml is None:                             # pragma: no cover - 取决于环境
        raise ToolConfigError(
            "加载 YAML 工具配置需要 pyyaml，但当前环境导入不到它；"
            "装：.venv/bin/pip install 'pyyaml>=6.0'（或改成 requirements.txt 里的依赖）")

    raw_path = Path(os.fspath(path))
    base = Path(cwd).resolve() if cwd is not None else Path.cwd()
    resolved = raw_path if raw_path.is_absolute() else (base / raw_path)
    resolved = resolved.resolve()
    source = str(resolved)

    if not resolved.exists():
        raise ToolConfigError(
            f"{source}: 配置文件不存在"
            f"（相对路径按 cwd={str(base)!r} 解析；检查 AGENT_RUNTIME_TOOLS 或部署挂载）")
    if not resolved.is_file():
        raise ToolConfigError(f"{source}: 不是文件（是个目录？）")

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolConfigError(f"{source}: 读取失败：{type(exc).__name__}: {exc}") from exc

    try:
        # safe_load：工具配置是**运维写的数据**，不是可执行代码。
        # 用 full_load/load 会让 `!!python/object` 这类标签直接把配置变成 RCE 入口。
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # 语法错必须带行号：YAML 的错因（缩进、冒号、tab）离开行号基本无法人工定位
        mark = getattr(exc, "problem_mark", None)
        where = (f"第 {mark.line + 1} 行第 {mark.column + 1} 列"
                 if mark is not None else "行号未知")
        problem = getattr(exc, "problem", None) or str(exc)
        raise ToolConfigError(f"{source}: YAML 语法错误（{where}）：{problem}") from exc

    if data is None:
        # 空文件 = 空配置。语义上等价于 `version: 1\ntools: []`，
        # 但没有 version 时按上面那条规则是报错 —— 空文件也走同一条规则，
        # 免得「空文件能过、少了 version 的文件不能过」这种自相矛盾。
        raise ToolConfigError(
            f"{source}: 配置是空的；至少写 `version: 1`（没有工具也要有版本号）")

    return _parse(data, base_dir=resolved.parent, source=source, env=os.environ)


def load_tools_from_env(env: Mapping[str, str] | None = None) -> LoadedTools:
    """从环境变量 `AGENT_RUNTIME_TOOLS` 指定的路径加载工具配置。

    未设置（或空串）→ **空工具集**，不报错：这是「这个进程不接工具」的合法部署
    （比如只做对话的实例），把它变成启动失败是拿运维当敌人。
    设了但文件坏了 → 报错（这一点和「未设置」必须区分开）。

    `env` 参数是为了测试注入：默认读 `os.environ`，传入映射则完全按它来
    （不合并 —— 半合并的环境最难复现）。
    """
    source_env = os.environ if env is None else env
    raw = source_env.get(ENV_VAR, "")
    if raw is None or str(raw).strip() == "":
        return LoadedTools()
    return load_tools(str(raw).strip(), cwd=Path.cwd())
