"""tests/test_store.py — W14：ADR-7 持久化续跑（stdlib unittest，无 pytest）。

覆盖（对应任务书 ①~④ 的 20 条验收 + 若干补充）：

- MemoryStore / FileStore 存取闭环、覆盖而非追加、delete 语义、list_pending 升序
- ctx_id 路径安全（`../x`、`a/b`、空串、`..`、绝对路径、glob 元字符）
- 信封校验：非 JSON → StoreCorrupt；schema_version 不认识 → StoreSchemaMismatch
- **原子性三种证法**：
  (a) save 后不留 `.tmp`；
  (b) `CrashStore` 在 `os.replace` **之前**写一半就死 → 旧文件字节级完好，
      且现场证据（`crashes[-1]["prefix"]`）本身不是合法 JSON ——
      证明"如果 rename 发生了，坏文件就会顶上去"；
  (c) 子进程真 SIGKILL（不跑任何清理代码）→ 既有文件仍是完整 JSON，
      孤儿 `.tmp` 不被 `list_pending()` 认。
- `save_checkpoint` / `load_checkpoint` round-trip：version 与 patch_log 完全一致
- `list_resumable` 读出 phase/task_id；坏条目降级成 error 条目而不整体崩
- 端到端崩溃续跑：挂起在 WAITING_USER → 落盘 → **丢弃原 Context** →
  `resume_from_store(decision=True)` → DONE；decision=False → 继续循环；
  正常结束 → pending 里不再有它；再次挂起 → 又被重新落盘

约定：Loop 的依赖全部是本地 fake（不 import demo/）；只用 contracts 的
Protocol 形状 + 真实的 `ContextHandle`（存的是它的序列化产物，必须用真的）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from contracts import (  # noqa: E402
    Budget,
    ContextSnapshot,
    ExecCtx,
    LLMResponse,
    NormalizedToolCall,
    Owner,
    Patch,
    Phase,
    Principal,
    SideEffect,
    TermReason,
    ToolManifest,
    ToolOutcome,
    ToolResult,
    Usage,
)
from core.context import ContextHandle  # noqa: E402
from core.loop import Loop              # noqa: E402
from core.store import (                # noqa: E402
    DEFAULT_ROOT,
    STORE_SCHEMA_VERSION,
    ContextNotFound,
    CrashStore,
    FileStore,
    MemoryStore,
    StoreCorrupt,
    StoreError,
    StoreSchemaMismatch,
    list_resumable,
    load_checkpoint,
    resume_from_store,
    save_checkpoint,
    validate_ctx_id,
)


# ══════════════════════════════════════════════════════════════
# fakes（Loop 的依赖：builder / llm / dispatcher）
# ══════════════════════════════════════════════════════════════
def resp(text: str = "", calls=(), finish: str = "stop", itok: int = 10, otok: int = 5):
    """构造 LLMResponse；calls 形如 [(id, name, args), ...]。"""
    return LLMResponse(
        text=text,
        tool_calls=tuple(NormalizedToolCall(id=c[0], name=c[1], args=c[2]) for c in calls),
        usage=Usage(input_tokens=itok, output_tokens=otok, model="mock-1"),
        finish_reason=finish,
    )


class ScriptedLLM:
    """按脚本吐响应（脚本用完就重复最后一条）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt, tools, e):
        index = self.calls
        self.calls += 1
        self.prompts.append(prompt)
        if not self.script:
            return resp(text="（脚本为空）")
        if index < len(self.script):
            return self.script[index]
        return self.script[-1]


class EchoDispatcher:
    """表驱动 dispatcher：名字 → ToolOutcome / [ToolOutcome...] / callable。"""

    manifests = [
        ToolManifest(name="lookup", side_effect=SideEffect.READ_ONLY),
        ToolManifest(name="write_note", side_effect=SideEffect.MUTATING),
    ]

    def __init__(self, table: dict | None = None):
        self.table = {k: list(v) if isinstance(v, list) else v
                      for k, v in (table or {}).items()}
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, name, args, e):
        self.calls.append((name, dict(args)))
        entry = self.table.get(name)
        if entry is None:
            return ToolOutcome.ok(ToolResult(tool=name, payload={"echo": args}))
        if callable(entry):
            return entry(name, args, e)
        if isinstance(entry, list):
            return entry.pop(0) if len(entry) > 1 else entry[0]
        return entry

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class PlainBuilder:
    """把 history 拼成 prompt —— 让断言能从 prompt 里看到"续跑接着哪段历史"。

    像真 Builder 一样把 `tool_calls`（工具名 + 参数）也渲染进去，否则"重启前的
    工具调用"这段上下文就丢了，续跑测试也就测不出东西。
    """

    def __init__(self):
        self.snapshots: list[ContextSnapshot] = []

    def build(self, snap, tools):
        self.snapshots.append(snap)
        history = snap.get("history") or []
        parts: list[str] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            parts.append(str(message.get("text") or ""))
            calls = message.get("tool_calls")
            if calls:
                parts.append(json.dumps(calls, ensure_ascii=False, default=str))
        return "GOAL>" + " ".join(parts)


LOOKUP = resp(text="我先查一下。", calls=[("c1", "lookup", {"q": "alpha"})])
WRITE = resp(text="我记一条。", calls=[("c2", "write_note", {"text": "alpha done"})])
FINAL = resp(text="任务完成：alpha 已处理。")
NEEDS_OK = ToolOutcome.ok(ToolResult(tool="write_note", payload={"written": True}))


def make_exec(max_steps: int = 8) -> ExecCtx:
    return ExecCtx(principal=Principal(), budget=Budget(max_steps=max_steps))


def make_ctx(goal: str = "处理 alpha", task_id: str = "task_store") -> ContextHandle:
    """一个带 goal/task_id 的 Context（goal 是 `resume_from_store` 的续跑入口）。"""
    return ContextHandle(initial={
        "goal": goal,
        "task_id": task_id,
        "principal": Principal().to_dict(),
    })


def make_loop(script, table=None, builder=None):
    return Loop(builder or PlainBuilder(), ScriptedLLM(script), EchoDispatcher(table))


class StoreCase(unittest.TestCase):
    """每个用例一个独立临时目录，用例结束整棵删掉。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "store"
        self.addCleanup(self._tmp.cleanup)

    def new_store(self) -> FileStore:
        return FileStore(self.root)


# ══════════════════════════════════════════════════════════════
# 1. ctx_id 路径安全
# ══════════════════════════════════════════════════════════════
class TestCtxIdValidation(StoreCase):
    def test_01_traversal_and_separators_rejected(self):
        bad = ["../x", "a/b", "", "..", ".", "/etc/passwd", "a\\b", "..%2fx",
               ".hidden", "a b", "a*b", "x" * 200, "\x00"]
        for ctx_id in bad:
            with self.subTest(ctx_id=ctx_id):
                with self.assertRaises(ValueError):
                    validate_ctx_id(ctx_id)

    def test_01b_non_str_rejected(self):
        for ctx_id in (None, 1, b"abc", ["a"], Path("a")):
            with self.subTest(ctx_id=ctx_id):
                with self.assertRaises(ValueError):
                    validate_ctx_id(ctx_id)

    def test_02_all_store_ops_validate(self):
        """save / load / delete / saved_at 四个入口都不许绕开校验。"""
        store = self.new_store()
        mem = MemoryStore()
        for target in (store, mem):
            for ctx_id in ("../escape", "a/b", ""):
                with self.subTest(store=type(target).__name__, ctx_id=ctx_id):
                    with self.assertRaises(ValueError):
                        target.save(ctx_id, "{}")
                    with self.assertRaises(ValueError):
                        target.load(ctx_id)
                    with self.assertRaises(ValueError):
                        target.delete(ctx_id)

    def test_03_legal_ids_stay_inside_root(self):
        store = self.new_store()
        for ctx_id in ("ctx_A", "task_1f2e", "turn-3", "a.b", "_x"):
            with self.subTest(ctx_id=ctx_id):
                store.save(ctx_id, "payload")
                self.assertEqual(store.load(ctx_id), "payload")
        # 所有落盘文件都在 root 里（没有一个跑到上层目录）
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         sorted(["ctx_A.json", "task_1f2e.json", "turn-3.json",
                                 "a.b.json", "_x.json"]))


# ══════════════════════════════════════════════════════════════
# 2. MemoryStore
# ══════════════════════════════════════════════════════════════
class TestMemoryStore(unittest.TestCase):
    def test_04_roundtrip(self):
        store = MemoryStore()
        store.save("ctx_1", '{"k": 1}')
        self.assertEqual(store.load("ctx_1"), '{"k": 1}')
        self.assertTrue(store.exists("ctx_1"))
        self.assertEqual(len(store), 1)

    def test_05_load_missing_returns_none(self):
        self.assertIsNone(MemoryStore().load("nope"))

    def test_06_delete_semantics(self):
        store = MemoryStore()
        store.save("ctx_1", "x")
        self.assertTrue(store.delete("ctx_1"))
        self.assertFalse(store.delete("ctx_1"), "删第二次必须 False")
        self.assertIsNone(store.load("ctx_1"))

    def test_07_overwrite_not_append(self):
        store = MemoryStore()
        store.save("ctx_1", "v1")
        store.save("ctx_1", "v2")
        self.assertEqual(store.load("ctx_1"), "v2")
        self.assertEqual(store.list_pending(), ["ctx_1"])
        self.assertEqual(store.saves, 2)

    def test_08_list_pending_ascending(self):
        store = MemoryStore()
        for ctx_id in ("ctx_c", "ctx_a", "ctx_b"):
            store.save(ctx_id, ctx_id)
            time.sleep(0.002)
        self.assertEqual(store.list_pending(), ["ctx_c", "ctx_a", "ctx_b"],
                         "必须按 saved_at 升序，不是字母序")

    def test_09_payload_must_be_str(self):
        store = MemoryStore()
        for payload in ({"k": 1}, b"raw", None, 3):
            with self.subTest(payload=payload):
                with self.assertRaises(StoreError):
                    store.save("ctx_1", payload)

    def test_10_stores_expose_context_store_protocol(self):
        """两种 store 的形状必须对得上 contracts.ContextStore（structural typing）。"""
        with tempfile.TemporaryDirectory() as tmp:
            for store in (MemoryStore(), FileStore(tmp)):
                for name in ("save", "load", "delete", "list_pending"):
                    with self.subTest(store=type(store).__name__, method=name):
                        self.assertTrue(callable(getattr(store, name, None)))


# ══════════════════════════════════════════════════════════════
# 3. FileStore 基础行为
# ══════════════════════════════════════════════════════════════
class TestFileStore(StoreCase):
    def test_11_roundtrip_real_file(self):
        store = self.new_store()
        self.assertTrue(self.root.is_dir(), "__init__ 就该建好目录")
        store.save("ctx_1", '{"hello": "世界"}')
        path = self.root / "ctx_1.json"
        self.assertTrue(path.is_file())
        self.assertEqual(store.load("ctx_1"), '{"hello": "世界"}')
        # 原始字符串一字不改（不做任何 pretty-print / 转义改写）
        env = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(env["payload"], '{"hello": "世界"}')

    def test_12_envelope_shape(self):
        store = self.new_store()
        store.save("ctx_1", "PAYLOAD")
        env = json.loads((self.root / "ctx_1.json").read_text(encoding="utf-8"))
        self.assertEqual(set(env), {"ctx_id", "saved_at", "schema_version", "payload"})
        self.assertEqual(env["ctx_id"], "ctx_1")
        self.assertEqual(env["schema_version"], STORE_SCHEMA_VERSION)
        self.assertIsInstance(env["saved_at"], (int, float))
        self.assertLess(abs(env["saved_at"] - time.time() * 1000.0), 60_000)

    def test_13_load_missing_returns_none(self):
        self.assertIsNone(self.new_store().load("ghost"))

    def test_14_corrupt_json_raises(self):
        store = self.new_store()
        (self.root / "broken.json").write_text("{不是 json", encoding="utf-8")
        with self.assertRaises(StoreCorrupt):
            store.load("broken")

    def test_15_non_object_or_missing_field_raises(self):
        store = self.new_store()
        cases = {
            "arr": "[1, 2, 3]",
            "no_schema": json.dumps({"ctx_id": "no_schema", "saved_at": 1.0,
                                     "payload": "p"}),
            "bad_payload": json.dumps({"ctx_id": "bad_payload", "saved_at": 1.0,
                                       "schema_version": STORE_SCHEMA_VERSION,
                                       "payload": {"not": "a string"}}),
            "mismatch": json.dumps({"ctx_id": "someone_else", "saved_at": 1.0,
                                    "schema_version": STORE_SCHEMA_VERSION,
                                    "payload": "p"}),
        }
        for ctx_id, body in cases.items():
            with self.subTest(case=ctx_id):
                (self.root / f"{ctx_id}.json").write_text(body, encoding="utf-8")
                with self.assertRaises(StoreError):
                    store.load(ctx_id)

    def test_16_unknown_schema_version_raises(self):
        store = self.new_store()
        for schema in (STORE_SCHEMA_VERSION + 1, 0, -1, 999):
            ctx_id = f"v{schema}".replace("-", "m")
            (self.root / f"{ctx_id}.json").write_text(json.dumps({
                "ctx_id": ctx_id, "saved_at": 1.0,
                "schema_version": schema, "payload": "p",
            }), encoding="utf-8")
            with self.subTest(schema=schema):
                with self.assertRaises(StoreSchemaMismatch):
                    store.load(ctx_id)
        # 非整数版本号同样是"不认识"，不许猜
        (self.root / "vstr.json").write_text(json.dumps({
            "ctx_id": "vstr", "saved_at": 1.0,
            "schema_version": "1", "payload": "p",
        }), encoding="utf-8")
        with self.assertRaises(StoreSchemaMismatch):
            store.load("vstr")

    def test_17_delete_true_then_false(self):
        store = self.new_store()
        store.save("ctx_1", "x")
        self.assertTrue(store.delete("ctx_1"))
        self.assertFalse((self.root / "ctx_1.json").exists())
        self.assertFalse(store.delete("ctx_1"))
        self.assertIsNone(store.load("ctx_1"))

    def test_18_list_pending_ascending(self):
        store = self.new_store()
        for ctx_id in ("ctx_c", "ctx_a", "ctx_b"):
            store.save(ctx_id, ctx_id)
            time.sleep(0.002)
        self.assertEqual(store.list_pending(), ["ctx_c", "ctx_a", "ctx_b"])

    def test_19_overwrite_is_not_append(self):
        store = self.new_store()
        store.save("ctx_1", "v1")
        store.save("ctx_1", "v2")
        self.assertEqual(store.load("ctx_1"), "v2")
        self.assertEqual(len(list(self.root.glob("ctx_1*"))), 1,
                         "同一 ctx_id 只能有一个文件（覆盖，不追加）")
        self.assertLess((self.root / "ctx_1.json").stat().st_size, 4096)

    def test_20_two_instances_same_root_are_isolated_by_ctx_id(self):
        a = FileStore(self.root)
        b = FileStore(self.root)
        a.save("ctx_a", "from-a")
        b.save("ctx_b", "from-b")
        self.assertEqual(a.load("ctx_a"), "from-a")
        self.assertEqual(b.load("ctx_b"), "from-b")
        self.assertEqual(a.load("ctx_b"), "from-b", "同 root 的实例共享磁盘")
        self.assertEqual(sorted(a.list_pending()), ["ctx_a", "ctx_b"])
        # 互不覆盖
        a.save("ctx_a", "from-a-2")
        self.assertEqual(b.load("ctx_a"), "from-a-2")
        self.assertEqual(a.load("ctx_b"), "from-b")

    def test_21_default_root_constant(self):
        self.assertEqual(DEFAULT_ROOT, "/tmp/agent-runtime-store")
        self.assertTrue(DEFAULT_ROOT.startswith("/"))

    def test_22_root_exists_as_file_raises(self):
        weird = Path(self._tmp.name) / "not-a-dir"
        weird.write_text("x", encoding="utf-8")
        with self.assertRaises(StoreError):
            FileStore(weird)


# ══════════════════════════════════════════════════════════════
# 4. 原子性（三种证法）
# ══════════════════════════════════════════════════════════════
class TestAtomicity(StoreCase):
    def test_23_no_tmp_left_after_save(self):
        store = self.new_store()
        for i in range(5):
            store.save("ctx_1", f"v{i}" * 100)
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, ["ctx_1.json"], f"残留了临时文件：{names}")

    def test_24_crash_before_replace_keeps_old_file_intact(self):
        """核心证法：崩在 os.replace **之前**，旧版本必须字节级完好。"""
        seed = self.new_store()
        seed.save("ctx_1", "OLD-PAYLOAD")
        before = (self.root / "ctx_1.json").read_bytes()

        crashing = CrashStore(self.root, fail_at=1)
        with self.assertRaises(RuntimeError) as caught:
            crashing.save("ctx_1", "NEW-" * 2000)
        self.assertIn("simulated crash", str(caught.exception))

        # 1) 崩溃真的发生在写入窗口里：写了一半、总量更大
        self.assertEqual(len(crashing.crashes), 1)
        evidence = crashing.crashes[0]
        self.assertGreater(evidence["written"], 0)
        self.assertLess(evidence["written"], evidence["total"])
        # 2) 那半截内容本身不是合法 JSON —— 如果它被 rename 上去，读端就会炸
        with self.assertRaises(json.JSONDecodeError):
            json.loads(evidence["prefix"])
        # 3) 目标文件一个字节都没变
        self.assertEqual((self.root / "ctx_1.json").read_bytes(), before)
        self.assertEqual(crashing.load("ctx_1"), "OLD-PAYLOAD")

    def test_25_crash_first_save_leaves_nothing_readable(self):
        crashing = CrashStore(self.root, fail_at=1)
        with self.assertRaises(RuntimeError):
            crashing.save("ctx_first", "x" * 500)
        self.assertIsNone(crashing.load("ctx_first"), "没有旧版本时，不能读出一个半成品")
        self.assertEqual(crashing.list_pending(), [],
                         "list_pending 不许返回半成品")

    def test_26_crash_at_second_save_then_retry_succeeds(self):
        store = CrashStore(self.root, fail_at=2)
        store.save("ctx_1", "v1")                 # 第 1 次：成功
        with self.assertRaises(RuntimeError):
            store.save("ctx_1", "v2" * 3000)      # 第 2 次：断电
        self.assertEqual(store.load("ctx_1"), "v1")
        store.save("ctx_1", "v3")                 # 第 3 次：恢复后重试
        self.assertEqual(store.load("ctx_1"), "v3")
        self.assertEqual(store.list_pending(), ["ctx_1"])
        # 崩溃留下的孤儿临时文件不影响任何读路径，且 delete 会把它扫干净
        self.assertEqual(sorted(p.name for p in self.root.glob("*.json")), ["ctx_1.json"])

    def test_27_orphan_tmp_is_invisible_to_readers(self):
        """崩溃残留的半个临时文件：不算 pending、读不出来、delete 时被收敛清掉。"""
        store = self.new_store()
        store.save("ctx_1", "good")
        orphan = self.root / "ctx_orphan.tmp.999.deadbeef"
        orphan.write_text('{"ctx_id": "ctx_orphan", "saved_at": 1.0, "schema_ver',
                          encoding="utf-8")

        self.assertEqual(store.list_pending(), ["ctx_1"],
                         "只写了一半的 ctx_orphan 不算 pending 项")
        self.assertIsNone(store.load("ctx_orphan"), "半个文件不许被读成一个 Context")
        self.assertEqual(store.load("ctx_1"), "good")
        self.assertEqual(store.stats()["temps"], 1)
        self.assertFalse(store.delete("ctx_orphan"), "没有主文件 → False")
        # delete 顺手收敛：孤儿临时文件被清掉
        self.assertFalse(orphan.exists())

    def test_28_real_sigkill_never_leaves_partial_json(self):
        """真·断电：子进程在 save 循环里被 SIGKILL，不跑任何清理代码。

        同步方式刻意**不碰 stdout/stderr 管道**：管道 + readline 在"子进程还在写、
        父进程等一行"这种组合下会把并发跑测试的别的进程也拖住。这里改成轮询
        目标文件的字节变化——"文件变了"就证明至少有一次完整 replace 发生过。
        """
        base = self.new_store()
        base_payload = "BASE|" + "b" * 4096
        base.save("ctx_kill", base_payload)
        target = self.root / "ctx_kill.json"
        before = target.read_bytes()

        # 子进程最多写 1000 次就自己退出：即使 kill 因为任何原因没到，
        # 也不会留下一个永远自旋的进程（不给跑并发测试的同侪留地雷）。
        child = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from core.store import FileStore\n"
            "store = FileStore(sys.argv[2])\n"
            "for i in range(1000):\n"
            "    store.save('ctx_kill', 'BIG|' + 'B' * 65536 + '|%d' % i)\n"
        )
        err_path = Path(self._tmp.name) / "child.err"
        advanced = False
        with open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [sys.executable, "-c", child, ROOT, str(self.root)],
                stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True,          # 独立进程组，便于收尸
            )
            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    try:
                        if target.read_bytes() != before:
                            advanced = True
                            break
                    except FileNotFoundError:
                        pass
                    if proc.poll() is not None:
                        break
                    time.sleep(0.005)
            finally:
                proc.kill()                  # SIGKILL：清理代码与 finally 都不会执行
                proc.wait(timeout=30)
        if not advanced:
            self.fail("子进程没能完成任何一次 save：\n"
                      + err_path.read_text(encoding="utf-8")[-2000:])

        # 断言 1：主文件仍是**完整可解析**的 JSON（不是半个）
        env = json.loads(target.read_text(encoding="utf-8"))   # 解析失败 = 原子性破了
        self.assertEqual(env["schema_version"], STORE_SCHEMA_VERSION)
        # 断言 2：内容要么是旧的 base，要么是某个完整的新版本
        payload = env["payload"]
        if payload != base_payload:
            self.assertRegex(payload, r"^BIG\|B{65536}\|\d+$",
                             "新版本必须是完整写出的那一份（不是截断的）")
        # 断言 3：任何 *.json 都不能是坏文件
        for path in self.root.glob("*.json"):
            with self.subTest(file=path.name):
                json.loads(path.read_text(encoding="utf-8"))
        # 断言 4：半成品不进待处理清单
        self.assertEqual(base.list_pending(), ["ctx_kill"])
        # 断言 5：真的被 kill 了（不是自己跑完的）—— 保证这一轮确实在"写的中途"被打断
        self.assertIsNotNone(proc.returncode)


# ══════════════════════════════════════════════════════════════
# 5. checkpoint round-trip
# ══════════════════════════════════════════════════════════════
def rich_ctx() -> ContextHandle:
    """一个"有内容"的 Context：多字段、多 patch、非零预算。"""
    ctx = make_ctx(goal="处理 alpha", task_id="task_r1")
    ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.WAITING_USER.value))
    ctx.apply(Patch("set", "loop_count", Owner.LOOP, 3))
    ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "user", "text": "hi"}))
    ctx.apply(Patch("append", "history", Owner.LOOP, {"role": "assistant", "text": "好的"}))
    ctx.apply(Patch("set", "pending_confirmation", Owner.LOOP, {
        "tool": "write_note", "args": {"text": "x"}, "args_hash": "abc123",
        "step": 3, "remaining_calls": [],
    }))
    ctx.apply(Patch("merge", "usage", Owner.LOOP, {"total_tokens": 42, "calls": 2}))
    ctx.apply(Patch("incr", "budget.token_used", Owner.LOOP, 120))
    ctx.apply(Patch("incr", "budget.tool_calls_used", Owner.LOOP, 1))
    return ctx


class TestCheckpoint(StoreCase):
    def test_29_roundtrip_version_and_patch_log(self):
        store = self.new_store()
        ctx = rich_ctx()
        returned = save_checkpoint(ctx, store, "ctx_rt")
        self.assertEqual(returned, "ctx_rt")

        restored = load_checkpoint(store, "ctx_rt")
        self.assertIsInstance(restored, ContextHandle)
        self.assertEqual(restored.version, ctx.version, "version 必须逐一相等")
        self.assertEqual([p.to_dict() for p in restored.patch_log],
                         [p.to_dict() for p in ctx.patch_log],
                         "patch_log 必须逐一相等（op/path/by/value 全比）")
        self.assertEqual(restored.snapshot().data, ctx.snapshot().data)
        self.assertEqual(restored.budget.token_used, ctx.budget.token_used)
        self.assertEqual(restored.budget.tool_calls_used, ctx.budget.tool_calls_used)
        self.assertEqual(restored.get("pending_confirmation")["tool"], "write_note")

    def test_30_restored_context_is_independent(self):
        store = self.new_store()
        ctx = rich_ctx()
        save_checkpoint(ctx, store, "ctx_rt")
        restored = load_checkpoint(store, "ctx_rt")

        version_before = ctx.version
        restored.apply(Patch("incr", "loop_count", Owner.LOOP, 100))
        self.assertEqual(ctx.version, version_before, "恢复出来的 Context 必须与原件彻底隔离")
        self.assertNotEqual(restored.get("loop_count"), ctx.get("loop_count"))

    def test_31_reason_becomes_history_marker(self):
        store = self.new_store()
        ctx = rich_ctx()
        save_checkpoint(ctx, store, "ctx_r", reason="挂起等审批 4 小时")
        restored = load_checkpoint(store, "ctx_r")
        texts = [str(m.get("text") or "") for m in restored.get("history")]
        self.assertTrue(any("挂起等审批 4 小时" in t for t in texts), texts)
        # 无 reason 时不写标记（不给 history 添噪声）
        ctx2 = make_ctx()
        n = len(ctx2.get("history") or [])
        save_checkpoint(ctx2, store, "ctx_noreason")
        self.assertEqual(len(ctx2.get("history") or []), n)

    def test_32_budget_override_on_load(self):
        store = self.new_store()
        save_checkpoint(rich_ctx(), store, "ctx_b")
        fresh = Budget(token_total=777, token_used=5, max_steps=3)
        restored = load_checkpoint(store, "ctx_b", fresh)
        self.assertEqual(restored.budget.token_total, 777)
        self.assertEqual(restored.budget.token_used, 5)
        self.assertEqual(restored.budget.max_steps, 3)

    def test_33_load_missing_raises_not_found(self):
        store = self.new_store()
        with self.assertRaises(ContextNotFound) as caught:
            load_checkpoint(store, "ghost")
        self.assertIn("ghost", str(caught.exception))
        self.assertIsInstance(caught.exception, StoreError)

    def test_34_load_corrupt_context_payload(self):
        """信封完好但里面不是可恢复的 Context → StoreCorrupt（不猜）。"""
        store = self.new_store()
        store.save("ctx_bad", json.dumps({"schema_version": 999, "version": 0, "data": {}}))
        with self.assertRaises(StoreCorrupt):
            load_checkpoint(store, "ctx_bad")
        store.save("ctx_garbage", "not-json-at-all")
        with self.assertRaises(StoreCorrupt):
            load_checkpoint(store, "ctx_garbage")

    def test_35_save_checkpoint_needs_serializable_ctx(self):
        store = self.new_store()
        with self.assertRaises(StoreError):
            save_checkpoint(object(), store, "ctx_obj")


# ══════════════════════════════════════════════════════════════
# 6. list_resumable
# ══════════════════════════════════════════════════════════════
class TestListResumable(StoreCase):
    def test_36_reads_phase_task_id(self):
        store = self.new_store()
        save_checkpoint(rich_ctx(), store, "ctx_wait")
        waiting = make_ctx(goal="另一个任务", task_id="task_other")
        waiting.apply(Patch("set", "phase", Owner.LOOP, Phase.WAITING_USER))
        waiting.apply(Patch("set", "pending_confirmation", Owner.LOOP,
                            {"tool": "send_email", "args": {"to": "x"}}))
        save_checkpoint(waiting, store, "ctx_wait2")

        items = list_resumable(store)
        self.assertEqual([i["ctx_id"] for i in items], ["ctx_wait", "ctx_wait2"])
        first, second = items
        self.assertEqual(first["phase"], Phase.WAITING_USER.value)
        self.assertEqual(first["task_id"], "task_r1")
        self.assertEqual(first["pending_confirmation"]["tool"], "write_note")
        self.assertIsInstance(first["saved_at"], float)
        self.assertEqual(second["task_id"], "task_other")
        self.assertEqual(second["pending_confirmation"],
                         {"tool": "send_email", "args": {"to": "x"}})
        for item in items:
            self.assertNotIn("error", item)

    def test_37_bad_entries_degrade_to_error(self):
        store = self.new_store()
        save_checkpoint(rich_ctx(), store, "ctx_good")
        (self.root / "ctx_broken.json").write_text("{oops", encoding="utf-8")
        store.save("ctx_badpayload", json.dumps({"schema_version": 12345}))

        items = list_resumable(store)
        by_id = {i["ctx_id"]: i for i in items}
        self.assertEqual(set(by_id), {"ctx_good", "ctx_broken", "ctx_badpayload"})
        self.assertNotIn("error", by_id["ctx_good"])
        self.assertIn("StoreCorrupt", by_id["ctx_broken"]["error"])
        self.assertIn("schema_version", by_id["ctx_badpayload"]["error"])
        for ctx_id in ("ctx_broken", "ctx_badpayload"):
            self.assertNotIn("phase", by_id[ctx_id])
            self.assertIn("saved_at", by_id[ctx_id], "坏条目也要能排序")

    def test_38_sorted_by_saved_at(self):
        store = self.new_store()
        for ctx_id in ("ctx_3", "ctx_1", "ctx_2"):
            ctx = make_ctx(task_id=ctx_id)
            ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.WAITING_USER.value))
            save_checkpoint(ctx, store, ctx_id)
            time.sleep(0.002)
        self.assertEqual([i["ctx_id"] for i in list_resumable(store)],
                         ["ctx_3", "ctx_1", "ctx_2"])

    def test_39_empty_store(self):
        self.assertEqual(list_resumable(self.new_store()), [])
        self.assertEqual(list_resumable(MemoryStore()), [])

    def test_40_works_with_memory_store(self):
        mem = MemoryStore()
        save_checkpoint(rich_ctx(), mem, "ctx_m")
        items = list_resumable(mem)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["phase"], "WAITING_USER")


# ══════════════════════════════════════════════════════════════
# 7. 端到端崩溃续跑（验收核心）
# ══════════════════════════════════════════════════════════════
class TestCrashResume(StoreCase):
    """剧本：第 2 步 write_note 需要人工确认 → 挂起 → 落盘 → 进程"死" → 续跑。"""

    def _suspend_to_store(self, ctx_id: str = "ctx_approval"):
        """跑到 WAITING_USER 并落盘，返回 (store, ctx_id, 原始 Result)。"""
        store = self.new_store()
        ctx = make_ctx(goal="处理 alpha", task_id="task_approval")
        loop = make_loop([LOOKUP, WRITE], table={"write_note": [
            ToolOutcome.needs_confirmation("T2 写库需要人工确认"),
            NEEDS_OK,
        ]})
        first = loop.run("处理 alpha", ctx, make_exec())
        self.assertEqual(first.phase, Phase.WAITING_USER, first.text)
        self.assertEqual(first.term_reason, TermReason.NEEDS_CONFIRMATION)
        save_checkpoint(ctx, store, ctx_id, reason="挂起等人工审批")
        return store, ctx_id, first

    def test_41_discard_context_then_resume_approved_to_done(self):
        store, ctx_id, first = self._suspend_to_store()
        self.assertEqual(store.list_pending(), [ctx_id])
        # 落盘的内容本身就带着续跑所需的全部信息
        snapshot = load_checkpoint(store, ctx_id)
        self.assertEqual(snapshot.get("phase"), Phase.WAITING_USER.value)
        self.assertEqual(snapshot.get("pending_confirmation")["tool"], "write_note")
        self.assertEqual(snapshot.get("loop_count"), 2)
        del snapshot                                # 模拟进程死：内存里什么都不剩

        # 新进程：新 Loop、新 LLM 脚本（续跑不该重放前两轮对话）、新 dispatcher
        loop2 = make_loop([FINAL], table={"write_note": NEEDS_OK})
        e2 = make_exec()
        result = resume_from_store(loop2, store, ctx_id, e2, decision=True,
                                  reason="人工审核通过")

        self.assertEqual(result.phase, Phase.DONE, result.text)
        self.assertEqual(result.term_reason, TermReason.DONE)
        self.assertEqual(loop2._dispatcher.count("write_note"), 1, "批准后必须真执行一次")
        self.assertTrue(e2.approved("write_note", snapshot_hash(first)))
        self.assertGreaterEqual(result.steps, 3, "续跑必须接着 loop_count=2 往下数")

    def test_42_resume_rejected_keeps_looping(self):
        store, ctx_id, _ = self._suspend_to_store("ctx_reject")
        loop2 = make_loop([
            resp(text="那我改用只读方案。", calls=[("c3", "lookup", {"q": "alpha-readonly"})]),
            FINAL,
        ])
        result = resume_from_store(loop2, store, ctx_id, make_exec(),
                                  decision=False, reason="不允许写库")
        self.assertEqual(result.phase, Phase.DONE, result.text)
        self.assertEqual(loop2._dispatcher.count("write_note"), 0, "拒绝的动作不许重放")
        self.assertEqual(loop2._dispatcher.count("lookup"), 1, "拒绝后要换方案继续跑")
        prompts = " ".join(loop2._llm.prompts)
        self.assertIn("不允许写库", prompts, "拒绝理由必须进 history")

    def test_43_pending_cleared_after_normal_end(self):
        store, ctx_id, _ = self._suspend_to_store("ctx_clear")
        self.assertIn(ctx_id, store.list_pending())
        resume_from_store(make_loop([FINAL]), store, ctx_id, make_exec(), decision=True)
        self.assertEqual(store.list_pending(), [], "正常结束后不该还留在 pending 里")
        self.assertFalse((self.root / f"{ctx_id}.json").exists())
        self.assertEqual(list_resumable(store), [])

    def test_44_resuspend_is_saved_again(self):
        store, ctx_id, _ = self._suspend_to_store("ctx_again")
        # 第二次调用 write_note 依旧要确认（模拟另一条 T2 动作）
        loop2 = make_loop([], table={"write_note": ToolOutcome.needs_confirmation(
            "又一次高危写操作需要确认")})
        result = resume_from_store(loop2, store, ctx_id, make_exec(), decision=True)

        self.assertEqual(result.phase, Phase.WAITING_USER)
        self.assertEqual(store.list_pending(), [ctx_id], "再次挂起必须重新落盘")
        again = load_checkpoint(store, ctx_id)
        self.assertEqual(again.get("phase"), Phase.WAITING_USER.value)
        self.assertEqual(again.get("pending_confirmation")["reason"],
                         "又一次高危写操作需要确认")
        self.assertIsNone(again.get("error"), "续跑中不该写 error 字段")
        self.assertEqual(loop2._llm.calls, 0, "挂起在重放阶段，还没轮到 LLM")

    def test_45_unknown_ctx_raises_explicit_error(self):
        store = self.new_store()
        with self.assertRaises(ContextNotFound) as caught:
            resume_from_store(make_loop([FINAL]), store, "ctx_nope", make_exec())
        message = str(caught.exception)
        self.assertIn("ctx_nope", message)
        self.assertIn("没有找到", message)
        # 非法 id 是另一类错误：ValueError（参数错 ≠ 数据缺）
        with self.assertRaises(ValueError):
            resume_from_store(make_loop([FINAL]), store, "../etc", make_exec())

    def test_46_non_waiting_phase_runs_from_goal(self):
        store = self.new_store()
        ctx = make_ctx(goal="把 alpha 处理掉", task_id="task_goal")
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.EXECUTING))
        ctx.apply(Patch("set", "loop_count", Owner.LOOP, 1))
        save_checkpoint(ctx, store, "ctx_goal")

        loop = make_loop([FINAL])
        result = resume_from_store(loop, store, "ctx_goal", make_exec())
        self.assertEqual(result.phase, Phase.DONE)
        self.assertIn("把 alpha 处理掉", " ".join(loop._llm.prompts),
                      "非 WAITING_USER 分支必须从 Context 里取回原 goal")
        self.assertEqual(store.list_pending(), [])

    def test_47_missing_goal_falls_back_to_continue(self):
        store = self.new_store()
        ctx = ContextHandle(initial={"task_id": "task_nogoal"})
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.PLANNING))
        save_checkpoint(ctx, store, "ctx_nogoal")
        loop = make_loop([FINAL])
        result = resume_from_store(loop, store, "ctx_nogoal", make_exec())
        self.assertEqual(result.phase, Phase.DONE)
        self.assertIn("继续", " ".join(loop._llm.prompts))

    def test_48_history_survives_restart(self):
        """续跑的 prompt 里必须能看到重启前的对话（否则等于从零开始）。"""
        store, ctx_id, _ = self._suspend_to_store("ctx_hist")
        loop2 = make_loop([FINAL])
        resume_from_store(loop2, store, ctx_id, make_exec(), decision=True)
        joined = " ".join(loop2._llm.prompts)
        self.assertIn("我先查一下", joined, "重启前 assistant 的历史必须在 prompt 里")
        self.assertIn("alpha done", joined, "重启前的工具调用参数必须在 prompt 里")
        self.assertIn("批准执行 write_note", joined)

    def test_49_budget_continues_from_checkpoint(self):
        """预算/步数不该因为重启而被清零（否则"有界"就不成立）。"""
        store = self.new_store()
        ctx = make_ctx(goal="跑满预算", task_id="task_budget")
        ctx.apply(Patch("set", "phase", Owner.LOOP, Phase.WAITING_USER.value))
        ctx.apply(Patch("set", "loop_count", Owner.LOOP, 5))
        ctx.apply(Patch("set", "pending_confirmation", Owner.LOOP, {"tool": "lookup",
                                                                   "args": {}, "args_hash": "x"}))
        save_checkpoint(ctx, store, "ctx_budget")
        # 脚本永远只吐一个工具动作 → 只能靠 max_steps 收口
        loop = make_loop([LOOKUP])
        result = resume_from_store(loop, store, "ctx_budget", make_exec(max_steps=6),
                                  decision=True)
        # 步数从 5 接着算：第 6 步就撞上限（若重启把 loop_count 清零，这里会跑满 6 步）
        self.assertEqual(result.term_reason, TermReason.MAX_STEPS, result.text)
        self.assertEqual(result.steps, 6)
        self.assertTrue(result.partial)


def snapshot_hash(first_result):
    """从 Result.artifacts 里取回 pending args_hash（续跑必须用同一个指纹）。"""
    for artifact in first_result.artifacts:
        if "pending_confirmation" in artifact:
            return artifact["pending_confirmation"]["args_hash"]
    raise AssertionError("挂起结果里没有 pending_confirmation")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
