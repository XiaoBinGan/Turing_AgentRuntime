"""tests/test_architecture.py — 目录边界测试（W13 重构的验收点）。

一条边界，三个方向：

    demo / agents  ──可以依赖──▶  adapters / core / plane / contracts
    adapters / core / plane  ──不许反依赖──▶  demo / agents
    adapters/     ──只放中性能力──▶  不许出现业务词汇（连接池 / 重启 / 告警 / …）

为什么值得一条测试：`adapters/` 曾经混进 4 套业务演练脚本（"连接池上限 20，峰值并发
180"、"重启 api 服务释放连接池"）。那种东西一旦回流，"Runtime 能力" 与 "演示数据"
就再也分不开 —— 换业务时得改 Runtime 目录。本测试把边界钉成可执行断言。

容错约定（并行施工）：
  * `agents/demo_extractor.py` / `agents/demo_template.py` 由其他 worker 同时在写，
    文件不存在或尚未 import 通 → `skipTest`，不红；
  * `plane/memory.py` / `builder.py` 正在被别人重构 → 本文件**不断言它们的内容**，
    只断言 import 方向与 adapters 的词汇。
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ADAPTERS_DIR = ROOT / "adapters"
RUNTIME_DIRS = ("core", "plane")
OPTIONAL_AGENT_FILES = ("agents/demo_extractor.py", "agents/demo_template.py")

#: Runtime 目录里不该出现的业务词汇（评审结论里点名的那批）
BUSINESS_VOCABULARY = ("连接池", "重启", "告警", "api-gateway", "排查", "运维")

#: 反向依赖的禁止前缀
FORBIDDEN_PREFIXES = ("agents", "demo")


def _py_files(*dirs: Path) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        out.extend(sorted(p for p in d.glob("*.py")))
    return out


def _imported_modules(path: Path) -> list[str]:
    """静态提取 import 的模块名（ast，跳过注释与字符串，避免误报）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level>0 是包内相对导入，不可能跨到 agents / demo
            if node.level == 0 and node.module:
                mods.append(node.module)
    return mods


def _dynamic_import_targets(text: str) -> list[str]:
    """兜底：`import_module("demo.x")` / `__import__("demo.x")` 这类字符串式动态导入。"""
    pattern = r"""(?:import_module|__import__)\s*\(\s*['"]([\w.]+)['"]"""
    return re.findall(pattern, text)


def _is_forbidden(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


class TestLayerBoundary(unittest.TestCase):
    """Runtime 目录与业务目录必须单向依赖，且 adapters 保持中性。"""

    # ── ① adapters 的词汇边界 ───────────────────────────────
    def test_adapters_contains_no_business_vocabulary(self):
        """adapters/ 是 Runtime 目录，不许出现业务词汇。"""
        files = _py_files(ADAPTERS_DIR)
        self.assertTrue(files, "adapters/ 下没有 .py 文件，测试会空转")
        self.assertIn(ADAPTERS_DIR / "llm_mock.py", files, "llm_mock.py 不在 adapters/ 里")

        offenders: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for word in BUSINESS_VOCABULARY:
                if word in text:
                    offenders.append(f"{path.relative_to(ROOT)} 含业务词 {word!r}")
        self.assertEqual(
            offenders, [],
            "adapters/ 是 Runtime 目录，业务演练脚本应住在 demo/fixtures.py：\n  "
            + "\n  ".join(offenders),
        )

    def test_adapters_does_not_import_demo_or_agents(self):
        """adapters/ 也不许 import 业务层（否则边界只是表面干净）。"""
        offenders: list[str] = []
        for path in _py_files(ADAPTERS_DIR):
            text = path.read_text(encoding="utf-8")
            for mod in _imported_modules(path) + _dynamic_import_targets(text):
                if _is_forbidden(mod):
                    offenders.append(f"{path.relative_to(ROOT)} import {mod}")
        self.assertEqual(offenders, [], "adapters/ 反向依赖了业务层：\n  " + "\n  ".join(offenders))

    # ── ② Runtime 不许反依赖 Agent / demo ────────────────────
    def test_core_and_plane_do_not_import_agents_or_demo(self):
        """Runtime 不许反向依赖 Agent / demo。"""
        dirs = [ROOT / d for d in RUNTIME_DIRS]
        files = _py_files(*dirs)
        self.assertTrue(files, "core/ 与 plane/ 下没有 .py 文件，测试会空转")

        offenders: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for mod in _imported_modules(path) + _dynamic_import_targets(text):
                if _is_forbidden(mod):
                    offenders.append(f"{path.relative_to(ROOT)} import {mod}")
        self.assertEqual(
            offenders, [],
            "Runtime 层不许反向依赖 agents/ 或 demo/：\n  " + "\n  ".join(offenders),
        )

    # ── ③ 反方向允许 ─────────────────────────────────────────
    def test_agents_and_demo_may_import_runtime(self):
        """反方向是允许的：Agent/demo 可以依赖 Runtime。"""
        # demo 侧：证明性用例 —— demo/fixtures.py 确实 import 了 adapters
        demo_fixtures = importlib.import_module("demo.fixtures")
        self.assertTrue(callable(demo_fixtures.goal_llm))
        src = (ROOT / "demo/fixtures.py").read_text(encoding="utf-8")
        self.assertIn("from adapters.llm_mock import", src,
                      "demo/fixtures.py 应当复用 Runtime 的中性 adapter，而不是另写一套")

        # agents 侧：由其他 worker 并行落盘，缺失/未 import 通就跳过（不红）
        checked = 0
        for rel in OPTIONAL_AGENT_FILES:
            path = ROOT / rel
            if not path.exists():
                continue
            mod_name = rel[:-3].replace("/", ".")
            try:
                importlib.import_module(mod_name)
            except Exception as exc:                                   # noqa: BLE001
                self.skipTest(f"{mod_name} 已落盘但当前不可导入（并行施工中）：{exc!r}")
            checked += 1

        if checked == 0:
            # 不是失败：别人可能还没落盘。这里只说明"未覆盖 agents 侧"。
            print(f"\n[skip-hint] {', '.join(OPTIONAL_AGENT_FILES)} 尚未落盘，"
                  f"本用例仅覆盖 demo → runtime 方向")

    # ── ④ 搬家结果真的存在 ───────────────────────────────────
    def test_demo_fixtures_exist_and_are_importable(self):
        from demo.fixtures import goal_llm
        self.assertTrue(callable(goal_llm))

        fixtures = importlib.import_module("demo.fixtures")
        for name in ("goal_llm", "endless_tool_llm", "semantic_loop_llm",
                     "confirmation_llm", "build_registry"):
            self.assertTrue(callable(getattr(fixtures, name, None)),
                            f"demo/fixtures.py 缺少 {name}()")

        # Runtime 侧不许再导出业务脚本
        from adapters import llm_mock
        for name in ("goal_llm", "endless_tool_llm", "semantic_loop_llm",
                     "confirmation_llm"):
            self.assertFalse(hasattr(llm_mock, name),
                             f"adapters/llm_mock.py 残留业务脚本 {name}()")
        # 中性能力必须留下
        for name in ("ScriptedLLM", "CallableLLM", "usage"):
            self.assertTrue(hasattr(llm_mock, name), f"adapters/llm_mock.py 缺少 {name}")

        # 业务词汇确实搬到了 fixture 层（与 ① 互为正反面）
        src = (ROOT / "demo/fixtures.py").read_text(encoding="utf-8")
        self.assertIn("连接池", src, "业务演练数据应当落在 demo/fixtures.py")

    # ── ⑤ agents/ 已落盘时，包本身应可导入 ───────────────────
    def test_agents_package_importable_if_present(self):
        """agents/ 已落盘时，包本身应可导入；未落盘则跳过。"""
        if importlib.util.find_spec("agents") is None:
            self.skipTest("agents/ 尚未落盘")
        try:
            importlib.import_module("agents")
        except Exception as exc:                                       # noqa: BLE001
            self.skipTest(f"agents/ 存在但当前不可导入（并行施工中）：{exc!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAgentLayerImplementsSPI(unittest.TestCase):
    """上层（agents/）必须**照契约实现**，而不是碰 Runtime。

    这组守卫的意义：Runtime 冻结 SPI 之后，上层理应只写 agents/。
    如果有人为了"让上层跑通"去改 core/ 或 contracts/，边界就破了——
    而这种破坏在功能测试里看不出来（功能是对的，架构错了）。

    所以这里钉的是**方向**：
      * agents/ 可以 import Runtime（正常）
      * Runtime 不许 import agents/（反向依赖 = 边界失效）
      * agents/ 必须真有业务词汇（否则说明它只是个空壳）
    """

    #: Runtime 目录：这些不许出现对 agents/ 的依赖
    RUNTIME_DIRS = ("core", "plane", "adapters")

    def test_runtime_does_not_import_agents(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for d in self.RUNTIME_DIRS:
            for py in (root / d).glob("*.py"):
                src = py.read_text(encoding="utf-8", errors="ignore")
                # 允许注释里提到 agents；只查 import 语句
                for m in re.finditer(r"^\s*(?:from|import)\s+(agents[\w.]*)", src, re.M):
                    offenders.append(f"{d}/{py.name}: {m.group(0).strip()}")
        self.assertEqual(offenders, [],
                         f"Runtime 反向依赖了 Agent 层：{offenders}")

    def test_builder_root_does_not_import_agents(self):
        from pathlib import Path
        import re
        root = Path(__file__).resolve().parent.parent
        for name in ("builder.py", "contracts.py", "serve.py", "toolconfig.py"):
            p = root / name
            if not p.exists():
                continue
            src = p.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(
                re.search(r"^\s*(?:from|import)\s+agents", src, re.M),
                f"{name} 不该依赖 Agent 层")

    def test_agents_layer_has_business_vocabulary(self):
        """agents/ 是**业务目录**：里面就该有业务词。

        如果 agents/ 里一个业务词都没有，说明它只是把 Runtime 又包了一层——
        那这个分层没有存在的意义。
        """
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        files = [p for p in (root / "agents").glob("*.py") if p.name != "__init__.py"]
        if not files:
            self.skipTest("agents/ 为空")
        vocab = ("服务", "指标", "日志", "发布", "回滚", "扩容", "告警", "连接池",
                 "p99", "部署", "排障", "容量")
        hits = 0
        for p in files:
            src = p.read_text(encoding="utf-8", errors="ignore")
            hits += sum(1 for w in vocab if w in src)
        self.assertGreater(hits, 0, "agents/ 里没有任何业务词汇，分层可能只是空壳")

    def test_spi_implementations_conform(self):
        """agents/ 里声称实现 SPI 的类，必须真有那个方法。"""
        from pathlib import Path
        import importlib
        root = Path(__file__).resolve().parent.parent
        expectations = {
            "agents.extractor": [("OpsExtractor", "extract")],
            "agents.planner": [("StaticDAGPlanner", "replan"), ("LLMPlanner", "replan")],
        }
        checked = 0
        for mod_name, pairs in expectations.items():
            try:
                mod = importlib.import_module(mod_name)
            except Exception:              # noqa: BLE001 - 上层未落盘则跳过
                continue
            for cls_name, method in pairs:
                cls = getattr(mod, cls_name, None)
                if cls is None:
                    continue
                checked += 1
                self.assertTrue(callable(getattr(cls, method, None)),
                                f"{mod_name}.{cls_name} 缺少 {method}")
        if checked == 0:
            self.skipTest("上层实现尚未落盘")


# ══════════════════════════════════════════════════════════════
# 核心零依赖 + Prompt 层语言中立
#
# 这两条是 README 里**公开承诺**的东西，所以必须有测试守着 ——
# 文档可以写错，测试不会。
# ══════════════════════════════════════════════════════════════

#: 标准库模块白名单：`core/` 只允许 import 这些（外加本仓自己的模块）。
#: 加新条目要写清为什么它属于标准库且不可替代。
_STDLIB_OK = frozenset("""
__future__ abc argparse array ast asyncio atexit base64 binascii bisect builtins bz2 calendar
codecs collections concurrent configparser contextlib copy csv ctypes dataclasses
datetime decimal difflib dis email enum errno fnmatch fractions functools gc getpass
gettext glob graphlib gzip hashlib heapq hmac html http inspect io itertools json
keyword linecache locale logging lzma math mimetypes multiprocessing numbers operator
os pathlib pickle pprint queue random re reprlib resource secrets select selectors
sched shlex shutil signal socket sqlite3 ssl stat statistics string struct subprocess
sys tarfile tempfile textwrap threading time token tokenize traceback types typing
unicodedata unittest urllib uuid warnings weakref xml zipfile zlib zoneinfo
""".split())

#: 本仓自己的顶层模块（不是第三方）
_OWN_MODULES = frozenset({"contracts", "core", "plane", "agents", "adapters",
                          "integrations", "builder", "auth", "toolconfig", "serve"})


def _imported_roots(path: Path) -> set[str]:
    """AST 取出一个文件 import 的顶层模块名（含函数内 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # 相对导入：本包内
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _in_try_import(node: ast.AST, tree: ast.AST) -> bool:
    """这个 import 是否在 `try/except ImportError` 里（允许的可选依赖）。"""
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.Try):
            continue
        if not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in parent.body):
            continue
        if any(n is node for n in ast.walk(parent)):
            # 只认"body 里的 import"，except 分支里的不算保护
            if node in list(parent.body) or any(
                    node is x for stmt in parent.body for x in ast.walk(stmt)):
                handlers = [h for h in parent.handlers
                            if h.type is not None
                            and isinstance(h.type, ast.Name)
                            and h.type.id in ("ImportError", "ModuleNotFoundError")]
                if handlers:
                    return True
    return False


class CoreZeroDependencyTest(unittest.TestCase):
    """`core/` 必须只靠标准库 —— README 公开承诺了，就得有测试。

    为什么重要：`requirements-core.txt` 是空文件，那是**设计**而不是疏忽。
    一旦 `core/` 里出现未保护的第三方 import，"最小安装能跑"就成了假话，
    而且踩坑的人会在最不该出问题的地方（安装阶段）失败。
    """

    def test_core_imports_only_stdlib(self):
        root = Path(__file__).resolve().parent.parent / "core"
        offenders: list[str] = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                roots: set[str] = set()
                if isinstance(node, ast.Import):
                    roots = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    roots = {node.module.split(".")[0]}
                for mod in roots:
                    if mod in _STDLIB_OK or mod in _OWN_MODULES:
                        continue
                    if _in_try_import(node, tree):
                        continue          # 可选依赖：允许
                    offenders.append(f"{path.name}: {mod}")
        self.assertEqual(offenders, [],
                         f"core/ 出现未保护的第三方 import：{offenders}")

    def test_observability_imports_without_third_party(self):
        """用 `-S`（不加载 site-packages）真跑一次导入，比 AST 更硬。"""
        import subprocess
        import sys
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-S", "-c",
             "from core.observability import PROMETHEUS_AVAILABLE, JsonLogger;"
             "print(PROMETHEUS_AVAILABLE)"],
            capture_output=True, text=True, cwd=str(root))
        self.assertEqual(result.returncode, 0,
                         f"核心在零第三方环境下导入失败：{result.stderr[-400:]}")
        self.assertIn("False", result.stdout,
                      "无 prometheus_client 时应报 False 而不是崩")

    def test_metrics_without_prometheus_raises_clearly(self):
        """没装 prometheus 时构造 Metrics 要**明确报错**，不能静默返回空指标。"""
        from core.observability import Metrics, MetricsError, PROMETHEUS_AVAILABLE
        if PROMETHEUS_AVAILABLE:
            self.skipTest("本环境装了 prometheus_client，无法验证降级路径")
        with self.assertRaises(MetricsError):
            Metrics()


class PromptLanguageNeutralTest(unittest.TestCase):
    """`builder.py` 不得出现 CJK 字面量。

    为什么：Prompt 组装是**形状**，语言相关的措辞属于模板
    （`agents/prompts.py` 或 `tools.yaml`）。代码里写死中文会让
    "同一运行时服务英文客户"变成改代码而不是换模板。
    """

    def test_no_cjk_literals(self):
        """只查**运行期字符串**，docstring 不算。

        docstring 是给人读的设计说明（本仓约定用中文写），它不会进 Prompt；
        真正要防的是"把中文措辞写进拼接出去的提示词"。
        """
        root = Path(__file__).resolve().parent.parent
        path = root / "builder.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        # 收集 docstring 节点（Module / FunctionDef / AsyncFunctionDef / ClassDef 的首条语句）
        docstrings: set[int] = set()
        owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        for node in ast.walk(tree):
            if isinstance(node, owners) and node.body:
                first = node.body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))

        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            if any("\u4e00" <= ch <= "\u9fff" for ch in node.value):
                offenders.append(f"line {node.lineno}: {node.value[:40]!r}")
        self.assertEqual(offenders, [],
                         f"builder.py 出现 CJK 运行期字符串：{offenders[:5]}")
