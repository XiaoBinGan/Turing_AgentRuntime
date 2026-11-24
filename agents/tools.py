"""agents/tools.py — 运维域业务工具（8 个）。

Runtime 的 Tool 面是**空的**：`serve.py` 默认 `tools=[]`，`Dispatcher` 只管
权限 / 审批 / 归一化，不管「这个进程能干什么」。这个文件就是**能干什么**。

分工（不要混）
==============

    ToolManifest   名字 + 副作用 + 风险档 + 入参契约   ← 本文件声明
    handler        fn(args, ctx) -> Any                ← 本文件实现
    Dispatcher     权限 / 审批 / 幂等 / 限流 / 脱敏      ← Runtime 负责，别在这里重写

所以这里**不做**三件事：不查权限（Dispatcher 第 ③ 步）、不缓存（第 ⑥ 步）、
不截断结果（第 ⑪ 步 Normalizer）。唯一一处「工具自己动手」的策略性处理是
`get_config` 的脱敏 —— 理由见 `mask_secrets` 的 docstring。

副作用定级（**这一列是本文件最重要的内容**，不许为了"跑得顺"降级）
==================================================================

    query_metrics        READ_ONLY    T0   纯查
    query_logs           READ_ONLY    T0   纯查
    list_deployments     READ_ONLY    T0   纯查
    get_config           READ_ONLY    T0   纯查（出站脱敏）
    restart_service      MUTATING     T2   改运行时状态，可重试、可再重启
    scale_service        MUTATING     T2   改副本数，可回改
    rollback_deployment  IRREVERSIBLE T3   版本回退，schema 迁移与已写数据不回来
    delete_namespace     IRREVERSIBLE T3   删命名空间，PV/PVC/备份一并消失

两条判据（与 `docs/TOOLS.md` §4.2 一致）：

  * T0 / T1 的分界是「要不要打扰人」→ 只读工具都不打扰人，所以全是 T0
    （它们的数据量小，没有 T1 那种「dump 全量日志」的体量）。
  * T2 / T3 的分界是「会不会造成无法回滚的损失」→ 重启与扩缩容都能再点一次
    改回来（所以 T2，进审批队列）；回滚与删命名空间**回不去**（所以 T3，
    Dispatcher 的语义是**硬拒**，连审批都不给）。

`idempotent` 与副作用分开填：`restart_service` 是 MUTATING 但**不**幂等 ——
同参数第二次调用不能命中幂等缓存，否则「第二次重启」会被缓存成「看起来成功」。

数据来源
========

全部来自 `agents/fake_backend.py` 的内存假集群，不连真网络（不 import
`requests` / `socket` / `urllib`）。要让工具打真后端，只换 `build_handlers()`
收到的 backend 对象，manifest 一行不用改。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

# 允许从任意工作目录 import（demo/run.py、toolconfig 的 __import__ 都可能换 cwd）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import ExecCtx, RiskTier, SideEffect, ToolManifest  # noqa: E402

from agents.fake_backend import (  # noqa: E402
    BACKEND,
    METRIC_NAMES,
    SERVICE_NAMES,
    FakeOpsBackend,
)

__all__ = [
    "MANIFESTS",
    "MASK",
    "get_config",
    "handlers",
    "list_deployments",
    "mask_secrets",
    "query_logs",
    "query_metrics",
    "register_tools",
    "restart_service",
    "rollback_deployment",
    "scale_service",
    "delete_namespace",
    "tool_names",
]

# ══════════════════════════════════════════════════════════════
# 脱敏（`get_config` 唯一的出站策略）
# ══════════════════════════════════════════════════════════════
#: 打码占位符。**刻意不用** Normalizer 的 `***REDACTED***`：
#: 两个占位符不同，用例才能证明「是工具自己脱的敏」，而不是 Dispatcher 的
#: Normalizer 顺手兜了一把（Normalizer 的敏感键正则里**没有**裸 `key`，
#: 所以 `signing_key` 这类字段只有本文件会挡住 —— 这条差异有用例守着）。
MASK = "***MASKED***"

#: 敏感键名：命中就走替换。
#: 用「包含」而不是「等于」：真实配置里叫 `db_password` / `api_token` /
#: `signing_key` / `bank_client_secret`，没有一个会精确等于 `password`。
#: 代价是 `monkey` 这类键也会被打码 —— **宁可多打**：多打一次是「看不到一个
#: 无害值」，漏打一次是「密钥进了 Prompt 与日志」。
_SENSITIVE_KEY = re.compile(
    r"(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|authorization|salt|cert|dsn|conn(?:ection)?[_-]?str)",
    re.IGNORECASE,
)

#: 裸 `key` 也打（`signing_key` / `encryption_key`）。单独一条是因为把它并进
#: 上面那条会把 `keyboard`、`monkey` 之外的东西也带走，分开写意图更清楚。
_BARE_KEY = re.compile(r"(?i)(?:^|[_.-])key(?:$|[_.-])|_key$|^key")

#: 值里的密钥形态（与 Normalizer 的口径**刻意重叠但独立**：工具层出站前拦一道，
#: Normalizer 是最后一道保险，两道都在才叫纵深）。
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN[^-]{0,60}PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # DSN 里的 userinfo 口令：scheme://user:password@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^@\s/]+@"),
)

#: `mask_secrets` 的稳定说明（进返回值，让模型知道「缺的值不是缺，是被挡住了」）。
_MASK_NOTE = "含 password/token/secret/key/credential 等敏感词的字段已打码"

#: 每个工具 description 末尾的统一「什么时候该停」句。
#:
#: 为什么这条要进**每个**工具的 description 而不是只写进 system 人格：
#: 模型决策时看的是**当前这一步要调的那个工具的 schema**，不是每次重读 system。
#: 实测 deepseek-flash 连发 18 次 `query_metrics`（多次参数完全一致）时，
#: 每一步它"眼前"只有工具清单 —— 停止条件放在那里才起作用。
STOP_HINT = (
    "何时该停：这一步的读数已经能支撑判断就停手写结论，"
    "不要用同一组参数重复调用（重复调用会被合并，不会带回新信息）。"
)


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key) or _BARE_KEY.search(key))


def _mask_text(text: str) -> str:
    """把字符串里的密钥形态替掉（私钥整块 / sk- / AKIA / DSN userinfo）。"""
    out = text
    for pattern in _VALUE_PATTERNS:
        if pattern.groups:
            out = pattern.sub(r"\1:" + MASK + "@", out)
        else:
            out = pattern.sub(MASK, out)
    return out


def mask_secrets(node: Any, *, path: str = "") -> tuple[Any, list[str]]:
    """递归脱敏：返回 `(脱敏后的结构, 命中的字段路径列表)`。

    为什么脱敏在**工具层**而不只靠 Normalizer：

      * Normalizer 的敏感键正则里没有裸 `key` —— `signing_key` 会原样进 Prompt；
      * Normalizer 只在结果**序列化之后**按形态匹配，而「哪个字段叫 password」
        这种结构信息在工具层最清楚（`db.password` 这种嵌套它也要走到）；
      * 出站通道不止一个：工具结果会进 Prompt、进 trace、进 artifacts。
        把策略放在最靠近数据的一层，多一个通道就自动被覆盖。

    保留结构（键还在、值被替换）而不是直接删字段：模型需要知道
    「这个配置**有** db_password，只是看不到」，否则它会去猜「是不是没配」。
    """
    hits: list[str] = []

    def walk(value: Any, here: str) -> Any:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                child = f"{here}.{name}" if here else name
                if _is_sensitive_key(name):
                    # 整棵子树打成标量：list/dict 里的每个元素都是同一类秘密
                    out[name] = MASK
                    hits.append(child)
                else:
                    out[name] = walk(item, child)
            return out
        if isinstance(value, (list, tuple)):
            return [walk(item, f"{here}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, str):
            masked = _mask_text(value)
            if masked != value:
                hits.append(here or "<value>")
            return masked
        return value

    return walk(node, path), hits


# ══════════════════════════════════════════════════════════════
# 入参契约（JSON Schema 子集，见 contracts.ToolManifest）
# ══════════════════════════════════════════════════════════════
_SCHEMA_METRICS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {
            "type": "string", "enum": list(SERVICE_NAMES),
            "description": "单个服务。与 services 二选一",
        },
        "services": {
            "type": "array", "items": {"type": "string", "enum": list(SERVICE_NAMES)},
            "minItems": 1, "maxItems": len(SERVICE_NAMES),
            "description": "多个服务一次查（对比用）。与 service 二选一",
        },
        "metric": {
            "type": "string", "enum": list(METRIC_NAMES),
            "description": "单个指标。与 metrics 二选一",
        },
        "metrics": {
            "type": "array", "items": {"type": "string", "enum": list(METRIC_NAMES)},
            "minItems": 1, "maxItems": len(METRIC_NAMES),
            "description": "多个指标一次查（批量取证用）",
        },
        "window_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
    },
    # **二选一用 anyOf 表达**，而不是"都不要求 + handler 抛异常"：
    # 前者在**校验层**就拒掉并回 "invalid args"（模型能照着改参数重来），
    # 后者要等 handler 执行才炸，错误形状也不一致（ValueError 而不是 ToolOutcome）。
    #
    # 早期要求 required=["service","metric"]，逼出"一次一个指标"的用法 ——
    # 真模型要做 3 服务 × 5 指标对比就得发 15 次调用，实测 18 次只查指标、
    # 直接把 max_steps 和工具预算耗光，最后没产出结论。
    "anyOf": [{"required": ["service"]}, {"required": ["services"]}],
    "allOf": [{"anyOf": [{"required": ["metric"]}, {"required": ["metrics"]}]}],
    "additionalProperties": False,
}

_SCHEMA_LOGS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {
            "type": "string", "enum": list(SERVICE_NAMES),
            "description": "单个服务。与 services 二选一",
        },
        "services": {
            "type": "array", "items": {"type": "string", "enum": list(SERVICE_NAMES)},
            "minItems": 1, "maxItems": len(SERVICE_NAMES),
            "description": "多个服务一次查（对比同一时间窗的错误行）。与 service 二选一",
        },
        "window_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
        "level": {"type": "string", "enum": ["DEBUG", "INFO", "WARN", "ERROR"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    # 与 `_SCHEMA_METRICS` 同一个理由：二选一用 anyOf 在**校验层**表达，
    # 否则「只给 services」会被 required=["service"] 拒掉，模型只能逐个服务发。
    "anyOf": [{"required": ["service"]}, {"required": ["services"]}],
    "additionalProperties": False,
}

_SCHEMA_DEPLOYMENTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": list(SERVICE_NAMES)},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": [],
    "additionalProperties": False,
}

_SCHEMA_CONFIG: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {
            "type": "string", "enum": list(SERVICE_NAMES),
            "description": "单个服务。与 services 二选一",
        },
        "services": {
            "type": "array", "items": {"type": "string", "enum": list(SERVICE_NAMES)},
            "minItems": 1, "maxItems": len(SERVICE_NAMES),
            "description": "多个服务一次查（核对同一批参数是否一致）。与 service 二选一",
        },
        "environment": {"type": "string", "enum": ["prod", "staging"]},
    },
    "anyOf": [{"required": ["service"]}, {"required": ["services"]}],
    "additionalProperties": False,
}

_SCHEMA_RESTART: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": list(SERVICE_NAMES)},
        "reason": {"type": "string", "maxLength": 200},
    },
    "required": ["service"],
    "additionalProperties": False,
}

_SCHEMA_SCALE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": list(SERVICE_NAMES)},
        "replicas": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "required": ["service", "replicas"],
    "additionalProperties": False,
}

_SCHEMA_ROLLBACK: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "enum": list(SERVICE_NAMES)},
        "to_version": {"type": "string", "maxLength": 32},
    },
    "required": ["service"],
    "additionalProperties": False,
}

_SCHEMA_DELETE_NS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    "required": ["namespace"],
    "additionalProperties": False,
}


# ══════════════════════════════════════════════════════════════
# 工具实现（`fn(args, ctx)`；ctx 用不到但要接住，见 toolconfig 的 arity 自适应）
# ══════════════════════════════════════════════════════════════
def query_metrics(args: dict, ctx: ExecCtx | None = None, *,
                  backend: FakeOpsBackend = BACKEND) -> dict:
    """查指标曲线 + 统计（只读）。**支持批量**。

    为什么支持批量（这是真模型跑出来的教训）：
      早期一次只查「一个服务的一个指标」。真模型要判断"是不是只有 api 劣化"，
      就得对比 3 个服务 × 5 个指标 = 15 次调用。实测它一轮发了 **18 次**
      `query_metrics`，把 `max_steps` 和工具预算全耗光，最后**没有产出结论**。
      工具粒度太细 = 逼模型做 N 次往返，而每次往返都烧 token。

    批量后：`services=["api","payments"], metrics=["p99","error_rate"]`
    一次拿 4 条曲线，语义完全一样，成本 1/4。

    参数二选一（都给了就用复数形式，单个字段被忽略）：
      * `service`  / `services`
      * `metric`   / `metrics`
    """
    services = args.get("services")
    if not services:
        single = args.get("service")
        if not single:
            raise ValueError("必须给 service 或 services")
        services = [single]
    metrics = args.get("metrics")
    if not metrics:
        single_m = args.get("metric")
        if not single_m:
            raise ValueError("必须给 metric 或 metrics")
        metrics = [single_m]

    window = int(args.get("window_minutes", 30))
    services = [str(x) for x in services]
    metrics = [str(x) for x in metrics]

    # 单个服务 + 单个指标：保持**原返回形状**（老调用方/用例不受影响）
    if len(services) == 1 and len(metrics) == 1:
        return backend.metric(services[0], metrics[0], window)

    out: dict[str, Any] = {"window_minutes": window, "results": []}
    for svc in services:
        for m in metrics:
            try:
                out["results"].append({"service": svc, "metric": m,
                                       "data": backend.metric(svc, m, window)})
            except Exception as exc:                           # noqa: BLE001
                # 批量里某一条没数据不该让整批失败 —— 记下来让模型自己判断
                out["results"].append({"service": svc, "metric": m,
                                       "error": f"{type(exc).__name__}: {exc}"})
    out["ok_count"] = sum(1 for r in out["results"] if "data" in r)
    out["error_count"] = len(out["results"]) - out["ok_count"]
    return out


def _services_arg(args: Mapping) -> list[str]:
    """把 ``service`` / ``services`` 归一成列表（批量工具共用）。

    与 `query_metrics` 同一口径：都给了就用复数形式；一个都没给就抛
    ``ValueError``（由 Dispatcher 转成 retryable 的 failed，模型能照着改参数）。
    """
    services = args.get("services")
    if services:
        return [str(x) for x in services]
    single = args.get("service")
    if not single:
        raise ValueError("必须给 service 或 services")
    return [str(single)]


def _batch_result(rows: list[dict], **extra: Any) -> dict:
    """批量返回的公共外壳：``{results, ok_count, error_count, **extra}``。

    ``ok_count`` / ``error_count`` 是给模型看的**一眼结论**：它不必数一遍
    ``results`` 就知道"这次批量里有没有失败"，也就不会为了确认而重发一次。
    """
    out: dict[str, Any] = dict(extra)
    out["results"] = rows
    out["ok_count"] = sum(1 for r in rows if "data" in r)
    out["error_count"] = len(rows) - out["ok_count"]
    return out


def query_logs(args: dict, ctx: ExecCtx | None = None, *,
               backend: FakeOpsBackend = BACKEND) -> dict:
    """按服务 + 时间窗 + 级别查日志（只读，新→旧）。**支持批量**。

    与 `query_metrics` 同一个教训：一次只查一个服务时，"三个服务在同一个窗口里
    各自报了什么错"要发三次调用，而模型往往只需要扫一眼哪家没有错误行。
    批量后 `services=["api","payments"]` 一次拿完。

    返回形状（**向后兼容**）：
      * 单查（``service``，或长度为 1 的 ``services``）→ 与老版本**逐字段相同**
        （``service`` / ``entries`` / ``total_matched`` …），老调用方不用改；
      * 批量 → ``{window_minutes, level, results:[{service, data}|{service, error}],
        ok_count, error_count}``。
    """
    services = _services_arg(args)
    window = int(args.get("window_minutes", 60))
    level = str(args.get("level", ""))
    limit = int(args.get("limit", 20))

    if len(services) == 1:
        return backend.logs(services[0], window, level, limit)

    rows: list[dict] = []
    for svc in services:
        try:
            rows.append({"service": svc,
                         "data": backend.logs(svc, window, level, limit)})
        except Exception as exc:                               # noqa: BLE001
            # 批量里某一个服务查不到，不该让整批失败：其余服务的证据仍然有效
            rows.append({"service": svc, "error": f"{type(exc).__name__}: {exc}"})
    return _batch_result(rows, window_minutes=window,
                         level=level.strip().upper() or "ALL")


def list_deployments(args: dict, ctx: ExecCtx | None = None, *,
                    backend: FakeOpsBackend = BACKEND) -> dict:
    """查最近发布（只读）。"""
    return backend.deployments(str(args.get("service", "")), int(args.get("limit", 10)))


def _masked_config(backend: FakeOpsBackend, service: str, environment: str) -> dict:
    """单个服务的配置（脱敏后的出站形状）。批量与单查共用，保证两条路径同形。"""
    raw = backend.config(service, environment)
    masked_values, hits = mask_secrets(raw["values"])
    return {
        "service": raw["service"],
        "environment": raw["environment"],
        "revision": raw["revision"],
        "values": masked_values,
        "masked_fields": hits,
        "masked_total": len(hits),
        "masking": _MASK_NOTE,
    }


def get_config(args: dict, ctx: ExecCtx | None = None, *,
               backend: FakeOpsBackend = BACKEND) -> dict:
    """查配置项，**敏感字段打码后才出站**。**支持批量**。

    批量场景：核对 ``pool_max`` / ``timeout`` 这类参数在多个服务上是否一致。
    早期只能一个一个查，模型为了横向对比会连发 N 次，每次都带回一份基本重复的
    打码配置。批量后一次拿全；返回的 ``data`` 就是单查那个对象，模型不必学两套
    解析方式。

    返回形状（**向后兼容**）：
      * 单查 → 老形状（``service`` / ``values`` / ``masked_fields`` …）；
      * 批量 → ``{environment, results:[{service, data}|{service, error}],
        ok_count, error_count}``。
    """
    services = _services_arg(args)
    environment = str(args.get("environment", "prod"))

    if len(services) == 1:
        return _masked_config(backend, services[0], environment)

    rows: list[dict] = []
    for svc in services:
        try:
            rows.append({"service": svc,
                         "data": _masked_config(backend, svc, environment)})
        except Exception as exc:                               # noqa: BLE001
            rows.append({"service": svc, "error": f"{type(exc).__name__}: {exc}"})
    return _batch_result(rows, environment=environment)


def restart_service(args: dict, ctx: ExecCtx | None = None, *,
                    backend: FakeOpsBackend = BACKEND) -> dict:
    """重启服务（MUTATING / T2：要人批）。"""
    return backend.restart(args["service"], str(args.get("reason", "")))


def scale_service(args: dict, ctx: ExecCtx | None = None, *,
                  backend: FakeOpsBackend = BACKEND) -> dict:
    """改副本数（MUTATING / T2：要人批）。"""
    return backend.scale(args["service"], int(args["replicas"]))


def rollback_deployment(args: dict, ctx: ExecCtx | None = None, *,
                         backend: FakeOpsBackend = BACKEND) -> dict:
    """回滚发布（IRREVERSIBLE / T3：Dispatcher 硬拒，本函数**永远走不到**）。

    保留实现不是「留个后门」：T3 的拦截发生在 Dispatcher 第 ⑤ 步，比任何
    handler 都早。这里的代码只有在「人工专线」（比如运维直接调后端）时才有意义，
    而那条路不经过 Agent。写全它，是为了让 T3 的语义（不可逆**动作**）有个
    诚实的实现对照，而不是一个 `raise NotImplementedError` 的空壳。
    """
    return backend.rollback(args["service"], str(args.get("to_version", "")))


def delete_namespace(args: dict, ctx: ExecCtx | None = None, *,
                     backend: FakeOpsBackend = BACKEND) -> dict:
    """删命名空间（IRREVERSIBLE / T3：Dispatcher 硬拒）。

    演示「危险操作在 Runtime 里是什么样子」：它不是待确认，是**拒绝** ——
    因为审批语义是「批准一次可回滚的动作」，而删命名空间没有「批准」这个档。
    """
    return backend.delete_namespace(args["namespace"])


# ══════════════════════════════════════════════════════════════
# manifest 声明（Builder 的工具描述、Dispatcher 的策略都读这里）
# ══════════════════════════════════════════════════════════════
MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest(
        name="query_metrics",
        side_effect=SideEffect.READ_ONLY,
        risk_tier=RiskTier.T0_SAFE,
        idempotent=True,
        timeout_ms=3000,
        required_scope="tool.read",
        per_turn_limit=20,
        max_result_bytes=32768,
        description=("查服务在时间窗内的指标曲线（p99 / error_rate / qps / cpu / memory），"
                     "返回 latest/avg/min/max 与窗口内前后半段的 delta_ratio。"
                     "**支持批量**：用 services=[...] + metrics=[...] 一次拿多条，"
                     "要做多服务/多指标对比时**务必一次查完**，不要逐个发。"
                     "判断「是不是劣化了」「涨了多少」时先用它。"
                     + " " + STOP_HINT),
        input_schema=_SCHEMA_METRICS,
    ),
    ToolManifest(
        name="query_logs",
        side_effect=SideEffect.READ_ONLY,
        risk_tier=RiskTier.T0_SAFE,
        idempotent=True,
        timeout_ms=3000,
        required_scope="tool.read",
        per_turn_limit=20,
        max_result_bytes=32768,
        description=("按服务 + 时间窗 + 级别查日志（新→旧）。指标说「劣化了」之后，"
                     "用它找具体错误行（如连接池耗尽、超时、上游 5xx）。"
                     "**支持批量**：services=[...] 一次扫多个服务（例如确认"
                     "「只有 api 在报错」），返回 {results, ok_count, error_count}。"
                     + " " + STOP_HINT),
        input_schema=_SCHEMA_LOGS,
    ),
    ToolManifest(
        name="list_deployments",
        side_effect=SideEffect.READ_ONLY,
        risk_tier=RiskTier.T0_SAFE,
        idempotent=True,
        timeout_ms=3000,
        required_scope="tool.read",
        per_turn_limit=10,
        description=("查最近发布（版本 / 时间 / 作者 / 变更摘要 / 是否可疑）。"
                     "判断「是不是发布引起的」时用它把时间线对齐；不传 service "
                     "就是全部服务的最近 N 条，对齐时间线不必逐个服务查。"
                     + " " + STOP_HINT),
        input_schema=_SCHEMA_DEPLOYMENTS,
    ),
    ToolManifest(
        name="get_config",
        side_effect=SideEffect.READ_ONLY,
        risk_tier=RiskTier.T0_SAFE,
        idempotent=True,
        timeout_ms=3000,
        required_scope="tool.read",
        per_turn_limit=10,
        description=("查服务在某个环境的配置项（结构保留、敏感字段打码）。"
                     "核对 pool_max / timeout 这类参数是否与预期一致时用它。"
                     "**支持批量**：services=[...] 一次核对多个服务，"
                     "返回 {results, ok_count, error_count}。"
                     + " " + STOP_HINT),
        input_schema=_SCHEMA_CONFIG,
    ),
    ToolManifest(
        name="restart_service",
        side_effect=SideEffect.MUTATING,
        risk_tier=RiskTier.T2_HIGH,
        # 不幂等：同参数第二次调用必须真的再重启一次，不能被幂等缓存吃掉
        idempotent=False,
        timeout_ms=10000,
        required_scope="tool.write",
        per_turn_limit=3,
        description=("重启一个服务（滚动重启全部副本）。**会丢掉进程内现场**，"
                     "排障时应在取证之后再考虑。需要人工审批，无审批只会得到待确认。"
                     "何时该停：取证没做完之前不要调它；已经定位到原因、且有回滚路径时"
                     "才交给审批，不要拿重启当「再试一次」。"),
        input_schema=_SCHEMA_RESTART,
    ),
    ToolManifest(
        name="scale_service",
        side_effect=SideEffect.MUTATING,
        risk_tier=RiskTier.T2_HIGH,
        idempotent=False,
        timeout_ms=10000,
        required_scope="tool.write",
        per_turn_limit=3,
        description=("把服务副本数改成 replicas（扩容/缩容）。改错了可以再改回来，"
                     "所以是 T2 待审批而不是硬拒。需要人工审批。"
                     "何时该停：水位与增速算清楚、目标副本数说得出依据时再提审批；"
                     "不要用反复扩容试探容量上限。"),
        input_schema=_SCHEMA_SCALE,
    ),
    ToolManifest(
        name="rollback_deployment",
        side_effect=SideEffect.IRREVERSIBLE,
        risk_tier=RiskTier.T3_CRITICAL,
        idempotent=False,
        timeout_ms=15000,
        required_scope="tool.write",
        per_turn_limit=1,
        description=("把服务回滚到上一个（或指定）版本。**schema 迁移与已写入的数据不会"
                     "随回滚恢复**，因此这是 T3：Runtime 直接拒绝，不接受审批确认。"
                     "何时该停：调用它之前就要停 —— 这一步不属于 Agent 的自助动作，"
                     "定位完成后把结论交给人即可，不要尝试绕过拒绝。"),
        input_schema=_SCHEMA_ROLLBACK,
    ),
    ToolManifest(
        name="delete_namespace",
        side_effect=SideEffect.IRREVERSIBLE,
        risk_tier=RiskTier.T3_CRITICAL,
        idempotent=False,
        timeout_ms=15000,
        required_scope="tool.write",
        per_turn_limit=1,
        description=("删除一个命名空间（含其下所有工作负载、配置与 PVC）。**不可恢复**，"
                     "T3：Runtime 直接拒绝，不接受审批确认。"
                     "何时该停：这里没有「何时该调」 —— 它是不可恢复动作，"
                     "Agent 侧永不自行发起，只输出建议与人确认点。"),
        input_schema=_SCHEMA_DELETE_NS,
    ),
)


#: 名字 → 实现。键必须与 `MANIFESTS` 一一对应（用例守着）。
_IMPLS: dict[str, Callable[[dict, ExecCtx | None], Any]] = {
    "query_metrics": query_metrics,
    "query_logs": query_logs,
    "list_deployments": list_deployments,
    "get_config": get_config,
    "restart_service": restart_service,
    "scale_service": scale_service,
    "rollback_deployment": rollback_deployment,
    "delete_namespace": delete_namespace,
}


def tool_names() -> list[str]:
    """已声明的工具名（与 `MANIFESTS` 同序）。"""
    return [m.name for m in MANIFESTS]


def handlers() -> dict[str, Callable[[dict, ExecCtx | None], Any]]:
    """`name -> fn` 的副本（调用方改它不影响本模块）。"""
    return dict(_IMPLS)


def register_tools(registry: Any) -> list[ToolManifest]:
    """把 8 个工具注册进 `plane.tool.ToolRegistry`（或任何有 `register` 的对象）。

    不做 `try/except` 吞错：同名注册在 Runtime 里是硬错误（静默覆盖会连权限
    声明一起换掉），吞掉它等于让「谁覆盖了谁」变成不可查的事。
    """
    for manifest in MANIFESTS:
        registry.register(manifest, _IMPLS[manifest.name])
    return list(MANIFESTS)


def build_handlers(backend: FakeOpsBackend) -> dict[str, Callable[[dict, ExecCtx | None], Any]]:
    """给**指定后端实例**造一组 handler（演示/用例要隔离状态时用）。

    存在的意义：模块级 `_IMPLS` 都闭包在进程级 `BACKEND` 上，两个用例
    「一个改状态、一个读状态」会互相污染。这里返回的 handler 用 `functools.partial`
    把 backend 绑死，签名仍是 `(args)` / `(args, ctx)`，Dispatcher 的 arity
    自适应照样认得。
    """
    from functools import partial

    impls: dict[str, Callable[[dict, ExecCtx | None], Any]] = {
        "query_metrics": partial(query_metrics, backend=backend),
        "query_logs": partial(query_logs, backend=backend),
        "list_deployments": partial(list_deployments, backend=backend),
        "get_config": partial(get_config, backend=backend),
        "restart_service": partial(restart_service, backend=backend),
        "scale_service": partial(scale_service, backend=backend),
        "rollback_deployment": partial(rollback_deployment, backend=backend),
        "delete_namespace": partial(delete_namespace, backend=backend),
    }
    return impls
