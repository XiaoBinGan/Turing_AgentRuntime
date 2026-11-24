"""agents/fake_backend.py — 运维域的**内存假后端**。

为什么需要一个假后端
====================

工具层（`agents/tools.py`）验的是「权限 / 审批 / 归一化 / 脱敏」这条链路，
不是「HTTP 客户端写得对不对」。所以这里不连任何真网络、不起 docker、
不读环境变量里的连接串 —— 一个 `dict` 就是整个集群。

三条硬约束（都是为了「演示可复现」）：

1. **确定性**：同一组调用永远得到同一份数据。没有 `random`、没有
   `time.time()`。时间用固定锚点 `ANCHOR_MS` 往前后推分钟数，
   否则「最近 1 小时的 p99 曲线」每次跑都不一样，用例写不出断言。
2. **可变状态集中在一处**：重启 / 扩缩容 / 回滚 / 删命名空间都会改
   `_state`，读工具只读 `_state`。用例可以直接读 `state()` 验证
   「工具真的改了东西」，而不是只看工具自己返回的 `{"ok": True}`。
3. **服务端不撒谎**：不存在的服务 / 指标 / 命名空间一律抛 `BackendError`，
   让 Dispatcher 把它变成 `ToolOutcome.failed`。伪装成「返回空结果」
   会让模型基于空数据一本正经地分析（这是运行时最贵的一种错）。

数据形状（演示用，够跑通三个技能）
==================================

    每个服务   metrics{p99,error_rate,qps,cpu,memory} / logs / deployments
                / config{prod,staging} / namespace / version / replicas
    p99 与 error_rate 在 api 上是**已经出事**的形状（上涨 + 报错），
    其他两个服务是正常形状 —— 这样「排障」与「容量」能给出不同结论。
"""
from __future__ import annotations

import copy
from typing import Any

__all__ = [
    "ANCHOR_MS",
    "MINUTE_MS",
    "BackendError",
    "FakeOpsBackend",
    "BACKEND",
    "METRIC_NAMES",
    "SERVICE_NAMES",
]

#: 时间锚点（2024-12-05T00:00:00Z 附近）。所有时间戳都是它 + 分钟偏移，
#: 不用 `time.time()`：演示与用例都需要「同一份数据」而不是「现在几点」。
ANCHOR_MS: int = 1_733_356_800_000
MINUTE_MS: int = 60_000

#: 演示里存在的服务。工具 schema 里的 enum 直接用这份清单（单一事实来源）。
SERVICE_NAMES: tuple[str, ...] = ("api", "payments", "search")

#: 指标名。cpu / memory 是 0..1 的占用比（容量技能用它算水位）。
METRIC_NAMES: tuple[str, ...] = ("p99", "error_rate", "qps", "cpu", "memory")

#: 指标单位（返回结果里带上，免得模型自己猜「3.2 是秒还是毫秒」）。
UNITS: dict[str, str] = {
    "p99": "ms",
    "error_rate": "ratio",
    "qps": "rps",
    "cpu": "ratio",
    "memory": "ratio",
}


class BackendError(Exception):
    """后端的「业务失败」：服务不存在 / 指标不支持 / 命名空间已被删。

    刻意用异常而不是返回 `{"error": ...}`：Dispatcher 会把异常转成
    `ToolOutcome.failed`，模型看到的是「这次调用失败了」，不会拿着一段
    「长得像正常结果」的错误文本继续推理。
    """


def _points(*values: float, step_minutes: int = 5) -> list[dict[str, Any]]:
    """把一串值摊成「时间戳 + 值」的点，最后一个点 = 锚点（当前时刻）。

    `values` 按时间升序给，函数负责把时间戳倒推出来 —— 写测试数据时
    只需要关心曲线的形状，不用手算 epoch 毫秒。
    """
    total = len(values)
    return [
        {"ts_ms": ANCHOR_MS - (total - 1 - i) * step_minutes * MINUTE_MS, "value": float(v)}
        for i, v in enumerate(values)
    ]


# ══════════════════════════════════════════════════════════════
# 预置数据（模块级模板；每个后端实例 deepcopy 一份，互不污染）
# ══════════════════════════════════════════════════════════════
_TEMPLATE: dict[str, dict[str, Any]] = {
    "api": {
        "namespace": "prod-core",
        "version": "v42",
        "replicas": 12,
        "owner": "team-core",
        # p99 从 180ms 涨到 3200ms：连接池被新版本的同步调用打满
        "metrics": {
            "p99": _points(180, 240, 390, 900, 1700, 2400, 2900, 3100, 3200, 3180),
            "error_rate": _points(0.001, 0.002, 0.004, 0.02, 0.06, 0.11, 0.14, 0.16, 0.17, 0.18),
            "qps": _points(820, 830, 845, 860, 870, 880, 885, 890, 895, 900),
            "cpu": _points(0.31, 0.33, 0.38, 0.47, 0.58, 0.66, 0.71, 0.74, 0.76, 0.77),
            "memory": _points(0.44, 0.45, 0.48, 0.55, 0.62, 0.68, 0.72, 0.75, 0.78, 0.80),
        },
        "logs": [
            {"level": "ERROR", "offset_min": 2,
             "message": "db acquire timeout after 3000ms (pool=db max=20 active=20 waiters=163)"},
            {"level": "ERROR", "offset_min": 4,
             "message": "ConnPoolExhausted: cannot acquire connection in 3000ms"},
            {"level": "WARN", "offset_min": 9,
             "message": "p99 latency 3180ms exceeds SLO 500ms"},
            {"level": "WARN", "offset_min": 17,
             "message": "gc pause 210ms (heap 3.1GiB/4GiB)"},
            {"level": "INFO", "offset_min": 26,
             "message": "rollout v42 completed: 12/12 pods ready"},
            {"level": "ERROR", "offset_min": 31,
             "message": "upstream payments responded 503 (circuit breaker open)"},
        ],
        "deployments": [
            {"version": "v42", "offset_min": 130, "author": "li.wei",
             "change": "把订单查询改成同步调用连接池（max_connections 保持 20）",
             "status": "succeeded", "suspect": True},
            {"version": "v41", "offset_min": 1_500, "author": "chen.qi",
             "change": "增加下单幂等键", "status": "succeeded", "suspect": False},
            {"version": "v40", "offset_min": 4_300, "author": "li.wei",
             "change": "日志字段规范化", "status": "succeeded", "suspect": False},
        ],
        "config": {
            "prod": {
                "db": {
                    "host": "pg-prod-01.internal",
                    "port": 5432,
                    "user": "app_rw",
                    "password": "pg-Prod-Pa55w0rd",
                },
                "db_password": "pg-Prod-Pa55w0rd",
                "signing_key": "ed25519-prod-signing-7f3a91",
                "api_token": "sk-live-9f8e7d6c5b4a3210",
                "tokens": ["sk-live-aaaaaaaaaaaaaaaa", "sk-live-bbbbbbbbbbbbbbbb"],
                "pool_max": 20,
                "acquire_timeout_ms": 3000,
                "feature_sync_order_query": True,
            },
            "staging": {
                "db": {"host": "pg-stg-01.internal", "port": 5432, "user": "app_rw",
                       "password": "pg-Stg-Pa55w0rd"},
                "db_password": "pg-Stg-Pa55w0rd",
                "signing_key": "ed25519-stg-signing-0000",
                "api_token": "sk-test-0000ffff0000ffff",
                "pool_max": 200,
                "acquire_timeout_ms": 3000,
                "feature_sync_order_query": False,
            },
        },
        "config_revision": "cfg-api-2024-12-05-a",
    },
    "payments": {
        "namespace": "prod-pay",
        "version": "v17",
        "replicas": 6,
        "owner": "team-pay",
        "metrics": {
            "p99": _points(120, 125, 118, 130, 127, 122, 119, 124, 121, 123),
            "error_rate": _points(0.0005, 0.0006, 0.0004, 0.0007, 0.0005,
                                  0.0006, 0.0005, 0.0004, 0.0005, 0.0006),
            "qps": _points(310, 312, 305, 318, 320, 315, 312, 310, 308, 311),
            "cpu": _points(0.22, 0.23, 0.22, 0.24, 0.23, 0.22, 0.23, 0.22, 0.23, 0.23),
            "memory": _points(0.35, 0.35, 0.36, 0.36, 0.35, 0.36, 0.35, 0.36, 0.35, 0.36),
        },
        "logs": [
            {"level": "INFO", "offset_min": 3, "message": "settlement batch 4412 flushed"},
            {"level": "INFO", "offset_min": 12, "message": "healthcheck ok (p99=123ms)"},
            {"level": "WARN", "offset_min": 25,
             "message": "retry 1/3 on downstream bank gateway (timeout 2000ms)"},
        ],
        "deployments": [
            {"version": "v17", "offset_min": 2_900, "author": "wang.min",
             "change": "结算批次大小 200 -> 500", "status": "succeeded", "suspect": False},
            {"version": "v16", "offset_min": 7_000, "author": "wang.min",
             "change": "增加对账重试退避", "status": "succeeded", "suspect": False},
        ],
        "config": {
            "prod": {
                "bank_gateway": {"host": "gw.bank.example", "timeout_ms": 2000},
                "bank_client_secret": "bank-prod-s3cr3t",
                "settlement_batch_size": 500,
                "reconcile_cron": "*/10 * * * *",
            },
            "staging": {
                "bank_gateway": {"host": "gw.stg.bank.example", "timeout_ms": 5000},
                "bank_client_secret": "bank-stg-s3cr3t",
                "settlement_batch_size": 100,
                "reconcile_cron": "*/10 * * * *",
            },
        },
        "config_revision": "cfg-pay-2024-12-02-b",
    },
    "search": {
        "namespace": "prod-search",
        "version": "v8",
        "replicas": 20,
        "owner": "team-search",
        "metrics": {
            "p99": _points(95, 98, 101, 97, 99, 96, 100, 98, 97, 99),
            "error_rate": _points(0.0008, 0.0009, 0.0007, 0.0008, 0.0009,
                                  0.0008, 0.0007, 0.0008, 0.0009, 0.0008),
            "qps": _points(1_500, 1_520, 1_515, 1_530, 1_540, 1_535, 1_520, 1_510, 1_505, 1_512),
            # search 已经在高位：容量技能的「要不要扩容」要靠它
            "cpu": _points(0.78, 0.80, 0.81, 0.83, 0.84, 0.86, 0.87, 0.88, 0.89, 0.90),
            "memory": _points(0.85, 0.86, 0.86, 0.87, 0.88, 0.89, 0.89, 0.90, 0.91, 0.92),
        },
        "logs": [
            {"level": "WARN", "offset_min": 6,
             "message": "index shard 7 lag 1200ms (budget 500ms)"},
            {"level": "INFO", "offset_min": 14, "message": "merge completed in 2.4s shards=7..12"},
            {"level": "WARN", "offset_min": 22,
             "message": "cpu throttling 12% on shard 3 (limit 2 cores)"},
        ],
        "deployments": [
            {"version": "v8", "offset_min": 5_200, "author": "zhao.yu",
             "change": "分片数 12 -> 16", "status": "succeeded", "suspect": False},
        ],
        "config": {
            "prod": {
                "shards": 16,
                "replicas": 2,
                "es_password": "es-Prod-Pa55",
                "bulk_size": 5000,
                "refresh_interval": "1s",
            },
            "staging": {
                "shards": 4,
                "replicas": 1,
                "es_password": "es-Stg-Pa55",
                "bulk_size": 1000,
                "refresh_interval": "5s",
            },
        },
        "config_revision": "cfg-search-2024-11-28-c",
    },
}

#: 命名空间清单（删命名空间时用它判断「有没有这个命名空间」）。
_NAMESPACES: dict[str, str] = {
    "prod-core": "api",
    "prod-pay": "payments",
    "prod-search": "search",
    "staging-core": "api",
}


# ══════════════════════════════════════════════════════════════
# 后端
# ══════════════════════════════════════════════════════════════
class FakeOpsBackend:
    """全部状态在内存里的一次性假集群。

    线程安全：**不是**线程安全的。Dispatcher 用单线程池调工具，
    多个 worker 并发写同一个实例并不在本模块的承诺范围内 ——
    演示里每个进程一个实例就够，真并发应该是真后端的事。
    """

    def __init__(self, template: dict[str, dict[str, Any]] | None = None) -> None:
        self._services: dict[str, dict[str, Any]] = copy.deepcopy(template or _TEMPLATE)
        self._namespaces: dict[str, str] = dict(_NAMESPACES)
        #: 副作用流水（测试与演示都能读，验证「工具真的做了事」）
        self._events: list[dict[str, Any]] = []

    # ── 读：元信息 ──────────────────────────────────────────────
    def services(self) -> list[str]:
        return sorted(self._services)

    def resolve(self, service: Any) -> dict[str, Any]:
        """按名取服务；不存在就抛 `BackendError`（不返回空壳）。"""
        name = str(service or "").strip()
        if not name:
            raise BackendError("service 不能为空")
        entry = self._services.get(name)
        if entry is None:
            raise BackendError(
                f"unknown service: {name!r}（可用：{', '.join(self.services())}）")
        return entry

    # ── 读：指标 ────────────────────────────────────────────────
    def metric(self, service: Any, metric: Any, window_minutes: int = 30) -> dict[str, Any]:
        """取一条指标曲线在时间窗内的点 + 汇总统计。

        `window_minutes` 按「锚点往前推」裁剪；窗口内一个点都没有时抛错
        （而不是返回空列表 —— 空曲线会让模型以为「指标不存在」）。
        """
        entry = self.resolve(service)
        name = str(metric or "").strip().lower()
        series = entry["metrics"].get(name)
        if series is None:
            raise BackendError(
                f"unknown metric: {name!r}（可用：{', '.join(sorted(entry['metrics']))}）")
        window = max(1, int(window_minutes))
        since = ANCHOR_MS - window * MINUTE_MS
        points = [p for p in series if p["ts_ms"] >= since]
        if not points:
            raise BackendError(
                f"metric {name!r} 在最近 {window} 分钟内没有数据点"
                f"（最小可用窗口：{self._min_window(series)} 分钟）")
        values = [p["value"] for p in points]
        half = len(values) // 2 or 1
        first_avg = sum(values[:half]) / float(half)
        last_avg = sum(values[-half:]) / float(half)
        return {
            "service": service,
            "metric": name,
            "unit": UNITS.get(name, ""),
            "window_minutes": window,
            "points": [{"ts_ms": p["ts_ms"], "value": round(p["value"], 4)} for p in points],
            "stats": {
                "latest": round(values[-1], 4),
                "avg": round(sum(values) / float(len(values)), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                # 窗口前半 vs 后半的均值变化：技能用它判断「在涨还是在平」
                "delta_ratio": round((last_avg - first_avg) / first_avg, 4) if first_avg else 0.0,
            },
        }

    @staticmethod
    def _min_window(series: list[dict[str, Any]]) -> int:
        span = ANCHOR_MS - series[0]["ts_ms"]
        return max(1, int(span // MINUTE_MS))

    # ── 读：日志 ────────────────────────────────────────────────
    def logs(self, service: Any, window_minutes: int = 60, level: str = "",
             limit: int = 20) -> dict[str, Any]:
        """按服务 + 时间窗 + 级别查日志，**新→旧**排序。"""
        entry = self.resolve(service)
        window = max(1, int(window_minutes))
        since = ANCHOR_MS - window * MINUTE_MS
        want = str(level or "").strip().upper()
        rows: list[dict[str, Any]] = []
        for item in entry["logs"]:
            ts = ANCHOR_MS - int(item["offset_min"]) * MINUTE_MS
            if ts < since:
                continue
            if want and item["level"] != want:
                continue
            rows.append({"ts_ms": ts, "level": item["level"],
                         "service": service, "message": item["message"]})
        rows.sort(key=lambda r: r["ts_ms"], reverse=True)
        clipped = rows[: max(1, int(limit))]
        return {
            "service": service,
            "window_minutes": window,
            "level": want or "ALL",
            "total_matched": len(rows),
            "returned": len(clipped),
            "entries": clipped,
        }

    # ── 读：发布 ────────────────────────────────────────────────
    def deployments(self, service: Any = "", limit: int = 10) -> dict[str, Any]:
        """查最近发布（不传 service = 全部服务的最近 N 条）。

        `suspect=True` 是演示里人工标注的「这次发布可疑」—— 真系统的等价物是
        「发布后 5 分钟内 SLO 开始劣化」，这里把它显式标出来，方便技能串起
        「p99 上涨 ← 可疑发布」这条因果线。
        """
        wanted = [str(service).strip()] if service else self.services()
        rows: list[dict[str, Any]] = []
        for name in wanted:
            entry = self.resolve(name)
            for dep in entry["deployments"]:
                rows.append({
                    "service": name,
                    "namespace": entry["namespace"],
                    "version": dep["version"],
                    "deployed_at_ms": ANCHOR_MS - int(dep["offset_min"]) * MINUTE_MS,
                    "minutes_ago": int(dep["offset_min"]),
                    "author": dep["author"],
                    "change": dep["change"],
                    "status": dep["status"],
                    "suspect": bool(dep["suspect"]),
                })
        rows.sort(key=lambda r: r["deployed_at_ms"], reverse=True)
        clipped = rows[: max(1, int(limit))]
        return {"count": len(clipped), "total": len(rows), "deployments": clipped}

    # ── 读：配置（**原值，脱敏在工具层做**） ────────────────────
    def config(self, service: Any, environment: str = "prod") -> dict[str, Any]:
        """取配置原值。**调用方负责脱敏** —— 这一层只忠实返回存储里的东西。

        为什么脱敏不放后端：脱敏是「出站策略」，属于工具层（进 Prompt 的那一端）；
        把脱敏塞进后端，将来多一个出站通道（比如又一个工具直接读同一个 dict）
        就得再实现一遍，两处规则一定会漂移。
        """
        entry = self.resolve(service)
        env = str(environment or "prod").strip().lower()
        values = entry["config"].get(env)
        if values is None:
            raise BackendError(
                f"service {service!r} 没有环境 {env!r}（可用："
                f"{', '.join(sorted(entry['config']))}）")
        return {"service": service, "environment": env,
                "revision": entry["config_revision"], "values": copy.deepcopy(values)}

    # ── 读：容量 ────────────────────────────────────────────────
    def capacity(self, service: Any, window_minutes: int = 30) -> dict[str, Any]:
        """水位 + 增速 + 余量：容量技能的原料（一条工具调用拿全）。"""
        entry = self.resolve(service)
        cpu = self.metric(service, "cpu", window_minutes)
        mem = self.metric(service, "memory", window_minutes)
        qps = self.metric(service, "qps", window_minutes)
        cpu_latest = cpu["stats"]["latest"]
        mem_latest = mem["stats"]["latest"]
        # 增速按「窗口后半段均值 / 前半段均值」的变化估：演示里够用，
        # 真系统换成一阶差分或线性回归，返回形状不变。
        per_window = cpu["stats"]["delta_ratio"]
        headroom = max(0.0, 1.0 - max(cpu_latest, mem_latest))
        # 半个窗口 = 后半段。把「半个窗口涨了 per_window」换算成「每小时涨多少」：
        # 每 5 分钟一个点，半个窗口就是 len(points)//2 个点，即若干个 5 分钟。
        half_points = max(1, len(cpu["points"]) // 2)
        half_hours = max(1e-6, half_points * 5.0 / 60.0)
        per_hour = round(per_window / half_hours, 4)
        return {
            "service": service,
            "namespace": entry["namespace"],
            "replicas": entry["replicas"],
            "replicas_per_pod_qps": round(qps["stats"]["latest"] / max(1, entry["replicas"]), 2),
            "water_level": {"cpu": cpu_latest, "memory": mem_latest},
            "headroom_ratio": round(headroom, 4),
            "growth": {"cpu_delta_ratio_in_window": per_window,
                       "cpu_growth_per_hour_est": per_hour},
            "window_minutes": int(window_minutes),
            "points_used": len(cpu["points"]),
        }

    # ── 写：三个副作用 + 一个不可逆 ────────────────────────────
    def restart(self, service: Any, reason: str = "") -> dict[str, Any]:
        """重启：**内存里的假动作** —— 记一次事件，附上「现场会丢」的警告。"""
        entry = self.resolve(service)
        event = {"action": "restart", "service": service, "reason": str(reason or ""),
                 "version": entry["version"], "replicas": entry["replicas"],
                 "at_ms": ANCHOR_MS}
        self._events.append(event)
        restarted = sum(1 for e in self._events
                        if e["action"] == "restart" and e["service"] == service)
        return {
            "service": service,
            "version": entry["version"],
            "restarted_pods": entry["replicas"],
            "restart_count_today": restarted,
            # 诚实提示：重启会清掉连接池 / in-flight 请求，现场（堆栈、连接数）随之消失
            "warning": "重启会丢掉当前进程内的现场（连接池状态、in-flight 请求），排障应先取证",
        }

    def scale(self, service: Any, replicas: int) -> dict[str, Any]:
        """扩缩容：真的改 `_state.replicas`（后续 capacity 读得到新值）。"""
        entry = self.resolve(service)
        target = int(replicas)
        if target < 1:
            raise BackendError(f"replicas 必须 >= 1，收到 {target}")
        before = int(entry["replicas"])
        entry["replicas"] = target
        self._events.append({"action": "scale", "service": service,
                             "from": before, "to": target, "at_ms": ANCHOR_MS})
        return {
            "service": service,
            "replicas_before": before,
            "replicas_after": target,
            "direction": "扩容" if target > before else ("缩容" if target < before else "不变"),
            "namespace": entry["namespace"],
            "applied": True,
        }

    def rollback(self, service: Any, to_version: str = "") -> dict[str, Any]:
        """回滚：把 `version` 改成上一个版本（或指定版本）。

        **这是不可逆的**：旧版本的 schema 迁移不会自动回退，回滚后写入的数据
        可能在新版本里读不出来 —— 工具层因此把它标成 T3（Dispatcher 硬拒）。
        """
        entry = self.resolve(service)
        history = [d["version"] for d in entry["deployments"]]
        target = str(to_version or "").strip()
        if target:
            if target not in history:
                raise BackendError(
                    f"service {service!r} 没有版本 {target!r}（历史：{', '.join(history)}）")
        else:
            candidates = [v for v in history if v != entry["version"]]
            if not candidates:
                raise BackendError(f"service {service!r} 没有可回滚的历史版本")
            target = candidates[0]
        before = entry["version"]
        entry["version"] = target
        self._events.append({"action": "rollback", "service": service,
                             "from": before, "to": target, "at_ms": ANCHOR_MS})
        return {
            "service": service,
            "from_version": before,
            "to_version": target,
            "rolled_back_pods": entry["replicas"],
            "irreversible": True,
            "note": "schema 迁移与已写入的数据不会随回滚恢复",
        }

    def delete_namespace(self, namespace: Any) -> dict[str, Any]:
        """删命名空间：**演示用的真不可逆操作** —— 删了就把数据从 `_state` 里摘掉。"""
        name = str(namespace or "").strip()
        if not name:
            raise BackendError("namespace 不能为空")
        if name not in self._namespaces:
            raise BackendError(
                f"unknown namespace: {name!r}（可用：{', '.join(sorted(self._namespaces))}）")
        service = self._namespaces.pop(name)
        removed = self._services.pop(service, None)
        self._events.append({"action": "delete_namespace", "namespace": name,
                             "service": service, "at_ms": ANCHOR_MS})
        return {
            "namespace": name,
            "deleted": True,
            "irreversible": True,
            "resources_removed": {
                "services": 1,
                "pods": int((removed or {}).get("replicas", 0)),
                "configmaps": len((removed or {}).get("config", {})),
                "pvc": 2,
            },
            "note": "PV/PVC 与备份已一并删除，无法通过回滚恢复",
        }

    # ── 观测 ────────────────────────────────────────────────────
    def events(self) -> list[dict[str, Any]]:
        """副作用流水（读工具永远不会往这里写）。"""
        return copy.deepcopy(self._events)

    def state(self) -> dict[str, Any]:
        """当前状态快照（用例用它断言「工具真的改了东西」）。"""
        return {"services": copy.deepcopy(self._services),
                "namespaces": dict(self._namespaces),
                "events": copy.deepcopy(self._events)}


#: 进程级默认实例：工具函数默认操作它（演示 / 单测共用一个就够）。
#: 需要隔离时自己造一个 `FakeOpsBackend()` 注入给 `tools.build_handlers(backend=...)`。
BACKEND = FakeOpsBackend()
