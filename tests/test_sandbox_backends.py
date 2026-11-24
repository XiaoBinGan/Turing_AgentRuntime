"""tests/test_sandbox_backends.py — W27 执行隔离后端 SPI 的单测。

约束（本机事实，测试必须遵守）：
* 本机 **有** `/usr/local/bin/docker` 但**没有** daemon，也**没有** nsjail。
  所以全部容器/内核后端的可用性判定走 `probe=` / `which=` 注入，
  执行层走 `backend._execute` monkeypatch（实例属性覆盖绑定方法）——
  **绝不真的调用 docker / nsjail**。
* 超时用例靠真实子进程（`python3` 脚本 + `sleep` 孙进程），验证「杀的是进程组」。
* 断言里不假设任何具体路径 / 用户名。

跑法：
    cd /Users/supre/Documents/tmp/agent-runtime && python3 -m unittest tests.test_sandbox_backends -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import SandboxPolicy  # noqa: E402
from plane.sandbox_backends import (  # noqa: E402
    DEFAULT_DOCKER_IMAGE,
    DEFAULT_PREFER,
    SHELL_META,
    STRENGTH_RANK,
    BackendSelector,
    DockerBackend,
    NsjailBackend,
    NullBackend,
    SandboxStrengthUnmet,
    SandboxTimeout,
    SandboxUnavailable,
    SubprocessBackend,
    assert_strength,
    enforceable_rlimits,
    guard_argv,
    run_guarded,
    strength_rank,
)
from plane.sandbox_tool import DEFAULT_ALLOWED_COMMANDS, HandlerSandbox  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _wait_gone(pid: int, timeout_s: float = 5.0) -> bool:
    """轮询直到 pid 消失（已回收/已死）。返回是否消失。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:          # pragma: no cover — 进程还在但换了属主
            return False
        time.sleep(0.02)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:              # pragma: no cover
        return True
    return True


class _FakeExec:
    """假执行层：记录 argv，返回预设输出。永不启动真实进程。"""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs) -> dict:
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        return {"exit_code": self.exit_code, "stdout": self.stdout,
                "stderr": self.stderr}


class _StubBackend:
    """最小后端替身：只实现契约，用于强度闸门与 run_guarded 的旁路测试。"""

    name = "stub"
    strength = "none"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def available(self) -> bool:
        return True

    def run(self, argv, *, cwd=None, timeout_ms=None, env=None) -> dict:
        self.calls.append(list(argv))
        return {"exit_code": 0, "stdout": "stub\n", "stderr": "",
                "duration_ms": 0.0, "argv": list(argv), "backend": self.name,
                "strength": self.strength, "truncated": False}


# ══════════════════════════════════════════════════════════════
# 1-7：SubprocessBackend
# ══════════════════════════════════════════════════════════════
class TestSubprocessBackend(unittest.TestCase):
    def test_01_runs_echo(self) -> None:
        """真实子进程执行 echo：exit_code 0、stdout 正确、结果字段齐全。"""
        box = SubprocessBackend(policy=SandboxPolicy())
        got = box.run(["echo", "hi"])
        self.assertEqual(got["exit_code"], 0)
        self.assertEqual(got["stdout"], "hi\n")
        self.assertEqual(got["stderr"], "")
        self.assertEqual(got["argv"], ["echo", "hi"])
        for key in ("exit_code", "stdout", "stderr", "duration_ms", "argv",
                    "backend", "strength", "truncated"):
            self.assertIn(key, got)
        self.assertGreaterEqual(got["duration_ms"], 0.0)

    def test_02_not_in_allowlist(self) -> None:
        """白名单外命令 → PermissionError（且根本没启动进程）。"""
        box = SubprocessBackend(policy=SandboxPolicy())   # 默认白名单无 rm
        with self.assertRaises(PermissionError):
            box.run(["rm", "-rf", "/"])

    def test_02b_bare_command_name_only(self) -> None:
        """白名单按 basename 比对，但不能用路径执行同名二进制。"""
        box = SubprocessBackend(policy=SandboxPolicy())
        with self.assertRaises(PermissionError):
            box.run(["/bin/echo", "x"])

    def test_03_shell_meta_rejected(self) -> None:
        """元字符注入 → PermissionError（逐字符覆盖 SHELL_META）。"""
        box = SubprocessBackend(policy=SandboxPolicy())
        for ch in sorted(SHELL_META):
            with self.subTest(meta=ch):
                with self.assertRaises(PermissionError):
                    box.run(["echo", f"a{ch}id"])
        # 经典注入串
        for payload in ("; id", "`id`", "$(id)", "a|b", "a&&b", "a>b", "x\nid"):
            with self.subTest(payload=payload):
                with self.assertRaises(PermissionError):
                    box.run(["echo", payload])

    def test_04_shape_errors(self) -> None:
        """空 argv / 非 list / 非 str 元素 → ValueError。"""
        box = SubprocessBackend(policy=SandboxPolicy())
        with self.assertRaises(ValueError):
            box.run([])
        with self.assertRaises(ValueError):
            box.run("echo hi")                        # str 不是 list
        with self.assertRaises(ValueError):
            box.run(("echo", "hi"))                   # tuple 也不是 list
        with self.assertRaises(ValueError):
            box.run(None)
        with self.assertRaises(ValueError):
            box.run(["echo", b"bytes"])               # 字节串逃逸

    def test_05_timeout_kills_process_group(self) -> None:
        """超时抛 SandboxTimeout，且**孙进程**一起死（不是只杀直接子进程）。"""
        script = Path(tempfile.mkdtemp(prefix="w27-")) / "spawn.py"
        self.addCleanup(shutil.rmtree, script.parent, True)   # 清理临时脚本，不留垃圾
        pidfile = script.parent / "child.pid"
        script.write_text(textwrap.dedent(
            """
            import subprocess, sys, time
            child = subprocess.Popen(["sleep", "30"])
            open(sys.argv[1], "w").write(str(child.pid))
            time.sleep(30)
            """
        ), encoding="utf-8")

        policy = SandboxPolicy(allowed_commands=("python3",), timeout_ms=400,
                               max_cpu_seconds=30)
        box = SubprocessBackend(policy=policy)
        started = time.monotonic()
        with self.assertRaises(SandboxTimeout) as ctx:
            box.run(["python3", str(script), str(pidfile)], timeout_ms=400)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0, "超时后没有及时返回")
        self.assertIsInstance(ctx.exception, TimeoutError)   # 兼容内置类型
        self.assertIn("timeout", str(ctx.exception))

        grandchild = int(pidfile.read_text(encoding="utf-8").strip())
        self.assertTrue(_wait_gone(grandchild),
                        f"孙进程 {grandchild} 未随进程组被杀 —— 进程组逃逸")

    def test_06_stdout_truncated(self) -> None:
        """stdout 超过 max_output_bytes → 字节级截断 + truncated=True。"""
        box = SubprocessBackend(policy=SandboxPolicy(max_output_bytes=64))
        got = box.run(["echo", "x" * 500])
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["stdout"].encode("utf-8")), 64)

    def test_06b_no_truncation_flag_when_small(self) -> None:
        box = SubprocessBackend(policy=SandboxPolicy(max_output_bytes=4096))
        got = box.run(["echo", "ok"])
        self.assertFalse(got["truncated"])

    def test_07_strength_and_name(self) -> None:
        """strength 如实申报 "process"，结果里带 backend/strength 便于审计。"""
        box = SubprocessBackend(policy=SandboxPolicy())
        self.assertEqual(box.name, "subprocess")
        self.assertEqual(box.strength, "process")
        got = box.run(["echo", "x"])
        self.assertEqual(got["backend"], "subprocess")
        self.assertEqual(got["strength"], "process")
        self.assertTrue(box.available())
        self.assertTrue(box.reason)

    def test_07b_missing_binary_is_unavailable_not_crash(self) -> None:
        """白名单里放了一个本机没装的命令 → SandboxUnavailable（可归因）。"""
        box = SubprocessBackend(policy=SandboxPolicy(
            allowed_commands=("definitely-not-a-real-binary-w27",)))
        with self.assertRaises(SandboxUnavailable):
            box.run(["definitely-not-a-real-binary-w27"])

    def test_07c_rlimits_are_best_effort_but_reported(self) -> None:
        """rlimit 拒绝不能打死执行；真实生效项由 enforceable_rlimits 探测。

        Darwin 内核拒绝 RLIMIT_AS/DATA（`current limit exceeds maximum limit`），
        所以这里不能断言 AS=True；能断言的是：执行照常成功、CPU 限制可真设。
        """
        box = SubprocessBackend(policy=SandboxPolicy(max_memory_mb=512))
        got = box.run(["echo", "still-runs"])
        self.assertEqual(got["exit_code"], 0)
        limits = enforceable_rlimits(SandboxPolicy(max_memory_mb=512))
        self.assertIn("RLIMIT_CPU", limits)
        self.assertTrue(limits["RLIMIT_CPU"], "CPU 限制应当能设上")
        self.assertIsInstance(limits["RLIMIT_AS"], bool)   # 平台相关，但必须可查


# ══════════════════════════════════════════════════════════════
# 8-11：DockerBackend（全部注入，不碰真实 daemon）
# ══════════════════════════════════════════════════════════════
class TestDockerBackend(unittest.TestCase):
    def test_08_available_false_without_cli(self) -> None:
        """which 注入返回 None → available False，reason 说明 not found in PATH。"""
        box = DockerBackend(which=lambda _name: None, policy=SandboxPolicy())
        self.assertFalse(box.available())
        self.assertIn("not found in PATH", box.reason)

    def test_09_available_false_when_daemon_down(self) -> None:
        """CLI 在但 `docker info` 退出非 0 → available False（两段式探测）。"""
        box = DockerBackend(which=lambda _name: "/usr/local/bin/docker",
                            policy=SandboxPolicy())
        box._probe_daemon = lambda: (1, "Cannot connect to the Docker daemon")  # noqa: SLF001
        self.assertFalse(box.available())
        self.assertIn("docker info failed: exit 1", box.reason)

    def test_09b_available_true_when_daemon_ok(self) -> None:
        box = DockerBackend(which=lambda _name: "/usr/local/bin/docker",
                            policy=SandboxPolicy())
        box._probe_daemon = lambda: (0, "27.0.0")          # noqa: SLF001
        self.assertTrue(box.available())
        self.assertEqual(box.strength, "container")

    def test_09c_probe_says_yes_without_touching_which(self) -> None:
        """probe 注入优先：不查 which、不跑 docker info（宿主无 docker 也能测）。"""
        def _boom(_name):
            raise AssertionError("probe 生效时不该调用 which")

        box = DockerBackend(probe=lambda name: True, which=_boom,
                            policy=SandboxPolicy())
        self.assertTrue(box.available())
        self.assertEqual(box.reason, "probe says yes")

    def test_09d_daemon_probe_timeout_is_reported(self) -> None:
        """`docker info` 卡死（SandboxTimeout）→ exit 124 + 明确理由，不静默可用。"""
        box = DockerBackend(which=lambda _name: "/usr/local/bin/docker",
                            policy=SandboxPolicy())

        def _hang(argv, **kwargs):
            raise SandboxTimeout("timeout", argv=argv)

        box._execute = _hang                                 # noqa: SLF001
        self.assertFalse(box.available())
        self.assertIn("124", box.reason)

    def test_10_run_argv_has_isolation_flags(self) -> None:
        """run 构造的 argv 必须含 --network=none / --memory / --cpus 与镜像。"""
        fake = _FakeExec(stdout="in-container\n")
        box = DockerBackend(probe=lambda _n: True, policy=SandboxPolicy(max_memory_mb=256))
        box._execute = fake                                  # noqa: SLF001

        got = box.run(["echo", "hi"])
        self.assertEqual(got["exit_code"], 0)
        self.assertEqual(got["stdout"], "in-container\n")
        self.assertEqual(got["backend"], "docker")
        self.assertEqual(got["strength"], "container")
        self.assertEqual(len(fake.calls), 1)
        host = fake.calls[0]
        self.assertEqual(host[:3], ["docker", "run", "--rm"])
        self.assertIn("--network=none", host)
        self.assertIn("--memory=256m", host)
        self.assertIn("--cpus=1", host)
        self.assertIn(DEFAULT_DOCKER_IMAGE, host)
        # 容器内命令必须原样在镜像之后、顺序不变
        idx = host.index(DEFAULT_DOCKER_IMAGE)
        self.assertEqual(host[idx + 1:], ["echo", "hi"])
        # 逻辑 argv 与宿主 argv 分离：返回给上层的是「命令」，不是 docker 参数
        self.assertEqual(got["argv"], ["echo", "hi"])
        self.assertEqual(box.last_host_argv, host)

    def test_11_cwd_mapped_to_work_volume(self) -> None:
        """cwd → `-v CWD:/work -w /work`（绝对化，防相对路径歧义）。"""
        fake = _FakeExec()
        box = DockerBackend(probe=lambda _n: True, policy=SandboxPolicy())
        box._execute = fake                                  # noqa: SLF001

        box.run(["echo", "hi"], cwd="/tmp")
        host = fake.calls[0]
        self.assertIn("-v", host)
        self.assertIn(f"{os.path.abspath('/tmp')}:/work", host)
        self.assertEqual(host[host.index("-w") + 1], "/work")

    def test_11b_cwd_defaults_to_process_cwd(self) -> None:
        box = DockerBackend(policy=SandboxPolicy())
        host = box.build_argv(["echo", "hi"])
        self.assertIn(f"{os.path.abspath(os.getcwd())}:/work", host)

    def test_11c_allow_network_switches_network_mode(self) -> None:
        """`allow_network=True` 才放开网络（默认 none，不静默放行）。"""
        off = DockerBackend(policy=SandboxPolicy()).build_argv(["echo", "x"])
        on = DockerBackend(policy=SandboxPolicy(allow_network=True)).build_argv(["echo", "x"])
        self.assertIn("--network=none", off)
        self.assertIn("--network=bridge", on)

    def test_11d_docker_run_still_guards_argv(self) -> None:
        """容器里也不是法外之地：白名单与元字符照样拦（纵深）。"""
        box = DockerBackend(probe=lambda _n: True, policy=SandboxPolicy())
        box._execute = _FakeExec()                           # noqa: SLF001
        with self.assertRaises(PermissionError):
            box.run(["echo", "; id"])
        with self.assertRaises(PermissionError):
            box.run(["rm", "-rf", "/"])


# ══════════════════════════════════════════════════════════════
# 12-13：NsjailBackend
# ══════════════════════════════════════════════════════════════
class TestNsjailBackend(unittest.TestCase):
    def test_12_available_false_without_binary(self) -> None:
        box = NsjailBackend(which=lambda _name: None, policy=SandboxPolicy())
        self.assertFalse(box.available())
        self.assertIn("not found in PATH", box.reason)
        self.assertEqual(box.strength, "kernel")

    def test_12b_available_true_with_binary(self) -> None:
        box = NsjailBackend(which=lambda _name: "/usr/local/bin/nsjail",
                            policy=SandboxPolicy())
        self.assertTrue(box.available())
        self.assertIn("nsjail", box.reason)

    def test_13_run_argv_has_kernel_flags(self) -> None:
        """run 的 argv 含 --time_limit 与 --disable_proc，命令在 `--` 之后。"""
        fake = _FakeExec(stdout="jailed\n")
        box = NsjailBackend(probe=lambda _n: True,
                            policy=SandboxPolicy(timeout_ms=2000, max_memory_mb=128,
                                                 max_cpu_seconds=3))
        box._execute = fake                                  # noqa: SLF001

        got = box.run(["echo", "hi"])
        self.assertEqual(got["stdout"], "jailed\n")
        self.assertEqual(got["backend"], "nsjail")
        self.assertEqual(got["strength"], "kernel")
        host = fake.calls[0]
        self.assertEqual(host[0], "nsjail")
        self.assertIn("--disable_proc", host)
        self.assertIn("--iface_no_lo", host)
        self.assertIn("--time_limit", host)
        self.assertEqual(host[host.index("--time_limit") + 1], "2")   # 2000ms → 2s
        self.assertEqual(host[host.index("--rlimit_as") + 1], "128")
        self.assertEqual(host[host.index("--rlimit_cpu") + 1], "3")
        self.assertEqual(host[-3:], ["--", "echo", "hi"])
        self.assertEqual(got["argv"], ["echo", "hi"])

    def test_13b_time_limit_rounds_up(self) -> None:
        """1.2s 不能被砍成 1s（可能把正常执行提前杀死）。"""
        box = NsjailBackend(policy=SandboxPolicy(timeout_ms=1200))
        host = box.build_argv(["echo", "x"])
        self.assertEqual(host[host.index("--time_limit") + 1], "2")


# ══════════════════════════════════════════════════════════════
# 14：NullBackend
# ══════════════════════════════════════════════════════════════
class TestNullBackend(unittest.TestCase):
    def test_14_always_refuses(self) -> None:
        box = NullBackend(policy=SandboxPolicy())
        self.assertFalse(box.available())
        self.assertEqual(box.strength, "none")
        self.assertTrue(box.reason)
        with self.assertRaises(SandboxUnavailable):
            box.run(["echo", "hi"])
        # 连形状错误也优先给「不可用」，不冒充成执行错误
        with self.assertRaises(SandboxUnavailable):
            box.run([])


# ══════════════════════════════════════════════════════════════
# 15-17 / 21：BackendSelector
# ══════════════════════════════════════════════════════════════
class TestBackendSelector(unittest.TestCase):
    def test_15_picks_first_available_in_prefer_order(self) -> None:
        """只有 docker 可用 → 选 docker（即使 subprocess 也可用）。"""
        sel = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                              probe=lambda name: name == "docker")
        self.assertEqual(sel.pick().name, "docker")
        self.assertEqual(sel.pick().strength, "container")

        sel2 = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                               probe=lambda name: name in ("nsjail", "docker"))
        self.assertEqual(sel2.pick().name, "nsjail")

    def test_15b_falls_through_to_subprocess(self) -> None:
        """强隔离都缺失 → 降级到 subprocess（strength 如实是 process）。"""
        sel = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                              probe=lambda name: name == "subprocess")
        picked = sel.pick()
        self.assertEqual(picked.name, "subprocess")
        self.assertEqual(picked.strength, "process")

    def test_16_all_unavailable_returns_null(self) -> None:
        sel = BackendSelector(probe=lambda _name: False)
        picked = sel.pick()
        self.assertIsInstance(picked, NullBackend)
        self.assertEqual(picked.name, "null")
        with self.assertRaises(SandboxUnavailable):
            picked.run(["echo", "hi"])

    def test_16b_unknown_backend_name_ignored_but_reported(self) -> None:
        """配置里写错的后端名（如 gvisor）不能静默消失。"""
        sel = BackendSelector(prefer=("gvisor", "subprocess"),
                              probe=lambda name: name == "subprocess")
        self.assertEqual(sel.pick().name, "subprocess")
        rows = sel.report()
        self.assertEqual(rows[0]["name"], "gvisor")
        self.assertFalse(rows[0]["available"])
        self.assertEqual(rows[0]["strength"], "unknown")
        self.assertIn("unknown backend name", rows[0]["reason"])

    def test_17_report_explains_every_backend(self) -> None:
        """report 每个后端都要有可读 reason（不能只给 False）。"""
        sel = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                              probe=lambda name: name == "subprocess")
        rows = sel.report()
        self.assertEqual([r["name"] for r in rows], ["nsjail", "docker", "subprocess"])
        for row in rows:
            self.assertIn("name", row)
            self.assertIn("strength", row)
            self.assertIn("available", row)
            self.assertTrue(row["reason"], f"{row['name']} 缺少 reason")
        self.assertEqual(rows[0]["reason"], "probe says no")
        self.assertFalse(rows[0]["available"])
        self.assertEqual(rows[2]["strength"], "process")
        self.assertTrue(rows[2]["available"])

    def test_17b_report_reasons_without_probe(self) -> None:
        """无 probe 时理由来自真实探测：which 失败 / docker info 失败。"""
        sel = BackendSelector(prefer=("nsjail", "docker"))
        rows = sel.report(refresh=True)
        self.assertEqual(rows[0]["reason"], "not found in PATH")
        self.assertFalse(rows[0]["available"])
        docker_row = rows[1]
        if not docker_row["available"]:      # 本机无 daemon 的典型分支
            self.assertTrue(docker_row["reason"].startswith("docker info failed")
                            or docker_row["reason"] == "not found in PATH")

    def test_17c_strength_rank_is_monotonic(self) -> None:
        self.assertLess(strength_rank("none"), strength_rank("process"))
        self.assertLess(strength_rank("process"), strength_rank("container"))
        self.assertLess(strength_rank("container"), strength_rank("kernel"))
        self.assertEqual(strength_rank("garbage"), 0)     # 拼错按最弱处理
        self.assertEqual(list(STRENGTH_RANK), ["none", "process", "container", "kernel"])
        self.assertEqual(DEFAULT_PREFER, ("nsjail", "docker", "subprocess"))

    def test_21_pick_is_deterministic(self) -> None:
        """同 prefer 两次构造 + 多次 pick，结果与 report 完全一致。"""
        sel = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                              probe=lambda name: name == "docker")
        first, second = sel.pick(), sel.pick()
        self.assertEqual((first.name, first.strength), (second.name, second.strength))
        self.assertIs(first, second)          # 缓存实例：不会两次之间改主意

        other = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                                probe=lambda name: name == "docker")
        self.assertEqual(sel.pick().name, other.pick().name)
        self.assertEqual(sel.report(), sel.report())
        self.assertEqual(sel.report(), other.report())


# ══════════════════════════════════════════════════════════════
# 18-20：require_backend 的 fail-closed
# ══════════════════════════════════════════════════════════════
class TestRequireBackend(unittest.TestCase):
    def test_18_container_required_but_only_subprocess_refuses(self) -> None:
        """require_backend="container" + 只有 subprocess → 拒绝执行（不是降级）。"""
        fake = _FakeExec()
        box = SubprocessBackend(policy=SandboxPolicy())
        box._execute = fake                                  # noqa: SLF001
        policy = SandboxPolicy(require_backend="container")

        with self.assertRaises(SandboxStrengthUnmet) as ctx:
            run_guarded(box, ["echo", "hi"], policy=policy)
        message = str(ctx.exception)
        self.assertIn("container", message)
        self.assertIn("process", message)
        self.assertIn("fail-closed", message)
        # 关键：命令**一次都没执行**（fail-closed 而不是 fail-then-warn）
        self.assertEqual(fake.calls, [])

    def test_18b_kernel_required_message_names_both_levels(self) -> None:
        """「要求 kernel 级，实际 subprocess 级」必须出现在错误信息里。"""
        box = SubprocessBackend(policy=SandboxPolicy())
        box._execute = _FakeExec()                           # noqa: SLF001
        with self.assertRaises(SandboxStrengthUnmet) as ctx:
            run_guarded(box, ["echo", "hi"],
                        policy=SandboxPolicy(require_backend="kernel"))
        message = str(ctx.exception)
        self.assertIn("kernel", message)
        self.assertIn("subprocess", message)
        self.assertIn("process 级", message)

    def test_18c_selector_pick_or_refuse(self) -> None:
        """选择器侧入口：pick_or_refuse 在强度不足时同样拒绝。"""
        sel = BackendSelector(prefer=("nsjail", "docker", "subprocess"),
                              probe=lambda name: name == "subprocess")
        with self.assertRaises(SandboxStrengthUnmet):
            sel.pick_or_refuse(SandboxPolicy(require_backend="container"))
        picked = sel.pick_or_refuse(SandboxPolicy(require_backend="process"))
        self.assertEqual(picked.name, "subprocess")

    def test_18d_unavailable_backend_refused_before_strength(self) -> None:
        """不可用后端在强度校验之前就被拒（理由更准确）。"""
        box = NullBackend(policy=SandboxPolicy())
        with self.assertRaises(SandboxUnavailable) as ctx:
            run_guarded(box, ["echo", "hi"], policy=SandboxPolicy())
        self.assertNotIsInstance(ctx.exception, SandboxStrengthUnmet)
        self.assertIn("null", str(ctx.exception))

    def test_19_process_required_passes(self) -> None:
        """require_backend="process" 且 subprocess 可用 → 正常执行。"""
        got = run_guarded(SubprocessBackend(policy=SandboxPolicy()), ["echo", "go"],
                          policy=SandboxPolicy(require_backend="process"))
        self.assertEqual(got["exit_code"], 0)
        self.assertEqual(got["stdout"], "go\n")
        self.assertEqual(got["strength"], "process")

    def test_19b_none_required_passes_on_null_strength_backend(self) -> None:
        self.assertIsNone(assert_strength(_StubBackend(), SandboxPolicy(require_backend="none")))

    def test_20_empty_require_backend_skips_strength_check(self) -> None:
        """require_backend="" → 完全不检查强度（弱后端也能放行）。"""
        stub = _StubBackend()
        self.assertIsNone(assert_strength(stub, SandboxPolicy()))
        got = run_guarded(stub, ["echo", "hi"], policy=SandboxPolicy())
        self.assertEqual(got["backend"], "stub")
        self.assertEqual(stub.calls, [["echo", "hi"]])
        # 同一后端一旦要求 process 级就被拒，说明差别只来自策略
        with self.assertRaises(SandboxStrengthUnmet):
            assert_strength(stub, SandboxPolicy(require_backend="process"))

    def test_20b_unknown_requirement_is_value_error(self) -> None:
        with self.assertRaises(ValueError):
            assert_strength(SubprocessBackend(policy=SandboxPolicy()),
                            SandboxPolicy(require_backend="gvisor"))

    def test_20c_run_guarded_gates_argv_before_execution(self) -> None:
        """闸门顺序：形状/白名单先于执行，一条非法命令不产生任何进程。"""
        fake = _FakeExec()
        box = SubprocessBackend(policy=SandboxPolicy())
        box._execute = fake                                  # noqa: SLF001
        with self.assertRaises(PermissionError):
            run_guarded(box, ["echo", "; id"], policy=SandboxPolicy())
        with self.assertRaises(ValueError):
            run_guarded(box, [], policy=SandboxPolicy())
        self.assertEqual(fake.calls, [])

    def test_20d_run_guarded_applies_policy_at_execution_time(self) -> None:
        """执行时用的策略 = 闸门用的策略（不留「按 A 放行、按 B 执行」的双标）。"""
        box = SubprocessBackend(policy=SandboxPolicy(max_output_bytes=8192))
        box._execute = _FakeExec(stdout="y" * 400)           # noqa: SLF001
        got = run_guarded(box, ["echo", "hi"],
                          policy=SandboxPolicy(max_output_bytes=32))
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["stdout"].encode("utf-8")), 32)
        # 结束后恢复原策略，不污染后端实例
        self.assertEqual(box.policy.max_output_bytes, 8192)


# ══════════════════════════════════════════════════════════════
# 22：guard_argv 与 plane/sandbox_tool.py 口径一致（16 条对照语料）
# ══════════════════════════════════════════════════════════════
class TestGuardParity(unittest.TestCase):
    #: 16 条对照语料（形状均为 list[str]，类型错误另由 test_04 锁定）
    CORPUS: tuple[list[str], ...] = (
        ["echo", "hi"],
        ["ls", "-1", "."],
        ["python3", "-c", "print(1)"],
        [],
        ["rm", "-rf", "/"],
        ["whoami"],
        ["/bin/echo", "x"],
        ["echo", "; id"],
        ["echo", "a|b"],
        ["echo", "a&b"],
        ["echo", "$HOME"],
        ["echo", "`id`"],
        ["echo", "a>b"],
        ["echo", "a<b"],
        ["echo", "a\nb"],
        ["echo", "a\rb"],
    )

    def test_22_guard_argv_parity(self) -> None:
        """16 条语料逐条断言：guard_argv 与 HandlerSandbox.gate 结论相同。"""
        self.assertEqual(len(self.CORPUS), 16, "对照语料必须是 16 条")

        allow = list(DEFAULT_ALLOWED_COMMANDS)
        policy = SandboxPolicy(allowed_commands=tuple(allow))
        mine_box = HandlerSandbox(allowlist=allow, timeout_ms=2000)

        def verdict(call) -> str | None:
            try:
                call()
            except Exception as exc:          # noqa: BLE001 — 比对的就是异常类型
                return type(exc).__name__
            return None

        for argv in self.CORPUS:
            with self.subTest(argv=argv):
                theirs = verdict(lambda a=argv: mine_box.gate(a))
                ours = verdict(lambda a=argv: guard_argv(a, policy))
                self.assertEqual(ours, theirs,
                                 f"{argv!r}: ours={ours} theirs={theirs}")

    def test_22b_metachar_set_matches_w3_definition(self) -> None:
        """元字符集合必须与 plane/tool.py 的定义完全一致（防止悄悄放宽）。"""
        from plane.tool import SubprocessSandbox
        self.assertEqual(SHELL_META, SubprocessSandbox.SHELL_META)


if __name__ == "__main__":
    unittest.main(verbosity=2)
