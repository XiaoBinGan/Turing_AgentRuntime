"""plane/tool.py — Tool Plane（W3）。

Agent 调工具的**唯一**副作用出口。所有调用必须穿过 `Dispatcher.invoke`：
绕过它 = 权限 / 审计 / 幂等 / 限流 / 预算 / 脱敏全部丢失。

执行链（顺序固定，任一步失败立即返回 ToolOutcome，绝不抛异常给调用方）：

    1. 找不到工具                → failed("unknown tool: X")
    2. 入参契约（manifest.input_schema）
       校验失败                  → failed("invalid args: ...", retryable=True)
       schema 自身崩了            → failed("input schema error: ...")，fail-closed
    3. 静态闸门（Policy）
       a. principal 缺 required_scope           → denied
       b. Policy.check 命中静态规则             → denied
       c. risk_tier >= T3                        → denied（T3 不可确认）
       d. risk_tier == T2 且无 approval          → needs_confirmation（不是 denied）
    4. 幂等：把 (name, args_hash, idempotency_key) 的已成功结果直接返回，deduped=True
    5. 限流：同 turn 内该工具调用数 >= per_turn_limit → denied
    6. 预算：tool_calls_used += 1，超预算 → failed("budget: ...") 且回滚计数
    7. Secret 注入：manifest.secrets 从 secrets 池取值，只传给 fn，绝不进 result
    8. 执行：ThreadPoolExecutor + future.result(timeout=timeout_ms)
       超时 → timeout(elapsed)；fn 抛异常 → failed(str(exc), retryable=manifest.idempotent)
    9. Normalizer：类型校验 → 大小截断 → 去重 → 脱敏 → ok(result)

**为什么入参校验排在 approval 之前**：参数都不合法的调用不该去打扰人做审批 ——
审批是给人的决策界面，先让它合格再上桌。放在这一位还有两个附加收益：
  * 失败不消耗 tool_calls 预算，也不会把审批队列灌满无效请求；
  * 错误信息里带具体字段名（``args.limit: 未声明的字段``），模型下一轮就能改对，
    而不是拿到一个"参数看起来没错但结果是空"的静默错误。

设计取舍见 README 段注释（幂等键、needs_confirmation）。
"""
from __future__ import annotations

import concurrent.futures as _futures
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

# 允许在任意工作目录下 import（demo/run.py 可能从别处启动）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import (  # noqa: E402
    ExecCtx,
    Principal,
    RiskTier,
    ToolManifest,
    ToolOutcome,
    ToolResult,
    args_hash,
    now_ms,
    validate_against_schema,
)

ToolFn = Callable[[dict, ExecCtx], Any]


# ══════════════════════════════════════════════════════════════
# 注册表（模拟 MCP 的 tools/list）
# ══════════════════════════════════════════════════════════════
class ToolRegistry:
    """工具清单 + 实现函数。

    `manifests()` 是 Builder 生成工具描述的**唯一**来源：模型只能看到注册过的工具，
    所以「模型幻觉出一个工具名」在 Dispatcher 第 1 步就被挡掉。
    """

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolManifest, ToolFn]] = {}
        #: **热加载的锁**。`replace_all` 会整体换掉 `_tools`，而并行的
        #: `Loop(parallel_tools=True)` 可能正在 `get()` / `manifests()`。
        #: 用 RLock：`replace_all` 内部要调 `register`（可重入）。
        #:
        #: 不加锁的后果是读到一个**半换完**的表：一部分新工具、一部分旧工具，
        #: 而这一瞬间模型可能正好看到一个已删除工具的描述。
        self._lock = threading.RLock()

    def register(self, manifest: ToolManifest, fn: ToolFn) -> None:
        if not isinstance(manifest, ToolManifest):
            raise TypeError("manifest 必须是 ToolManifest")
        if not manifest.name:
            raise ValueError("manifest.name 不能为空")
        if not callable(fn):
            raise TypeError(f"tool {manifest.name} 的实现不可调用")
        with self._lock:
            if manifest.name in self._tools:
                # 同名注册 = 静默覆盖 = 权限被换掉都不知情，直接拒绝
                raise ValueError(f"tool 已注册: {manifest.name}")
            self._tools[manifest.name] = (manifest, fn)

    def replace_all(self, items: Iterable[tuple[ToolManifest, ToolFn]]) -> dict[str, Any]:
        """**原地**整体替换工具表（热加载入口）。

        为什么是「原地」而不是「新建一个 ToolRegistry」：`Dispatcher` 在构造时
        持有了 registry 的引用，`_loop_factory` 又闭包捕获了那个 dispatcher。
        新建对象后替换 `app.state` 只影响之后创建的 Loop，**正在跑的 Loop
        仍然用旧表** —— 那种"热加载看起来成功了"更难查。

        先校验全部条目再替换：中途抛错会留下半张表。

        返回 `{"added": [...], "removed": [...], "changed": [...]}` 供端点回报 diff
        （运维需要知道"我改的那一项到底生效了没"）。
        """
        incoming: dict[str, tuple[ToolManifest, ToolFn]] = {}
        for manifest, fn in items:
            if not isinstance(manifest, ToolManifest):
                raise TypeError("manifest 必须是 ToolManifest")
            if not manifest.name:
                raise ValueError("manifest.name 不能为空")
            if not callable(fn):
                raise TypeError(f"tool {manifest.name} 的实现不可调用")
            if manifest.name in incoming:
                raise ValueError(f"工具名重复: {manifest.name}")
            incoming[manifest.name] = (manifest, fn)

        with self._lock:
            old = dict(self._tools)
            added = sorted(set(incoming) - set(old))
            removed = sorted(set(old) - set(incoming))
            changed = sorted(
                name for name in set(old) & set(incoming)
                if old[name][0] != incoming[name][0]
            )
            # 单次赋值 = 原子换表：读者要么看到旧表要么看到新表，看不到中间态。
            self._tools = incoming
        return {"added": added, "removed": removed, "changed": changed,
                "total": len(incoming)}

    def get(self, name: str) -> tuple[ToolManifest, ToolFn] | None:
        return self._tools.get(name)

    def manifests(self) -> list[ToolManifest]:
        return [m for m, _ in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


# ══════════════════════════════════════════════════════════════
# 结果规范化
# ══════════════════════════════════════════════════════════════
_JSON_SCALARS = (dict, list, str, int, float, bool, type(None))


def json_text(obj: Any) -> str:
    """稳定序列化：不可序列化的对象降级成 str，绝不为序列化失败买单。"""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=False)
    except (TypeError, ValueError):
        return repr(obj)


class Normalizer:
    """四道处理：类型校验 → 大小上限截断 → 去重 → 脱敏。

    截断后的原文落盘到 `artifact_dir/{uuid}.json`，由 OWNER_TABLE 里
    `Owner.NORMALIZER` 记录的 `artifacts` 字段引用，模型只拿到预览 + 引用路径。
    """

    REDACTION = "***REDACTED***"
    PREVIEW_CHARS = 4096

    # 敏感信息正则：顺序有意义（整块私钥先吃掉，再吃残余 header）
    _PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"-----BEGIN[^-]{0,60}PRIVATE KEY-----[\s\S]*?"
                   r"-----END[^-]{0,60}PRIVATE KEY-----"),
        re.compile(r"-----BEGIN[^-]{0,60}PRIVATE KEY-----"),
        re.compile(r"sk-[A-Za-z0-9]{16,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"""(?i)password["']?\s*[:=]\s*["']?[^\s"',}]+"""),
    )

    # 键值分离形态：{"password": "hunter2"} 里键和值是两个字符串，
    # 上面的 `password\s*[:=]\s*\S+` 匹配不到，所以按敏感键名再兜一层。
    _SENSITIVE_KEY = re.compile(
        r"^(?:[a-z0-9]+[_.-])*(?:password|passwd|pwd|secret|token|api[_-]?key|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret|credential|"
        r"authorization|passphrase)s?$",
        re.IGNORECASE,
    )

    def __init__(self, max_bytes: int = 65536, artifact_dir: str | None = None) -> None:
        self.max_bytes = int(max_bytes)
        self.artifact_dir = artifact_dir or os.path.join(
            tempfile.gettempdir(), "agent-runtime-artifacts")
        self.artifacts: list[dict] = []      # 每次落盘的记录（供 Result.artifacts 用）

    # ── 对外入口 ────────────────────────────────────────────────
    def normalize(self, tool: str, raw: Any, manifest: ToolManifest) -> ToolResult:
        t0 = now_ms()

        # ① 类型校验：非法类型先降级成 str（保证后续三道永远有东西可处理）
        raw = self._coerce(raw)
        src = json_text(raw)
        bytes_in = len(src.encode("utf-8"))

        # ② 大小上限
        limit = int(manifest.max_result_bytes) if manifest.max_result_bytes > 0 else self.max_bytes
        payload: Any = raw
        truncated = False
        carry = 0                      # 落盘前脱敏的命中数，最后并入 redacted
        if len(src.encode("utf-8")) > limit:
            # 注意：artifact 是交给下游/人看的原文，不能成为脱敏的旁路。
            # 所以先对序列化文本脱敏，预览与落盘共用这份安全文本（payload 结构仍是截断壳）。
            safe_src, carry = self._redact_text(src)
            ref = self._spill(tool, safe_src)
            payload = {
                "_truncated": True,
                "preview": safe_src[: self.PREVIEW_CHARS],
                "artifact_ref": ref,
                "total_bytes": bytes_in,
            }
            truncated = True

        # ③ 去重（按 dict 元素的 id）
        payload, _removed = self._dedupe(payload)

        # ④ 脱敏
        payload, redacted = self._redact(payload)
        redacted += carry

        return ToolResult(
            tool=tool,
            payload=payload,
            bytes_in=bytes_in,
            bytes_out=len(json_text(payload).encode("utf-8")),
            truncated=truncated,
            deduped=False,
            redacted=redacted,
            duration_ms=now_ms() - t0,
        )

    # ── ① 类型校验 ──────────────────────────────────────────────
    @staticmethod
    def _coerce(raw: Any) -> Any:
        if isinstance(raw, _JSON_SCALARS):
            return raw
        if isinstance(raw, (bytes, bytearray)):
            try:
                return bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                return repr(bytes(raw))
        if isinstance(raw, tuple):
            return list(raw)
        if isinstance(raw, (set, frozenset)):
            return sorted(raw, key=repr)
        return str(raw)

    # ── ② 大小上限 ──────────────────────────────────────────────
    def _spill(self, tool: str, text: str) -> str:
        os.makedirs(self.artifact_dir, exist_ok=True)
        path = os.path.join(self.artifact_dir, f"{uuid.uuid4().hex}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.artifacts.append({"path": path, "bytes": len(text.encode("utf-8")), "tool": tool})
        return path

    # ── ③ 去重 ──────────────────────────────────────────────────
    def _dedupe(self, node: Any) -> tuple[Any, int]:
        if isinstance(node, dict):
            out: dict = {}
            total = 0
            for k, v in node.items():
                vv, n = self._dedupe(v)
                out[k] = vv
                total += n
            return out, total
        if isinstance(node, list):
            items: list = []
            total = 0
            for it in node:
                vv, n = self._dedupe(it)
                items.append(vv)
                total += n
            # 只有「元素全是带 id 的 dict」才代表结果集，才做 id 去重
            if items and all(isinstance(x, dict) and "id" in x for x in items):
                seen: set[str] = set()
                kept: list = []
                for x in items:
                    key = _key_of(x["id"])
                    if key in seen:
                        total += 1
                        continue
                    seen.add(key)
                    kept.append(x)
                items = kept
            return items, total
        return node, 0

    # ── ④ 脱敏 ──────────────────────────────────────────────────
    def _redact(self, node: Any) -> tuple[Any, int]:
        if isinstance(node, str):
            return self._redact_text(node)
        if isinstance(node, list):
            out_list: list = []
            total = 0
            for it in node:
                vv, n = self._redact(it)
                out_list.append(vv)
                total += n
            return out_list, total
        if isinstance(node, dict):
            out_dict: dict = {}
            total = 0
            for k, v in node.items():
                nk = k
                if isinstance(k, str):
                    nk, n = self._redact_text(k)
                    total += n
                vv, n = self._redact(v)
                total += n
                # 敏感键名 + 标量值 → 整值抹掉（保留结构，别把 dict/list 打成字符串）
                if (isinstance(nk, str) and self._SENSITIVE_KEY.match(nk.strip())
                        and isinstance(vv, (str, int, float))
                        and not isinstance(vv, bool)
                        and vv != self.REDACTION):
                    total += 1
                    vv = self.REDACTION
                out_dict[nk] = vv
            return out_dict, total
        return node, 0

    def _redact_text(self, text: str) -> tuple[str, int]:
        hits = 0
        for pat in self._PATTERNS:
            text, n = pat.subn(self.REDACTION, text)
            hits += n
        return text, hits


def _key_of(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


# ══════════════════════════════════════════════════════════════
# 静态策略闸门
# ══════════════════════════════════════════════════════════════
class Policy:
    """静态规则拦截：不依赖模型自觉，也不依赖 approval 流程。

    `check` 返回 None = 放行；返回字符串 = 拒绝原因（Dispatcher 转成 denied）。
    """

    _RM_RF = re.compile(r"\brm\s+(?:-{1,2}[\w-]+\s+)*-[a-z]*r[a-z]*f\b")
    _RM_FR = re.compile(r"\brm\s+-[a-z]*f[a-z]*r\b")
    _PIPE_SHELL = re.compile(
        r"\b(?:curl|wget)\b[^\n;|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|python3?|perl|ruby)\b")
    _SUDO = re.compile(r"(?:^|[^\w])sudo(?:[^\w]|$)")
    _ETC_WRITE = re.compile(
        r"(?:>|>>|\btee\b|\bwrite\b|\bchmod\b|\bchown\b|\brm\b|\bmv\b|\bsed\s+-i\b"
        r"|\bcp\b|\bdrop\b|\btruncate\b)")
    _DROP_TABLE = re.compile(r"\bdrop\s+table\b")
    _DROP_DB = re.compile(r"\bdrop\s+(?:database|schema)\b")

    def check(self, principal: Principal, name: str,
              args: dict, manifest: ToolManifest) -> str | None:
        text = f"{name} {json_text(args)}".lower()

        if self._RM_RF.search(text) or self._RM_FR.search(text):
            return "destructive command: rm -rf"
        if self._PIPE_SHELL.search(text):
            return "remote code execution pipeline: download | shell"
        if self._SUDO.search(text):
            return "privilege escalation: sudo"
        if "/etc/" in text and self._ETC_WRITE.search(text):
            return "write attempt under /etc/"
        if self._DROP_TABLE.search(text):
            return "destructive SQL: DROP TABLE"
        if self._DROP_DB.search(text):
            return "destructive SQL: DROP DATABASE"
        return None


# ══════════════════════════════════════════════════════════════
# 最小沙箱执行器
# ══════════════════════════════════════════════════════════════
class SubprocessSandbox:
    """`shell=False` + 可执行白名单 + 元字符黑名单 + 输出截断。"""

    SHELL_META = frozenset(";|&$><`\n\r")
    OUT_LIMIT = 8192

    def __init__(self, allowlist: list[str] | None = None, timeout_ms: int = 3000) -> None:
        self.allowlist = list(allowlist) if allowlist is not None else [
            "echo", "ls", "cat", "python3"]
        self.timeout_ms = int(timeout_ms)

    def run(self, argv: list[str]) -> dict:
        if not argv:
            raise ValueError("argv 不能为空")
        if not all(isinstance(a, str) for a in argv):
            raise PermissionError("argv 元素必须是 str（禁止字节串/对象逃逸）")

        exe = argv[0]
        allowed = {os.path.basename(a) for a in self.allowlist}
        if exe not in allowed and os.path.basename(exe) not in allowed:
            raise PermissionError(f"executable 不在白名单: {exe!r}")
        if os.path.sep in exe:      # 白名单按 basename 比对，禁止借此执行同名的任意路径
            raise PermissionError(f"executable 必须是裸命令名: {exe!r}")

        for i, arg in enumerate(argv):
            bad = self.SHELL_META.intersection(arg)
            if bad:
                raise PermissionError(
                    f"argv[{i}] 含 shell 元字符 {sorted(bad)}: {arg!r}")

        t0 = now_ms()
        try:
            proc = subprocess.run(
                argv, shell=False, capture_output=True, text=True,
                timeout=max(0.001, self.timeout_ms / 1000.0),
                env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                     "LANG": "C.UTF-8"},
            )
            code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            code = 124
            out = _as_text(exc.stdout)
            err = f"timeout after {self.timeout_ms}ms"
        return {
            "exit_code": int(code),
            "stdout": _clip(out, self.OUT_LIMIT),
            "stderr": _clip(err, self.OUT_LIMIT),
            "duration_ms": round(now_ms() - t0, 3),
        }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


# ══════════════════════════════════════════════════════════════
# 调度器
# ══════════════════════════════════════════════════════════════
class Dispatcher:
    """唯一的副作用出口。

    幂等键设计：`(name, args_hash(args), e.idempotency_key or "turn:<turn_id>")`。
    - 带上 tool 名，避免两个工具撞同一个 args 形状时互相污染；
    - key 为空时退化成「本 turn 内同参数只跑一次」——这是重试语义的正确默认值，
      因为重试总在同一 turn 里发生；跨 turn 的幂等必须由上层显式给 idempotency_key
      （比如重放/补偿场景的 `task:<id>:step:<n>`）。
    只有 manifest.idempotent 的工具才读写缓存：非幂等工具（发消息、扣款）重复执行
    就是真的重复副作用，缓存反而会骗模型「已经发过了」。
    """

    CACHE_LIMIT = 256

    def __init__(
        self,
        registry: ToolRegistry,
        policy: Policy | None = None,
        secrets: dict[str, str] | None = None,
        normalizer: Normalizer | None = None,
        tracer: Any = None,
        artifact_dir: str = "/tmp/agent-runtime-artifacts",
    ) -> None:
        self.registry = registry
        self.policy = Policy() if policy is None else policy
        self.secrets: dict[str, str] = dict(secrets or {})
        # 显式传入的 normalizer 保留它自己的 max_bytes / artifact_dir 配置
        if normalizer is not None:
            self.normalizer = normalizer
        else:
            self.normalizer = Normalizer(artifact_dir=artifact_dir)
        self.tracer = tracer
        self.artifact_dir = artifact_dir
        self._cache: dict[tuple[str, str, str], ToolResult] = {}
        self._turn_calls: dict[tuple[str, str], int] = {}
        #: **记账锁**：并行调用（`Loop(parallel_tools=True)`）下，多个线程会同时
        #: 碰这三处可变状态 —— 幂等缓存、按 turn 的调用计数、以及 ExecCtx 的预算计数器。
        #:
        #: 不加锁的后果是**静默失效**：
        #:   * `_turn_calls[key] = used + 1` 丢失更新 → `per_turn_limit` 被绕过
        #:     （限 10 次的工具能被调 40 次）
        #:   * 幂等缓存 check-then-act 竞争 → 同一个幂等工具被真跑两次（有副作用时是事故）
        #:   * `budget.tool_calls_used += 1` 非原子 → 预算闸被削弱
        #:
        #: 锁**只保护记账**，不包住 handler 执行（否则并行退化成串行）。
        self._lock = threading.RLock()
        self.denied_log: list[dict] = []      # 对应上下文里的 denied_log（Owner.DISPATCHER）
        self.trace_log: list[dict] = []       # 无 tracer 时的轻量审计

    # ── 对外入口 ────────────────────────────────────────────────
    def capabilities(self) -> frozenset[str]:
        """本 Dispatcher **真的**提供哪些保障。

        这份申报必须与实际行为一致 —— `tests/test_dispatch_capabilities.py`
        逐项实测：申报了某项，就要有对应的行为测试跟着；反之亦然。
        """
        from contracts import DISPATCH_CAPABILITIES
        return frozenset(DISPATCH_CAPABILITIES)

    def manifests(self) -> list[ToolManifest]:
        """当前注册的全部工具清单（Gateway 用它构造 Prompt 的工具表）。"""
        getter = getattr(self.registry, "manifests", None)
        return list(getter()) if callable(getter) else []

    def invoke(self, name: str, args: dict, e: ExecCtx) -> ToolOutcome:
        t0 = now_ms()
        try:
            # 模型可能吐出 list/None/怪类型：先收口成 dict，收不了就当场失败
            args = dict(args or {})
            digest = args_hash(args)
        except Exception as exc:
            outcome = ToolOutcome.failed(
                f"invalid args for {name}: {type(exc).__name__}: {exc}")
            self.trace_log.append({"tool": str(name), "args_hash": "", "kind": outcome.kind,
                                   "reason": outcome.reason,
                                   "duration_ms": round(now_ms() - t0, 3)})
            return outcome
        try:
            with self._span("tool.invoke", {"tool": name, "args_hash": digest,
                                            "turn": e.turn_id, "depth": e.depth}):
                outcome = self._invoke(name, args, digest, e, t0)
        except Exception as exc:  # 最后一道保险：Dispatcher 永不向上抛
            outcome = ToolOutcome.failed(f"dispatcher error: {type(exc).__name__}: {exc}")
        self.trace_log.append({
            "tool": name, "args_hash": digest, "kind": outcome.kind,
            "reason": outcome.reason, "duration_ms": round(now_ms() - t0, 3),
        })
        if outcome.kind in ("denied", "needs_confirmation"):
            self.denied_log.append({"tool": name, "args_hash": digest,
                                    "kind": outcome.kind, "reason": outcome.reason,
                                    "turn_id": e.turn_id})
        return outcome

    # ── 主链 ────────────────────────────────────────────────────
    def _invoke(self, name: str, args: dict, digest: str,
                e: ExecCtx, t0: float) -> ToolOutcome:
        # ① 工具存在性
        entry = self.registry.get(name)
        if entry is None:
            return ToolOutcome.failed(f"unknown tool: {name}")
        manifest, fn = entry

        # ② 入参契约：**在 scope / policy / risk_tier / approval 之前**
        # 语义选择：
        #   * input_schema=None → 不校验（老工具没有声明契约，不许对它们报错）
        #   * 有契约 → 用 contracts 的校验器（不在这里重写第二套规则）
        #   * 校验器自己崩了 → fail-closed（schema 写坏不能变成放行）
        # retryable=True 是刻意的：模型看到"哪个字段错了"就能改参数重试，
        # 这是把「静默错误」换回「可纠正错误」的唯一通路。
        schema = getattr(manifest, "input_schema", None)
        if schema is not None:
            try:
                errs = validate_against_schema(schema, args)
            except Exception as exc:            # noqa: BLE001 — schema 写坏：fail closed
                return ToolOutcome.failed(
                    f"input schema error: {type(exc).__name__}: {exc}",
                    retryable=False)
            if errs:
                return ToolOutcome.failed(
                    "invalid args: " + "; ".join(str(e) for e in errs),
                    retryable=True)

        # ③ scope
        if manifest.required_scope and not e.principal.can(manifest.required_scope):
            return ToolOutcome.denied(
                f"principal {e.principal.user_id} lacks scope "
                f"{manifest.required_scope!r} for {name}")

        # ④ 静态策略
        if self.policy is not None:
            try:
                verdict = self.policy.check(e.principal, name, args, manifest)
            except Exception as exc:      # 策略自身崩了 → fail closed
                verdict = f"policy error: {type(exc).__name__}: {exc}"
            if verdict:
                return ToolOutcome.denied(f"policy: {verdict}")

        # ⑤ 风险分级
        tier = int(manifest.risk_tier)
        if tier >= int(RiskTier.T3_CRITICAL):
            return ToolOutcome.denied(
                f"{name} is T{tier} ({manifest.side_effect.value}); "
                "critical tools cannot be confirmed, only refused")
        if tier >= int(RiskTier.T2_HIGH) and not e.approved(name, digest):
            return ToolOutcome.needs_confirmation(
                f"{name} requires approval (tier T{tier}, "
                f"side_effect={manifest.side_effect.value}, args_hash={digest})")

        # ⑥⑦⑧ 是**一个原子决策**：查缓存 → 限流 → 占预算 → 计数。
        # 并行下它们必须一起做，否则任何一处 check-then-act 都会被竞争打破。
        with self._lock:
            # ⑥ 限流（按 turn 计）—— **在缓存之前**，这是刻意的。
            #
            # `per_turn_limit` 限的是**调用次数**，不是"真执行次数"。
            # 理由：模型用同一组参数反复调同一个工具，是"原地打转"的典型信号，
            # 该被这道闸拦住。如果先查缓存，命中就直接返回，计数永远不涨，
            # `per_turn_limit` 形同虚设（实测：limit=2 的只读工具能连调 4 次）。
            #
            # 防"烧钱"是另一道闸（⑧ 预算）的职责：缓存命中不占预算。
            # 两道闸各管一件事，不合并。
            turn_key = (e.turn_id, name)
            used = self._turn_calls.get(turn_key, 0)
            limit = int(manifest.per_turn_limit)
            if limit > 0 and used >= limit:
                return ToolOutcome.denied(
                    f"rate limit: {name} called {used}x in turn {e.turn_id} "
                    f"(per_turn_limit={limit}；含幂等缓存命中)")
            self._turn_calls[turn_key] = used + 1

            # ⑦ 幂等缓存：命中就返回，**不占预算**（没有真执行）
            #
            # 两条**独立**的缓存理由，不能合并：
            #   * `manifest.idempotent`：工具自身是只读的 → 同参数结果可复用。
            #     这是 `ToolManifest` 按 side_effect 推导出来的默认值。
            #   * `e.idempotency_key`：调用方**显式声明**"这一步只做一次"。
            #     写工具靠它防重复部署 —— 业务幂等的唯一正确入口。
            #
            # 早先只用前者，于是写工具默认 `idempotent=False` 时
            # 连显式给的 `idempotency_key` 一起失效了（实测被
            # `test_same_args_twice_deduped_and_fn_runs_once` 抓住）。
            # 语义应该反过来：写工具**默认不缓存**，但**显式要求**就缓存。
            cache_key = (name, digest, e.idempotency_key or f"turn:{e.turn_id}")
            # 判定交给 manifest 的三态实现：读写共用同一套逻辑，
            # 否则会出现"写进去了但读不出来"（两边条件漂移）。
            cacheable = manifest.cacheable(
                has_business_key=bool(e.idempotency_key))
            if cacheable and cache_key in self._cache:
                return ToolOutcome.ok(replace(self._cache[cache_key], deduped=True))

            # ⑧ 预算（只对**将要真执行**的调用记账）
            e.budget.tool_calls_used += 1
            over = e.budget.exhausted()
            if over:
                e.budget.tool_calls_used -= 1      # 回滚：被预算拒的调用不计数
                self._turn_calls[turn_key] = used  # 回滚：没执行也不算"调过"
                return ToolOutcome.failed(f"budget exhausted: {over}")

        # ⑨ Secret 注入
        call_args = args
        secret_values: list[str] = []
        if manifest.secrets:
            injected: dict[str, str] = {}
            missing: list[str] = []
            for slot in manifest.secrets:
                value = self.secrets.get(slot)
                if value is None:
                    missing.append(slot)
                else:
                    injected[slot] = value
                    secret_values.append(str(value))
            if missing:
                with self._lock:                   # 回滚也是记账，必须原子
                    e.budget.tool_calls_used -= 1
                    self._turn_calls[turn_key] = used
                return ToolOutcome.failed(
                    f"missing secret(s) for {name}: {', '.join(missing)}")
            call_args = dict(args)
            call_args["__secrets__"] = injected      # 只给 fn 看，绝不进 result

        # ⑩ 带超时执行
        timeout_s = max(0.001, int(manifest.timeout_ms) / 1000.0)
        box: dict[str, Any] = {}

        def _runner() -> None:
            try:
                box["value"] = fn(call_args, e)
            except BaseException as exc:              # noqa: BLE001 — 由 box 携带
                box["error"] = exc

        pool = _futures.ThreadPoolExecutor(max_workers=1,
                                           thread_name_prefix=f"tool-{name}")
        try:
            fut = pool.submit(_runner)
            fut.result(timeout=timeout_s)
        except _futures.TimeoutError:
            return ToolOutcome.timeout(now_ms() - t0)
        finally:
            pool.shutdown(wait=False)                 # 卡死的线程不拖住调用方

        if "error" in box:
            exc = box["error"]
            return ToolOutcome.failed(f"{type(exc).__name__}: {exc}",
                                      retryable=manifest.idempotent is not False)

        raw = box.get("value")

        # 防御：实现方若把注入的 secrets 原样回抛，先摘掉再规范化
        if isinstance(raw, dict):
            raw = {k: v for k, v in raw.items() if k != "__secrets__"}
        if secret_values:
            raw, hits = self._scrub(raw, secret_values)
        else:
            hits = 0

        # ⑪ 规范化
        try:
            result = self.normalizer.normalize(name, raw, manifest)
        except Exception as exc:
            return ToolOutcome.failed(f"normalize error: {type(exc).__name__}: {exc}")
        if hits:
            result = replace(result, redacted=result.redacted + hits)
        result = replace(result, duration_ms=round(now_ms() - t0, 3))

        if manifest.cacheable(has_business_key=bool(e.idempotency_key)):
            self._remember(cache_key, result)
        return ToolOutcome.ok(result)

    # ── 辅助 ────────────────────────────────────────────────────
    def _remember(self, key: tuple[str, str, str], result: ToolResult) -> None:
        if len(self._cache) >= self.CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)), None)
        with self._lock:                           # 缓存写入同样要原子
            self._cache[key] = result

    def _scrub(self, node: Any, secret_values: Iterable[str]) -> tuple[Any, int]:
        """把 secret 的字面值从结果里抹掉（兜底，不是主防线）。"""
        values = [v for v in secret_values if v]
        if not values:
            return node, 0
        if isinstance(node, str):
            hits = 0
            for v in values:
                if v in node:
                    hits += node.count(v)
                    node = node.replace(v, Normalizer.REDACTION)
            return node, hits
        if isinstance(node, list):
            out_l: list = []
            total = 0
            for it in node:
                vv, n = self._scrub(it, values)
                out_l.append(vv)
                total += n
            return out_l, total
        if isinstance(node, dict):
            out_d: dict = {}
            total = 0
            for k, v in node.items():
                vv, n = self._scrub(v, values)
                out_d[k] = vv
                total += n
            return out_d, total
        return node, 0

    def _span(self, name: str, attrs: dict):
        if self.tracer is None:
            return contextlib.nullcontext()
        try:
            return self.tracer.span(name, attrs)
        except Exception:
            return contextlib.nullcontext()

    # ── 观测 ────────────────────────────────────────────────────
    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        for rec in self.trace_log:
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
        return {"calls": len(self.trace_log), "kinds": kinds,
                "cache_entries": len(self._cache),
                "artifacts": len(self.normalizer.artifacts)}

    def artifacts(self) -> list[dict]:
        return list(self.normalizer.artifacts)


__all__ = [
    "Dispatcher",
    "Normalizer",
    "Policy",
    "SubprocessSandbox",
    "ToolRegistry",
    "json_text",
]
