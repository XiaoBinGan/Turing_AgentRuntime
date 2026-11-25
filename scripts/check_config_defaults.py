#!/usr/bin/env python3
"""核对「文档承诺的默认值」与「代码实际默认值」。

**这个脚本是为了堵一类特定的坑**：文档写着一件事，生产路径做着另一件事。

真实例子：`builder.py` 的 docstring 说「预算吃紧的场景应当用 `CALIBRATED`」，
`agents/prompts.py` 也抄了正确值（0.68 tok/字），但 `serve.py` 那条**真正的
生产路径**用的是 `DEFAULT_PROFILE`（1.0 tok/字）—— 纯中文高估 1.57x，
白扔 36% 预算。

写文档时是清醒的，接代码时没回头核对，于是两边走了不同的路。
`check_readme_facts.py` 查得出「模块数漂了」，查不出「默认值反了」。

只核对**能被机械验证**的默认值。措辞与设计判断不在此列。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _serve_profile_default(env: dict | None = None) -> tuple[float, float]:
    """真实装配一次，取 serve 路径最终用的 token profile 参数。"""
    from serve import build_gateway_from_env
    gw = build_gateway_from_env(env=env or {})
    loop = gw.loop_factory("probe", None)
    pf = loop._builder.profile
    return float(pf.cjk_per_token), float(pf.other_chars_per_token)


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    # ① serve 默认档必须与 CALIBRATED 一致（而不是保守的 DEFAULT_PROFILE）
    from builder import CALIBRATED, DEFAULT_PROFILE
    cjk, other = _serve_profile_default({})
    checks.append((
        f"serve 默认 cjk_per_token={cjk} == CALIBRATED {CALIBRATED.cjk_per_token}",
        abs(cjk - CALIBRATED.cjk_per_token) < 1e-9,
        f"生产路径用了保守档（{DEFAULT_PROFILE.name}），纯中文会高估 "
        f"{DEFAULT_PROFILE.cjk_per_token / CALIBRATED.cjk_per_token:.2f}x",
    ))
    checks.append((
        f"serve 默认 other_chars_per_token={other} == CALIBRATED {CALIBRATED.other_chars_per_token}",
        abs(other - CALIBRATED.other_chars_per_token) < 1e-9,
        "英文/标识符估算档与标定值不一致",
    ))

    # ② 显式要求保守档时必须真的切过去（否则开关是假的）
    cjk_c, _ = _serve_profile_default({"AGENT_RUNTIME_TOKEN_PROFILE": "conservative"})
    checks.append((
        "AGENT_RUNTIME_TOKEN_PROFILE=conservative 真的生效",
        abs(cjk_c - DEFAULT_PROFILE.cjk_per_token) < 1e-9,
        f"设了 conservative 但 cjk_per_token={cjk_c}",
    ))

    # ③ 文档里提到的默认值必须存在（防止写了不存在的开关名）
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deploy = (ROOT / "docs/design/DEPLOY.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"AGENT_RUNTIME_[A-Z0-9_]+", readme + deploy))
    missing = []
    for name in sorted(documented):
        if name in ("AGENT_RUNTIME_API_KEYS", "AGENT_RUNTIME_JWT_SECRET"):  # 只出现在示例里
            continue
        if name not in deploy and name not in readme:
            continue
        # 在 serve.py 里查是否有引用
        src = (ROOT / "serve.py").read_text(encoding="utf-8")
        if name not in src:
            missing.append(name)
    checks.append((
        f"文档里 {len(documented)} 个 AGENT_RUNTIME_* 都能在 serve.py 找到",
        not missing,
        f"文档提到但代码里没有：{missing}",
    ))

    failed = 0
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       {detail}")
            failed += 1

    print()
    if failed:
        print(f"{failed} 项默认值/文档不一致 —— 改代码，或者改文档。")
    else:
        print(f"{len(checks)} 项全部一致。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
