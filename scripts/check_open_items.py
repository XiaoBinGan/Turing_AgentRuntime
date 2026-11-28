#!/usr/bin/env python3
"""校验「未验证 / 仍然开放的」清单没有腐化。

**为什么需要它**：这份仓里已经出现过两次同一个失误 —— 能力测完了，文档还写着
「未做」。

  * 官网 `docs/index.html` 挂着「8 worker 曲线 · 需第二台机器」，而 64 核池上
    早就跑出 65.3× 的完整曲线；
  * `CAPACITY.md §9` 写「长稳只跑了 20 分钟」，同一份文件的 §3.2 就有 150 分钟
    的完整数据。

`scripts/check_readme_facts.py` 查不出这类问题：它校验的是**数字**（用例数、
模块数、门禁道数），而「这条还在不在待办里」是**语义**。语义没法自动判，
但可以逼出一次人工确认 —— 给每条开放项打一个「最后确认日期」的标记：

    - **长稳 > 4 小时**：已跑 150 分钟 …… <!-- checked: 2026-11-28 -->

规则（任一不满足即 FAIL）：

  1. 每条开放项**必须**带 `<!-- checked: YYYY-MM-DD -->` 标记；
  2. 日期必须可解析，且不超过 `MAX_AGE_DAYS` 天（默认 60）—— 过期即重新确认；
  3. 条目里反引号包住的仓库路径**必须存在**（证据引用不能是死链）。

刻意**不**做的：判断「这条到底该不该还在清单里」。那需要人。这个脚本只保证
「有人最近看过它，并且它引用的东西还在」。

用法：
    .venv/bin/python scripts/check_open_items.py             # 门禁模式
    .venv/bin/python scripts/check_open_items.py -v          # 打印每条
    .venv/bin/python scripts/check_open_items.py --max-age 90
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 需要校验的「开放项」列表。新增一处清单就加一行。
TARGETS = (
    ("docs/design/CAPACITY.md", r"^## 9\. 已知未做$"),
    ("README.md", r"^## 已验证与未验证$"),
)

#: 开放项允许的标记格式（HTML 注释：渲染时不可见）
MARKER = re.compile(r"<!--\s*checked:\s*(\d{4}-\d{2}-\d{2})\s*-->")

#: 反引号里的仓库路径：只认带目录分隔或已知扩展名的，避免把 `` `ls` `` 当路径
_PATHLIKE = re.compile(r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]*|[A-Za-z0-9_-]+\.(?:py|sh|md|yaml|yml|json|toml))`")

MAX_AGE_DAYS = 60


def _section_lines(text: str, header_re: str) -> list[str]:
    """取出标题匹配到的章节正文（到下一个同级或更高级标题为止）。"""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if re.match(header_re, line):
            inside = True
            continue
        if inside and re.match(r"^## ", line):
            break
        if inside:
            out.append(line)
    return out


def _items(section: list[str]) -> list[str]:
    """章节里的顶层列表项（`- ` 开头）；续行并回上一项。"""
    items: list[str] = []
    for line in section:
        if line.startswith("- "):
            items.append(line)
        elif items and line.startswith("  ") and line.strip():
            items[-1] += " " + line.strip()
    return items


def check(max_age_days: int = MAX_AGE_DAYS, *, verbose: bool = False,
          root: Path | None = None) -> int:
    """`root` 可注入（测试用临时目录）；缺省是本仓根。"""
    base = Path(root) if root is not None else ROOT
    today = _dt.date.today()
    errors: list[str] = []
    total = 0

    for rel, header in TARGETS:
        path = base / rel
        if not path.exists():
            errors.append(f"{rel}: 文件不存在")
            continue
        items = _items(_section_lines(path.read_text(encoding="utf-8"), header))
        if not items:
            errors.append(f"{rel}: 找不到开放项列表（标题 {header!r}）—— 改标题就要同步改本脚本")
            continue
        for raw in items:
            total += 1
            title = raw[2:].split("：")[0][:40]
            marker = MARKER.search(raw)
            if marker is None:
                errors.append(f"{rel}: 开放项缺少 `<!-- checked: YYYY-MM-DD -->` 标记 → {title}")
                continue
            try:
                checked = _dt.date.fromisoformat(marker.group(1))
            except ValueError:
                errors.append(f"{rel}: 标记日期不合法 {marker.group(1)!r} → {title}")
                continue
            age = (today - checked).days
            if age > max_age_days:
                errors.append(
                    f"{rel}: 开放项已 {age} 天未确认（> {max_age_days}）→ {title}")
            for candidate in _PATHLIKE.findall(raw):
                if not (base / candidate).exists():
                    errors.append(f"{rel}: 引用的路径不存在 {candidate!r} → {title}")
            if verbose:
                print(f"  ok  {rel}: {title}  (checked {checked}, {age}d)")

    if errors:
        print("❌ 开放项清单需要更新：", file=sys.stderr)
        for err in errors:
            print(f"   - {err}", file=sys.stderr)
        print(
            "\n修法：确认该项是否真的还没做。做完了就从清单里删掉；\n"
            "      还没做就把 `<!-- checked: 今天 -->` 更新到今天。",
            file=sys.stderr)
        return 1

    # 只在 `-v` 时打印成功摘要：门禁脚本本身不该往 stdout 写东西 ——
    # `verify.sh` 用 `tail -1 | grep '^OK'` 判断结果，任何多余输出都会让它误判。
    if verbose:
        print(f"  {total} 条开放项全部在有效期内，引用路径均可达")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="校验未验证清单的时效性与证据引用")
    ap.add_argument("--max-age", type=int, default=MAX_AGE_DAYS,
                    help=f"标记超过多少天算过期（默认 {MAX_AGE_DAYS}）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    return check(args.max_age, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
