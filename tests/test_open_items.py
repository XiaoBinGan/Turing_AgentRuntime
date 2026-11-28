"""`scripts/check_open_items.py` 的自测：开放项过期/缺标记/死链都要红。

这个门禁的价值全在"能红"：一个永远绿的检查比没有检查更糟 —— 它让人以为
文档被守着了。所以四条失败路径逐条造。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.check_open_items import check  # noqa: E402

TODAY = _dt.date.today()
FRESH = TODAY.isoformat()
STALE = (TODAY - _dt.timedelta(days=400)).isoformat()

CAPACITY = """# 容量基线

## 9. 已知未做

{items}

## 附录 A

## 不是开放项
- 这一条在 §9 之外，不该被算进去
"""

README = """# README

## 已验证与未验证

**仍然开放的**：

{items}

## 贡献
"""


def _write(root: pathlib.Path, capacity_items: str, readme_items: str) -> None:
    (root / "docs" / "design").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "design" / "CAPACITY.md").write_text(
        CAPACITY.format(items=capacity_items), encoding="utf-8")
    (root / "README.md").write_text(README.format(items=readme_items),
                                    encoding="utf-8")


class TestOpenItemsCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def _run(self) -> int:
        return check(max_age_days=60, root=self.tmp)

    def test_fresh_items_pass(self) -> None:
        _write(self.tmp,
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->",
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->")
        self.assertEqual(self._run(), 0)

    def test_missing_marker_fails(self) -> None:
        """没有标记 = 没人确认过，必须红（否则清单可以永久腐化）。"""
        _write(self.tmp, "- **长稳**：还没跑", f"- **长稳**：还没跑 <!-- checked: {FRESH} -->")
        self.assertEqual(self._run(), 1)

    def test_stale_marker_fails(self) -> None:
        """400 天没确认过 = 早就该复核了。"""
        _write(self.tmp,
               f"- **长稳**：还没跑 <!-- checked: {STALE} -->",
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->")
        self.assertEqual(self._run(), 1)

    def test_dangling_path_fails(self) -> None:
        """引用的证据文件不存在 = 死链，读者无从核对。"""
        _write(self.tmp,
               f"- **长稳**：见 `scripts/nope_not_here.sh` <!-- checked: {FRESH} -->",
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->")
        self.assertEqual(self._run(), 1)

    def test_existing_path_passes(self) -> None:
        (self.tmp / "scripts").mkdir()
        (self.tmp / "scripts" / "soak_test.py").write_text("", encoding="utf-8")
        _write(self.tmp,
               f"- **长稳**：见 `scripts/soak_test.py` <!-- checked: {FRESH} -->",
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->")
        self.assertEqual(self._run(), 0)

    def test_section_boundary_respected(self) -> None:
        """§9 之后、下一个 `##` 之前的列表不算开放项（否则收进一堆误报）。"""
        _write(self.tmp,
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->",
               f"- **长稳**：还没跑 <!-- checked: {FRESH} -->")
        # 「不是开放项」那条没有标记，若被算进去就会红
        self.assertEqual(self._run(), 0)

    def test_real_repo_is_clean(self) -> None:
        """本仓当前状态必须过 —— 这条防止有人改文档时顺手删标记。"""
        self.assertEqual(check(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
