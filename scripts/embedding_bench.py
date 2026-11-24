#!/usr/bin/env python3
"""scripts/embedding_bench.py — 手动冒烟：**字符 bigram vs 真 embedding 的召回质量对比**。

**为什么必须有这个脚本**：单测只能证明"链路对得上"（假向量 → 排序 → 返回值），
不能证明"语义真的比字面强"。后者是一个**经验事实**，只能在真网关上量：

    bigram    : 用户换个说法问 → 目标记忆 ≈0.07 → 排到第 8 → 等于没召回
    embedding : 同一对文本余弦 0.81 → 排第 1 → 召回了

所以这里用 10 条真实风格的记忆 + 5 条**刻意换说法**的查询，把两种打分的命中排名
并排打出来。命中排名（rank）比分数更有说服力：分数是相对的，排名是用户看到的。

用法：

    export BABEL_KEY=sk-...
    export BABEL_BASE=https://www.babelark.ai/v1          # 可省，有默认值
    export BABEL_EMBED_MODEL=text-embedding-v4            # 可省
    python3 scripts/embedding_bench.py

不设 ``BABEL_KEY`` 时直接 skip（退出码 0），可以放进 CI 而不需要密钥。

**不参与单测**：单测全 fake、不联网（`tests/test_embeddings.py`）。
"""
from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.embeddings import EmbeddingError, from_env                # noqa: E402
from plane.memory import text_similarity                               # noqa: E402
from plane.semantic import SemanticSimilarity                          # noqa: E402

# ══════════════════════════════════════════════════════════════
# 语料：10 条记忆 + 5 条「换说法」查询
# ══════════════════════════════════════════════════════════════
#: 记忆文本刻意写成线上真实风格：术语密度高、句子短、带数值。
MEMORIES: list[str] = [
    "连接池耗尽导致超时",                                  # 0
    "P99 延迟从 80ms 涨到 2s",                             # 1
    "鉴权失败率上升，根因是 token 过期",                   # 2
    "日志盘占用 92%，按当前增速 6 天写满",                 # 3
    "灰度发布必须先过全量回归",                            # 4
    "变更回滚走 helm rollback 到上一版本",                 # 5
    "GPU 实例按小时计费导致月度成本翻倍",                  # 6
    "新人入职第一天要装 uv 和 pre-commit",                 # 7
    "冷启动时缓存命中率只有 12%",                          # 8
    "召回模块的 owner 是 W26",                             # 9
]

#: (查询, 目标记忆下标)。查询一律**换说法**：允许字形几乎不重合，
#: 只保留"同一个意思" —— 这正是 bigram 失效、embedding 生效的区间。
QUERIES: list[tuple[str, int]] = [
    ("数据库连接满了打不开", 0),
    ("接口怎么突然慢成这样", 1),
    ("谁在反复报登录过期", 2),
    ("磁盘还剩几天可用", 3),
    ("上线前需要准备什么", 4),
]


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补空格（中文算 2 列），否则表格会歪。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _skip(message: str) -> int:
    print(f"[skip] {message}")
    return 0


def _ranks_bigram(queries: list[tuple[str, int]]) -> list[list[tuple[int, float]]]:
    return [[(i, text_similarity(q, text)) for i, text in enumerate(MEMORIES)]
            for q, _ in queries]


def _ranks_embedding(queries: list[tuple[str, int]],
                     sim: SemanticSimilarity) -> list[list[tuple[int, float]]]:
    return [sim.rank(q, MEMORIES) for q, _ in queries]


def _rank_of(ranked: list[tuple[int, float]], target: int) -> int:
    """目标在第几名（1 起）。找不到返回 -1（不该发生）。"""
    for position, (index, _score) in enumerate(ranked, start=1):
        if index == target:
            return position
    return -1


def _score_of(ranked: list[tuple[int, float]], target: int) -> float:
    for index, score in ranked:
        if index == target:
            return score
    return 0.0


def _summary(rows: list[tuple[int, int]], total: int) -> str:
    """rows: [(目标名次, 目标名次)] → hit@1 / hit@3 / MRR。"""
    hit1 = sum(1 for a, _ in rows if a == 1)
    hit3 = sum(1 for a, _ in rows if a <= 3)
    mrr_a = sum(1.0 / a for a, _ in rows if a > 0) / total
    return f"hit@1={hit1}/{total}  hit@3={hit3}/{total}  MRR={mrr_a:.3f}"


def _print_table(title: str, ranked_all: list[list[tuple[int, float]]],
                 queries: list[tuple[str, int]], top: int = 3) -> None:
    print(f"\n{title}")
    print("─" * 96)
    print(f"{_pad('查询', 30)}{_pad('目标记忆', 44)}{_pad('目标名次', 14)}Top-3（名次:分数:文本）")
    for (query, target), ranked in zip(queries, ranked_all):
        position = _rank_of(ranked, target)
        cells = []
        for k, (index, score) in enumerate(ranked[:top], start=1):
            mark = "*" if index == target else " "
            cells.append(f"{k}{mark}{score:.3f} {MEMORIES[index][:14]}")
        print(f"{_pad(query, 30)}{_pad(MEMORIES[target], 44)}"
              f"{_pad(f'#{position}  {_score_of(ranked, target):.3f}', 14)}"
              + " | ".join(cells))
    print("─" * 96)
    print("（Top-3 里带 * 的是目标记忆；名次越小越该被召回。"
          "注意名次好≠分高：候选全都不像时，略像的那条也会 #1，所以 score 也要看）")


def main() -> int:
    key = os.environ.get("BABEL_KEY") or os.environ.get("BABEL_API_KEY") or ""
    if not key.strip():
        return _skip("未设置 BABEL_KEY，跳过真实 embedding 对比（单测全 fake，不联网）")

    provider = from_env("BABEL")
    if provider is None:
        return _skip("BABEL_* 配置不完整，跳过（只设了 key 但 base/model 不可用）")

    print(f"[env] endpoint={provider.endpoint}")
    print(f"[env] model={provider.model} batch_size={provider.batch_size}")
    print(f"[env] 记忆 {len(MEMORIES)} 条 / 查询 {len(QUERIES)} 条（每条查询都换了说法）")

    semantic = SemanticSimilarity(provider)
    try:
        emb_ranks = _ranks_embedding(QUERIES, semantic)
    except EmbeddingError as exc:                       # rank 内部已降级，这里是兜底
        print(f"[error] embedding 调用失败：{exc}")
        return 1
    except Exception as exc:                            # noqa: BLE001 — 冒烟脚本要给出人话
        print(f"[error] {type(exc).__name__}: {exc}")
        print("[hint] 检查网络 / BABEL_BASE / 余额；单测不受影响（全 fake）。")
        return 1

    big_ranks = _ranks_bigram(QUERIES)

    if semantic.stats()["provider_errors"]:
        print(f"[warn] 已降级 {semantic.stats()['fallback_hits']} 次："
              f"{semantic.stats()['last_error']}")
        print("[warn] 下面的『表 B』实际上是 bigram，对比无意义。")
        return 1

    _print_table("表 A：字符 bigram Jaccard（现状，plane/memory.text_similarity）",
                 big_ranks, QUERIES)
    _print_table("表 B：真语义向量（OpenAICompatEmbeddings → SemanticSimilarity.rank）",
                 emb_ranks, QUERIES)

    rows = [(_rank_of(big, target), _rank_of(emb, target)) for (_, target), big, emb
            in zip(QUERIES, big_ranks, emb_ranks)]
    print("\n汇总（名次越小越好）")
    print("─" * 96)
    print(f"{_pad('', 18)}{_pad('bigram', 40)}embedding")
    print(f"{_pad('逐条名次', 18)}"
          + _pad(" ".join(f"#{a}" for a, _ in rows), 40)
          + " ".join(f"#{b}" for _, b in rows))
    print(f"{_pad('命中率', 18)}{_pad(_summary(rows, len(QUERIES)), 40)}"
          f"{_summary([(b, a) for a, b in rows], len(QUERIES))}")
    print("─" * 96)

    stats = provider.stats()
    print(f"[usage] requests={stats['requests']} sent_texts={stats['sent_texts']} "
          f"cached={stats['cached']} dim={stats['dim']} "
          f"tokens_est={stats['tokens_est']} cost_estimate≈${stats['cost_estimate']:.6f}")

    better = sum(1 for a, b in rows if b < a)
    worse = sum(1 for a, b in rows if b > a)
    print(f"\n结论：{better} 条查询排名提升、{worse} 条下降（共 {len(rows)} 条）。")
    if better and not worse:
        print("      bigram 的失效区间是『同义改写』；这一栏全绿说明换 embedding 是有意义的。")
    elif worse:
        print("      有下降项 → 先看是哪条：短查询 + 专有名词时向量也会偏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
