"""core/observability.py — W30：生产可观测性收口（结构化日志 / Prometheus 指标 / 优雅关闭）。

三件工具各回答一个运维问题：

    JsonLogger           出了事能不能按字段筛？（grep tenant=acme level=ERROR）
    Metrics              现在什么水位？（QPS / 延迟分位 / 错误率 / token 成本）
    ShutdownCoordinator  发版时能不能不丢请求？（停接新 → 等飞行中 → 跑 flush 钩子）

与 `plane/quality.py` 的 `Tracer` 分工不同，不重叠：
    Tracer  进程内 span 树，回答"慢在哪 / 贵在哪"，给人读，随请求结束就没了；
    本模块  **跨进程**的三件套：可筛日志（进日志系统）、可抓指标（进时序库）、
            可收口关闭（进发版流程）。两者共用同一批事实（tenant / latency / tokens），
            但一个面向人、一个面向机器。

依赖策略（借库 vs 手写，逐个说的都是"为什么不自己写"）：

    Metrics              借 **prometheus_client**。文本 exposition、label 值转义、
                         浮点格式（`le="0.005"` 而不是 `0.0050000000000000001`）、
                         `+Inf` 桶、HELP/TYPE 规则、同名同 label 集合的检测，全是对接
                         抓取端的硬规格；手写版（本文件早期那 200 行）的正确性上限
                         只是"我想到的那些边界"。私有 `CollectorRegistry` 保证隔离。
                         **仍然手写**的只有 `percentiles()`：库不做进程内分位。
    JsonLogger           手写。标准库 `logging` 没有"一行一 JSON + 保留键不可伪造 +
                         强制脱敏（大小写/分隔符归一）"这层语义；包 Handler 只会更长。
    ShutdownCoordinator  手写。协议就是"停接新 → 等飞行中 → 跑钩子"三件事，加上
                         假时钟下也要能超时的硬保险，库生态里没有等价物。
    instrument_gateway   手写。纯装配（包 Gateway + fail-open 记账），无等价库。

设计约束：
1. **依赖只借该借的**：如上。核心业务路径仍只用标准库（见 requirements.txt）。
2. **fail-open**：观测面绝不反噬业务面 —— sink 抛异常、hook 抛异常、序列化失败
   都只影响观测，不影响回合本身（与 `Gateway._audit` 同一条纪律）。
3. **可测**：sink / clock 可注入，`records()` 给出已输出内容，exposition 可被
   `prometheus_client.parser` 完整解析回来（换库后的一致性由库 + 解析往返共同保证）。
4. **不 import core.gateway**（避免耦合与循环导入）：需要 `TurnResult` 时惰性导入。

Prometheus 文本格式（库产出，形如）：

    # HELP agent_runtime_turns_total Total turns
    # TYPE agent_runtime_turns_total counter
    agent_runtime_turns_total{tenant="acme"} 12
    # HELP agent_runtime_turn_duration_seconds Turn latency
    # TYPE agent_runtime_turn_duration_seconds histogram
    agent_runtime_turn_duration_seconds_bucket{le="0.1"} 5
    agent_runtime_turn_duration_seconds_bucket{le="+Inf"} 12
    agent_runtime_turn_duration_seconds_sum 3.2
    agent_runtime_turn_duration_seconds_count 12
"""
from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import json
import math
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, Iterator, Mapping, Sequence

# `prometheus_client` 是**可选**依赖：核心运行时必须能在只有标准库的环境里导入
# （`requirements-core.txt` 因此是空文件，`tests/test_architecture.py` 会验证这一点）。
# 装了就用品它的文本 exposition / label 转义 / 直方图分桶；没装就在 `render()`
# 时**明确报错**而不是静默返回空串 —— 后者会让 `/metrics` 看起来正常但什么指标都没有，
# 那是最难查的一类故障。
try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:                                        # pragma: no cover
    CollectorRegistry = Counter = Gauge = Histogram = None  # type: ignore[assignment]
    generate_latest = None                                 # type: ignore[assignment]
    PROMETHEUS_AVAILABLE = False

__all__ = [
    "PROMETHEUS_AVAILABLE",
    "JsonLogger",
    "Metrics",
    "MetricsError",
    "ShutdownCoordinator",
    "InstrumentedGateway",
    "instrument_gateway",
    "HISTOGRAM_BUCKETS",
    "REDACTED",
    "LEVELS",
]

#: 被脱敏字段的替换值（与 plane.governance.REDACTED 同形，便于日志里 grep）
REDACTED = "***"

#: 级别 → 数值。WARN 是规范名（WARNING / ERR / CRITICAL 都映射到它）
LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_LEVEL_ALIASES = {
    "TRACE": "DEBUG",
    "WARNING": "WARN",
    "ERR": "ERROR",
    "CRITICAL": "ERROR",
    "FATAL": "ERROR",
}

#: 默认直方图桶（秒）。与 Prometheus 客户端库的一致，端点便于和别处的面板对齐
HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

#: 这三类键**永远**脱敏，不可通过 redact_keys 关闭（安全底线，不是可选项）
_MANDATORY_REDACT = frozenset({"apikey", "authorization", "password"})

#: JSON 记录的保留键。字段名撞上它们时改名，绝不允许调用方覆盖 ts/level
_RESERVED_FIELDS = ("ts", "level", "event", "service")

_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


# ══════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════
def _norm_key(key: Any) -> str:
    """键名归一：大小写不敏感 + 忽略分隔符。

    `api_key` / `API_KEY` / `apiKey` / `api-key` 归一后都是 `apikey`——
    脱敏不能因为拼写风格漏掉一个（漏一个就是泄漏一份凭证）。
    """
    return re.sub(r"[\s_\-]+", "", str(key)).lower()


def _level_name(level: Any) -> str:
    name = str(level or "").strip().upper()
    name = _LEVEL_ALIASES.get(name, name)
    if name not in LEVELS:
        raise ValueError(f"未知日志级别 {level!r}，只允许 {sorted(LEVELS)}")
    return name


def _escape_label(value: Any) -> str:
    """Prometheus label 值转义：反斜杠、双引号、换行（顺序不能反）。"""
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _iso_ms(epoch: float) -> str:
    """epoch 秒 → ISO8601（UTC，毫秒精度，`Z` 结尾）。

    带毫秒是硬要求：同一秒内多个回合的日志必须能按 ts 排出先后。
    """
    moment = _dt.datetime.fromtimestamp(float(epoch), tz=_dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ══════════════════════════════════════════════════════════════
# 一、JsonLogger：一行一个 JSON
# ══════════════════════════════════════════════════════════════
class JsonLogger:
    """结构化日志：一行一个 JSON，默认写 stderr（stdout 归协议）。

    为什么是 JSON 行而不是 f-string：自由文本只能"看"，JSON 行能"筛"。
    `grep tenant=acme level=ERROR` 这种查询是排障的第一步，前提是每个字段
    都是独立键。

    sink 契约：`Callable[[str], None]`，收到**一行**（不含换行符）。
    默认 sink 写 stderr —— stdout 归协议（事件流 / 输出），日志混进去会污染管道。

    线程安全：`log()` 会在锁内拼记录、写记录；并发写同一 sink 时行不交错。
    """

    def __init__(
        self,
        *,
        sink: Callable[[str], None] | None = None,
        level: str = "INFO",
        service: str = "agent-runtime",
        clock: Callable[[], float] | None = None,
        redact_keys: Sequence[str] = ("api_key", "authorization", "password"),
    ) -> None:
        self._sink = sink if sink is not None else _stderr_sink
        if not callable(self._sink):
            raise TypeError("sink 必须可调用：Callable[[str], None]")
        self.service = str(service)
        self._clock = clock if clock is not None else time.time
        if not callable(self._clock):
            raise TypeError("clock 必须可调用：Callable[[], float]")
        self._level = _level_name(level)
        # 归一后的脱敏键集合：调用方的 ∪ 强制底线（底线不可关闭）
        self._redact: frozenset[str] = self._build_redact(redact_keys)
        self._records: list[dict] = []
        self._lock = threading.RLock()

    # ── 配置 ────────────────────────────────────────────────
    @staticmethod
    def _build_redact(redact_keys: Sequence[str] | None) -> frozenset[str]:
        keys = set(_MANDATORY_REDACT)
        try:
            for key in redact_keys or ():
                keys.add(_norm_key(key))
        except TypeError:
            # 传了个不可迭代的东西：退回底线，不让配置错误炸在第一个请求上
            pass
        return frozenset(keys)

    def set_level(self, level: str) -> None:
        self._level = _level_name(level)

    @property
    def level(self) -> str:
        return self._level

    def add_redact_keys(self, *keys: str) -> None:
        """追加脱敏字段（运行期热加，比如新接了一个上游的凭证字段名）。"""
        with self._lock:
            self._redact = frozenset(set(self._redact) | {_norm_key(k) for k in keys})

    # ── 输出 ────────────────────────────────────────────────
    def log(self, level: str, event: str, /, **fields: Any) -> dict:
        """写一条。返回该条记录（即使被级别过滤掉也返回，便于调用方断言）。

        顺序固定：ts / level / event / service 打头，业务字段按传入顺序跟在后面。
        """
        name = _level_name(level)
        record = self._build(name, event, fields)
        if LEVELS[name] < LEVELS[self._level]:
            return copy.deepcopy(record)
        self._emit(record)
        # 返回副本：调用方改返回值不能改到 records() 里的同一条
        return copy.deepcopy(record)

    def debug(self, event: str, /, **fields: Any) -> dict:
        return self.log("DEBUG", event, **fields)

    def info(self, event: str, /, **fields: Any) -> dict:
        return self.log("INFO", event, **fields)

    def warn(self, event: str, /, **fields: Any) -> dict:
        return self.log("WARN", event, **fields)

    #: `logging` 的习惯拼法，等价于 warn
    warning = warn

    def error(self, event: str, /, **fields: Any) -> dict:
        return self.log("ERROR", event, **fields)

    def records(self) -> list[dict]:
        """已输出的记录（测试/自检用；深拷贝，外部改不到内部状态）。"""
        with self._lock:
            return [copy.deepcopy(r) for r in self._records]

    def __call__(self, level: str, event: str, /, **fields: Any) -> dict:
        return self.log(level, event, **fields)

    # ── 内部 ────────────────────────────────────────────────
    def _build(self, level: str, event: str, fields: Mapping[str, Any]) -> dict:
        try:
            ts = _iso_ms(self._clock())
        except Exception:
            ts = _iso_ms(time.time())
        record: dict[str, Any] = {
            "ts": ts,
            "level": level,
            "event": str(event),
            "service": self.service,
        }
        for key, value in fields.items():
            name = str(key)
            if name in _RESERVED_FIELDS:
                # 保留键不可覆盖：否则业务字段能伪造 level/ts，日志就不可信了
                name = f"field_{name}"
            if _norm_key(name) in self._redact:
                record[name] = REDACTED
            else:
                record[name] = self._scrub(value)
        return record

    def _scrub(self, value: Any) -> Any:
        """递归脱敏嵌套结构：`{"meta": {"api_key": "..."}}` 同样不能漏。"""
        try:
            if isinstance(value, Mapping):
                out: dict[Any, Any] = {}
                for k, v in value.items():
                    out[k] = REDACTED if _norm_key(k) in self._redact else self._scrub(v)
                return out
            if isinstance(value, (list, tuple)):
                return [self._scrub(v) for v in value]
        except Exception:
            return value
        return value

    def _emit(self, record: dict) -> None:
        """序列化 + 落 sink。任何一步失败都吞掉（fail-open：日志坏了业务照跑）。"""
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:
            # 记录里有连 repr 都炸的对象：退化成最小可用记录，至少留痕
            line = json.dumps(
                {"ts": record.get("ts", ""), "level": record.get("level", "ERROR"),
                 "event": record.get("event", "log_error"),
                 "service": self.service, "log_error": "unserializable_fields"},
                ensure_ascii=False)
        with self._lock:
            self._records.append(record)
        try:
            self._sink(line)
        except Exception:
            pass


def _stderr_sink(line: str) -> None:
    """默认 sink：stderr 一行。broken pipe / 编码错也要吞。"""
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        return
    try:
        sys.stderr.flush()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# 二、Metrics：Prometheus 指标（内核用 prometheus_client）
# ══════════════════════════════════════════════════════════════
class MetricsError(ValueError):
    """指标登记 / 写入违规。

    继承 `ValueError`：手写版抛的就是 ValueError，按 `except ValueError` 捕获的
    调用方不用改；单独命名是为了让"指标配置错了"在 except 里能和"业务参数错了"分开。
    """


class Metrics:
    """指标登记表 + Prometheus text exposition（薄封装 `prometheus_client`）。

    三种指标：
        counter    只增（turns_total / tokens_total / errors_total）
        gauge      当前值（inflight / queue_depth），覆盖不累加
        observe    直方图（延迟），产出 `_bucket{le=...}` / `_sum` / `_count`

    为什么不再手写 exposition：文本格式、label 转义、浮点格式（`le="0.005"` 而不是
    `0.0050000000000000001`）、`+Inf` 桶、HELP/TYPE 的重复规则、同名不同 label 集合
    的检测，全是对接抓取端的硬规格，官方库已经在几百个边界用例上钉住了。手写版的
    上限是"我想到的那些边界"，这是典型的该用成熟组件的地方。

    仍然自己写的那一小块：**进程内分位**（`percentiles()`）。prometheus_client 不做
    进程内分位，它的分位走 `histogram_quantile` 的查询端聚合。

    两条一致性纪律（写入即报错，不等到抓取时才发现）：
    1. **同名同类型**：一个名字既当 counter 又当 histogram，exposition 直接非法。
    2. **同名同 label 集合**：同一 family 混两套 label，时序库里是两坨无法 JOIN 的半残数据。
    两条都抛 `MetricsError`（ValueError 子类），消息里点名 metric 全名。

    隔离：默认**私有 `CollectorRegistry`**。往全局 `REGISTRY` 里注册会让第二次
    `Metrics()` 撞 `Duplicated timeseries`，也会把本模块的指标混进别人的 exposition。

    库的行为差异（实测，不是猜测）：
    - family 顺序 = **注册顺序**（`CollectorRegistry.collect()` 不排序），family 内
      series 顺序 = **首次写入顺序**；label 名在每行内按字母序（这一点库有排序）。
      旧手写版是按名字/标签串全排序的，所以"跨两次运行逐字节 diff"不再成立。
    - 每个 counter/histogram 会额外暴露一个 `<name>_created` 时间戳 gauge（库默认，
      见 `prometheus_client.metrics._use_created`）。它带墙钟，跨进程不可 diff；
      需要干净 exposition 的部署用环境变量 `PROMETHEUS_DISABLE_CREATED_SERIES=1`
      或 `prometheus_client.disable_created_metrics()` 关掉 —— 那是**进程全局**开关，
      本模块不擅自替调用方改全局状态。
    - counter 只能 `inc` 非负值（旧手写版允许负增量）。要减就用 gauge。

    线程安全：全部写入/渲染都在 RLock 内（Gateway 是多线程的）。
    """

    def __init__(self, *, prefix: str = "agent_runtime",
                 registry: Any = None) -> None:
        self.prefix = str(prefix or "")
        if self.prefix and not _NAME_RE.match(self.prefix):
            raise MetricsError(
                f"非法 prefix {self.prefix!r}（Prometheus 指标名不允许的字符）")
        # 默认私有 registry：不污染全局 REGISTRY，多次构造互不干扰
        if not PROMETHEUS_AVAILABLE:
            raise MetricsError(
                "Metrics 需要 prometheus_client（可选依赖）。"
                "装法：pip install prometheus_client；"
                "或者不要构造 Metrics —— 观测面缺席不该拦住业务面。")
        self._registry = registry if registry is not None else CollectorRegistry()
        self._lock = threading.RLock()
        # metric 全名 → 类型（counter / gauge / histogram）
        self._types: dict[str, str] = {}
        # metric 全名 → 库里的 collector（Counter / Gauge / Histogram）
        self._families: dict[str, Any] = {}
        # metric 全名 → 已声明的 label 名集合（用于"同名同 label 集合"校验与报错文案）
        self._label_keys: dict[str, frozenset[str]] = {}
        self._helps: dict[str, str] = {}
        # 原始样本（有界），percentiles() 用它做进程内估算
        self._raw: dict[str, deque] = {}
        self.sample_limit = 2048

    # ── 写入 ────────────────────────────────────────────────
    def counter(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        """累加（只增）。`value` 负数会被库拒绝，见类文档里的行为差异。"""
        full = self._full(name)
        delta = _as_float(value, "counter", full)
        with self._lock:
            child = self._child(full, "counter", labels)
            try:
                child.inc(delta)
            except ValueError as exc:       # 库：counter 不接受负增量
                raise MetricsError(f"counter {full} 累加失败：{exc}") from exc

    def gauge(self, name: str, value: float, labels: dict | None = None) -> None:
        """覆盖写。gauge 是"当前值"，累加就是 bug（重启后翻倍）。"""
        full = self._full(name)
        number = _as_float(value, "gauge", full)
        with self._lock:
            child = self._child(full, "gauge", labels)
            child.set(number)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        """直方图取样：由库按 `HISTOGRAM_BUCKETS` 落桶（`le` 语义：<= 边界进桶）。"""
        full = self._full(name)
        sample = _as_float(value, "histogram", full)
        with self._lock:
            child = self._child(full, "histogram", labels)
            child.observe(sample)
            raw = self._raw.get(full)
            if raw is None:
                raw = deque(maxlen=self.sample_limit)
                self._raw[full] = raw
            raw.append(sample)

    def describe(self, name: str, help_text: str) -> None:
        """给 metric 挂 HELP 文本（不挂也能渲染，会自动生成）。

        必须在**首次写入前**调用：prometheus_client 的 HELP 在 collector 创建时固化，
        创建之后没有公开 setter（`InstrumentedGateway` 就是先 describe 再记账）。
        晚到的 describe 只记录不生效，不会抛错（观测面 fail-open）。
        """
        full = self._full(name)
        with self._lock:
            self._helps[full] = str(help_text)

    # ── 读取 ────────────────────────────────────────────────
    def percentiles(self, name: str) -> dict:
        """**进程内估算**分位（p50 / p95 / p99），跨 label 汇总。

        这是本模块自己算的最后一块，也是唯一一块 prometheus_client 不管的事：库把样本
        落进桶就丢掉了，分位要靠查询端的 `histogram_quantile(rate(...))` 聚合。

        所以这里的数字**只代表当前进程、当前副本、最近 `sample_limit` 个样本**：
        单机排障够用，做告警阈值/跨副本 SLO 一律用 `histogram_quantile`：
            histogram_quantile(0.99, sum by (le) (rate(agent_runtime_turn_duration_seconds_bucket[5m])))
        用原始样本线性插值（而不是桶内插值）：桶是给 Prometheus 抓的（便宜、可聚合），
        进程内分位是给人看的（要准）。样本有界，估算不占内存。
        """
        full = self._full(name)
        with self._lock:
            raw = self._raw.get(full)
            samples = sorted(raw) if raw else []
        if not samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        return {
            "p50": round(_quantile(samples, 0.50), 9),
            "p95": round(_quantile(samples, 0.95), 9),
            "p99": round(_quantile(samples, 0.99), 9),
        }

    def value(self, name: str, labels: dict | None = None) -> float:
        """读一个 series 的当前值（counter 累计 / gauge 当前）；不存在返回 0.0。

        走库的 `collect()` 而不是私有 `_value`：私有属性跨版本会变，`collect()` 是
        `generate_latest` 自己用的公开接口。
        """
        full = self._full(name)
        wanted = {str(k): str(v) for k, v in (labels or {}).items()}
        with self._lock:
            obj = self._families.get(full)
            if obj is None:
                return 0.0
            for sample in _samples(obj):
                if sample.name == full and dict(sample.labels) == wanted:
                    return float(sample.value)
        return 0.0

    def snapshot(self) -> dict:
        """只读快照（测试/健康检查用）：{"counters": {...}, "histograms": {...}}。"""
        counters: dict[str, dict[str, float]] = {}
        histograms: dict[str, dict] = {}
        with self._lock:
            for full, kind in self._types.items():
                obj = self._families.get(full)
                if obj is None:
                    continue
                for sample in _samples(obj):
                    # `_bucket` 带 `le` 标签、`_created` 是墙钟：都不是 series 的"值"
                    key = _label_str(tuple(sorted(sample.labels.items())))
                    if kind == "counter" and sample.name == full:
                        counters.setdefault(full, {})[key] = float(sample.value)
                    elif kind == "histogram" and sample.name in (f"{full}_count", f"{full}_sum"):
                        entry = histograms.setdefault(full, {}).setdefault(
                            key, {"count": 0, "sum": 0.0})
                        if sample.name.endswith("_count"):
                            entry["count"] = int(sample.value)
                        else:
                            entry["sum"] = round(float(sample.value), 9)
        return {"counters": counters, "histograms": histograms}

    # ── 渲染 ────────────────────────────────────────────────
    def render(self) -> str:
        """Prometheus text exposition（就是 `generate_latest` 的字节，不做二次加工）。

        顺序由库决定：family 按注册顺序，family 内按首次写入顺序，label 名行内字母序。
        同一进程内重复渲染逐字节相同（`_created` 时间戳在创建时固化）。
        """
        with self._lock:
            return generate_latest(self._registry).decode("utf-8")

    # ── 内部 ────────────────────────────────────────────────
    def _full(self, name: Any) -> str:
        raw = str(name or "").strip()
        if not raw:
            raise MetricsError("metric 名不能为空")
        if self.prefix and not (raw == self.prefix or raw.startswith(self.prefix + "_")):
            raw = f"{self.prefix}_{raw}"
        self._validate(raw)
        return raw

    @staticmethod
    def _validate(full: str) -> None:
        if not _NAME_RE.match(full):
            raise MetricsError(
                f"非法 metric 名 {full!r}：只允许 [a-zA-Z_:][a-zA-Z0-9_:]*"
                f"（写错了会在抓取端静默丢弃，所以在写入时就炸）")

    def _child(self, full: str, kind: str, labels: Mapping[str, Any] | None) -> Any:
        """登记并取回该 series 的写入句柄（无 label 时就是 collector 本身）。"""
        pairs = {str(k): str(v) for k, v in (labels or {}).items()}
        self._declare(full, kind, frozenset(pairs))
        obj = self._families[full]
        if not pairs:
            return obj
        try:
            return obj.labels(**pairs)
        except ValueError as exc:       # 库：Incorrect label names / Invalid label metric name
            raise MetricsError(
                f"metric {full!r} 的 label {sorted(pairs)} 无法登记：{exc}"
                f"（label 名只允许 [a-zA-Z_][a-zA-Z0-9_]*，且必须与首次写入一致）"
            ) from exc

    def _declare(self, full: str, kind: str, keys: frozenset[str]) -> None:
        known = self._types.get(full)
        if known is not None and known != kind:
            raise MetricsError(
                f"metric {full!r} 已登记为 {known}，不能当 {kind} 用"
                f"（同名不同类型会产出非法 exposition）")
        seen = self._label_keys.get(full)
        if seen is None:
            self._label_keys[full] = keys
        elif seen != keys:
            diff = sorted(seen.symmetric_difference(keys))
            raise MetricsError(
                f"metric {full!r} 的 label 集合不一致：已有 {sorted(seen)}，"
                f"本次 {sorted(keys)}，差异 {diff}"
                f"（同一 family 混两套 label 会让面板 JOIN 出空洞）")
        if full not in self._families:
            self._families[full] = self._make(full, kind, sorted(keys))
            self._types[full] = kind

    def _make(self, full: str, kind: str, labelnames: Sequence[str]) -> Any:
        """建 collector 并注册到**私有** registry。HELP 文本在创建这一刻固化。"""
        help_text = self._helps.get(full) or _default_help(full)
        try:
            if kind == "counter":
                return Counter(full, help_text, labelnames, registry=self._registry)
            if kind == "gauge":
                return Gauge(full, help_text, labelnames, registry=self._registry)
            return Histogram(full, help_text, labelnames,
                             buckets=HISTOGRAM_BUCKETS, registry=self._registry)
        except ValueError as exc:       # 库：DuplicateTimeseries / Invalid metric name
            raise MetricsError(f"metric {full!r}（{kind}）登记失败：{exc}") from exc


def _as_float(value: Any, kind: str, full: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{kind} {full} 的值必须是数字，收到 {value!r}") from exc


def _samples(metric: Any) -> Iterator[Any]:
    """扁平化 `collector.collect()` 的样本列表（含 `_bucket` / `_sum` / `_count`）。"""
    for family in metric.collect():
        yield from family.samples


def _quantile(sorted_samples: Sequence[float], q: float) -> float:
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_samples[0])
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_samples[lo]) + (float(sorted_samples[hi]) - float(sorted_samples[lo])) * frac


def _label_str(key: tuple) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in key)
    return "{" + inner + "}"


def _default_help(full: str) -> str:
    """没 describe 时自动生成可读 HELP（至少不会是空串）。"""
    words = full.replace(":", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else full


# ══════════════════════════════════════════════════════════════
# 三、ShutdownCoordinator：优雅关闭
# ══════════════════════════════════════════════════════════════
class ShutdownCoordinator:
    """优雅关闭三步：**拒绝新请求 → 等飞行中任务 → 执行清理钩子**。

    为什么不能直接 `sys.exit()`：飞行中的回合可能已经扣了 token、写了一半
    上下文、正要落 checkpoint。硬杀会留下"扣了钱但没有结果"的脏账，
    也会丢掉最后一批指标和日志——出事故时最想看的那几十行正好没了。

    `drain_timeout_s` 到了还没排空怎么办：**照样执行钩子并返回 drained=False**。
    flush 比"等一个卡死的请求"重要；调用方拿到 remaining 自行决定要不要强杀。

    `clock` 可注入（elapsed_s 用它算），但超时判断另有一道真实时钟硬保险，
    防止测试里给的假时钟不走导致 wait() 永不返回。
    """

    def __init__(self, *, drain_timeout_s: float = 10.0,
                 clock: Callable[[], float] | None = None) -> None:
        try:
            timeout = float(drain_timeout_s)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"drain_timeout_s 必须是数字，收到 {drain_timeout_s!r}") from exc
        if timeout < 0:
            raise ValueError(f"drain_timeout_s 必须 >= 0，收到 {timeout!r}")
        self.drain_timeout_s = timeout
        self._clock = clock if clock is not None else time.monotonic
        if not callable(self._clock):
            raise TypeError("clock 必须可调用：Callable[[], float]")
        self._cond = threading.Condition(threading.RLock())
        self._shutting = False
        self._inflight = 0
        self._names: dict[str, int] = {}
        self._hooks: list[Callable[[], None]] = []
        self._hooks_done = False
        self._began_at: float | None = None
        #: 钩子抛的异常（不向外抛，留证据给运维查）
        self.hook_errors: list[str] = []

    # ── 状态 ────────────────────────────────────────────────
    def begin(self) -> None:
        """进入 draining：从这一刻起 `is_shutting_down()` 为 True，新请求应被拒。"""
        with self._cond:
            if not self._shutting:
                self._shutting = True
                try:
                    self._began_at = float(self._clock())
                except Exception:
                    self._began_at = None
            self._cond.notify_all()

    def is_shutting_down(self) -> bool:
        return self._shutting

    def inflight(self) -> int:
        with self._cond:
            return self._inflight

    def active_names(self) -> dict:
        """当前飞行中任务的按名计数（排障用：到底是 turn 卡住还是 tool 卡住）。"""
        with self._cond:
            return dict(self._names)

    @property
    def began_at(self) -> float | None:
        return self._began_at

    # ── 任务登记 ────────────────────────────────────────────
    @contextlib.contextmanager
    def track(self, name: str = "task") -> Iterator["ShutdownCoordinator"]:
        """登记一个飞行中任务。**异常原样传播**，只保证计数一定归零。"""
        key = str(name or "task")
        with self._cond:
            self._inflight += 1
            self._names[key] = self._names.get(key, 0) + 1
        try:
            yield self
        finally:
            with self._cond:
                if self._inflight > 0:
                    self._inflight -= 1
                left = self._names.get(key, 0)
                if left <= 1:
                    self._names.pop(key, None)
                else:
                    self._names[key] = left - 1
                self._cond.notify_all()

    # ── 清理钩子 ────────────────────────────────────────────
    def on_shutdown(self, fn: Callable[[], None]) -> None:
        """注册清理钩子（flush 指标 / flush 日志 / 关连接池）。按注册顺序执行。

        钩子抛异常只记到 `hook_errors`，不影响后续钩子——关停路径上
        一个坏钩子不能拦住其它 flush。
        """
        if not callable(fn):
            raise TypeError("on_shutdown 只接受可调用对象")
        with self._cond:
            already_done = self._hooks_done
            if not already_done:
                self._hooks.append(fn)
        if already_done:
            # 注册得比关停还晚：立刻补跑，别让"注册了但永远不会执行"成为静默偏差
            self._run_one(fn)

    def _run_one(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            self.hook_errors.append(f"{type(exc).__name__}: {exc}")

    def run_hooks(self) -> int:
        """执行（且只执行一次）全部钩子，返回执行条数。"""
        with self._cond:
            if self._hooks_done:
                return 0
            self._hooks_done = True
            hooks = list(self._hooks)
        for fn in hooks:
            self._run_one(fn)
        return len(hooks)

    # ── 排空 ────────────────────────────────────────────────
    def wait(self) -> dict:
        """等飞行中任务收口，然后执行清理钩子。

        返回 `{"drained": bool, "remaining": n, "elapsed_s": float}`。
        幂等：重复调用会立刻返回（钩子只跑一次）。
        """
        try:
            t0 = float(self._clock())
        except Exception:
            t0 = time.monotonic()
        # 真实时钟硬保险：注入的假时钟不走时，这里仍然能超时退出
        hard_deadline = time.monotonic() + max(0.0, self.drain_timeout_s) + 1.0
        with self._cond:
            while self._inflight > 0:
                try:
                    elapsed = float(self._clock()) - t0
                except Exception:
                    elapsed = 0.0
                if elapsed >= self.drain_timeout_s:
                    break
                if time.monotonic() >= hard_deadline:
                    break
                slice_s = min(0.05, max(0.001, self.drain_timeout_s - elapsed))
                self._cond.wait(timeout=slice_s)
            try:
                elapsed = max(0.0, float(self._clock()) - t0)
            except Exception:
                elapsed = 0.0
            remaining = self._inflight
        self.run_hooks()
        return {"drained": remaining == 0, "remaining": remaining,
                "elapsed_s": round(elapsed, 6)}


# ══════════════════════════════════════════════════════════════
# 四、装配：instrument_gateway
# ══════════════════════════════════════════════════════════════
#: 指标名固定（面板/告警按这四个名字写，不许改）
METRIC_TURNS = "turns_total"
METRIC_DURATION = "turn_duration_seconds"
METRIC_TOKENS = "turn_tokens_total"
METRIC_ERRORS = "turn_errors_total"

_METRIC_HELP = {
    METRIC_TURNS: "Total turns",
    METRIC_DURATION: "Turn latency",
    METRIC_TOKENS: "Tokens consumed by turns",
    METRIC_ERRORS: "Turn errors by reason",
}

#: 关停期间拒绝请求时返回的错误串（客户端据此判断可重试）
SHUTTING_DOWN = "shutting_down"


class InstrumentedGateway:
    """包住 Gateway：每个 turn 记日志 + 打指标 + 尊重 shutdown。

    组合而非继承（`__getattr__` 转发），这样 `core/gateway.py` 一个字符都不用改：
    接入层是**闸门**，不能为了观测去动它的代码路径（改了就要重验 29 个安全用例）。
    转发保证 `gw.audit_log` / `gw.snapshot()` / `gw.denied_total` 等照旧可用。
    """

    def __init__(self, gateway: Any, *, logger: JsonLogger,
                 metrics: Metrics, shutdown: ShutdownCoordinator) -> None:
        # 用 object.__setattr__ 之外的方式存也行；关键是 gateway 在实例字典里，
        # 这样 __getattr__ 只在属性确实不存在时才触发，不会递归。
        self.gateway = gateway
        self.logger = logger
        self.metrics = metrics
        self.shutdown = shutdown
        for short, help_text in _METRIC_HELP.items():
            try:
                metrics.describe(short, help_text)
            except Exception:
                pass

    def __getattr__(self, item: str) -> Any:
        return getattr(self.__dict__["gateway"], item)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"InstrumentedGateway({self.gateway!r})"

    # ── 一次性返回 ──────────────────────────────────────────
    def handle(self, request: Any) -> Any:
        """永不抛异常：关停 → 拒绝；内部故障 → ok=False（观测面 fail-open）。"""
        if self.shutdown.is_shutting_down():
            return self._rejected_turn(request, SHUTTING_DOWN)

        t0 = time.perf_counter()
        tenant = _tenant_of(request)
        try:
            with self.shutdown.track("turn"):
                result = self.gateway.handle(request)
        except Exception as exc:     # 契约说 gateway 不抛；真抛了也不能炸调用方
            result = None
            duration_s = max(0.0, time.perf_counter() - t0)

            def _note() -> None:
                self.metrics.counter(METRIC_TURNS, {"tenant": tenant, "ok": "false"})
                self.metrics.observe(METRIC_DURATION, duration_s)
                self.metrics.counter(
                    METRIC_ERRORS, {"reason": f"instrument_error:{type(exc).__name__}"})
                self.logger.error("turn.crashed", tenant=tenant, ok=False,
                                  error=f"{type(exc).__name__}: {exc}")

            self._safe(_note)
            return _make_turn_result(
                ok=False, error=f"internal_error: {type(exc).__name__}: {exc}")

        duration_s = _duration_of(result, t0)
        self._safe(lambda: self._record(result, tenant, duration_s))
        return result

    # ── 流式事件 ────────────────────────────────────────────
    def handle_stream(self, request: Any) -> Iterator[dict]:
        """转发事件流，顺带按 `completed` / `rejected` 事件记账。"""
        if self.shutdown.is_shutting_down():
            tenant = _tenant_of(request)
            self._count_rejection(tenant, SHUTTING_DOWN)
            yield {"type": "rejected", "seq": 0,
                   "data": {"error": SHUTTING_DOWN, "audit_id": ""}}
            return

        t0 = time.perf_counter()
        tenant = _tenant_of(request)
        try:
            with self.shutdown.track("turn_stream"):
                for event in self.gateway.handle_stream(request):
                    self._observe_event(event, tenant, t0)
                    yield event
        except Exception as exc:     # 流中崩了：给一条终态而不是让迭代器裸抛
            self.metrics.counter(METRIC_ERRORS,
                                 {"reason": f"stream_error:{type(exc).__name__}"})
            self.logger.error("turn.stream_crashed", tenant=tenant, ok=False,
                              error=f"{type(exc).__name__}: {exc}")
            yield {"type": "completed", "seq": 2,
                   "data": {"text": "", "steps": 0, "ok": False, "phase": "",
                            "term_reason": "", "error": "internal_error",
                            "audit_id": ""}}

    # ── 记账 ────────────────────────────────────────────────
    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        """观测面 fail-open：指标/日志自己炸了，不接管回合的结局。"""
        try:
            fn()
        except Exception:
            pass

    def _observe_event(self, event: Any, tenant: str, t0: float) -> None:
        if not isinstance(event, Mapping):
            return
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        if kind == "rejected":
            self._count_rejection(tenant, _code_only(str(data.get("error") or "unknown")))
            return
        if kind != "completed":
            return
        ok = bool(data.get("ok"))
        duration_s = max(0.0, time.perf_counter() - t0)
        self.metrics.counter(METRIC_TURNS,
                             {"tenant": tenant, "ok": "true" if ok else "false"})
        self.metrics.observe(METRIC_DURATION, duration_s)
        self.metrics.counter(METRIC_TOKENS, {"tenant": tenant}, value=0.0)
        self.logger.info("turn.completed", tenant=tenant, ok=ok,
                         phase=str(data.get("phase") or ""),
                         term_reason=str(data.get("term_reason") or ""),
                         steps=int(data.get("steps") or 0),
                         stream=True, error=_code_only(str(data.get("error") or "")))

    def _record(self, result: Any, tenant: str, duration_s: float) -> None:
        ok = bool(getattr(result, "ok", False))
        self.metrics.counter(METRIC_TURNS,
                             {"tenant": tenant, "ok": "true" if ok else "false"})
        self.metrics.observe(METRIC_DURATION, duration_s)
        self.metrics.counter(METRIC_TOKENS, {"tenant": tenant}, value=_tokens_of(result))
        code = ""
        if not ok:
            code = _code_only(str(getattr(result, "error", "") or "unknown"))
            self.metrics.counter(METRIC_ERRORS, {"reason": code})
        # 日志字段里**不放 goal 全文**（同 audit 纪律：只放可归因的元数据）
        fields = {
            "tenant": tenant,
            "ok": ok,
            "phase": str(getattr(result, "phase", "") or ""),
            "term_reason": str(getattr(result, "term_reason", "") or ""),
            "steps": int(getattr(result, "steps", 0) or 0),
            "latency_ms": round(duration_s * 1000.0, 3),
        }
        if code:
            fields["error"] = code
        if ok:
            self.logger.info("turn.completed", **fields)
        else:
            self.logger.error("turn.failed", **fields)

    def _count_rejection(self, tenant: str, reason: str) -> None:
        self._safe(lambda: self.metrics.counter(
            METRIC_TURNS, {"tenant": tenant, "ok": "false"}))
        self._safe(lambda: self.metrics.counter(
            METRIC_ERRORS, {"reason": reason or "unknown"}))
        self._safe(lambda: self.logger.warn("turn.rejected", tenant=tenant, ok=False,
                                            error=reason or "unknown"))

    def _rejected_turn(self, request: Any, reason: str) -> Any:
        tenant = _tenant_of(request)
        self._count_rejection(tenant, reason)
        return _make_turn_result(ok=False, error=reason)


def instrument_gateway(gateway: Any, *, logger: JsonLogger | None = None,
                       metrics: Metrics | None = None,
                       shutdown: ShutdownCoordinator | None = None) -> InstrumentedGateway:
    """便利装配：`instrument_gateway(Gateway(...))` 一行拿到带观测的接入层。

    三个参数都可省：缺省各自 new 一个（logger 写 stderr、metrics 空表、
    shutdown 从不关停）。已经包过的直接返回原对象，避免指标翻倍计数。
    """
    if isinstance(gateway, InstrumentedGateway):
        return gateway
    return InstrumentedGateway(
        gateway,
        logger=logger or JsonLogger(),
        metrics=metrics or Metrics(),
        shutdown=shutdown or ShutdownCoordinator(),
    )


# ── 装配用的小工具（全部 fail-open）──────────────────────────
def _tenant_of(request: Any) -> str:
    """尽力从请求里抠 tenant；抠不到就是空串。**绝不为归因去炸请求**。"""
    try:
        if isinstance(request, Mapping):
            principal = request.get("principal")
            if isinstance(principal, Mapping):
                tenant = principal.get("tenant")
                if isinstance(tenant, str):
                    return tenant.strip()
            elif principal is not None:
                tenant = getattr(principal, "tenant", "")
                if isinstance(tenant, str):
                    return tenant.strip()
    except Exception:
        pass
    return ""


def _code_only(error: str) -> str:
    """错误串 → 错误码（`bad_request: goal 必填` → `bad_request`）。

    详细 reason 可能夹带用户输入片段，指标 label 会被长期保存，不能进。
    """
    text = str(error or "").strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip()
    # 兜底：冒号前太长说明不是错误码（自由文本），按 unknown 处理
    return head if 0 < len(head) <= 64 else "unknown"


def _tokens_of(result: Any) -> float:
    """从 TurnResult.vertex 里读 token 消耗（`budget.token.used`）。读不到算 0。"""
    try:
        vertex = getattr(result, "vertex", None)
        if not isinstance(vertex, Mapping):
            return 0.0
        budget = vertex.get("budget")
        if not isinstance(budget, Mapping):
            return 0.0
        token = budget.get("token")
        if not isinstance(token, Mapping):
            return 0.0
        used = token.get("used")
        return float(used or 0)
    except Exception:
        return 0.0


def _duration_of(result: Any, t0: float) -> float:
    """优先用 Gateway 自己量的 latency_ms（不含包装开销），退化到本地计时。"""
    try:
        latency_ms = float(getattr(result, "latency_ms", 0.0) or 0.0)
    except Exception:
        latency_ms = 0.0
    if latency_ms > 0:
        return latency_ms / 1000.0
    return max(0.0, time.perf_counter() - t0)


def _make_turn_result(**kwargs: Any) -> Any:
    """惰性取 `TurnResult`（本模块不 import core.gateway，避免耦合与循环）。"""
    try:
        from core.gateway import TurnResult  # 局部导入：只在这一刻需要
        return TurnResult(**kwargs)
    except Exception:
        return _FallbackTurnResult(**kwargs)


class _FallbackTurnResult:
    """`core.gateway` 不可用时的等价占位（字段与 TurnResult 对齐，含 to_dict）。"""

    def __init__(self, ok: bool = False, text: str = "", phase: str = "",
                 term_reason: str = "", task_id: str = "", steps: int = 0,
                 partial: bool = False, error: str = "", audit_id: str = "",
                 latency_ms: float = 0.0, vertex: dict | None = None) -> None:
        self.ok = ok
        self.text = text
        self.phase = phase
        self.term_reason = term_reason
        self.task_id = task_id
        self.steps = steps
        self.partial = partial
        self.error = error
        self.audit_id = audit_id
        self.latency_ms = latency_ms
        self.vertex = dict(vertex or {})

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "phase": self.phase,
                "term_reason": self.term_reason, "task_id": self.task_id,
                "steps": self.steps, "partial": self.partial,
                "error": self.error, "audit_id": self.audit_id,
                "latency_ms": self.latency_ms, "vertex": dict(self.vertex)}


class QueueProgressSink:
    """`contracts.ProgressSink` 的队列实现：**有界、满了丢弃、绝不阻塞**。

    为什么有界：子 Agent 暴走会疯狂 emit（每步都播）。无界队列 = 内存泄漏，
    而且这种泄漏只在"长了就有问题"的时候才被发现。

    为什么丢弃而不是阻塞：播报是**观测面**。观测面拖死主链路是反的 ——
    宁可少播报几条，也要保证回合能跑完。丢弃数会在 `finish` 事件里带出去
    （`dropped`），让"播报不全"这件事**可见**，而不是静默。

    线程安全：`queue.Queue` 本身线程安全，多 worker 并发 `emit` 不需要额外锁。
    """

    def __init__(self, maxsize: int = 1024) -> None:
        import queue as _queue
        self._q: Any = _queue.Queue(maxsize=max(1, int(maxsize)))
        self.maxsize = max(1, int(maxsize))
        self._dropped = 0
        self._lock = threading.Lock()
        self._closed = False

    # ── ProgressSink ────────────────────────────────────────
    def emit(self, kind: str, data: Any = None) -> None:
        if self._closed:
            return
        item = {"kind": str(kind), "data": dict(data or {})}
        try:
            self._q.put_nowait(item)
        except Exception:                                      # noqa: BLE001 - 满 / 已关闭
            with self._lock:
                self._dropped += 1

    # ── 消费端（StreamingGateway 用）──────────────────────────
    def drain(self, limit: int = 64) -> list[dict]:
        """取走最多 `limit` 条。**永不阻塞** —— 队列空就返回空列表。"""
        out: list[dict] = []
        while len(out) < limit:
            try:
                out.append(self._q.get_nowait())
            except Exception:                                  # noqa: BLE001
                break
        return out

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def close(self) -> None:
        """关流后不再接收（避免已结束回合的事件串到下一个回合）。"""
        self._closed = True
