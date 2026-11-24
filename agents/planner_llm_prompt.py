"""agents/planner_llm_prompt.py —— `LLMPlanner` 的「快照压缩 + 计划解析」两段胶水。

为什么单独一个文件：这两段全是**内容判断**，不是机制。

  1. `project_snapshot()`：Context 快照是给机器读的（history / tool_results 全量、
     字段一堆），直接塞给模型有两类事故——① 窗口被历史对话吃掉，真正该看的
     「死锁原因 + 最近一次工具回包」反而被截断；② 模型开始对着历史复述而不是
     重写计划。所以要**有损压缩**：目标、阶段、旧计划摘要、死锁原因、最近几条
     历史与工具回包。哪些算「最近几条」是业务口径，Runtime 不掺和。

  2. `parse_plan()`：模型输出必须被**严格**解析。坏 JSON、裸文本、Markdown 围栏、
     多吐了十步，都不能让规划路径崩 —— 崩了死锁就变成异常，而异常这条路上
     Loop 只会记一条 warning 然后判 NO_PROGRESS，真正的失败原因（模型又胡说）
     就被埋了。所以这里一律「解析不出 → None」，由 `LLMPlanner` 决定怎么退。

输出形状固定为（`plan` 字段的惯用形状，见 `core/context.py` 与 Runtime 测试）：

    {"source": "llm", "reason": "...", "steps": [{"step": ..., "tool": ...,
      "args": {...}, "why": ...}, ...], "truncated": false, "dropped": 0}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "ParsedPlan",
    "build_replan_prompt",
    "parse_plan",
    "project_snapshot",
]


# ══════════════════════════════════════════════════════════════
# 压缩参数（全是业务口径：多长算「一条」、保留几条）
# ══════════════════════════════════════════════════════════════
#: 历史保留条数 / 单条截断长度
_HISTORY_ITEMS = 6
_HISTORY_CHARS = 240
#: 工具回包保留条数 / 单条截断长度
_RESULT_ITEMS = 6
_RESULT_CHARS = 400
#: 观测、错误、旧计划里单条文字的上限
_NOTE_CHARS = 200
#: 快照投影整体的兜底上限（防止模型吐出来的 prompt 本身超窗）
_VIEW_CHARS = 6000

_MAX_STEPS_HARD_CAP = 32        # 再怎么写计划也不该一步给 100 条，防呆


@dataclass(frozen=True)
class ParsedPlan:
    """解析结果。`steps` 已截到 `max_steps`，`dropped` 是被丢掉的原始条数。"""

    steps: tuple[dict, ...]
    dropped: int = 0
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.steps)

    def as_dicts(self) -> list[dict]:
        return [dict(s) for s in self.steps]


# ══════════════════════════════════════════════════════════════
# ① 快照 → 模型能读的投影
# ══════════════════════════════════════════════════════════════
def _clip(value: Any, limit: int = _NOTE_CHARS) -> str:
    """任何值 → 定长字符串（保持确定性：不做随机化、不加时间戳）。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _as_mapping(snapshot: Any) -> Mapping[str, Any]:
    """快照可能是 `Mapping`，也可能是 Runtime 的 `ContextSnapshot`（有 `.data`）。

    两种都接住：契约上写的是 `Mapping`，但 `core/loop.py` 真实传的是
    `ctx.snapshot()` 的返回值（一个带 `.data` 的冻结 dataclass）。
    不兼容这一点，实现就会在死锁那一刻才炸 —— 那是最不该炸的地方。
    """
    if isinstance(snapshot, Mapping):
        return snapshot
    data = getattr(snapshot, "data", None)
    if isinstance(data, Mapping):
        return data
    return {}


def _goal_of(snapshot: Mapping[str, Any]) -> str:
    """目标优先取显式字段，其次取 history 里第一条 user 消息。"""
    for key in ("goal", "objective", "task"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    history = snapshot.get("history")
    if isinstance(history, (list, tuple)):
        for item in history:
            if isinstance(item, Mapping) and item.get("role") == "user":
                return _clip(item.get("text"), _HISTORY_CHARS)
    return ""


def _tail(items: Any, limit: int, chars: int) -> list[str]:
    """取尾部 N 条并逐条截断；空/非列表 → 空列表（**没有 history 也能工作**）。"""
    if not isinstance(items, (list, tuple)):
        return []
    tail = list(items)[-limit:]
    return [_clip(item, chars) for item in tail]


def project_snapshot(snapshot: Any) -> dict:
    """把只读快照压成「重写计划所需要的那些事实」。纯函数、确定性。"""
    snap = _as_mapping(snapshot)
    view: dict[str, Any] = {
        "目标": _goal_of(snap),
        "阶段": _clip(snap.get("phase"), 40),
        "轮次": snap.get("loop_count"),
        "旧计划": _clip(snap.get("plan"), _NOTE_CHARS * 2),
        "当前错误": _clip(snap.get("error"), _NOTE_CHARS),
        "最近历史": _tail(snap.get("history"), _HISTORY_ITEMS, _HISTORY_CHARS),
        "最近工具回包": _tail(snap.get("tool_results"), _RESULT_ITEMS, _RESULT_CHARS),
        "最近观测": _tail(snap.get("observations"), _RESULT_ITEMS, _RESULT_CHARS),
    }
    # 其它字段只报名字：模型知道"还有这些上下文"就够，不必看内容
    if snap:
        known = {"goal", "objective", "task", "phase", "loop_count", "plan", "error",
                 "history", "tool_results", "observations"}
        others = sorted(k for k in snap if k not in known)
        if others:
            view["其它字段"] = others
    return view


# ══════════════════════════════════════════════════════════════
# ② Prompt
# ══════════════════════════════════════════════════════════════
def build_replan_prompt(snapshot: Any, reason: str, *, max_steps: int = 6) -> str:
    """拼 replan prompt。要求模型**只输出 JSON**，形状在 prompt 里写死。"""
    limit = max(1, min(int(max_steps), _MAX_STEPS_HARD_CAP))
    view = project_snapshot(snapshot)
    body = json.dumps(view, ensure_ascii=False, indent=2, default=str)
    if len(body) > _VIEW_CHARS:
        body = body[:_VIEW_CHARS] + "\n… （快照过长，已截断）"

    return "\n".join([
        "你在修一个卡住的执行回合：沿用当前做法已经连续多步没有新进展。",
        "你的任务不是解释为什么卡住，而是给一份**换过路的**新计划。",
        "",
        "【死锁原因】",
        str(reason or "（未提供）"),
        "",
        "【已知事实（只读快照）】",
        body,
        "",
        "【输出要求】",
        "1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释文字。",
        "2. 形状：",
        '   {"steps": [{"step": "这一步做什么", "tool": "工具名或空串",',
        '              "args": {}, "why": "为什么这样能绕开死锁"}]}',
        f"3. 最多 {limit} 步，多了会被截断。",
        "4. 每一步必须是可观测的动作：能看到结果、能判断成败。",
        "5. 不要重复「最近历史」里已经失败过的做法。",
    ])


# ══════════════════════════════════════════════════════════════
# ③ 严格解析
# ══════════════════════════════════════════════════════════════
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)

#: 文字字段的优先顺序（"name" 放最后：它也可能被模型当成工具名用）
_TEXT_KEYS = ("step", "action", "goal", "description", "task", "text", "name")
_TOOL_KEYS = ("tool", "tool_name")
_ARGS_KEYS = ("args", "arguments", "params", "input")
_WHY_KEYS = ("why", "reason", "rationale")


def _balanced(text: str, start: int) -> str | None:
    """从 `start` 处的 `{` / `[` 起，取第一个括号配平的片段（跳过字符串里的括号）。"""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _json_candidates(text: str) -> Iterable[str]:
    """按「可信度从高到低」给出候选 JSON 文本，逐个试。"""
    stripped = text.strip()
    if not stripped:
        return
    yield stripped
    for match in _FENCE.finditer(text):
        fenced = match.group(1).strip()
        if fenced:
            yield fenced
    # 最小化提取：第一个括号起的配平片段（吃掉「好的，计划如下：{...}」这种前缀）
    for index, ch in enumerate(text):
        if ch in "{[":
            piece = _balanced(text, index)
            if piece:
                yield piece
            break


def _extract_raw_steps(obj: Any) -> list | None:
    """从任意 JSON 结构里捞出「步骤列表」这一层。"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, Mapping):
        for key in ("steps", "plan", "tasks", "actions", "todo", "items"):
            value = obj.get(key)
            if isinstance(value, list):
                return value
        # 模型只给了一步、直接摊平成对象
        if any(k in obj for k in _TEXT_KEYS + _TOOL_KEYS):
            return [obj]
        return None
    if isinstance(obj, str) and obj.strip():
        return [obj]
    return None


def _first_text(entry: Mapping[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_str(entry: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_step(entry: Any) -> dict | None:
    """单条步骤归一化。无法变成「可执行的一句话」就返回 None（调用方计 dropped）。"""
    if isinstance(entry, str):
        text = entry.strip()
        return {"step": text, "tool": "", "args": {}, "why": ""} if text else None
    if not isinstance(entry, Mapping):
        return None

    tool = _first_str(entry, _TOOL_KEYS)
    text = _first_text(entry)
    if not text and tool:
        text = f"调用 {tool}"          # 只有工具名也算一步，但要说清是调用
    if not text:
        return None
    args = {}
    for key in _ARGS_KEYS:
        value = entry.get(key)
        if isinstance(value, Mapping):
            args = dict(value)
            break
    return {
        "step": text,
        "tool": tool,
        "args": args,
        "why": _first_str(entry, _WHY_KEYS),
    }


def parse_plan(text: Any, *, max_steps: int = 6) -> ParsedPlan | None:
    """模型输出 → `ParsedPlan`；任何解析不出（空 / 坏 JSON / 空步骤）→ `None`。

    **绝不抛异常**：解析失败是常态（模型爱加解释、爱用围栏），
    返回值就能表达，抛出去只会让调用方多一层 try。
    """
    if not isinstance(text, str) or not text.strip():
        return None

    parsed: Any = None
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        break
    if parsed is None:
        return None

    raw_steps = _extract_raw_steps(parsed)
    if not raw_steps:
        return None

    normalized: list[dict] = []
    for entry in raw_steps:
        step = _normalize_step(entry)
        if step is not None:
            normalized.append(step)
    if not normalized:
        return None

    limit = max(1, min(int(max_steps), _MAX_STEPS_HARD_CAP))
    truncated = len(normalized) > limit
    kept = tuple(normalized[:limit])
    return ParsedPlan(
        steps=kept,
        dropped=len(raw_steps) - len(kept),
        truncated=truncated,
    )
