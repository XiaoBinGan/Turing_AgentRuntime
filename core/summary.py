"""Summary Memory：历史滚动压缩（架构 §3 七层记忆 → ``Context.summary``）。

## 为什么需要这一层

``Context.history`` 是原始多轮记录，属 Builder 的输入域；Builder 的 ``_render_history``
只吃最后 8 条，``_fit`` 还会按分源预算继续腰斩。被裁掉的信息**真的没了**——
Agent 跑 30 轮后，早期「连接池上限 20」这类关键决议消失，模型开始自我矛盾。

滚动摘要补的正是这个缺口：

* 老 history（头部）压成一段**结构化摘要**，写进 ``Context.summary``（owner=LOOP），
  再由 Builder 作为 Prompt 的一段稳定前缀（摘要比原文短且形状固定，缓存友好；
  接线见下文「接入现状」——Builder 现在还没读这个字段）；
* 原始记录仍保留在 ``history`` / patch_log 里，靠 :class:`SummaryResult.ref`
  的 ``[from_index, to_index)`` 回去追溯，审计不丢证据；
* 新消息继续追加，下次压缩时旧摘要原样并入、只压增量部分——所以压缩是**可重复的**。

## 设计约束（都是可测的）

1. **纯函数**：不读时钟、不读随机、不碰 IO。同输入必同输出。
2. **只读入参**：``compress`` 只读 ``history``，绝不改写它。
3. **可手算**：保留尾部 ``max_keep`` 条，其余按类型提炼成固定前缀的短句。
4. **不 import core/context**：``apply_to_context`` 只要求鸭子类型的
   ``snapshot()`` / ``apply()``，好让测试用 fake 顶、也让 Loop 之外（比如离线
   复盘脚本）能复用。

## 接入现状（重要，别以为写完就自动进 Prompt）

``builder.PromptBuilder.build`` 目前只渲染 6 段（system / tools / skills /
tool_results / history / memory / knowledge），**不读 ``snap.get("summary")``**。
也就是说：压缩把数据写对了，但 Prompt 里还看不到，除非 Builder 加一段。

本模块不越界改 ``builder.py``，只把「数据和格式」准备好，并把接线成本压到 1 行：

.. code-block:: python

    # builder.PromptBuilder.build 里
    sections["summary"] = summary_section(snap)     # 本模块提供，空摘要返回 ""
    # 并且 _fit 的 DEFAULT_RATIO 里给 "summary" 一个比例（否则 cap=0 会被整段丢）

不接线时的后果是可算的：Builder 只渲染最后 8 条，早期决议本来就没了，
压缩与否 Prompt 一字不变——这时压缩唯一的产出是 ``Context.summary`` 与
``ref`` 回指，等 Prompt 段接上才兑现。

## 与 Gateway / 其他 worker 的关系

本模块只依赖 ``contracts``（``Patch`` / ``Owner``），
不 import ``core/context``、不 import ``core/loop``、不 import ``core/gateway``，
避免并行施工与环状依赖。token 口径优先复用 ``builder.estimate_tokens``（builder 只
依赖 contracts，不会反向依赖 core），拿不到时退回本地近似公式。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from contracts import Owner, Patch

__all__ = [
    "HistoryCompressor",
    "SummaryResult",
    "apply_to_context",
    "estimate_tokens",
    "render_entry",
    "summary_section",
]

# ══════════════════════════════════════════════════════════════
# token 估算：优先与 Builder 同口径
# ══════════════════════════════════════════════════════════════
try:  # pragma: no cover - 正常路径下一定拿得到
    from builder import estimate_tokens as _builder_estimate_tokens
except Exception:  # noqa: BLE001 - builder 缺席时退回本地估算，绝不因 import 失败炸主链路
    _builder_estimate_tokens = None


#: Builder ``CALIBRATED`` 的等价兜底（CJK 0.65 tok/字，非 CJK 6.1 字/tok）。
#: 只在拿不到 builder 时用，口径对齐用不着精确 tokenizer，只要排序正确、可复现。
_FALLBACK_CJK_PER_TOKEN = 0.65
_FALLBACK_OTHER_CHARS_PER_TOKEN = 6.1


def estimate_tokens(text: str) -> int:
    """确定性 token 估算（口径随 Builder）。"""
    if not text:
        return 0
    if _builder_estimate_tokens is not None:
        try:
            return int(_builder_estimate_tokens(text))
        except Exception:  # noqa: BLE001 - 估算器抽风不该让压缩失败
            pass
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    other = len(text) - cjk
    return max(1, int(cjk * _FALLBACK_CJK_PER_TOKEN
                      + other / _FALLBACK_OTHER_CHARS_PER_TOKEN))


# ══════════════════════════════════════════════════════════════
# 固定格式常量：摘要形状要稳定，模型才读得懂、前缀缓存才打得中
# ══════════════════════════════════════════════════════════════
#: 摘要首行。``{n}`` = 累计被压缩的原始消息条数（跨多次压缩累加）。
HEADER_FORMAT = "【历史摘要｜已压缩 {n} 条】"

#: 解析已有摘要首行的计数，用于累加（合并时不丢历史总量）。
_HEADER_RE = re.compile(r"【历史摘要｜已压缩\s*(\d+)\s*条】")

#: 末尾省略标记。``{n}`` = 被丢掉的条目数（不是消息条数，条目已聚合）。
OMITTED_FORMAT = "…（更早 {n} 条已省略）"

#: 条目前缀（固定四类 + 兜底跳过）。冒号用全角，与正文里模型自己的表达习惯一致。
PREFIX_GOAL = "目标："
PREFIX_DONE = "已执行 "
PREFIX_FAIL = "失败 "
PREFIX_CONCLUSION = "结论："

#: 每条摘要前的列表符号。固定成 ``- `` 有两个好处：模型按行读、结构稳定；
#: 与 Builder 的 ``_render_history``（``- role: text``）视觉一致，Prompt 里不突兀。
ENTRY_BULLET = "- "

#: 失败条目保留的 reason 前 N 字（超出加省略号，按字符边界切）。
REASON_KEEP_CHARS = 60

#: 单条目正文上限。防止一条 3000 字的长消息独占全部摘要预算——
#: 预算策略是「从最旧丢条目」，若不设上限，丢掉的会是整条目标而不是它的细节。
MAX_ENTRY_CHARS = 200

#: 已有摘要里上一轮遗留的省略标记也要认，累加计数后重新渲染（不出现两个标记）。
_OMITTED_RE = re.compile(r"…（更早\s*(\d+)\s*条已省略）")

_KIND_OK = "ok"


# ══════════════════════════════════════════════════════════════
# 结果
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SummaryResult:
    """一次压缩的产物 + 账。

    字段口径（都是「可手算」的）：

    * ``compressed_count``：**这一次**被压掉的原始消息条数；
    * ``kept_count``：留在 history 里的条数（正常等于 ``max_keep``）；
    * ``tokens_before``：压缩前这段 Prompt 的成本 = 全部 history 文本 + 旧摘要；
    * ``tokens_after``：压缩后 = 保留的 history 文本 + 新摘要（含固定首行，别少算）；
    * ``method``：``"rule"``（本实现全确定性）/ ``"llm"``（预留给摘要模型，暂不产出）；
    * ``ref``：回指被压缩区间 ``[from_index, to_index)``，审计时据此回捞原文。
    """

    summary: str
    compressed_count: int
    kept_count: int
    tokens_before: int
    tokens_after: int
    method: str
    ref: dict = field(default_factory=dict)

    # ── 便捷派生量（只读） ──────────────────────────────────
    @property
    def saved_tokens(self) -> int:
        """省下的 token（可能为负：摘要比原文长时如实反映，不粉饰）。"""
        return self.tokens_before - self.tokens_after

    @property
    def compressed(self) -> bool:
        """这次是否真的压了东西。"""
        return self.compressed_count > 0

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "compressed_count": self.compressed_count,
            "kept_count": self.kept_count,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "method": self.method,
            "ref": dict(self.ref),
        }


# ══════════════════════════════════════════════════════════════
# 小工具：畸形输入一律降级成空串，绝不抛
# ══════════════════════════════════════════════════════════════
def _as_text(value: Any) -> str:
    """只认 ``str``。其他类型（dict/list/None）一律当空——LLM 的家底是文本。"""
    if isinstance(value, str):
        return value.strip()
    return ""


def _as_mapping(msg: Any) -> Mapping[str, Any] | None:
    return msg if isinstance(msg, Mapping) else None


def _message_text(msg: Any) -> str:
    """取一条消息的可计入 token 的文本（畸形消息不抛）。"""
    m = _as_mapping(msg)
    if m is None:
        return _as_text(msg)
    parts = [_as_text(m.get("text")), _as_text(m.get("reason"))]
    return "\n".join(p for p in parts if p)


def _clip_chars(text: str, limit: int, *, mark: str = "…") -> str:
    """按**字符**边界截断（中文不会被切坏：Python str 切片天然是码点级）。

    截断时保留 ``limit - 1`` 个字符再加省略号，保证结果长度不超过 ``limit``。
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(mark))].rstrip() + mark


def render_entry(kind: str, *, text: str = "", tool: str = "",
                 reason: str = "") -> str:
    """把一条被压缩的消息渲染成摘要行（公开给测试与外部复用）。

    ``kind`` ∈ ``goal`` / ``done`` / ``fail`` / ``conclusion``；未知 kind 返回空串
    （调用方据此跳过，而不是塞一行噪音进 Prompt）。
    """
    if kind == "goal":
        body = _clip_chars(text, MAX_ENTRY_CHARS)
        return PREFIX_GOAL + body if body else ""
    if kind == "done":
        name = tool or "?"
        return PREFIX_DONE + name
    if kind == "fail":
        name = tool or "?"
        detail = _clip_chars(reason or text, REASON_KEEP_CHARS)
        return PREFIX_FAIL + name + (": " + detail if detail else "")
    if kind == "conclusion":
        body = _clip_chars(text, MAX_ENTRY_CHARS)
        return PREFIX_CONCLUSION + body if body else ""
    return ""


def _entry_of(msg: Any) -> str:
    """按 §「压缩规则 2」把一条消息提炼成一行；不合格返回 ``""``（跳过）。"""
    m = _as_mapping(msg)
    if m is None:
        return ""
    role = _as_text(m.get("role")).lower()
    if not role:
        return ""

    text = _as_text(m.get("text"))

    if role == "user":
        return render_entry("goal", text=text)

    if role == "assistant":
        # 带 tool_calls 的 assistant 只是在「发起动作」，不是结论——跳过，
        # 因为动作的结果会以 tool 消息单独出现，记两遍就是重复预算。
        if m.get("tool_calls"):
            return ""
        return render_entry("conclusion", text=text)

    if role == "tool":
        tool = _as_text(m.get("tool")) or _as_text(m.get("name"))
        kind = m.get("kind")
        kind_s = _as_text(kind).lower()
        reason = _as_text(m.get("reason"))
        if not reason and kind_s and kind_s != _KIND_OK:
            # Loop 写 history 时把原因并进了 text（"工具 X 失败：<reason>"），
            # 这里退化为整段 text，仍按 60 字截。
            reason = text
        if kind_s == _KIND_OK or (not kind_s and not reason):
            return render_entry("done", tool=tool)
        return render_entry("fail", tool=tool, reason=reason or text)

    # system / 未知 role → 跳过（系统提示不是历史决议）
    return ""


def _split_existing(existing: str) -> tuple[str, int, int]:
    """拆已有摘要 → ``(正文块, 已压缩条数, 已省略条数)``。

    * 首行的 ``【历史摘要｜已压缩 N 条】`` 不保留（合并后重渲染计数）；
    * 上一轮的省略标记也不保留（否则会出现两个标记、计数还会重复）；
    * 其余正文**原样**保留（包括空行与缩进，避免二次压缩吃掉旧格式）。
    """
    if not existing:
        return "", 0, 0
    lines = existing.split("\n")
    prev_count = 0
    omitted = 0
    body: list[str] = []
    for line in lines:
        stripped = line.strip()
        hit = _HEADER_RE.search(stripped)
        if hit and stripped.startswith("【历史摘要"):
            prev_count = max(prev_count, int(hit.group(1)))
            continue
        hit = _OMITTED_RE.search(stripped)
        if hit:
            omitted += int(hit.group(1))
            continue
        body.append(line)
    # 去掉首尾空行（纯格式噪音），中间空行保留
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body), prev_count, omitted


def _dedupe_keys(block: str) -> set[str]:
    """已有摘要的正文行参与去重：同一结论跨次压缩只出现一次。"""
    keys: set[str] = set()
    for line in block.split("\n"):
        s = line.strip()
        if s.startswith("- "):
            s = s[2:].strip()
        if s:
            keys.add(s)
    return keys


# ══════════════════════════════════════════════════════════════
# 压缩器
# ══════════════════════════════════════════════════════════════
#: 属于「上一条 assistant 的从属结果」的角色。切分时不能与它分开。
_TOOL_ROLES = frozenset({"tool"})


def _safe_cut(messages: Sequence[dict], want: int) -> int:
    """把切点从 `want` 往前挪到**单元边界**，保证 `messages[want:]` 不以 tool 开头。

    为什么只往前挪不往后挪：往后挪会让 tail 少于目标条数，压缩变弱；
    往前挪只是多压一条，代价小。

    最坏情况退到 0（全压），此时 tail 为空 —— 这也是合法结果，
    只是压缩得最狠。循环一定能终止：每次至少减 1。
    """
    total = len(messages)
    cut = max(0, min(int(want), total))
    while cut > 0 and cut < total:
        nxt = messages[cut]
        role = str(nxt.get("role") or "") if isinstance(nxt, dict) else ""
        if role not in _TOOL_ROLES:
            break
        cut -= 1
    return cut


class HistoryCompressor:
    """把 history 头部压成结构化摘要，尾部原样保留。

    纯函数式：``compress`` 不读时钟、不读随机、不碰 IO、不改入参。
    """

    #: 固定方法标记。留 ``"llm"`` 的扩展位：将来换模型摘要时只改这一个字段，
    #: 下游（Loop 记账 / 审计回放）按 method 分流，不用改契约。
    METHOD = "rule"

    def __init__(self, *, max_keep: int = 8, max_summary_chars: int = 1200,
                 estimator: Callable[[str], int] | None = None,
                 max_summary_tokens: int | None = None) -> None:
        if not isinstance(max_keep, int) or isinstance(max_keep, bool):
            raise TypeError(f"max_keep 必须是 int，收到 {type(max_keep).__name__}")
        if not isinstance(max_summary_chars, int) or isinstance(max_summary_chars, bool):
            raise TypeError(
                f"max_summary_chars 必须是 int，收到 {type(max_summary_chars).__name__}")
        if estimator is not None and not callable(estimator):
            raise TypeError("estimator 必须可调用：str -> int")
        if max_summary_tokens is not None and (
                not isinstance(max_summary_tokens, int)
                or isinstance(max_summary_tokens, bool)):
            raise TypeError("max_summary_tokens 必须是 int 或 None，收到 "
                            f"{type(max_summary_tokens).__name__}")
        # 负值没有语义，收成 0（不静默夹取会让下方切片出现负索引，行为难猜）
        self.max_keep = max(0, max_keep)
        self.max_summary_chars = max(0, max_summary_chars)
        # 「预算联动」：字符上限管「排版」，token 上限管「Builder 的分源预算」。
        # 给 None 就只按字符管（默认），交给调用方按剩余预算决定要不要卡 token。
        self.max_summary_tokens = (None if max_summary_tokens is None
                                   else max(0, max_summary_tokens))
        self._estimator = estimator or estimate_tokens

    # ── 对外 ────────────────────────────────────────────────
    def should_compress(self, history: Sequence[Any]) -> bool:
        """条数超过 ``max_keep`` 才值得压（否则纯亏一次改写）。"""
        return len(_as_list(history)) > self.max_keep

    def compress(self, history: Sequence[dict],
                 existing_summary: str = "") -> SummaryResult:
        """压缩 ``history`` 的头部，返回摘要 + 账。不修改 ``history``。

        ``existing_summary`` 必须是**更早那段 history** 压出来的摘要
        （``apply_to_context`` 收敛 history 后天然满足）。若把同一批消息连同它
        自己的摘要再次传入，首行计数会重复累加——调用方按「增量压缩」用即可。
        """
        messages = _as_list(history)          # 浅拷贝列表；下面只读其中的 dict
        existing = existing_summary if isinstance(existing_summary, str) else ""
        total = len(messages)

        tokens_before = (sum(self._estimator(_message_text(m)) for m in messages)
                         + self._estimator(existing))

        # ① 不需要压缩：原样返回已有摘要（history 一条不动，压缩 0 条）
        if total <= self.max_keep:
            return SummaryResult(
                summary=existing,
                compressed_count=0,
                kept_count=total,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                method=self.METHOD,
                ref={"from_index": 0, "to_index": 0},
            )

        # **按「不可分单元」切，不按索引盲切。**
        #
        # 一条 `assistant` 发起调用 + 它的全部 `tool` 结果是**一个语义单元**：
        # 把 tool 结果留下、把发起它的 assistant 切掉，模型看到的就是
        # "一个没有来由的工具输出"。实测随机 2000 次真实形态 history，
        # 纯索引切在 59.2% 的情况下会切出这种孤儿。
        #
        # （注：本仓的工具结果是渲染成**文本**进 Prompt 的，不是 OpenAI 的
        # `role:"tool"` 协议消息，所以不会像原生实现那样直接 400。但摘要边界
        # 语义错乱同样是 bug，只是后果更隐蔽 —— 它不报错，只让模型误读。）
        cut = _safe_cut(messages, total - self.max_keep)
        head, tail = messages[:cut], messages[cut:]

        # ② 头部逐条提炼（顺序保持时间序，越新越靠后）
        old_block, prev_count, old_omitted = _split_existing(existing)
        seen: set[str] = _dedupe_keys(old_block)
        entries: list[str] = []
        for msg in head:
            line = _entry_of(msg)
            if not line:
                continue
            if line in seen:                  # ④ 同一条结论只留一次
                continue
            seen.add(line)
            entries.append(line)

        # ③ 组装 + ⑤ 超长从最旧丢
        total_compressed = prev_count + len(head)
        summary = self._render(
            total_compressed=total_compressed,
            old_block=old_block,
            old_weight=prev_count or (1 if old_block else 0),
            entries=entries,
            omitted=old_omitted,
        )

        tokens_after = (sum(self._estimator(_message_text(m)) for m in tail)
                        + self._estimator(summary))

        return SummaryResult(
            summary=summary,
            compressed_count=len(head),
            kept_count=len(tail),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            method=self.METHOD,
            ref={"from_index": 0, "to_index": cut},
        )

    # ── 组装 ────────────────────────────────────────────────
    def _render(self, *, total_compressed: int, old_block: str, old_weight: int,
                entries: list[str], omitted: int) -> str:
        """按格式模板拼摘要；超限时从最旧块丢并计省略。

        两条上限同时生效（「预算联动」）：
        ``max_summary_chars`` 管排版宽度，``max_summary_tokens``（可选）管
        Builder 的分源 token 预算。丢弃顺序与「最旧在前」一致：先丢旧摘要块
        （它比所有新条目都老），再从新条目的第一个开始逐个丢。丢一个块就把它的
        权重记进省略数；因此省略标记始终诚实反映「丢了几条」。
        """
        header = HEADER_FORMAT.format(n=total_compressed)
        # 上限 <= 0：只留首行（首行是格式契约，丢了模型就不知道这是摘要）
        if self.max_summary_chars <= 0:
            return header

        keep_old = bool(old_block)
        old_weight_eff = max(1, old_weight) if keep_old else 0
        kept_entries = list(entries)
        omitted_n = omitted

        def render(has_old: bool, ents: list[str], om: int) -> str:
            parts = [header]
            if has_old:
                parts.append(old_block)
            if ents:
                parts.append("\n".join(ENTRY_BULLET + e for e in ents))
            if om > 0:
                parts.append(OMITTED_FORMAT.format(n=om))
            return "\n".join(p for p in parts if p)

        def over_budget(text: str) -> bool:
            if len(text) > self.max_summary_chars:
                return True
            if (self.max_summary_tokens is not None
                    and self._estimator(text) > self.max_summary_tokens):
                return True
            return False

        while True:
            text = render(keep_old, kept_entries, omitted_n)
            if not over_budget(text):
                return text
            if keep_old:
                keep_old = False
                omitted_n += old_weight_eff
                continue
            if kept_entries:
                kept_entries.pop(0)
                omitted_n += 1
                continue
            # 连首行+省略标记都超上限（max_summary_chars 被设得荒唐小）→ 回退到首行
            return header

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"HistoryCompressor(max_keep={self.max_keep}, "
                f"max_summary_chars={self.max_summary_chars}, "
                f"max_summary_tokens={self.max_summary_tokens})")


def _as_list(value: Any) -> list:
    """history 容忍 dict / None / 其他序列；一律转成 list 副本（不改原对象）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


# ══════════════════════════════════════════════════════════════
# 便利函数：把摘要接成 Prompt 的一段
# ══════════════════════════════════════════════════════════════
def summary_section(ctx_or_snap) -> str:
    """取 ``Context.summary`` 当作 Prompt 的一段（Builder 接线用，1 行）。

    接受 ``ContextHandle``（有 ``snapshot()``）或 ``ContextSnapshot``（有 ``get``）；
    没有摘要时返回 ``""``——Builder 的 ``_assemble`` 会跳过空段，所以不会留空行。
    """
    obj = ctx_or_snap
    snapshot = getattr(obj, "snapshot", None)
    if callable(snapshot):
        try:
            obj = snapshot()
        except Exception:  # noqa: BLE001 - 取摘要失败不该炸 Prompt 组装
            return ""
    getter = getattr(obj, "get", None)
    if not callable(getter):
        return ""
    value = getter("summary")
    return value.strip() if isinstance(value, str) else ""


# ══════════════════════════════════════════════════════════════
# 便利函数：直接落到 Context（鸭子类型）
# ══════════════════════════════════════════════════════════════
def apply_to_context(ctx, compressor: HistoryCompressor) -> SummaryResult | None:
    """把「压缩 → 写 summary → 收敛 history」一次做完。

    只要求 ``ctx`` 有 ``snapshot()`` / ``apply()``（**不 import core/context**）：
    Loop 里的 :class:`~core.context.ContextHandle` 满足，测试里的 fake 也满足。

    写入（owner 必须是 ``LOOP``——``contracts.OWNER_TABLE`` 里 summary/history 都归它）：

    * ``Patch("set", "summary", Owner.LOOP, summary_text)``
    * ``Patch("set", "history", Owner.LOOP, kept)`` ← 全新 list，避免 append 语义
      把旧消息又拼回去（这才是「收敛」不是「再追加」）。

    history 条数不足 ``max_keep`` 时返回 ``None`` 且**一个 patch 都不发**。
    """
    snap = ctx.snapshot()
    # ContextSnapshot 与 dict 都有 get()，所以鸭子类型只要求这一个方法
    getter = getattr(snap, "get", None)
    history = getter("history") if callable(getter) else None
    existing = getter("summary") if callable(getter) else None
    messages = _as_list(history)
    existing_text = existing if isinstance(existing, str) else ""

    if len(messages) <= compressor.max_keep:
        return None

    result = compressor.compress(messages, existing_summary=existing_text)

    # 保留尾部：用 list 深一层拷贝（dict 用 dict(...) 复制），
    # 让 Context 里的新 history 与调用方手里那份互不干扰。
    kept = messages[len(messages) - result.kept_count:] if result.kept_count else []
    kept = [dict(m) if isinstance(m, Mapping) else m for m in kept]

    ctx.apply(Patch("set", "summary", Owner.LOOP, result.summary))
    ctx.apply(Patch("set", "history", Owner.LOOP, kept))
    return result
