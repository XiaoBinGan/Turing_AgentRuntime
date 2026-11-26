#!/usr/bin/env python3
"""核对 README 里那些「可被代码证伪」的数字。

为什么要这个脚本：README 里的 `136 modules` / `14 protocols` / `2167 tests`
这类数字**会随代码漂移**，而且漂了没人知道 —— 文档错了不会让构建失败。
本仓的一条纪律是「文档可以写错，测试不会」，那就把文档也纳入测试。

只核对**能被机械验证**的项：模块数、协议数、测试数、场景数、空依赖清单。
不核对措辞与设计判断 —— 那些需要人。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _count_modules() -> int:
    # 排除构建产物：`python -m build` 会在 build/lib/ 复制整棵树，
    # 不排除的话模块数会翻倍（实测 138 -> 194），而这个脚本会误报。
    skip = {".venv", "__pycache__", "build", "dist", ".git", "site"}
    return sum(1 for p in ROOT.rglob("*.py")
               if not (set(p.parts) & skip) and p.name != "__init__.py")


def _count_protocols() -> int:
    tree = ast.parse((ROOT / "contracts.py").read_text(encoding="utf-8"))
    return sum(1 for node in tree.body
               if isinstance(node, ast.ClassDef)
               and any(isinstance(b, ast.Name) and b.id == "Protocol"
                       for b in node.bases))


def _count_tests() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=str(ROOT), capture_output=True, text=True)
    m = re.search(r"Ran (\d+) tests", out.stderr + out.stdout)
    return int(m.group(1)) if m else -1


def _count_scenarios() -> tuple[int, int]:
    """跑 demo 拿场景数与断言数。

    **不要调 `./verify.sh`** —— 它现在会调用本脚本，形成递归（踩过一次：
    整条验收挂死，看起来像"变慢了"而不是"死循环"）。直接跑 demo 即可。
    """
    out = subprocess.run([sys.executable, "demo/run.py"], cwd=str(ROOT),
                         capture_output=True, text=True)
    blob = out.stdout + out.stderr
    m = re.search(r"(\d+)/(\d+) 场景跑通", blob)
    if not m:
        return (-1, -1)
    # 断言数在逐场景输出里（`断言: ... → PASS`），不在汇总行
    passes = blob.count("→ PASS")
    return (int(m.group(1)), passes)


def _core_requirements_empty() -> bool:
    path = ROOT / "requirements-core.txt"
    lines = [ln.strip() for ln in path.read_text().splitlines()]
    return all(not ln or ln.startswith("#") for ln in lines)


def main() -> int:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    mods = _count_modules()
    protos = _count_protocols()
    tests = _count_tests()
    scen, assertions = _count_scenarios()

    checks: list[tuple[str, bool, str]] = []

    for claimed in set(int(x) for x in re.findall(r"(\d+)\s*modules", readme)):
        checks.append((f"模块数 {claimed} == 实际 {mods}", claimed == mods,
                       f"README 写 {claimed}，实际 {mods}"))

    for claimed in set(int(x) for x in
                       re.findall(r"(\d+)\s*(?:冻结)?(?:个)?协议", readme)
                       + re.findall(r"(\d+)\s*protocols?", readme)):
        checks.append((f"协议数 {claimed} == 实际 {protos}", claimed == protos,
                       f"README 写 {claimed}，实际 {protos}"))

    for claimed in set(int(x) for x in re.findall(r"(\d+)\s*tests", readme)):
        checks.append((f"用例数 {claimed} == 实际 {tests}", claimed == tests,
                       f"README 写 {claimed}，实际 {tests}"))

    for claimed in set(int(x) for x in re.findall(r"(\d+)\s*(?:个)?端到端场景", readme)):
        if scen > 0:
            checks.append((f"场景数 {claimed} == 实际 {scen}", claimed == scen,
                           f"README 写 {claimed}，实际 {scen}"))

    for claimed in set(int(x) for x in re.findall(r"(\d+)\s*(?:条)?断言", readme)):
        if assertions > 0:
            checks.append((f"断言数 {claimed} == 实际 {assertions}",
                           claimed == assertions,
                           f"README 写 {claimed}，实际 {assertions}"))

    # ── 门禁道数：文档说的数必须等于 verify.sh 实际跑的 ──
    # 这一类是"功能加了但文档没跟上"的典型。③c 加进来之后，README 与官网
    # 都还写着 6 道 —— 两个文件互相抄，谁也发现不了。
    try:
        vy = (ROOT / "verify.sh").read_text(encoding="utf-8")
        head = vy.split("# ⑥ 可选")[0]          # 不含 VERIFY_* 条件块
        actual_gates = len(set(re.findall(r"[①②③④⑤⑥⑦⑧⑨][a-z]?", head)))
        for doc in ("README.md", "docs/index.html"):
            try:
                txt = (ROOT / doc).read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            # 文档里既有阿拉伯数字也有中文数字，两种都要认
            claim_7 = len(re.findall(r"(?:7 gates|7 道门禁|七道门禁)", txt))
            claim_6 = len(re.findall(r"(?:6 gates|6 道门禁|六道门禁)", txt))
            claimed = 6 if (claim_6 and not claim_7) else (7 if claim_7 else None)
            if claimed is None:
                continue
            checks.append((f"门禁道数 {doc} 写 {claimed} == 实际 {actual_gates}",
                           claimed == actual_gates,
                           f"{doc} 写 {claimed} 道，verify.sh 实际 {actual_gates} 道"
                           f"（编号 ①②③③b③c④⑤ 各算一道）"))
    except FileNotFoundError:
        pass

    # 版本一致性：pyproject.toml 与 CHANGELOG 最新条目必须同号。
    # 两者不同步时，`pip install` 拿到的版本与 changelog 讲的对不上，
    # 用户会以为是发行事故。
    try:
        import tomllib
        py_version = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"## \[([0-9]+\.[0-9]+\.[0-9]+)\]", changelog)
        cl_version = m.group(1) if m else ""
        checks.append((f"版本号 pyproject {py_version} == changelog {cl_version}",
                       py_version == cl_version,
                       f"pyproject={py_version} changelog={cl_version}，两者必须一致"))
    except (FileNotFoundError, KeyError):
        pass

    if "requirements-core.txt` is empty" in readme or "零第三方" in readme:
        checks.append(("核心依赖清单确为空", _core_requirements_empty(),
                       "README 声称空，但 requirements-core.txt 有非注释行"))

    failed = 0
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       {detail}")
            failed += 1

    if not checks:
        print("  （README 里没找到可核对的数字，跳过）")
    print()
    if failed:
        print(f"{failed} 项与代码不符 —— 改 README，或者改代码。")
    else:
        print(f"{len(checks)} 项全部一致。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
