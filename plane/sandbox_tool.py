"""plane/sandbox_tool.py — 把 W3 的执行沙箱接进 Tool Plane（W10）。

问题：`plane/tool.py` 里的 `SubprocessSandbox` 只是**库**，不是**工具**。
没有 `ToolManifest` 它就不会出现在 `registry.manifests()` 里，模型永远看不到它，
于是沙箱存在却不被使用 —— 等于不存在；就算绕过 Dispatcher 直接调它，
`scope / policy / approval / 预算 / 限流 / 审计 / 脱敏` 全部丢失。

本模块只做一件事：把沙箱包装成一个**正经工具** `sandbox_run`，
让它只能从 `Dispatcher.invoke` 进来，走完整链路：

    ToolRegistry.get → required_scope → Policy.check → 风险分级(T2 → HITL 审批)
    → 幂等(本工具 idempotent=False，永不缓存) → per_turn_limit → Budget
    → ThreadPoolExecutor(timeout_ms) → fn → Normalizer(截断/脱敏) → ToolOutcome

设计要点
--------
1. **不碰冻结文件**：`contracts.py` / `plane/tool.py` 只读；本模块靠继承 + 组合复用。
2. **两层纵深**：handler 内先做「白名单 + 元字符 + 超时 + 截断 + 脱敏」，
   Dispatcher 再做一遍「策略 + 审批 + 截断 + 脱敏」。任一层单独失效都不至于裸奔。
3. **非幂等**：`idempotent=False`。`echo` 看起来无副作用，但沙箱执行的是**任意命令**，
   把「同 argv 第二次直接返回缓存」当默认值等于骗模型「命令已经跑过了」。
   Dispatcher 因此不会为它写缓存，同一 argv 每次都真执行 —— 这是正确行为。
4. **审批粒度是 (tool, args_hash)**：`Approval(args_hash=args_hash({"argv": [...], ...}))`。
   改一个字符就是另一次调用，需要重新审批。
5. **超时语义**：handler 用 manifest 的 `timeout_ms` 作为子进程超时，超时抛
   `TimeoutError`（不是返回 124）；Dispatcher 另有自己的 future 超时兜底。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_sandbox_tool -v
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

# 允许在任意工作目录下 import（demo/run.py 可能从别处启动）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import (  # noqa: E402
    ExecCtx,
    RiskTier,
    SideEffect,
    ToolManifest,
    args_hash,
    now_ms,
)
from plane.tool import SubprocessSandbox, ToolRegistry  # noqa: E402

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════
#: 默认命令白名单。只放**只读观察类**命令；写类命令必须由调用方显式放宽。
DEFAULT_ALLOWED_COMMANDS: tuple[str, ...] = ("echo", "ls", "cat", "python3", "git", "grep")

#: 敏感信息脱敏（与 Normalizer 同款语义，顺序有意义：越具体越先吃）
REDACTION = "***REDACTED***"
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                                  # OpenAI 风格 key
    re.compile(r"""(?i)\bpassword["']?\s*[:=]\s*["']?[^\s"',})]+"""),    # password=...
    re.compile(r"""(?i)\bAWS_SECRET\w*\s*[:=]\s*["']?[^\s"',})]+"""),    # AWS_SECRET_ACCESS_KEY=...
)

#: manifest 默认超时；handler 的子进程超时取同一个值
DEFAULT_TIMEOUT_MS = 8000


# ══════════════════════════════════════════════════════════════
# manifest
# ══════════════════════════════════════════════════════════════
SANDBOX_MANIFEST = ToolManifest(
    name="sandbox_run",
    side_effect=SideEffect.MUTATING,
    risk_tier=RiskTier.T2_HIGH,
    idempotent=False,               # 任意命令：绝不缓存
    timeout_ms=DEFAULT_TIMEOUT_MS,
    max_result_bytes=65536,
    required_scope="tool.write",
    secrets=(),
    per_turn_limit=10,
    description=(
        "在受限子进程中执行一条命令（shell=False）。"
        "argv 必须是 list[str]；命令受白名单与元字符黑名单约束；"
        "输出按字节截断并脱敏后返回 {exit_code, stdout, stderr, duration_ms, argv}。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"},
                     "minItems": 1, "maxItems": 32},
            "cwd": {"type": "string", "maxLength": 512},
        },
        "required": ["argv"],
        "additionalProperties": False,
    },
)

#: demo 用的只读工具（证明同一 registry 里 T0 与 T2 能共存）
QUERY_LOGS_MANIFEST = ToolManifest(
    name="query_logs",
    side_effect=SideEffect.READ_ONLY,
    risk_tier=RiskTier.T0_SAFE,
    idempotent=True,
    timeout_ms=2000,
    max_result_bytes=16384,
    required_scope="tool.read",
    description="按关键词查日志（mock）：返回 {query, count, rows:[{id, level, text}]}",
    input_schema={
        # 这个 mock 工具的契约就是「可选关键词 query」，别照抄其他工具的字段
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": 256}},
        "additionalProperties": False,
    },
)


# ══════════════════════════════════════════════════════════════
# 执行器：复用 W3 的白名单/元字符定义，重写 run 以满足本工具的返回契约
# ══════════════════════════════════════════════════════════════
class HandlerSandbox(SubprocessSandbox):
    """W3 `SubprocessSandbox` 的薄派生。

    为什么不直接用父类：父类 `run()` 是三处语义冲突的合体——
      ① 超时返回 `exit_code=124` 而不是抛异常 → Dispatcher 会把「超时」记成普通失败；
      ② 输出在 8192 字符处自己加 `\\n...[truncated]` → 和本工具的字节截断打架；
      ③ 不接受 `cwd`。
    所以这里**继承以复用** `SHELL_META` / `allowlist` / `timeout_ms` / `OUT_LIMIT`
    的定义与默认值，只重写 `run()`。闸门判定与父类是否一致由
    `tests/test_sandbox_tool.py::test_gate_parity_with_w3_sandbox` 做对照锁定。
    """

    #: 不在执行层截断（交给 handler 按 max_output_bytes 做字节级截断）
    OUT_LIMIT = 1 << 20
    #: 与父类一致的极简环境：不给子进程任何继承来的环境变量（key 泄漏面收敛）
    ENV = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "LANG": "C.UTF-8",
    }

    def __init__(self, allowlist: list[str] | None = None,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        super().__init__(allowlist=allowlist, timeout_ms=timeout_ms)

    # ── 闸门（等价于父类 run() 前半段，抽出来给 cwd 分支复用）─────
    def gate(self, argv: Any) -> list[str]:
        if not isinstance(argv, list):
            raise ValueError(f"argv 必须是 list[str]，收到 {type(argv).__name__}")
        if not argv:
            raise ValueError("argv 不能为空")
        if not all(isinstance(a, str) for a in argv):
            # 任务契约把「不是 list[str]」一律归为 ValueError（比 W3 的 PermissionError 更严：
            # 类型错误是调用方的编程错误，不是权限问题，两者不该混成同一个异常类别）
            raise ValueError("argv 元素必须是 str（收到非字符串元素）")

        exe = argv[0]
        allowed = {os.path.basename(a) for a in self.allowlist}
        if exe not in allowed and os.path.basename(exe) not in allowed:
            raise PermissionError(f"executable 不在白名单: {exe!r}")
        if os.path.sep in exe:
            # 白名单按 basename 比对，禁止借此执行同名任意路径（./echo、/tmp/ls）
            raise PermissionError(f"executable 必须是裸命令名: {exe!r}")

        for i, arg in enumerate(argv):
            bad = self.SHELL_META.intersection(arg)
            if bad:
                raise PermissionError(
                    f"argv[{i}] 含 shell 元字符 {sorted(bad)}: {arg!r}")
        return list(argv)

    # ── 执行 ────────────────────────────────────────────────────
    def run(self, argv: list[str], cwd: str | None = None) -> dict:
        """`shell=False` 执行。超时 → TimeoutError（不返回 124）。"""
        argv = self.gate(argv)
        t0 = now_ms()
        try:
            proc = subprocess.run(
                argv,
                shell=False,                     # 绝不拼字符串，绝不经过 shell
                capture_output=True,
                text=True,
                errors="replace",                # 二进制输出（cat 到非文本）不炸解码
                cwd=cwd or None,
                timeout=max(0.001, int(self.timeout_ms) / 1000.0),
                env=dict(self.ENV),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"timeout after {self.timeout_ms}ms: argv={argv!r}") from exc
        except FileNotFoundError as exc:          # 白名单内但本机没装
            raise RuntimeError(f"executable 不可用: {argv[0]!r}") from exc
        return {
            "exit_code": int(proc.returncode),
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "duration_ms": round(now_ms() - t0, 3),
            "argv": list(argv),
        }


# ══════════════════════════════════════════════════════════════
# 处理函数
# ══════════════════════════════════════════════════════════════
def redact_text(text: str) -> tuple[str, int]:
    """把敏感片段换成 `***REDACTED***`，返回 (新文本, 命中数)。"""
    hits = 0
    for pat in _REDACT_PATTERNS:
        text, n = pat.subn(REDACTION, text)
        hits += n
    return text, hits


def _truncate_bytes(text: str, limit: int) -> tuple[str, bool, int]:
    """按 UTF-8 字节截断（不切断多字节字符），返回 (文本, 是否截断, 原始字节数)。"""
    data = text.encode("utf-8")
    total = len(data)
    if limit <= 0 or total <= limit:
        return text, False, total
    return data[:limit].decode("utf-8", errors="ignore"), True, total


def sandbox_handler(allowed_commands: list[str] | None = None,
                    max_output_bytes: int = 8192,
                    *,
                    timeout_ms: int = DEFAULT_TIMEOUT_MS,
                    sandbox: HandlerSandbox | None = None,
                    ) -> Callable[[dict, ExecCtx], dict]:
    """构造 `sandbox_run` 的实现。

    严格执行语义（顺序固定，任何一种失败都抛异常，由 Dispatcher 归一成 ToolOutcome）：

    1. `args["argv"]` 必须是 `list[str]` 且非空            → `ValueError`
    2. 可执行文件必须在白名单内（默认 DEFAULT_ALLOWED_COMMANDS）→ `PermissionError`
    3. 任何 argv 元素含 `; | & $ ` > < \\n` 等元字符        → `PermissionError`
    4. 子进程 `shell=False`，超时取 `timeout_ms`（默认 = manifest.timeout_ms）
                                                          → `TimeoutError`
    5. 返回 `{exit_code, stdout, stderr, duration_ms, argv}`（`args["cwd"]` 可选，
       必须是已存在的目录，否则 `ValueError`）
    6. stdout / stderr 各自超过 `max_output_bytes` 时按字节截断，并补
       `"truncated": True` 与 `"total_bytes"`（截断前 stdout+stderr 的总字节数）
    7. stdout / stderr 里的 `sk-…` / `password=…` / `AWS_SECRET…` → `***REDACTED***`

    注意：脱敏在截断**之前**做。否则一个正好跨过截断边界的 key 会留下
    可辨识的半截明文（`sk-abcdefgh` 被切一半也仍然是泄漏）。
    """
    box = sandbox if sandbox is not None else HandlerSandbox(
        allowlist=list(allowed_commands) if allowed_commands is not None
        else list(DEFAULT_ALLOWED_COMMANDS),
        timeout_ms=timeout_ms,
    )
    limit = int(max_output_bytes)

    def handler(args: dict, e: ExecCtx) -> dict:
        if e is not None:
            if getattr(e, "aborted", False):
                raise RuntimeError("exec ctx aborted: 不启动新的副作用")
            if e.deadline_passed():
                raise TimeoutError("deadline passed before exec")

        if not isinstance(args, dict):
            raise ValueError(f"args 必须是 dict，收到 {type(args).__name__}")

        argv = box.gate(args.get("argv"))          # ① ② ③ 全在这一步

        cwd = args.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ValueError(f"cwd 必须是 str，收到 {type(cwd).__name__}")
            if not os.path.isdir(cwd):
                raise ValueError(f"cwd 不是目录: {cwd!r}")

        raw = box.run(argv, cwd=cwd or None)       # ④ 超时抛 TimeoutError

        out_safe, _ = redact_text(raw["stdout"])   # ⑦ 先脱敏
        err_safe, _ = redact_text(raw["stderr"])
        out_text, out_cut, out_total = _truncate_bytes(out_safe, limit)   # ⑥ 再截断
        err_text, err_cut, err_total = _truncate_bytes(err_safe, limit)

        payload: dict[str, Any] = {
            "exit_code": int(raw["exit_code"]),
            "stdout": out_text,
            "stderr": err_text,
            "duration_ms": float(raw["duration_ms"]),
            "argv": list(raw["argv"]),
        }
        if out_cut or err_cut:
            payload["truncated"] = True
            payload["total_bytes"] = out_total + err_total
        return payload

    return handler


# ══════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════
def register_sandbox(registry: ToolRegistry,
                     *,
                     allowed_commands: list[str] | None = None,
                     max_output_bytes: int = 8192,
                     risk_tier: RiskTier = RiskTier.T2_HIGH,
                     ) -> ToolManifest:
    """把 `sandbox_run` 注册进 registry，返回**最终使用**的 manifest。

    返回的是真值而不是主张：`registry.get("sandbox_run")` 拿到的就是它，
    调用方（demo / Builder）可以直接把 manifest 交给模型看。

    `risk_tier` 可下调（比如 demo 里想免审批就传 T1_LOW）。上调到 T3 是合法的，
    但 Dispatcher 对 T3 只会 **denied**（T3 不可确认），这是刻意保留的硬边界。
    """
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry 必须是 plane.tool.ToolRegistry")
    if not isinstance(risk_tier, RiskTier):
        raise TypeError("risk_tier 必须是 RiskTier")

    manifest = replace(SANDBOX_MANIFEST, risk_tier=risk_tier)
    fn = sandbox_handler(
        allowed_commands=allowed_commands,
        max_output_bytes=max_output_bytes,
        timeout_ms=manifest.timeout_ms,          # 超时以 manifest 为唯一真源
    )
    registry.register(manifest, fn)              # 同名会抛 ValueError，不做静默覆盖
    return manifest


# ══════════════════════════════════════════════════════════════
# 分层便利函数
# ══════════════════════════════════════════════════════════════
def query_logs_handler(args: dict, e: ExecCtx) -> dict:
    """mock 日志查询：T0 只读工具，用来对照「同一 registry 里的不同风险层」。"""
    query = str((args or {}).get("query", "") or "")
    rows = [
        {"id": "log-1", "level": "INFO", "text": f"mock entry for query={query!r}"},
        {"id": "log-2", "level": "WARN", "text": f"mock warning for query={query!r}"},
    ]
    return {"query": query, "count": len(rows), "rows": rows}


def make_demo_registry(*, allow_write: bool = True,
                       ) -> tuple[ToolRegistry, ToolManifest]:
    """建一个可跑 demo 的 registry：`sandbox_run`(MUTATING/T2) + `query_logs`(READ_ONLY/T0)。

    `allow_write=False` 的语义是「这次部署不打算产生副作用」：沙箱仍然注册
    （否则调用方会拿到 `unknown tool`，看不见真相），但风险等级抬到 T3_CRITICAL，
    Dispatcher 会直接 denied 并留下审计记录 —— 拒绝要可解释，不能靠工具消失。
    返回 `(registry, sandbox_manifest)`。
    """
    registry = ToolRegistry()
    tier = RiskTier.T2_HIGH if allow_write else RiskTier.T3_CRITICAL
    manifest = register_sandbox(registry, risk_tier=tier)
    registry.register(QUERY_LOGS_MANIFEST, query_logs_handler)
    return registry, manifest


__all__ = [
    "DEFAULT_ALLOWED_COMMANDS",
    "DEFAULT_TIMEOUT_MS",
    "HandlerSandbox",
    "QUERY_LOGS_MANIFEST",
    "REDACTION",
    "SANDBOX_MANIFEST",
    "make_demo_registry",
    "query_logs_handler",
    "redact_text",
    "register_sandbox",
    "sandbox_handler",
]
