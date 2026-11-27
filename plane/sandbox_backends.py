"""plane/sandbox_backends.py — 执行隔离后端 SPI（W27）。

问题
----
`plane/tool.py` 的 `SubprocessSandbox` 只有「subprocess + 命令白名单 + 元字符黑名单」。
它能挡手滑，**挡不住逃逸**：子进程和 agent 同 uid、同文件系统、同网络命名空间，
一个 `python3 -c "..."` 就能读 `~/.ssh`、打到内网、写任意文件。
企业环境要的是 docker / nsjail / gVisor，但原实现**没有任何后端抽象** ——
升级隔离只能改工具实现，等于没有升级路径。

本模块把「隔离强度」提升为**一等能力**：后端自报 `strength`，自报 `available()`，
上层据此选择或降级。核心反对的故障模式是 **「配置了却没生效」**：

    policy.require_backend = "kernel"   # 写了，但环境里没 nsjail
    → 旧行为：静默退回 subprocess，照样执行（最危险）
    → 本模块：`SandboxStrengthUnmet` 直接拒绝（fail-closed）

契约（`contracts.SandboxBackend`）
----------------------------------
    每个后端：`name` / `strength` / `available()` / `run(argv, *, cwd, timeout_ms, env)`
    run 返回：{"exit_code","stdout","stderr","duration_ms","argv","backend","strength","truncated"}

四个后端
--------
======================  ============  ==============================================
后端                     strength      一句话
======================  ============  ==============================================
`SubprocessBackend`     process       同机子进程；`shell=False` + 白名单 + 元字符 +
                                     rlimit（CPU/内存）。能挡手滑，挡不住逃逸。
`DockerBackend`         container     `docker run --rm --network=none --memory --cpus`；
                                     可用性 = which(docker) + `docker info` 退出 0。
`NsjailBackend`         kernel        `nsjail --disable_proc --iface_no_lo --time_limit`；
                                     可用性 = which(nsjail)（**注意**：不探测 root/cap 权限）。
`NullBackend`           none          永远不可用，run 必抛 `SandboxUnavailable`。
                                     用于「要求强隔离但环境不具备」时**明确失败**。
======================  ============  ==============================================

为什么 `NullBackend` 是一个后端而不是「None」
--------------------------------------------
让「拒绝执行」也走同一个接口：`selector.pick()` 永远返回一个后端对象，
调用方不需要写 `if backend is None` 分支，也就不会出现「忘了判空所以裸跑」。
拒绝是一个**可观测的决策**，不是一个空指针。

两道闸门
--------
1. `guard_argv(argv, policy)`：形状 + 白名单 + 元字符。判定口径与
   `plane/sandbox_tool.py::HandlerSandbox.gate` **逐条一致**（同一个 16 条语料对照，
   见 `tests/test_sandbox_backends.py::test_22_guard_argv_parity`）。
   两处实现是刻意的冗余：沙箱被绕过时，另一处仍然拦得住。
2. `assert_strength(backend, policy)`：`policy.require_backend` 非空时按强度等级
   单调比较，不够就抛 `SandboxStrengthUnmet`。

`run_guarded()` 把两道闸门 + 可用性检查 + 执行串起来，是给调用方的唯一入口。

诚实声明（本机 macOS 的事实）
-----------------------------
* `RLIMIT_AS` / `RLIMIT_DATA` 在 Darwin 上被内核拒绝
  （`ValueError: current limit exceeds maximum limit`）。实现里是 best-effort：
  设不上就跳过，不让一条尽力项打死整个执行。`enforceable_rlimits()` 可以在
  运行时查询**真实生效**的限制项 —— 别信配置，信探测。
* `DockerBackend` 杀掉的是宿主侧的 `docker` CLI 进程，不是容器本身；
  极端情况下容器会活到 `--rm` 的清理路径上。真要做到「超时即容器即死」，
  需要 `--cidfile` + `docker kill` 兜底（未实现，见 `run` 的注释）。
* `NsjailBackend.available()` 只探测 PATH。nsjail 通常需要 root 或
  `kernel.unprivileged_userns_clone`，这些**没有**探测 → 可能出现
  「available() 为真但 run 失败」。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_sandbox_backends -v
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

# 允许在任意工作目录下 import（demo/run.py 可能从别处启动）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import SandboxPolicy, now_ms  # noqa: E402

__all__ = [
    "BASE_ENV",
    "DEFAULT_DOCKER_IMAGE",
    "DEFAULT_PREFER",
    "SHELL_META",
    "STRENGTH_RANK",
    "BackendSelector",
    "DockerBackend",
    "NsjailBackend",
    "NullBackend",
    "SandboxStrengthUnmet",
    "SandboxTimeout",
    "SandboxUnavailable",
    "SubprocessBackend",
    "assert_strength",
    "enforceable_rlimits",
    "guard_argv",
    "run_guarded",
    "strength_rank",
]

# ══════════════════════════════════════════════════════════════
# 常量（**自己实现一份**，不 import plane.tool 的私有常量：
# 沙箱闸门不能因为别人重命名一个属性就静默失效）
# ══════════════════════════════════════════════════════════════
#: shell 元字符黑名单。与 `plane/tool.py::SubprocessSandbox.SHELL_META`
#: 及 `plane/sandbox_tool.py::HandlerSandbox.gate` 完全同集合（对照测试锁定）。
SHELL_META = frozenset(";|&$><`\n\r")

#: 强度等级：单调序，比较用 `>=`；"none" 是最低（NullBackend）
STRENGTH_RANK: dict[str, int] = {
    "none": 0,
    "process": 1,
    "container": 2,
    "kernel": 3,
}

#: 默认优先级：强隔离在前。找不到就逐级降级，降级必须可见（report）。
DEFAULT_PREFER: tuple[str, ...] = ("nsjail", "docker", "subprocess")

#: docker 默认镜像：小、带 python3、无 curl/wget（攻击面收敛）
DEFAULT_DOCKER_IMAGE = "python:3.11-alpine"

#: 极简环境：不继承调用方的环境变量（key 泄漏面收敛）。
#: 与 `plane/tool.py` 的同名语义一致。
BASE_ENV: dict[str, str] = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "LANG": "C.UTF-8",
}

#: 探测 docker daemon 的超时上限（任务要求 3s）
DOCKER_INFO_TIMEOUT_MS = 3000


# ══════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════
class SandboxUnavailable(RuntimeError):
    """后端不可用 / 拒绝执行。`NullBackend` 用它表达「明确失败」。"""


class SandboxTimeout(TimeoutError):
    """执行超时。继承 `TimeoutError`，让只捕获内置类型的调用方也能接住。

    超时的语义是「进程组已被 SIGKILL」——不是「返回 124 让上层猜」。
    `partial_stdout` / `partial_stderr` 保留超时前已读到的输出。
    """

    def __init__(self, message: str, *, argv: Sequence[str] | None = None,
                 partial_stdout: str = "", partial_stderr: str = "") -> None:
        super().__init__(message)
        self.argv = list(argv or [])
        self.partial_stdout = partial_stdout
        self.partial_stderr = partial_stderr


class SandboxStrengthUnmet(SandboxUnavailable):
    """策略要求的隔离强度达不到 → fail-closed 拒绝执行。

    继承 `SandboxUnavailable`：调用方 `except SandboxUnavailable` 一把兜住
    「没后端」和「后端太弱」两种拒绝，不用记两个类型。
    """


def strength_rank(name: str) -> int:
    """强度名 → 等级。未知名字按 0（最弱）处理，让拼写错误 fail-closed。"""
    return STRENGTH_RANK.get(str(name or "").strip().lower(), 0)


# ══════════════════════════════════════════════════════════════
# 闸门一：argv
# ══════════════════════════════════════════════════════════════
def guard_argv(argv: Sequence[str], policy: SandboxPolicy) -> None:
    """执行前闸门。判定顺序与 `HandlerSandbox.gate` 逐条相同。

    | 条件                                   | 异常            |
    |----------------------------------------|-----------------|
    | 不是 `list`（含 tuple / str / None）   | `ValueError`    |
    | 空列表                                 | `ValueError`    |
    | 元素不是 `str`                         | `ValueError`    |
    | `argv[0]` 不在白名单                   | `PermissionError` |
    | `argv[0]` 含路径分隔符（`/bin/echo`）  | `PermissionError` |
    | 任何参数含 `SHELL_META`                | `PermissionError` |

    类型错误归 `ValueError`、权限错误归 `PermissionError`：
    「传错了类型」是调用方的编程错误，「不许执行」是策略结论，混成一类会让
    上层没法分别处理（前者该重试，后者重试一万次也没用）。
    """
    if not isinstance(argv, list):
        raise ValueError(f"argv 必须是 list[str]，收到 {type(argv).__name__}")
    if not argv:
        raise ValueError("argv 不能为空")
    if not all(isinstance(a, str) for a in argv):
        raise ValueError("argv 元素必须是 str（收到非字符串元素）")
    if not isinstance(policy, SandboxPolicy):
        raise TypeError(f"policy 必须是 SandboxPolicy，收到 {type(policy).__name__}")

    allowed = {os.path.basename(a) for a in policy.allowed_commands}
    exe = argv[0]
    if exe not in allowed and os.path.basename(exe) not in allowed:
        raise PermissionError(f"executable 不在白名单: {exe!r}")
    if os.path.sep in exe:
        # 白名单按 basename 比对；不拦这条就能用 ./echo、/tmp/ls 执行任意同名二进制
        raise PermissionError(f"executable 必须是裸命令名: {exe!r}")

    for i, arg in enumerate(argv):
        bad = SHELL_META.intersection(arg)
        if bad:
            raise PermissionError(
                f"argv[{i}] 含 shell 元字符 {sorted(bad)}: {arg!r}")


# ══════════════════════════════════════════════════════════════
# 闸门二：隔离强度
# ══════════════════════════════════════════════════════════════
def assert_strength(backend: Any, policy: SandboxPolicy) -> None:
    """`policy.require_backend` 非空时校验强度，不够就 fail-closed。

    `require_backend=""`（默认）→ 不检查，任何后端都放行。
    `require_backend="kernel"` 而拿到 `subprocess` → `SandboxStrengthUnmet`，
    错误信息里写清「要求 X 级，实际 Y 级」，因为这条信息要进审计日志。
    """
    required = str(getattr(policy, "require_backend", "") or "").strip().lower()
    if not required:
        return
    if required not in STRENGTH_RANK:
        raise ValueError(
            f"未知的隔离强度要求: {required!r}（可选 {sorted(STRENGTH_RANK)}）")

    actual = str(getattr(backend, "strength", "none") or "none").strip().lower()
    if strength_rank(actual) < STRENGTH_RANK[required]:
        raise SandboxStrengthUnmet(
            f"策略要求 {required} 级隔离（require_backend={required!r}），"
            f"实际后端 {getattr(backend, 'name', '?')} 只有 {actual} 级 "
            f"→ 拒绝执行（fail-closed）")


# ══════════════════════════════════════════════════════════════
# 底层执行原语
# ══════════════════════════════════════════════════════════════
def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """按 UTF-8 字节截断（不切断多字节字符）→ (文本, 是否截断)。"""
    data = text.encode("utf-8", errors="ignore")
    if limit <= 0 or len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


def _kill_group(proc: subprocess.Popen) -> None:
    """杀**整个进程组**。

    只 `proc.kill()` 会漏掉孙进程：`python3 -c "subprocess.Popen(['sleep','999'])"`
    在父进程死后继续跑 —— 真正的逃逸不是杀掉的那个，是忘掉的那个。
    """
    if os.name == "nt":                                  # Windows 无进程组语义
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _exec_process(argv: Sequence[str], *, timeout_ms: int, env: dict | None = None,
                  cwd: str | None = None,
                  preexec_fn: Callable[[], None] | None = None) -> dict:
    """统一的子进程执行层：`shell=False` + 独立会话 + 超时杀进程组。

    返回 `{"exit_code","stdout","stderr"}`；超时抛 `SandboxTimeout`（带部分输出）。
    这一层被四个后端共用，也是测试 monkeypatch 的接缝（`_execute`）。
    """
    if not argv:
        raise ValueError("argv 不能为空")
    timeout_s = max(0.001, int(timeout_ms) / 1000.0)

    kwargs: dict[str, Any] = {
        "shell": False,                      # 绝不拼字符串、绝不经过 shell
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "errors": "replace",                 # cat 到二进制也不炸解码
        "env": dict(env) if env is not None else dict(BASE_ENV),
    }
    if cwd:
        kwargs["cwd"] = cwd
    if os.name != "nt":
        kwargs["start_new_session"] = True    # setsid：超时能整组杀
        if preexec_fn is not None:
            kwargs["preexec_fn"] = preexec_fn

    try:
        proc = subprocess.Popen(list(argv), **kwargs)
    except FileNotFoundError as exc:          # 白名单内但本机没装
        raise SandboxUnavailable(f"executable 不可用: {argv[0]!r}") from exc
    except OSError as exc:                    # cwd 不存在 / 权限不足
        raise SandboxUnavailable(
            f"无法启动 {list(argv)!r}: {exc}") from exc

    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=2.0)   # 回收僵尸，别留 defunct
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise SandboxTimeout(
            f"timeout after {int(timeout_ms)}ms（进程组已 SIGKILL）: argv={list(argv)!r}",
            argv=argv, partial_stdout=out or "", partial_stderr=err or "") from None

    return {
        "exit_code": int(proc.returncode),
        "stdout": out or "",
        "stderr": err or "",
    }


# ── rlimit（POSIX；Windows 无 resource 模块）─────────────────────
def _rlimit_hook(policy: SandboxPolicy) -> Callable[[], None] | None:
    """构造 `preexec_fn`：限制 CPU 与内存。Windows / 无 resource → None。

    **best-effort**：Darwin 内核拒绝 `RLIMIT_AS` / `RLIMIT_DATA`
    （`ValueError: current limit exceeds maximum limit`）。设不上就跳过 ——
    一条尽力项设不上，不该让整条命令执行失败；真实生效项用
    `enforceable_rlimits()` 探测，别信配置。
    """
    if os.name == "nt":
        return None
    try:
        import resource  # noqa: PLC0415 — 仅 POSIX 存在
    except ImportError:                                  # pragma: no cover
        return None

    cpu = max(1, int(getattr(policy, "max_cpu_seconds", 10) or 10))
    mem_mb = int(getattr(policy, "max_memory_mb", 512) or 0)

    def hook() -> None:
        def apply(limit_name: str, soft: int, hard: int) -> None:
            limit = getattr(resource, limit_name, None)
            if limit is None:
                return
            try:
                resource.setrlimit(limit, (soft, hard))
            except (ValueError, OSError):
                pass                                     # 内核拒绝：跳过

        apply("RLIMIT_CPU", cpu, cpu + 1)                # 硬限只多给 1s 收尾
        apply("RLIMIT_CORE", 0, 0)                       # 不留 core dump（可能含密钥）
        if mem_mb > 0:
            nbytes = mem_mb * 1024 * 1024
            apply("RLIMIT_AS", nbytes, nbytes)
            apply("RLIMIT_DATA", nbytes, nbytes)

    return hook


_RLIMIT_PROBE_CACHE: dict[int, dict[str, bool]] = {}


def enforceable_rlimits(policy: SandboxPolicy | None = None) -> dict[str, bool]:
    """探测本机**真实生效**的 rlimit 项（真实子进程内 setrlimit，不是猜）。

    返回 `{"RLIMIT_CPU": True, "RLIMIT_AS": False, ...}`。
    这是对「配置了却没生效」的直接解药：`max_memory_mb=512` 写了但内核拒绝，
    这里会诚实地告诉你 `False`。
    """
    pol = policy if isinstance(policy, SandboxPolicy) else SandboxPolicy()
    key = int(getattr(pol, "max_memory_mb", 512) or 0)
    if key in _RLIMIT_PROBE_CACHE:
        return dict(_RLIMIT_PROBE_CACHE[key])

    mem = max(1, key) * 1024 * 1024
    cpu = max(1, int(getattr(pol, "max_cpu_seconds", 10) or 10))
    code = (
        "import json, resource\n"
        "specs = {'RLIMIT_CPU': (%d, %d), 'RLIMIT_CORE': (0, 0),"
        " 'RLIMIT_AS': (%d, %d), 'RLIMIT_DATA': (%d, %d)}\n"
        "out = {}\n"
        "for name, (soft, hard) in specs.items():\n"
        "    lim = getattr(resource, name, None)\n"
        "    if lim is None:\n"
        "        out[name] = False\n"
        "        continue\n"
        "    try:\n"
        "        resource.setrlimit(lim, (soft, hard))\n"
        "    except (ValueError, OSError):\n"
        "        out[name] = False\n"
        "    else:\n"
        "        out[name] = True\n"
        "print(json.dumps(out))\n"
    ) % (cpu, cpu + 1, mem, mem, mem, mem)

    if os.name == "nt":                                  # 无 resource 模块
        result = {k: False for k in
                  ("RLIMIT_CPU", "RLIMIT_CORE", "RLIMIT_AS", "RLIMIT_DATA")}
        _RLIMIT_PROBE_CACHE[key] = dict(result)
        return result

    try:
        raw = _exec_process([sys.executable, "-c", code],
                            timeout_ms=5000, env=dict(BASE_ENV))
        import json  # noqa: PLC0415 — 只在探测路径用
        parsed = json.loads(raw["stdout"].strip() or "{}")
        result = {str(k): bool(v) for k, v in parsed.items()}
    except (SandboxUnavailable, SandboxTimeout, ValueError):
        result = {k: False for k in
                  ("RLIMIT_CPU", "RLIMIT_CORE", "RLIMIT_AS", "RLIMIT_DATA")}
    _RLIMIT_PROBE_CACHE[key] = dict(result)
    return result


# ══════════════════════════════════════════════════════════════
# 后端基类
# ══════════════════════════════════════════════════════════════
class _BaseBackend:
    """公共骨架：策略、探测缓存、可用性理由、结果组装。

    `probe: Callable[[str], bool] | None` 是**可注入的可用性判定**：
    本机没有 docker/nsjail，测试用它让后端「假装可用」，不触碰真实 daemon。
    """

    name: str = "base"
    strength: str = "none"

    def __init__(self, *, policy: SandboxPolicy | None = None,
                 probe: Callable[[str], bool] | None = None) -> None:
        self.policy: SandboxPolicy = policy if policy is not None else SandboxPolicy()
        self.probe = probe
        self._available: bool | None = None
        self._reason: str = ""

    # ── 可用性 ────────────────────────────────────────────────
    @property
    def reason(self) -> str:
        """最近一次 `available()` 给出的理由（不可用时必须非空）。"""
        return self._reason

    def _note(self, ok: bool, reason: str) -> bool:
        self._available = bool(ok)
        self._reason = reason
        return self._available

    def reset(self) -> None:
        """清探测缓存（测试 / 环境变化后重探）。"""
        self._available = None
        self._reason = ""

    def available(self) -> bool:            # pragma: no cover - 子类实现
        raise NotImplementedError

    # ── 执行层接缝（测试 monkeypatch 这里，永不真的调 docker/nsjail）──
    def _execute(self, argv: Sequence[str], *, timeout_ms: int,
                 env: dict | None = None, cwd: str | None = None,
                 preexec_fn: Callable[[], None] | None = None) -> dict:
        return _exec_process(argv, timeout_ms=timeout_ms, env=env,
                            cwd=cwd, preexec_fn=preexec_fn)

    # ── 结果组装 ──────────────────────────────────────────────
    def _result(self, inner_argv: Sequence[str], started_ms: float,
                raw: dict) -> dict:
        limit = int(getattr(self.policy, "max_output_bytes", 8192) or 0)
        out, out_cut = _truncate(str(raw.get("stdout") or ""), limit)
        err, err_cut = _truncate(str(raw.get("stderr") or ""), limit)
        return {
            "exit_code": int(raw.get("exit_code", -1)),
            "stdout": out,
            "stderr": err,
            "duration_ms": round(now_ms() - started_ms, 3),
            "argv": list(inner_argv),
            "backend": self.name,
            "strength": self.strength,
            "truncated": bool(out_cut or err_cut),
        }

    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout_ms: int | None = None,
            env: dict | None = None) -> dict:          # pragma: no cover
        raise NotImplementedError

    def __repr__(self) -> str:                             # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r} strength={self.strength!r}>"


class SubprocessBackend(_BaseBackend):
    """同机子进程：`shell=False` + 白名单 + 元字符 + rlimit。

    这是**降级基线**，不是隔离。它挡得住「手滑」（`rm -rf /` 被白名单拦掉），
    挡不住逃逸（同 uid、同文件系统、同网络）。`strength="process"` 如实申报。
    """

    name = "subprocess"
    strength = "process"

    def __init__(self, *, policy: SandboxPolicy | None = None,
                 probe: Callable[[str], bool] | None = None) -> None:
        super().__init__(policy=policy, probe=probe)
        #: 最近一次执行实际用了哪些限制项（对「配置了没生效」的现场记录）
        self.last_exec_env: dict[str, str] = {}

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        if self.probe is not None:
            ok = bool(self.probe(self.name))
            return self._note(ok, "probe says yes" if ok else "probe says no")
        return self._note(True, "always available（无外部依赖，但没有命名空间隔离）")

    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout_ms: int | None = None,
            env: dict | None = None) -> dict:
        guard_argv(argv, self.policy)                    # 纵深：后端自己也拦一遍
        inner = list(argv)
        ms = int(timeout_ms if timeout_ms is not None else self.policy.timeout_ms)
        merged = dict(BASE_ENV)
        if env:
            merged.update({str(k): str(v) for k, v in env.items()})
        self.last_exec_env = dict(merged)

        started = now_ms()
        raw = self._execute(inner, timeout_ms=ms, env=merged, cwd=cwd,
                            preexec_fn=_rlimit_hook(self.policy))
        return self._result(inner, started, raw)


class DockerBackend(_BaseBackend):
    """容器隔离。`strength="container"`：独立 PID/net/mount 命名空间。

    可用性判定是**两段式**，缺一不可：
      1. `shutil.which("docker")` —— CLI 在不在；
      2. `docker info` 退出码 0 —— daemon 活着。
    只查 ① 是经典事故：CLI 装了、daemon 没起，`docker run` 全报错，
    而 `available()` 一直说「可用」。
    """

    name = "docker"
    strength = "container"

    def __init__(self, *, image: str = DEFAULT_DOCKER_IMAGE,
                 policy: SandboxPolicy | None = None,
                 probe: Callable[[str], bool] | None = None,
                 which: Callable[[str], str | None] | None = None,
                 info_timeout_ms: int = DOCKER_INFO_TIMEOUT_MS,
                 extra_flags: Sequence[str] = ()) -> None:
        super().__init__(policy=policy, probe=probe)
        self.image = str(image)
        self._which = which if which is not None else shutil.which
        self.info_timeout_ms = int(info_timeout_ms)
        #: 额外 docker run 参数（硬化项由运维按环境决定，默认不加：
        #: 默认 argv 与文档一字不差，便于审计比对）
        self.extra_flags: tuple[str, ...] = tuple(extra_flags)
        self.last_host_argv: list[str] = []

    # ── 可用性 ────────────────────────────────────────────────
    def available(self) -> bool:
        if self._available is not None:
            return self._available
        if self.probe is not None:
            ok = bool(self.probe(self.name))
            return self._note(ok, "probe says yes" if ok else "probe says no")
        path = self._which("docker")
        if not path:
            return self._note(False, "not found in PATH")
        code, detail = self._probe_daemon()
        if code == 0:
            # 守护进程活着还不够：镜像不在本地时 run 会走「自动 pull」，
            # 在 8s 的工具超时里表现为神秘超时（本机实测踩到过）。
            # 可用性必须把「镜像是否就绪」算进去，否则就是"配置了却没生效"。
            if not self._image_present():
                return self._note(
                    False,
                    f"daemon ok but image {self.image!r} not present"
                    f"（先用 docker pull {self.image} 预取）")
            return self._note(True, f"docker info ok（{path}）")
        return self._note(False, f"docker info failed: exit {int(code)}"
                                 + (f"（{detail.strip().splitlines()[0][:120]}）"
                                    if detail.strip() else ""))

    def _image_present(self) -> bool:
        """镜像是否已在本地。可被测试 monkeypatch。"""
        try:
            raw = self._execute(["docker", "image", "inspect", self.image],
                                timeout_ms=self.info_timeout_ms, env=dict(BASE_ENV))
        except (SandboxTimeout, SandboxUnavailable, OSError):
            return False
        return int(raw.get("exit_code", -1)) == 0

    def _probe_daemon(self) -> tuple[int, str]:
        """`docker info` 探测。返回 (exit_code, 输出)。可被测试 monkeypatch。"""
        try:
            raw = self._execute(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                timeout_ms=self.info_timeout_ms, env=dict(BASE_ENV))
        except SandboxTimeout:
            return 124, "docker info timeout"
        except (SandboxUnavailable, OSError) as exc:
            return 127, str(exc)
        code = int(raw.get("exit_code", -1))
        out = str(raw.get("stdout") or "").strip()
        err = str(raw.get("stderr") or "").strip()
        # **退出码 0 不等于 daemon 活着**：带 `--format` 时 Docker CLI 在
        # daemon 不可达的情况下照样退出 0、stdout 为空、错误只落在 stderr
        # （macOS Docker Desktop 27.5.1 实测；同机不带 --format 的
        # `docker info` / `docker version` 才老老实实退 1）。
        # 只看退出码的后果是把「daemon 没起」误报成
        # 「daemon ok 但镜像不在」，把排查引向错误的方向 —— 报错文案错，
        # 比没有文案更贵。
        if code == 0 and not out:
            return 1, (err or "docker info 退出 0 但无输出（daemon 不可达）")
        return code, (err or out)

    # ── argv 构造（可单独测，不执行）──────────────────────────
    def build_argv(self, argv: Sequence[str], *, cwd: str | None = None,
                   env: dict | None = None) -> list[str]:
        """`docker run --rm --network=none -v CWD:/work -w /work --memory=.. --cpus=1 IMG argv...`"""
        workdir = os.path.abspath(cwd) if cwd else os.path.abspath(os.getcwd())
        net = "bridge" if getattr(self.policy, "allow_network", False) else "none"
        cmd: list[str] = [
            "docker", "run", "--rm",
            f"--network={net}",
            f"--memory={int(self.policy.max_memory_mb)}m",
            "--cpus=1",
            "-v", f"{workdir}:/work",
            "-w", "/work",
        ]
        cmd.extend(self.extra_flags)
        for key, value in sorted((env or {}).items()):
            cmd.extend(["-e", f"{key}={value}"])
        cmd.append(self.image)
        cmd.extend(list(argv))
        return cmd

    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout_ms: int | None = None,
            env: dict | None = None) -> dict:
        guard_argv(argv, self.policy)
        inner = list(argv)
        ms = int(timeout_ms if timeout_ms is not None else self.policy.timeout_ms)
        host_argv = self.build_argv(inner, cwd=cwd, env=env)
        self.last_host_argv = list(host_argv)

        started = now_ms()
        # 注意：超时杀的是宿主侧 docker CLI，不是容器。
        # 要「超时即容器死」需 --cidfile + docker kill 兜底（未实现，已在模块头声明）。
        raw = self._execute(host_argv, timeout_ms=ms, env=dict(BASE_ENV), cwd=None)
        return self._result(inner, started, raw)


class NsjailBackend(_BaseBackend):
    """内核级隔离（nsjail：命名空间 + seccomp + rlimit + 无 loopback）。

    `available()` **只**探测 PATH —— 是否具备 userns/root 权限、seccomp 策略是否
    被内核允许，都没有探测。这是刻意保守的诚实：宁可 available()=True 而在 run
    时报错（可观测），也不假装自己知道。
    """

    name = "nsjail"
    strength = "kernel"

    def __init__(self, *, policy: SandboxPolicy | None = None,
                 probe: Callable[[str], bool] | None = None,
                 which: Callable[[str], str | None] | None = None,
                 binary: str = "nsjail",
                 search_path: str | None = BASE_ENV["PATH"]) -> None:
        super().__init__(policy=policy, probe=probe)
        self._which = which if which is not None else shutil.which
        self.binary = binary
        #: 查找 `binary` 的 PATH。默认用 `BASE_ENV` 的**固定** PATH，不随
        #: 调用方进程的 PATH 漂移（ssh 非登录 shell 里没有 /usr/local/bin，
        #: 会让"装了 nsjail"变成"not found in PATH"）。测试传 `""` 关掉这一层。
        self.search_path = search_path
        self.last_host_argv: list[str] = []

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        if self.probe is not None:
            ok = bool(self.probe(self.name))
            return self._note(ok, "probe says yes" if ok else "probe says no")
        # **先按 `BASE_ENV` 的确定 PATH 找，再退回调用方进程的 PATH。**
        # 只用进程 PATH 会让结论取决于"谁启动的进程"：ssh 非登录 shell 的 PATH
        # 里没有 /usr/local/bin，于是机器上明明装了 nsjail（`只用 sudo` 装到
        # /usr/local/bin）却报 `not found in PATH`，后端静默降级成 subprocess。
        path = None
        if self.search_path:
            path = shutil.which(self.binary, path=self.search_path)
        path = path or self._which(self.binary)
        if not path:
            return self._note(False, "not found in PATH")
        return self._note(True, f"found in PATH（{path}；未探测 userns/root 权限）")

    def build_argv(self, argv: Sequence[str], *, cwd: str | None = None,
                   timeout_ms: int | None = None,
                   env: dict | None = None) -> list[str]:
        """`nsjail --quiet --disable_proc --iface_no_lo --time_limit N --rlimit_as .. -- argv...`"""
        pol = self.policy
        ms = int(timeout_ms if timeout_ms is not None else pol.timeout_ms)
        time_limit_s = max(1, (ms + 999) // 1000)          # 向上取整，别把 1.2s 砍成 1s
        cmd: list[str] = [
            self.binary,
            "--quiet",
            "--disable_proc",                              # 不挂 /proc：看不见宿主进程表
            "--iface_no_lo",                               # 连 loopback 都没有 → 打不到本机服务
            "--time_limit", str(time_limit_s),
            "--rlimit_as", str(int(pol.max_memory_mb)),    # nsjail 单位是 MiB
            "--rlimit_cpu", str(int(pol.max_cpu_seconds)),
        ]
        # **只挂"跑命令需要的最小集合"，不挂 `/`。**
        # nsjail 新建 mount namespace，里面默认是空的 —— 什么都不挂时白名单里的
        # 命令一个都不存在，每次 run 都是
        #   `execve('/usr/bin/echo') failed: No such file or directory`
        # 而 `available()` 仍为 True（它只查 PATH），于是"内核级隔离后端可用"
        # 这件事从头到尾没被真跑过。
        # 但**不能图省事挂 `-R /`**：宿主根的 bind mount 会把 `/proc` 和
        # `/sys` 一起带回来，`--disable_proc` 与 `--iface_no_lo` 的承诺当场作废
        # （`ls /sys/class/net` 能看见宿主所有网卡、`cat /proc/self/status`
        # 能看见宿主进程表）。只读挂载可执行文件所在的最小目录。
        for _root in ("/bin", "/usr", "/lib", "/lib64", "/etc/ld.so.cache"):
            if os.path.exists(_root):
                cmd.extend(["-R", _root])
        cmd.extend([
            # `/dev/null` 之类：不给的话很多程序会报错，但不挂整个 /dev
            "-R", "/dev/null",
        ] if os.path.exists("/dev/null") else [])
        # 网络：nsjail 默认新建 netns（`--disable_clone_newnet` 反而会共享宿主网络，
        # 语义正好相反，不传）。`--iface_no_lo` 连 loopback 都不给 → 打不到本机服务。
        # 只有显式 allow_network=True 时才放开 loopback。
        if getattr(pol, "allow_network", False):
            cmd = [c for c in cmd if c != "--iface_no_lo"]
        if cwd:
            cmd.extend(["--cwd", os.path.abspath(cwd)])
        for key, value in sorted((env or {}).items()):
            cmd.extend(["--env", f"{key}={value}"])
        cmd.append("--")
        # **nsjail 用 `execve` 执行，不做 PATH 查找。**
        # 而 `guard_argv()` 要求的恰恰是"裸命令名"（白名单比对的是 basename），
        # 于是两条约束叠加起来，`run(["echo", ...])` 必然得到
        #   `execve('echo') failed: No such file or directory`
        # —— 后端"可用"却永远跑不起来，且只有真装了 nsjail 的机器才看得见。
        # 这里只把 argv[0] 换成绝对路径，白名单校验仍在 `guard_argv` 里做。
        resolved = list(argv)
        if resolved:
            exe = shutil.which(str(resolved[0]), path=BASE_ENV["PATH"])
            if exe:
                resolved[0] = exe
        cmd.extend(resolved)
        return cmd

    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout_ms: int | None = None,
            env: dict | None = None) -> dict:
        guard_argv(argv, self.policy)
        inner = list(argv)
        ms = int(timeout_ms if timeout_ms is not None else self.policy.timeout_ms)
        host_argv = self.build_argv(inner, cwd=cwd, timeout_ms=ms, env=env)
        self.last_host_argv = list(host_argv)

        started = now_ms()
        raw = self._execute(host_argv, timeout_ms=ms, env=dict(BASE_ENV), cwd=None)
        return self._result(inner, started, raw)


class NullBackend(_BaseBackend):
    """永远不可用、永远拒绝执行。

    用途：`BackendSelector` 全不可用时的返回值，以及
    「策略要求强隔离而环境不具备」时的显式失败。
    返回一个**会拒绝的后端**，好过返回 `None` 让调用方忘了判空。
    """

    name = "null"
    strength = "none"

    def available(self) -> bool:
        return self._note(False, "null backend 永远不可用：任何 run 都抛 SandboxUnavailable")

    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout_ms: int | None = None,
            env: dict | None = None) -> dict:
        raise SandboxUnavailable(
            "NullBackend 拒绝执行：没有可用的隔离后端（argv="
            f"{list(argv) if isinstance(argv, (list, tuple)) else argv!r}）")


# ══════════════════════════════════════════════════════════════
# 选择器
# ══════════════════════════════════════════════════════════════
_BACKEND_NAMES = ("subprocess", "docker", "nsjail", "null")


class BackendSelector:
    """按 `prefer` 顺序选第一个 `available()` 的后端。

    * 全不可用 → 返回 `NullBackend`（**不抛错**：选不出后端是常态，
      该由上层结合 `require_backend` 决定是拒绝还是降级 —— 选择器不替上层做决定）。
    * `report()` 必须能解释**每个**后端为什么不可用：运维看到的应该是
      `docker info failed: exit 1`，而不是一句「docker 不可用」。
    * 确定性：`prefer` 相同 → `pick()` 结果相同；后端实例与探测结果缓存，
      不会两次 pick 之间悄悄改主意。
    """

    def __init__(self, *, prefer: Sequence[str] = DEFAULT_PREFER,
                 probe: Callable[[str], bool] | None = None,
                 policy: SandboxPolicy | None = None) -> None:
        self.prefer: tuple[str, ...] = tuple(prefer) if prefer else DEFAULT_PREFER
        self.probe = probe
        self.policy = policy
        self._built: dict[str, _BaseBackend] = {}
        self._null = NullBackend(policy=policy, probe=probe)

    # ── 构造后端（惰性、缓存）────────────────────────────────
    def _build(self, name: str) -> _BaseBackend | None:
        key = str(name).strip().lower()
        if key in self._built:
            return self._built[key]
        backend: _BaseBackend | None
        if key == "subprocess":
            backend = SubprocessBackend(policy=self.policy, probe=self.probe)
        elif key == "docker":
            backend = DockerBackend(policy=self.policy, probe=self.probe)
        elif key == "nsjail":
            backend = NsjailBackend(policy=self.policy, probe=self.probe)
        elif key == "null":
            backend = NullBackend(policy=self.policy, probe=self.probe)
        else:
            backend = None
        if backend is not None:
            self._built[key] = backend
        return backend

    def candidates(self) -> list[_BaseBackend]:
        """`prefer` 里所有**已知**后端（未知名字不进列表，进 report）。"""
        out: list[_BaseBackend] = []
        for name in self.prefer:
            backend = self._build(name)
            if backend is not None:
                out.append(backend)
        return out

    # ── 选择 ──────────────────────────────────────────────────
    def pick(self) -> _BaseBackend:
        for backend in self.candidates():
            if backend.available():
                return backend
        return self._null

    def pick_or_refuse(self, policy: SandboxPolicy | None = None) -> _BaseBackend:
        """选一个后端，并**当场**按策略校验强度（fail-closed）。

        与 `pick()` 的区别就是这一句 `assert_strength`：
        `pick()` 允许降级，`pick_or_refuse()` 不允许。
        """
        pol = policy if policy is not None else self.policy
        backend = self.pick()
        if pol is not None:
            assert_strength(backend, pol)
        return backend

    # ── 自报 ──────────────────────────────────────────────────
    def report(self, *, refresh: bool = False) -> list[dict]:
        """`[{"name","strength","available","reason"}]` —— 每个 prefer 项一条。

        未知后端名也会出现（`strength="unknown"`, `available=False`），
        因为配置里拼错的 `"gvisor"` 必须可见，不能被静默丢弃。
        """
        rows: list[dict] = []
        for raw_name in self.prefer:
            key = str(raw_name).strip().lower()
            backend = self._build(raw_name)
            if backend is None:
                rows.append({
                    "name": str(raw_name),
                    "strength": "unknown",
                    "available": False,
                    "reason": f"unknown backend name: {raw_name!r}"
                              f"（已知 {sorted(_BACKEND_NAMES)}）",
                })
                continue
            if refresh:
                backend.reset()
            ok = backend.available()
            rows.append({
                "name": backend.name,
                "strength": backend.strength,
                "available": bool(ok),
                "reason": backend.reason or ("available" if ok else "unknown reason"),
            })
        return rows

    def __repr__(self) -> str:                             # pragma: no cover
        return f"<BackendSelector prefer={self.prefer!r}>"


# ══════════════════════════════════════════════════════════════
# 统一入口：两道闸门 + 可用性 + 执行
# ══════════════════════════════════════════════════════════════
def run_guarded(backend: Any, argv: Sequence[str], *, policy: SandboxPolicy,
                cwd: str | None = None, timeout_ms: int | None = None,
                env: dict | None = None) -> dict:
    """执行一次命令，**先把两道闸门走完**，任何一道不过就拒绝。

    顺序固定：
      1. `guard_argv`     —— 形状/白名单/元字符                  → ValueError / PermissionError
      2. `backend.available()`                                   → SandboxUnavailable
      3. `assert_strength` —— `policy.require_backend` 不满足     → SandboxStrengthUnmet
      4. `backend.run`    —— 超时                                → SandboxTimeout

    强制点：执行时把 `backend.policy` 临时设为本函数的 `policy`，
    确保「闸门用的策略」与「执行时的 rlimit/截断策略」是同一份，
    不会出现「按 A 策略放行、按 B 策略执行」的双标（结束时恢复原值）。
    """
    if not isinstance(policy, SandboxPolicy):
        raise TypeError(f"policy 必须是 SandboxPolicy，收到 {type(policy).__name__}")
    guard_argv(argv, policy)
    if not backend.available():
        raise SandboxUnavailable(
            f"后端 {getattr(backend, 'name', '?')} 不可用："
            f"{getattr(backend, 'reason', '') or '未知原因'}")
    assert_strength(backend, policy)

    original = getattr(backend, "policy", None)
    try:
        if isinstance(backend, _BaseBackend):
            backend.policy = policy
        return backend.run(argv, cwd=cwd, timeout_ms=timeout_ms, env=env)
    finally:
        if isinstance(backend, _BaseBackend):
            backend.policy = original if original is not None else policy
