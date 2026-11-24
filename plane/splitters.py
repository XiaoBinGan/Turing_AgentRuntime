"""plane/splitters.py — 文档切分 SPI：零依赖默认实现 + LlamaIndex 可选实现。

**为什么做成 SPI**：分块策略是**业务选择**，不是存储细节。
Markdown 手册、PDF 论文、代码库、聊天记录 —— 它们的自然边界完全不同。

| 实现 | 依赖 | 适用 |
|---|---|---|
| `HeadingSplitter`（默认） | 无 | Markdown / 带标题的运维手册，确定性、可复现 |
| `LlamaIndexSplitter` | `llama-index-core` | 需要句子边界 / token 感知 / 重叠窗口 |

两者都返回 `list[str]`，`KnowledgeBase` 不关心用的是哪个 —— 换策略不改代码。
"""
from __future__ import annotations

import re
from typing import Any

from contracts import DocumentSplitter

__all__ = [
    "HeadingSplitter",
    "LlamaIndexSplitter",
    "make_splitter",
    "configure_splitter",
    "splitter_available",
]

try:                                                           # 可选依赖（体积大）
    from llama_index.core.node_parser import SentenceSplitter as _LISplitter
    _LLAMA = True
except Exception:                                              # noqa: BLE001
    _LLAMA = False
    _LISplitter = None                                         # type: ignore

#: Markdown / 纯文本标题（`## 3.2 主从切换` 与 `3.2 主从切换` 都认）
_HEADING_RE = re.compile(r"^(?:#{1,6}\s*)?(\d+(?:\.\d+)*)\s+\S")
_SENT_END = re.compile(r"(?<=[。！？；!?;])\s*|\n{2,}")


class HeadingSplitter:
    """按标题/空行切，超长按句子边界再切。**确定性**（同输入同输出）。

    与 `plane/knowledge.py` 内置的分块口径一致 —— 换实现不该改变已有索引的结果。
    """

    name = "heading"

    def __init__(self, *, min_chars: int = 200, max_chars: int = 600) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))

    def split(self, text: str, *, max_chars: int | None = None) -> list[str]:
        cap = int(max_chars) if max_chars else self.max_chars
        cap = max(1, cap)
        out: list[str] = []
        for block in self._blocks(text or ""):
            out.extend(self._fit(block, cap))
        # 短块向后合并（同一节内），避免「3 数据库运维手册」这种垃圾块
        merged: list[str] = []
        for piece in out:
            if merged and len(merged[-1]) < self.min_chars and \
                    len(merged[-1]) + len(piece) + 1 <= cap:
                merged[-1] = merged[-1] + "\n" + piece
            else:
                merged.append(piece)
        return [m.strip() for m in merged if m.strip()]

    @staticmethod
    def _blocks(text: str) -> list[str]:
        blocks, cur = [], []
        for line in text.splitlines():
            if _HEADING_RE.match(line.strip()) and cur:
                blocks.append("\n".join(cur))
                cur = [line]
            elif not line.strip() and cur and len("\n".join(cur)) >= 200:
                blocks.append("\n".join(cur))
                cur = []
            else:
                cur.append(line)
        if cur:
            blocks.append("\n".join(cur))
        return [b for b in blocks if b.strip()]

    @staticmethod
    def _fit(block: str, cap: int) -> list[str]:
        block = block.strip()
        if len(block) <= cap:
            return [block] if block else []
        parts, cur = [], ""
        for sent in _SENT_END.split(block):
            if not sent:
                continue
            if cur and len(cur) + len(sent) > cap:
                parts.append(cur)
                cur = sent
            else:
                cur = cur + sent if cur else sent
            while len(cur) > cap:                     # 超长单句：硬切
                parts.append(cur[:cap])
                cur = cur[cap:]
        if cur:
            parts.append(cur)
        return [p.strip() for p in parts if p.strip()]


class LlamaIndexSplitter:
    """用 LlamaIndex 的 SentenceSplitter（token/句子感知 + 重叠窗口）。

    收益：句子边界更自然、可设 overlap 保上下文、按 token 而非字符计量。
    代价：多一层依赖。**所以它是可选的，不是默认。**
    """

    name = "llama-index"

    def __init__(self, *, max_chars: int = 600, overlap: int = 60) -> None:
        if not _LLAMA:
            raise RuntimeError("llama-index-core 未安装（pip install llama-index-core）")
        self.max_chars = max(1, int(max_chars))
        self.overlap = max(0, int(overlap))

    def split(self, text: str, *, max_chars: int | None = None) -> list[str]:
        cap = int(max_chars) if max_chars else self.max_chars
        # LlamaIndex 按 token 计量；用 1 token ≈ 1.5 字符保守换算
        chunk_size = max(32, int(cap / 1.5))
        overlap = max(0, min(int(self.overlap / 1.5), chunk_size // 2))
        parser = _LISplitter(chunk_size=chunk_size, chunk_overlap=overlap)
        return [c.strip() for c in parser.split_text(text or "") if c.strip()]


def splitter_available() -> dict[str, dict]:
    return {
        "heading": {"available": True, "reason": "零依赖默认实现"},
        "llama-index": {"available": _LLAMA,
                        "reason": "ok" if _LLAMA else "llama-index-core 未安装"},
    }


def make_splitter(kind: str = "heading", **kw: Any) -> DocumentSplitter:
    """按名字造切分器。未知名字 → 报错（不静默降级）。"""
    k = (kind or "heading").strip().lower()
    if k in ("heading", "default", "auto", ""):
        return HeadingSplitter(**kw)
    if k in ("llama-index", "llama_index", "llamaindex", "li"):
        return LlamaIndexSplitter(**kw)
    raise ValueError(f"未知切分器 {kind!r}（可选 heading / llama-index）")


def configure_splitter(env: dict | None = None) -> dict:
    """从环境变量装配。`SPLITTER` / `SPLITTER_MAX_CHARS` / `SPLITTER_OVERLAP`。

    返回 `{"splitter": obj|None, "name": str, "reason": str}` —— **失败要说清原因**。
    """
    import os
    e = dict(os.environ if env is None else env)
    kind = e.get("SPLITTER", "heading")
    max_chars = int(e.get("SPLITTER_MAX_CHARS") or 600)
    overlap = int(e.get("SPLITTER_OVERLAP") or 60)
    try:
        if kind.lower() in ("llama-index", "llama_index", "llamaindex", "li"):
            obj = make_splitter("llama-index", max_chars=max_chars, overlap=overlap)
        else:
            obj = make_splitter("heading", max_chars=max_chars)
        return {"splitter": obj, "name": obj.name, "reason": "ok"}
    except Exception as exc:                                   # noqa: BLE001
        return {"splitter": None, "name": kind,
                "reason": f"{type(exc).__name__}: {exc}"}