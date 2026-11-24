"""tests/test_observability.py — W30 生产可观测性收口自带用例（stdlib unittest）。

覆盖三件套 + 一个装配：
    JsonLogger            9 条：JSON 行 / 必含字段 / 级别过滤 / 脱敏（含大小写）/
                                sink fail-open / records / ts 毫秒 / 各级别字段
    Metrics              19 条（内核已换成官方 prometheus_client，所以只测行为契约）：
                               11-25 契约：counter 累加 / label 独立 / gauge 覆盖 / 桶 /
                               +Inf / sum+count / HELP+TYPE / 渲染字节稳定 / label 往返 /
                               分位 / 空渲染 / label 集合不一致 / 类型冲突 / prefix /
                               桶集合与 le 语义（其中 18/19/22/23/25 按实测库行为改写）
                               41-44 换库收益：官方 parser 解析 / 私有 registry 隔离 /
                               不污染全局 REGISTRY / 桶边界无浮点噪声
    ShutdownCoordinator   6 条：begin 置位 / track 计数 / wait 等线程 / 钩子执行 /
                               超时返回 remaining / 钩子抛异常 fail-open
    instrument_gateway    6 条：正常回合记指标与日志 / 关停拒绝不抛 / 日志可 grep /
                               __getattr__ 转发 / 流式关停 / 重复装配不翻倍

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_observability -v

只 import 本 worker 新建的 core/observability.py，以及**只读**依赖
core/gateway.py / plane/governance.py / contracts.py（用于装配用例的假 Loop）。
不碰任何别人的文件。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import Patch, Owner, Phase, Result, TermReason  # noqa: E402
from core.gateway import RUN_SCOPE, Gateway, TurnResult  # noqa: E402
from prometheus_client import REGISTRY, generate_latest  # noqa: E402
from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

from core.observability import (  # noqa: E402
    HISTOGRAM_BUCKETS, REDACTED, InstrumentedGateway, JsonLogger, Metrics,
    MetricsError, ShutdownCoordinator, instrument_gateway,
)
from plane.governance import Governance  # noqa: E402

GOAL = "把上季度的营收按区域汇总一下"


# ══════════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════════
class Collector:
    """收集 sink 输出（模拟日志文件/日志系统）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


class ExplodingSink:
    """每次都炸的 sink：验证 fail-open（日志系统挂了不能拖死业务）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, line: str) -> None:
        self.calls += 1
        raise RuntimeError("log backend down")


def principal_dict(**kw) -> dict:
    base = {"user_id": "u_1", "roles": ["engineer"], "tenant": "acme",
            "scopes": [RUN_SCOPE, "tool.*", "memory.*"]}
    base.update(kw)
    return base


def request(**kw) -> dict:
    base = {"goal": GOAL, "principal": principal_dict()}
    base.update(kw)
    return base


class FakeLoop:
    """假 Loop：返回预设 Result；可选地把 token 消耗写回 Context 预算。"""

    def __init__(self, result: Result | None = None, tokens: int = 0) -> None:
        self.result = result if result is not None else Result(
            phase=Phase.DONE, term_reason=TermReason.DONE,
            text="已按区域汇总完毕。", partial=False, steps=3)
        self.tokens = tokens
        self.run_calls = 0

    def run(self, goal, ctx, e):
        self.run_calls += 1
        if self.tokens and ctx is not None:
            ctx.apply(Patch("incr", "budget.token_used", Owner.LOOP, self.tokens))
        return self.result

    def resume(self, ctx, e, decision=True, reason=""):
        self.run_calls += 1
        return self.result


def make_gateway(loop=None) -> Gateway:
    return Gateway(lambda goal, tc: loop or FakeLoop(), governance=Governance())


def metric_line(text: str, name: str, labels: str = "") -> str | None:
    """从 exposition 文本里取某条 series 行（找不到返回 None）。"""
    prefix = f"{name}{labels} "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    return None


def metric_value(text: str, name: str, labels: str = "") -> float | None:
    line = metric_line(text, name, labels)
    if line is None:
        return None
    return float(line.rsplit(" ", 1)[1])


# ══════════════════════════════════════════════════════════════
# 一、JsonLogger（9）
# ══════════════════════════════════════════════════════════════
class TestJsonLogger(unittest.TestCase):
    def setUp(self) -> None:
        self.sink = Collector()
        self.log = JsonLogger(sink=self.sink, clock=lambda: 1_700_000_000.0)

    def test_01_one_log_is_one_json_line(self) -> None:
        self.log.info("turn.completed", tenant="acme", ok=True)
        self.assertEqual(len(self.sink.lines), 1)
        self.assertNotIn("\n", self.sink.lines[0])          # 一条就是一行
        parsed = json.loads(self.sink.lines[0])
        self.assertIsInstance(parsed, dict)

    def test_02_required_fields_present(self) -> None:
        rec = self.log.info("turn.completed", tenant="acme")
        for key in ("ts", "level", "event", "service"):
            self.assertIn(key, rec)
            self.assertIn(f'"{key}":', self.sink.lines[0])
        self.assertEqual(rec["service"], "agent-runtime")
        self.assertEqual(rec["event"], "turn.completed")

    def test_03_level_filter_drops_debug_at_info(self) -> None:
        self.log.set_level("INFO")
        self.log.debug("noise", detail="不该出现")
        self.log.info("kept")
        self.assertEqual(len(self.sink.lines), 1)
        self.assertEqual(json.loads(self.sink.lines[0])["event"], "kept")
        self.log.set_level("DEBUG")
        self.log.debug("now_visible")
        self.assertEqual(len(self.sink.lines), 2)

    def test_04_redacts_api_key(self) -> None:
        self.log.info("llm.call", api_key="sk-live-DEADBEEF", tenant="acme")
        line = self.sink.lines[0]
        self.assertNotIn("DEADBEEF", line)
        self.assertNotIn("sk-live", line)
        self.assertEqual(json.loads(line)["api_key"], REDACTED)

    def test_05_redact_is_case_insensitive(self) -> None:
        self.log.info("llm.call", API_KEY="sk-upper", Authorization="Bearer xyz",
                      Password="hunter2", note="ok")
        parsed = json.loads(self.sink.lines[0])
        self.assertEqual(parsed["API_KEY"], REDACTED)
        self.assertEqual(parsed["Authorization"], REDACTED)
        self.assertEqual(parsed["Password"], REDACTED)
        self.assertEqual(parsed["note"], "ok")
        for secret in ("sk-upper", "Bearer", "hunter2"):
            self.assertNotIn(secret, self.sink.lines[0])

    def test_06_sink_exception_is_fail_open(self) -> None:
        sink = ExplodingSink()
        log = JsonLogger(sink=sink)
        rec = log.error("turn.failed", tenant="acme", api_key="sk-x")   # 不抛
        self.assertEqual(rec["level"], "ERROR")
        self.assertEqual(sink.calls, 1)
        self.assertEqual(len(log.records()), 1)     # 记录仍然留在内存里

    def test_07_records_returns_emitted(self) -> None:
        self.log.info("a")
        self.log.warn("b")
        self.log.debug("filtered_out")
        recs = self.log.records()
        self.assertEqual([r["event"] for r in recs], ["a", "b"])
        recs.append({"forged": True})               # 返回副本，改不到内部
        self.assertEqual(len(self.log.records()), 2)

    def test_08_ts_is_iso8601_with_milliseconds(self) -> None:
        rec = self.log.info("tick")
        self.assertRegex(rec["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        # 注入固定时钟 → ts 可确定（日志测试不能依赖真实时间）
        self.assertEqual(rec["ts"], "2023-11-14T22:13:20.000Z")

    def test_09_each_level_maps_to_its_field(self) -> None:
        self.log.set_level("DEBUG")
        self.log.debug("d")
        self.log.info("i")
        self.log.warn("w")
        self.log.error("e")
        self.assertEqual([r["level"] for r in self.log.records()],
                         ["DEBUG", "INFO", "WARN", "ERROR"])
        self.assertEqual([json.loads(l)["level"] for l in self.sink.lines],
                         ["DEBUG", "INFO", "WARN", "ERROR"])

    def test_10_reserved_fields_cannot_be_spoofed(self) -> None:
        """业务字段撞上保留键时改名，不许伪造 level/ts（否则日志不可信）。"""
        rec = self.log.info("x", level="ERROR", ts="1970-01-01T00:00:00.000Z")
        self.assertEqual(rec["level"], "INFO")
        self.assertEqual(rec["ts"], "2023-11-14T22:13:20.000Z")
        self.assertEqual(rec["field_level"], "ERROR")


# ══════════════════════════════════════════════════════════════
# 二、Metrics（19）：换库后的行为契约，而不是库的内部实现
#     11-25  ← 原有契约（其中 18/19/22/23/25 按实测的库行为改写）
#     41-44  ← 换库带来的实际收益（官方 parser / 私有 registry / 全局隔离 / 桶文本）
# ══════════════════════════════════════════════════════════════
class TestMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.m = Metrics()

    # ── 必须保留的行为契约 ──────────────────────────────────
    def test_11_counter_accumulates(self) -> None:
        self.m.counter("turns_total")
        self.m.counter("turns_total")
        self.m.counter("turns_total", value=3)
        self.assertEqual(self.m.value("turns_total"), 5.0)
        self.assertIn("agent_runtime_turns_total 5", self.m.render())

    def test_12_counter_labels_are_independent(self) -> None:
        self.m.counter("turns_total", {"tenant": "acme", "ok": "true"})
        self.m.counter("turns_total", {"tenant": "acme", "ok": "true"})
        self.m.counter("turns_total", {"tenant": "globex", "ok": "true"})
        text = self.m.render()
        self.assertEqual(metric_value(text, "agent_runtime_turns_total",
                                      '{ok="true",tenant="acme"}'), 2.0)
        self.assertEqual(metric_value(text, "agent_runtime_turns_total",
                                      '{ok="true",tenant="globex"}'), 1.0)

    def test_13_gauge_overwrites_not_accumulates(self) -> None:
        self.m.gauge("inflight", 5)
        self.m.gauge("inflight", 2)
        self.assertEqual(self.m.value("inflight"), 2.0)
        self.assertIn("agent_runtime_inflight 2", self.m.render())

    def test_14_histogram_bucket_counts(self) -> None:
        for sample in (0.001, 0.02, 0.2, 2.0, 20.0):
            self.m.observe("turn_duration_seconds", sample)
        text = self.m.render()
        # le 是累计语义：0.005 只有 0.001；0.025 加 0.02；0.5 再加 0.2；…
        bucket = "agent_runtime_turn_duration_seconds_bucket"
        self.assertEqual(metric_value(text, bucket, '{le="0.005"}'), 1.0)
        self.assertEqual(metric_value(text, bucket, '{le="0.025"}'), 2.0)
        self.assertEqual(metric_value(text, bucket, '{le="0.5"}'), 3.0)
        self.assertEqual(metric_value(text, bucket, '{le="10.0"}'), 4.0)

    def test_15_inf_bucket_equals_total(self) -> None:
        for sample in (0.001, 0.2, 20.0, 99.0):
            self.m.observe("turn_duration_seconds", sample)
        text = self.m.render()
        inf = metric_value(text, "agent_runtime_turn_duration_seconds_bucket", '{le="+Inf"}')
        count = metric_value(text, "agent_runtime_turn_duration_seconds_count")
        self.assertEqual(inf, 4.0)
        self.assertEqual(inf, count)

    def test_16_sum_and_count(self) -> None:
        self.m.observe("turn_duration_seconds", 0.1)
        self.m.observe("turn_duration_seconds", 0.2)
        self.m.observe("turn_duration_seconds", 0.7)
        text = self.m.render()
        self.assertEqual(metric_value(text, "agent_runtime_turn_duration_seconds_count"), 3.0)
        self.assertAlmostEqual(
            metric_value(text, "agent_runtime_turn_duration_seconds_sum"), 1.0, places=9)

    def test_17_render_has_help_and_type(self) -> None:
        self.m.counter("turns_total", {"tenant": "acme"})
        self.m.observe("turn_duration_seconds", 0.05)
        text = self.m.render()
        self.assertIn("# HELP agent_runtime_turns_total ", text)
        self.assertIn("# TYPE agent_runtime_turns_total counter", text)
        self.assertIn("# TYPE agent_runtime_turn_duration_seconds histogram", text)
        # HELP/TYPE 每个 family 只出现一次
        self.assertEqual(text.count("# TYPE agent_runtime_turns_total"), 1)

    def test_18_render_is_byte_stable_within_process(self) -> None:
        """改写：确定性现在由库给，但**排序规则**变了，所以按实测结果钉死。

        实测（prometheus_client 0.26）：family 顺序 = **注册顺序**（registry 不排序），
        family 内 series 顺序 = **首次写入顺序**，label 名在行内按字母序。
        旧手写版是全排序的，因此"跨两次运行逐字节 diff"不再成立 —— 这是换库的真实代价。
        """
        for tenant in ("globex", "acme", "initech"):
            self.m.counter("turns_total", {"tenant": tenant})
        self.m.observe("turn_duration_seconds", 0.3)
        self.m.gauge("inflight", 1)
        first = self.m.render()
        self.assertEqual(first, self.m.render())
        self.assertEqual(first, self.m.render())

        families = re.findall(r"^# TYPE (\S+)", first, re.M)
        # 注册顺序；`_created` 时间戳 gauge 紧跟它的父 family（库默认行为）
        self.assertEqual(families, ["agent_runtime_turns_total",
                                    "agent_runtime_turns_created",
                                    "agent_runtime_turn_duration_seconds",
                                    "agent_runtime_turn_duration_seconds_created",
                                    "agent_runtime_inflight"])
        lines = [l for l in first.splitlines() if l.startswith("agent_runtime_turns_total{")]
        self.assertEqual([re.search(r'tenant="(\w+)"', l).group(1) for l in lines],
                         ["globex", "acme", "initech"])       # 首次写入顺序
        # 行内 label 名按字母序（库排序，跟手写版一致）
        self.assertTrue(all(l.startswith('agent_runtime_turns_total{tenant="') for l in lines))

    def test_19_label_values_survive_round_trip(self) -> None:
        """改写：转义现在由库保证，所以验"库写出 + 官方 parser 读回"一字不差。"""
        weird = 'a"b\\c\nd'
        self.m.counter("turns_total", {"tenant": weird, "note": "tab\there"})
        text = self.m.render()
        line = [l for l in text.splitlines() if l.startswith("agent_runtime_turns_total{")]
        self.assertEqual(len(line), 1)
        self.assertIn(r'a\"b\\c\nd', line[0])        # 反斜杠 / 双引号 / 换行都被转义

        samples = [s for fam in text_string_to_metric_families(text) for s in fam.samples
                   if s.name == "agent_runtime_turns_total"]
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].labels["tenant"], weird)      # 往返一致
        self.assertEqual(samples[0].labels["note"], "tab\there")  # 制表符原样保留（规范允许）
        self.assertEqual(samples[0].value, 1.0)

    def test_20_percentiles_are_sane(self) -> None:
        for sample in (0.001, 0.01, 0.02, 0.05, 0.2, 1.0):
            self.m.observe("turn_duration_seconds", sample)
        pct = self.m.percentiles("turn_duration_seconds")
        self.assertEqual(set(pct), {"p50", "p95", "p99"})
        self.assertLess(pct["p50"], pct["p99"])
        self.assertLessEqual(pct["p50"], pct["p95"])
        self.assertAlmostEqual(pct["p50"], 0.035, places=9)

    def test_21_empty_metrics_render_is_empty(self) -> None:
        self.assertEqual(self.m.render(), "")
        self.assertEqual(self.m.percentiles("nothing"), {"p50": 0.0, "p95": 0.0, "p99": 0.0})
        self.assertEqual(self.m.value("nothing"), 0.0)

    def test_22_label_set_mismatch_raises_metrics_error(self) -> None:
        """改写：错误类型 ValueError → MetricsError（仍是 ValueError 子类），消息点名 metric。"""
        self.m.counter("turns_total", {"tenant": "acme"})
        with self.assertRaises(MetricsError) as ctx:
            self.m.counter("turns_total", {"tenant": "acme", "ok": "true"})
        message = str(ctx.exception)
        self.assertIn("label", message)
        self.assertIn("agent_runtime_turns_total", message)      # 点名 metric
        self.assertIsInstance(ctx.exception, ValueError)         # 老调用方仍能捕到

    def test_23_type_conflict_raises_metrics_error(self) -> None:
        self.m.counter("turns_total")
        with self.assertRaises(MetricsError) as ctx:
            self.m.observe("turns_total", 0.1)
        self.assertIn("counter", str(ctx.exception))
        self.assertIn("agent_runtime_turns_total", str(ctx.exception))

    def test_24_prefix_is_not_doubled(self) -> None:
        """装配层用的名字已经带前缀（硬规格），不能渲染成 agent_runtime_agent_runtime_*。"""
        full = "agent_runtime_turns_total"
        self.m.counter(full, {"tenant": "acme"})
        text = self.m.render()
        self.assertIn("agent_runtime_turns_total{tenant=\"acme\"} 1", text)
        self.assertNotIn("agent_runtime_agent_runtime", text)

    def test_25_bucket_set_and_le_semantics(self) -> None:
        """桶集合 = HISTOGRAM_BUCKETS，升序，`+Inf` 收尾；落在边界上的样本进该桶。"""
        for bound in HISTOGRAM_BUCKETS:
            self.m.observe("turn_duration_seconds", bound)
        text = self.m.render()
        les = re.findall(r'le="([^"]+)"', text)
        self.assertEqual([float(le) for le in les if le != "+Inf"],
                         sorted(HISTOGRAM_BUCKETS))
        self.assertEqual(les[-1], "+Inf")
        # 恰好落在边界上的样本按 le 语义计入该桶
        self.assertEqual(metric_value(text, "agent_runtime_turn_duration_seconds_bucket",
                                      '{le="0.005"}'), 1.0)
        self.assertEqual(metric_value(text, "agent_runtime_turn_duration_seconds_bucket",
                                      '{le="+Inf"}'), float(len(HISTOGRAM_BUCKETS)))

    # ── 换库带来的实际收益 ──────────────────────────────────
    def test_41_render_parses_with_official_parser(self) -> None:
        """收益：不用自己证明格式对 —— 官方 parser 能完整解析回来。"""
        self.m.counter("turns_total", {"tenant": "acme", "ok": "true"})
        self.m.gauge("inflight", 3)
        for sample in (0.001, 0.2, 20.0):
            self.m.observe("turn_duration_seconds", sample)
        text = self.m.render()

        families = list(text_string_to_metric_families(text))
        by_name = {fam.name: fam for fam in families}
        # counter family 名会被库/parser 去掉 `_total`，sample 名保留全名
        self.assertEqual(by_name["agent_runtime_turns"].type, "counter")
        self.assertEqual(by_name["agent_runtime_inflight"].type, "gauge")
        self.assertEqual(by_name["agent_runtime_turn_duration_seconds"].type, "histogram")
        self.assertEqual(by_name["agent_runtime_turns"].documentation, "Agent runtime turns total")

        counter_samples = by_name["agent_runtime_turns"].samples
        self.assertEqual([(s.labels, s.value) for s in counter_samples
                          if s.name == "agent_runtime_turns_total"],
                         [({"tenant": "acme", "ok": "true"}, 1.0)])
        hist_samples = by_name["agent_runtime_turn_duration_seconds"].samples
        self.assertEqual({s.name for s in hist_samples},
                         {"agent_runtime_turn_duration_seconds_bucket",
                          "agent_runtime_turn_duration_seconds_sum",
                          "agent_runtime_turn_duration_seconds_count"})
        self.assertEqual([s.value for s in hist_samples
                          if s.name.endswith("_count")], [3.0])

    def test_42_two_instances_are_isolated(self) -> None:
        """收益：私有 registry —— 第二个实例不再撞 `Duplicated timeseries`。"""
        a = Metrics()
        b = Metrics()
        a.counter("turns_total")
        b.counter("turns_total")
        b.counter("turns_total")
        self.assertIn("agent_runtime_turns_total 1.0", a.render())
        self.assertIn("agent_runtime_turns_total 2.0", b.render())
        self.assertEqual(a.value("turns_total"), 1.0)
        self.assertEqual(b.value("turns_total"), 2.0)
        # 造多少个都不炸（旧实现若用全局 REGISTRY 会在这里抛 ValueError）
        for _ in range(5):
            Metrics().counter("turns_total")

    def test_43_global_registry_stays_clean(self) -> None:
        """收益：本模块的指标不会混进别人的 `generate_latest(REGISTRY)`。"""
        self.m.counter("isolation_probe_total", {"tenant": "acme"})
        self.m.gauge("isolation_probe_gauge", 1)
        self.assertIn("agent_runtime_isolation_probe_total", self.m.render())
        global_text = generate_latest(REGISTRY).decode()
        self.assertNotIn("agent_runtime_isolation_probe_total", global_text)
        self.assertNotIn("agent_runtime_isolation_probe_gauge", global_text)

    def test_44_bucket_boundary_text_has_no_float_noise(self) -> None:
        """收益：桶边界文本与 Go client 同源（floatToGoString），不是 naiv 的 str(float)。

        实测 0.26：`le="0.005"` / `le="2.5"`；整数边界用 repr → `le="1.0"`（Go client 是
        `le="1"`）。这里钉的是**库的真实输出**，库改格式时这条会先红。
        """
        for bound in HISTOGRAM_BUCKETS:
            self.m.observe("turn_duration_seconds", bound)
        text = self.m.render()
        self.assertIn('le="0.005"', text)
        self.assertIn('le="0.01"', text)
        self.assertIn('le="0.025"', text)
        self.assertIn('le="2.5"', text)
        self.assertIn('le="1.0"', text)
        for noisy in ("0.005000", "0.010000", "0.025000000000000001", "2.500000"):
            self.assertNotIn(noisy, text)


# ══════════════════════════════════════════════════════════════
# 三、ShutdownCoordinator（6）
# ══════════════════════════════════════════════════════════════
class TestShutdown(unittest.TestCase):
    def test_26_begin_flips_flag(self) -> None:
        sd = ShutdownCoordinator()
        self.assertFalse(sd.is_shutting_down())
        sd.begin()
        self.assertTrue(sd.is_shutting_down())

    def test_27_track_counts_inflight(self) -> None:
        sd = ShutdownCoordinator()
        self.assertEqual(sd.inflight(), 0)
        with sd.track("turn"):
            self.assertEqual(sd.inflight(), 1)
            with sd.track("tool"):
                self.assertEqual(sd.inflight(), 2)
                self.assertEqual(sd.active_names(), {"turn": 1, "tool": 1})
            self.assertEqual(sd.inflight(), 1)
        self.assertEqual(sd.inflight(), 0)
        self.assertEqual(sd.active_names(), {})

    def test_28_track_decrements_on_exception(self) -> None:
        sd = ShutdownCoordinator()
        with self.assertRaises(RuntimeError):
            with sd.track("turn"):
                raise RuntimeError("boom")
        self.assertEqual(sd.inflight(), 0)      # 计数不能因为异常泄漏

    def test_29_wait_waits_for_inflight_then_drains(self) -> None:
        sd = ShutdownCoordinator(drain_timeout_s=5.0)
        finished: list[str] = []

        def worker() -> None:
            with sd.track("turn"):
                time.sleep(0.15)
                finished.append("done")

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(400):                    # 等 worker 真的进了 track
            if sd.inflight() == 1:
                break
            time.sleep(0.005)
        self.assertEqual(sd.inflight(), 1)

        sd.begin()
        result = sd.wait()
        self.assertEqual(result["drained"], True)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(finished, ["done"])
        self.assertGreaterEqual(result["elapsed_s"], 0.05)
        thread.join(timeout=2.0)

    def test_30_wait_timeout_reports_remaining(self) -> None:
        sd = ShutdownCoordinator(drain_timeout_s=0.05)
        sd.begin()
        held = sd.track("stuck")
        held.__enter__()
        try:
            result = sd.wait()
        finally:
            held.__exit__(None, None, None)
        self.assertEqual(result["drained"], False)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(sd.inflight(), 0)      # 退出后计数归零

    def test_31_hooks_run_once_and_fail_open(self) -> None:
        sd = ShutdownCoordinator(drain_timeout_s=0.05)
        order: list[str] = []

        def bad() -> None:
            raise RuntimeError("flush backend down")

        sd.on_shutdown(lambda: order.append("first"))
        sd.on_shutdown(bad)
        sd.on_shutdown(lambda: order.append("third"))
        sd.begin()
        first = sd.wait()
        sd.wait()                               # 幂等：钩子不重复执行
        self.assertEqual(order, ["first", "third"])
        self.assertEqual(first["drained"], True)
        self.assertEqual(len(sd.hook_errors), 1)
        self.assertIn("RuntimeError", sd.hook_errors[0])

    def test_32_hook_registered_after_wait_runs_immediately(self) -> None:
        sd = ShutdownCoordinator(drain_timeout_s=0.05)
        sd.begin()
        sd.wait()
        late: list[str] = []
        sd.on_shutdown(lambda: late.append("late"))
        self.assertEqual(late, ["late"])        # 不让"注册了但永不执行"变成静默偏差


# ══════════════════════════════════════════════════════════════
# 四、instrument_gateway（6）
# ══════════════════════════════════════════════════════════════
class TestInstrumentGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.sink = Collector()
        self.log = JsonLogger(sink=self.sink)
        self.metrics = Metrics()
        self.sd = ShutdownCoordinator(drain_timeout_s=1.0)
        self.loop = FakeLoop(tokens=42)
        self.gw = instrument_gateway(
            make_gateway(self.loop), logger=self.log, metrics=self.metrics,
            shutdown=self.sd)

    def test_33_normal_turn_records_metrics(self) -> None:
        res = self.gw.handle(request())
        self.assertTrue(res.ok, res.error)
        text = self.metrics.render()
        self.assertEqual(metric_value(text, "agent_runtime_turns_total",
                                      '{ok="true",tenant="acme"}'), 1.0)
        self.assertEqual(metric_value(text, "agent_runtime_turn_duration_seconds_count"), 1.0)
        self.assertEqual(metric_value(text, "agent_runtime_turn_tokens_total",
                                      '{tenant="acme"}'), 42.0)
        self.assertIsNone(metric_line(text, "agent_runtime_turn_errors_total",
                                      '{reason="unknown"}'))
        self.assertEqual(self.metrics.value("turns_total",
                                            {"tenant": "acme", "ok": "true"}), 1.0)

    def test_34_shutdown_rejects_without_raising(self) -> None:
        self.sd.begin()
        res = self.gw.handle(request())
        self.assertIsInstance(res, TurnResult)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "shutting_down")
        self.assertEqual(self.loop.run_calls, 0)    # 没进 Loop
        self.assertEqual(len(self.gw.audit_log), 0)  # 也没写审计（没到接入层）
        text = self.metrics.render()
        self.assertEqual(metric_value(text, "agent_runtime_turns_total",
                                      '{ok="false",tenant="acme"}'), 1.0)
        self.assertEqual(metric_value(text, "agent_runtime_turn_errors_total",
                                      '{reason="shutting_down"}'), 1.0)

    def test_35_log_has_tenant_and_ok_for_grep(self) -> None:
        self.gw.handle(request())
        recs = self.log.records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["tenant"], "acme")
        self.assertIs(rec["ok"], True)
        self.assertEqual(rec["event"], "turn.completed")
        # 真能 grep：整行里出现 `"tenant": "acme"` 与 `"ok": true`
        line = self.sink.lines[0]
        self.assertIn('"tenant": "acme"', line)
        self.assertIn('"ok": true', line)
        self.assertIn('"level": "INFO"', line)

    def test_36_failed_turn_logs_error_with_code_only(self) -> None:
        failing = FakeLoop(result=None)
        gw = instrument_gateway(make_gateway(failing), logger=self.log,
                                metrics=self.metrics, shutdown=self.sd)
        res = gw.handle({"goal": GOAL})          # 缺 principal → 拒绝
        self.assertFalse(res.ok)
        rec = self.log.records()[-1]
        self.assertEqual(rec["level"], "ERROR")
        self.assertEqual(rec["tenant"], "")      # 坏请求也要能归因，但不炸
        self.assertTrue(rec["error"].startswith("unauthorized")
                        or rec["error"].startswith("bad_request"), rec["error"])
        # 指标 label 只放错误码，不放 reason 全文（会被长期保存）
        self.assertIn('reason="bad_request"', self.metrics.render())
        self.assertNotIn("principal", self.metrics.render())

    def test_37_getattr_forwards_to_gateway(self) -> None:
        self.assertTrue(hasattr(self.gw, "snapshot"))
        self.assertEqual(self.gw.rate_limit_per_minute, 60)
        self.gw.handle(request())
        self.assertEqual(len(self.gw.audit_log), 1)      # 转发到真 Gateway 的列表
        self.assertEqual(self.gw.snapshot()["audit_count"], 1)

    def test_38_stream_events_pass_through_and_shutdown_rejects(self) -> None:
        events = list(self.gw.handle_stream(request()))
        self.assertEqual([e["type"] for e in events], ["accepted", "started", "completed"])
        self.assertEqual(metric_value(self.metrics.render(),
                                      "agent_runtime_turns_total",
                                      '{ok="true",tenant="acme"}'), 1.0)

        self.sd.begin()
        rejected = list(self.gw.handle_stream(request()))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["type"], "rejected")
        self.assertEqual(rejected[0]["data"]["error"], "shutting_down")

    def test_39_instrument_twice_is_idempotent(self) -> None:
        again = instrument_gateway(self.gw)
        self.assertIsInstance(again, InstrumentedGateway)
        self.assertIs(again, self.gw)            # 指标不能翻倍计数
        again.handle(request())
        self.assertEqual(metric_value(self.metrics.render(), "agent_runtime_turns_total",
                                      '{ok="true",tenant="acme"}'), 1.0)

    def test_40_inflight_is_tracked_around_turn(self) -> None:
        seen: list[int] = []
        inner = FakeLoop()

        def run(goal, ctx, e):
            seen.append(self.sd.inflight())      # 回合执行期间必须有飞行中登记
            return inner.run(goal, ctx, e)

        gw = instrument_gateway(make_gateway(inner), logger=self.log,
                                metrics=self.metrics, shutdown=self.sd)
        gw.gateway.loop_factory = lambda goal, tc: type(
            "L", (), {"run": staticmethod(run), "resume": staticmethod(run)})()
        gw.handle(request())
        self.assertEqual(seen, [1])
        self.assertEqual(self.sd.inflight(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
