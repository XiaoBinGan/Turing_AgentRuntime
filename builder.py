"""builder.py — Prompt 构建（纯函数，INV-2）。

签名里没有 ExecCtx、没有 IO、没有时间、没有随机数。
**同一个 snapshot 两次 build 必须字节级一致**，这是可缓存、可回放、可单测的前提。

架构边界（本次重构的结论）：

* **Runtime 的职责**：分段（system/tools/tool_results/history/memory/knowledge）
  + 各段预算 + 裁剪优先级 + 确定性。
* **Agent 的职责**：每段怎么说话 —— 人格、段标签、各段 header、约束前缀、
  不可信数据包裹词 —— 全部通过 :class:`PromptTemplate` 注入。

因此本模块里**没有任何一句写死的中文 Prompt 措辞**，`DEFAULT_TEMPLATE` 全中性
（`system` 为空串）。需要那套中文措辞的示例 Agent 见 ``agents/demo_template.py``。
（文件内的中文只出现在注释与 docstring 里，这是 BRIEF「注释用中文」的要求；
断言见 ``tests/test_builder.py::test_default_template_output_has_no_cjk``，
它同时对源码做 AST 级扫描，确保没有任何非 docstring 字符串字面量含 CJK。）

预算裁剪策略（优先级从高到低）：
    system > tools > tool_results(最新) > history(最新) > memory_hits > knowledge_hits
超预算时从最低优先级开始截断，绝不截断 system。

**tools 的账要按 adapter 的形状算**（见下节 ``estimate_tool_schema_tokens``）：
真正发给模型的是 JSON Schema，不是本模块渲染的那段人读文本，两者差 2~3 倍。
``PromptBuilder`` 默认把这笔开销从预算里预留掉，且可用预算有 40% 地板价。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from contracts import ContextSnapshot, ToolManifest


@dataclass(frozen=True)
class TokenProfile:
    """token 估算档位。

    默认档刻意保守（**高估**）：低估会让 Prompt 爆窗，高估只是多裁一点。
    但它确实高估 —— 实测 deepseek-flash 上英文高估 1.73x、中文 1.44x，
    所以预算吃紧的场景应当用 :data:`CALIBRATED`。

    换模型 / 换分词器必须重新标定：下面那张表是**某次实测的结果**，不是物理常数。
    """
    name: str = "default"
    cjk_per_token: float = 1.0            # 1 个 CJK 字符折合多少 token
    other_chars_per_token: float = 3.5    # 非 CJK 多少字符折合 1 token
    #: 服务端对 `tools` 的固定结构开销（与工具数量无关的一次性成本）。
    #: **实测跨模型差 5 倍**（gemini 51 / GLM 157 / deepseek 274 / qwen 273），
    #: 所以它必须是可标定字段，不能写死。取上界偏保守（高估只会多裁一点）。
    tool_overhead_fixed: float = 200.0
    note: str = "conservative default: over-trim rather than overflow"


DEFAULT_PROFILE = TokenProfile()

#: 实测标定（2026-11 / deepseek-flash / babelark 网关 / 仅 messages 部分）：
#:   英文 1399 字符 -> 230 tokens；中文 390 字符 -> 255 tokens
#: 换算得：CJK 0.65 tok/字，非 CJK 6.1 字/tok。
CALIBRATED = TokenProfile(
    name="measured-2026-11",
    # 实测 4 个模型（deepseek-flash / qwen3.5-plus / GLM-5.2 / gemini-3.8-flash）：
    #   中文高估 1.44~1.57x → 0.68 tok/字
    #   英文高估 1.73~1.99x → 6.5 字符/tok
    #   单工具 tools 总开销实测 51~274 → 取上界，扣除 schema 正文约 34 tok 后固定项 240
    cjk_per_token=0.68,
    other_chars_per_token=6.5,
    tool_overhead_fixed=240.0,
    note="measured across 4 models; tool overhead spans 51..274 by model",
)


def estimate_tokens(text: str, profile: TokenProfile = DEFAULT_PROFILE) -> int:
    """确定性 token 估算：排序正确、可复现，不求与真实 tokenizer 精确对齐。"""
    if not text:
        return 0
    # 用 ord 比较而不是字面 CJK 字符：源码里就不出现任何 CJK 字面量（可自查）
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    other = len(text) - cjk
    return max(1, int(cjk * profile.cjk_per_token
                      + other / profile.other_chars_per_token))


# ══════════════════════════════════════════════════════════════
# tools JSON schema 的真实开销
#
# 这一节修的是一个**账实不符**：Builder 把工具渲染成人读文本
#
#     - query_logs [read_only/T0_SAFE]: ……
#
# 但 adapter 发给模型的是 **JSON Schema**，形状、长度都不同。两者差 2~3 倍，
# 于是预算显示「还有余量」而真实请求已超窗；或者反过来，为了腾地方裁了 history，
# 而真正占地方的是 schema。所以预算必须按 adapter 的形状算，不能按渲染文本算。
# ══════════════════════════════════════════════════════════════

#: 服务端对 ``tools`` 字段的**结构开销**：与字符数无关的固定部分
#: （工具注册、function 包装、``tool_choice`` 等的模板化编码）。
#:
#: 来源（实测，不是拍脑袋）：一个 186 字符的 function schema，真实请求 token 增量
#: 为 **+258 tok**；按 :data:`DEFAULT_PROFILE` 的字符估算只有 ``186 / 3.5 ≈ 53`` tok。
#: 两者相差 ~205 → 服务端固定吃掉约 200 tok，单靠字符数算不出来。
#: 取整 200（宁可高估：低估会爆窗，高估只是多裁一点）。
#: 历史默认值（保留给 `DEFAULT_PROFILE.tool_overhead_fixed` 引用与旧测试）。
#: **真实取值已改为按 profile 标定**：实测跨模型 51~274，写死单值必然偏差。
TOOL_SCHEMA_FIXED_OVERHEAD_TOKENS = 200

#: 预留 tools 开销后，可用预算不得低于 ``total_tokens`` 的这个比例。
#: 理由：工具一多就把预算吃到 0 → history / memory 全被裁光 → 模型失忆，
#: 比少量超窗更糟（超窗有重试和降级，失忆没有）。
MIN_AVAILABLE_BUDGET_RATIO = 0.40

#: 与 ``adapters/openai_compat.py::TOOL_PARAMETERS`` **等价的副本**。
#:
#: 为什么不 import adapter：依赖方向是 ``adapter → builder``，反过来 import 会让
#: Runtime 依赖传输层（换一个协议就得改预算代码）。代价是这份常量会漂移，
#: **改 adapter 的 tools 形状时，这里必须同步**，
#: ``tests/test_prompt_budget.py`` 里有一条形状对齐用例专门盯它。
_CONSERVATIVE_TOOL_PARAMETERS: dict = {
    "type": "object",
    "properties": {"args": {"type": "object"}},
    "required": ["args"],
}


def _request_parameters(manifest: ToolManifest) -> dict:
    """adapter 实际发送的 ``parameters`` 对象。

    同步点（改 ``adapters/openai_compat.py::tool_schema`` 时跟着改这里）：

    * ``input_schema`` 是**非空** dict → 原样发送该 schema；空 dict 视为未声明；
    * 否则（``None`` / 非 dict）→ 退回保守容器 ``{"args": object}`` ——
      ToolManifest 没有参数结构信息，编造精确 schema 只会让模型按幻觉字段名调用。

    注意「空 dict」这一条：初期 adapter 用 ``isinstance(declared, dict)``，
    ``input_schema={}`` 会原样发出 ``"parameters": {}`` —— 而 ``parameters`` 是必填的
    完整 schema，严格服务会 400。现已修正为「空 dict 当没声明」，
    估算器必须跟着同步，否则算出来的字节和发出去的不一致。
    """
    schema = getattr(manifest, "input_schema", None)
    if isinstance(schema, dict) and schema:
        return dict(schema)
    return {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in _CONSERVATIVE_TOOL_PARAMETERS.items()}


def tool_schema_request_payload(manifest: ToolManifest) -> dict:
    """单个工具的 OpenAI function schema（**adapter 发出去的那个形状**）。

    形状照抄 ``adapters/openai_compat.py::OpenAICompatAdapter.tool_schema``：
    ``{"type": "function", "function": {name, description, parameters}}``。
    键顺序也照抄 —— adapter 用 ``json.dumps(..., ensure_ascii=False)`` 序列化，
    键序影响字节数，进而影响估算。

    Args:
        manifest: 工具清单条目。

    Returns:
        可直接放进请求体 ``tools`` 数组的 dict。
    """
    name = str(getattr(manifest, "name", "") or "unknown_tool")
    description = str(getattr(manifest, "description", "") or "")
    if not description:
        # 与 adapter 一致：没有 description 时合成一行，保证两条路径字面相同
        side = getattr(getattr(manifest, "side_effect", None), "value", "read_only")
        try:
            tier = int(getattr(manifest, "risk_tier", 0))
        except (TypeError, ValueError):
            tier = 0
        description = f"{name} (side_effect={side}, risk_tier=T{tier})"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _request_parameters(manifest),
        },
    }


def _schema_text(payload: dict) -> str:
    """与 adapter 的 ``json.dumps(body, ensure_ascii=False)`` 同一序列化口径。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def estimate_tool_schema_tokens(tools: Sequence[ToolManifest],
                                profile: TokenProfile = DEFAULT_PROFILE) -> int:
    """估算 adapter 实际发送的 tools JSON 的 token 开销。

    必须用**与 adapter 一致的形状**（先把 manifest 转成 OpenAI function schema
    再估算），不能用 Builder 自己渲染的那段人读文本 —— 两者差 2~3 倍，
    这正是本次要修的问题。

    公式::

        overhead = 0                                       若 tools 为空
        overhead = profile.tool_overhead_fixed
                 + Σ estimate_tokens(json(schema_i), profile)   否则

    空工具集返回 0（不是 200）：adapter 在 ``tools`` 为空时**根本不发**
    ``tools`` 键（``_request_body`` 里 ``if tools_schema:``），没有开销可算。

    Args:
        tools: 工具清单（顺序无关：逐项求和，加法可交换）。
        profile: 字符→token 档位，影响每条 schema 的字符折算。

    Returns:
        非负整数，单位 token。
    """
    items = list(tools or ())
    if not items:
        return 0
    body_tokens = sum(
        estimate_tokens(_schema_text(tool_schema_request_payload(m)), profile)
        for m in items
    )
    return int(profile.tool_overhead_fixed) + body_tokens


#: 各段的固定顺序（渲染顺序 + 组装顺序 + 预算表键，三处共用）
SECTION_ORDER: tuple[str, ...] = (
    "system", "tools", "skills", "tool_results", "history", "summary",
    "memory", "knowledge",
)


@dataclass(frozen=True)
class PromptTemplate:
    """Prompt 的措辞与标签。**这是 Agent 层的东西**，Runtime 只负责把它填进预算框架。

    所有字段默认值都中性（无中文、无人格设定）：Runtime 不替 Agent 说话。

    占位符约定（用字面替换实现，确定性、不会因模板笔误抛异常）：

    * ``principal_line`` 支持 ``{tenant}`` / ``{roles}``
    * ``safety_note`` 支持 ``{untrusted_open}`` / ``{untrusted_close}``

    ``untrusted_open`` / ``untrusted_close`` 的默认值必须与
    ``plane/governance.py`` 产出的包裹文本保持一致（跨模块契约，不要改）。
    """

    #: 由 Agent 提供；空字符串 = 不带 system 段（Runtime 不设定人格）
    system: str = ""
    #: 段标签：label 决定 ``<<<LABEL>>>`` 长什么样
    section_labels: Mapping[str, str] = field(default_factory=lambda: {
        "system": "SYSTEM", "tools": "TOOLS", "skills": "SKILLS",
        "tool_results": "TOOL_RESULTS", "history": "HISTORY", "summary": "SUMMARY",
        "memory": "MEMORY", "knowledge": "KNOWLEDGE",
    })
    #: Profile 注入到 system 段的前缀
    constraint_prefix: str = ""
    tools_header: str = ""
    memory_header: str = ""
    knowledge_header: str = ""
    tool_results_header: str = ""
    history_header: str = ""
    untrusted_open: str = "<external_data_untrusted>"
    untrusted_close: str = "</external_data_untrusted>"
    state_label: str = "STATE"
    #: 技能被预算截断时的提示语（Agent 层措辞；Runtime 默认留空）
    skills_truncated_note: str = ""

    # ── 为「删净中文措辞」而必须外置的槽位（默认全空 = 不渲染）──────────
    #: system 段里的主体行，支持 {tenant} / {roles}
    principal_line: str = ""
    #: system 段里的不可信数据告示，支持 {untrusted_open} / {untrusted_close}
    safety_note: str = ""
    #: 单条结果被截断时的标签，渲染成 `` [label]``
    truncated_label: str = ""
    #: 「这条是重复调用、返回的是缓存」的标记。**必须由模板给措辞**：
    #: builder 不许有业务语言（ADR-8），否则换个语言/业务就要改 Runtime 源码。
    deduped_label: str = ""
    #: 「同一调用出现了 N 次」的标记，支持 ``{count}`` 占位符。
    #: 重复调用会被 :meth:`PromptBuilder._merge_tool_results` 合并成一条（只留最新一次）；
    #: 合并后**必须能看出它重复过几次**，否则模型会以为"我只查过一次"，也就看不出
    #: 自己陷在同一个动作里。措辞同样归模板，空串 = 不标注（builder 保持中性）。
    repeat_label: str = ""
    #: 收敛指引（Agent 层措辞：什么时候该停止取证、直接下结论）。
    #: Runtime 只负责把它放进 system 段最前面（与人格、约束同级），不负责内容。
    convergence_note: str = ""
    #: 整段按预算截断时追加的标签，渲染成 ``…[label]``
    trim_label: str = ""

    def __post_init__(self) -> None:
        # 复制一份 section_labels：调用方之后改自己的 dict 不能静默改变 Prompt
        object.__setattr__(self, "section_labels", dict(self.section_labels))


#: 全中性默认模板：没有中文、没有人格设定。
#: Runtime 给任何 Agent 用都不该替它说话，所以这里必须是空的。
DEFAULT_TEMPLATE = PromptTemplate()


class PromptBuilder:
    """ContextSnapshot → Prompt。纯函数。"""

    #: 各来源的 token 上限占总预算的比例
    DEFAULT_RATIO = {
        "system": 0.10,
        "tools": 0.20,
        "tool_results": 0.30,
        "history": 0.20,
        "summary": 0.10,
        "skills": 0.12,
        "memory": 0.12,
        "knowledge": 0.08,
    }

    def __init__(self, template: PromptTemplate = DEFAULT_TEMPLATE,
                 total_tokens: int = 4000,
                 profile: TokenProfile = DEFAULT_PROFILE,
                 *, reserve_tool_schema: bool = True, tokenizer=None):
        """
        Args:
            template: 措辞与标签（Agent 层注入）。
            tokenizer: 可选 `count(text) -> int` 计量器。给了就用它（模型的真分词器），
                没给用 `profile` 启发式。**按模型选对很重要**：中文场景下
                tiktoken 比标定启发式高估近 2 倍（见 adapters/tokenizer.py）。
            total_tokens: 窗口总量。``reserve_tool_schema=True`` 时这是**含 tools
                JSON schema 在内的总量**，不是给 messages 的配额。
            profile: token 估算档位。
            reserve_tool_schema: 是否把 tools JSON schema 的开销从预算里预留掉。
                默认 ``True``（账实相符）；``False`` 是给老调用留的逃生门 ——
                行为与引入本参数之前逐字节一致。
        """
        self.template = template
        self.total_tokens = total_tokens
        self.profile = profile
        #: 可选：`count(text) -> int` 的计量器（见 adapters/tokenizer.py）。
        #: 给了就用它（模型的真分词器），没给就用 profile 启发式。
        #: **按模型选对很重要**：中文场景下 tiktoken 比标定启发式高估近 2 倍。
        self.tokenizer = tokenizer
        self.reserve_tool_schema = reserve_tool_schema

    # ── 预算核算（纯函数：不看 IO、不看时间、不看随机数）──────────
    def budget_breakdown(self, tools: Sequence[ToolManifest] = ()) -> dict:
        """算出这次 ``build()`` 会怎么花预算。纯函数。

        Returns:
            ``{"total": int, "tool_overhead": int, "available": int, "clamped": bool}``

            * ``available`` 是真正留给各段文本的 token 数；
            * 未 clamp 时恒有 ``available + tool_overhead == total``；
            * ``clamped=True`` 表示 tools 开销太大、被压到地板价
              ``total * MIN_AVAILABLE_BUDGET_RATIO``（此时
              ``available + tool_overhead > total`` —— 这是**故意的**：
              宁可少量超窗，也不让 history/memory 被裁光）；
            * ``reserve_tool_schema=False`` 时 ``tool_overhead`` 恒为 0、
              ``available == total``，如实反映「这次 build 根本没预留」。
        """
        total = int(self.total_tokens)
        overhead = (estimate_tool_schema_tokens(tools, self.profile)
                    if self.reserve_tool_schema else 0)
        floor = max(0, int(total * MIN_AVAILABLE_BUDGET_RATIO))
        available = total - overhead
        clamped = False
        if available < floor:
            available = floor
            clamped = True
        return {
            "total": total,
            "tool_overhead": overhead,
            "available": available,
            "clamped": clamped,
        }

    # ── 纯函数入口 ────────────────────────────────────────────
    def build(self, snap: ContextSnapshot, tools: Sequence[ToolManifest] = ()) -> str:
        # 预算先扣 tools schema 的固定+结构开销，再拿剩下的钱分配各段
        budget = self.budget_breakdown(tools)["available"]
        sections: dict[str, str] = {
            "system": self._render_system(snap),
            "tools": self._render_tools(tools),
            "skills": self._render_skills(snap),
            "tool_results": self._render_tool_results(snap),
            "history": self._render_history(snap),
            "summary": self._render_summary(snap),
            "memory": self._render_memory(snap),
            "knowledge": self._render_knowledge(snap),
        }
        fitted = self._fit(sections, budget)
        return self._assemble(fitted, snap)

    # ── 各段渲染（顺序与内容全部确定，措辞全部来自模板）──────────
    def _render_system(self, snap: ContextSnapshot) -> str:
        t = self.template
        principal = snap.get("principal", {}) or {}
        lines: list[str] = []
        if t.system:
            lines.append(t.system)
        # 收敛指引放在人格后面、约束前面：它是「什么时候停」的行为约束，
        # 与「能做什么」的人格分开，但都属于 system 区（不会被预算裁掉）。
        if t.convergence_note:
            lines.append(t.convergence_note)
        # Profile 记忆是刚性约束，注入到 system 区，优先级最高（结构性行为，与措辞无关）
        for hit in self._as_list(snap.get("memory_hits")):
            if isinstance(hit, dict) and hit.get("kind") == "profile":
                lines.append(f"{t.constraint_prefix}{hit.get('text', '')}")
        if t.principal_line:
            lines.append(self._fill(t.principal_line, **{
                "tenant": principal.get("tenant", "-"),
                "roles": ",".join(principal.get("roles", []) or []),
            }))
        if t.safety_note:
            lines.append(self._fill(t.safety_note, **{
                "untrusted_open": t.untrusted_open,
                "untrusted_close": t.untrusted_close,
            }))
        return "\n".join(lines)

    def _render_tools(self, tools: Sequence[ToolManifest]) -> str:
        if not tools:
            return ""
        out = [self.template.tools_header] if self.template.tools_header else []
        for m in sorted(tools, key=lambda x: x.name):
            out.append(
                f"- {m.name} [{m.side_effect.value}/{m.risk_tier.name}]: {m.description}"
            )
        return "\n".join(out)

    #: 尾部保留**完整**载荷的条数（最近的证据最可能要引用）
    TOOL_RESULTS_FULL_TAIL = 4
    #: 更早的载荷压成多少字符（只留"我查过什么、结论是什么"的轮廓）
    TOOL_RESULTS_OLD_LIMIT = 140
    #: 合并**之后**的总条数上限（防止极端回合把这一段撑爆）。
    #:
    #: 40 → 80 的依据：真模型一个多轮排查回合能发 60+ 次调用，40 条上限会让
    #: **最早那批关键证据淡出视野**（它看不到"我早期已经排掉这条线"）。上限不是
    #: token 预算，不需要为"省"而压低 —— 真正的开销由各段的 token 上限管。
    #: 之所以敢翻倍：重复调用会被 :meth:`_merge_tool_results` 合并，条目增长比调用
    #: 次数慢得多（复刻的劣化轨迹：96 次调用只对应 38 条唯一证据，约 2.5x），
    #: 所以 80 条覆盖的真实历史长度是原来的数倍，而不是 2 倍。
    TOOL_RESULTS_MAX_ITEMS = 80

    def _render_tool_results(self, snap: ContextSnapshot) -> str:
        """渲染工具结果：**去重合并 + 尾部完整 + 早期压缩**，而不是只留最后 N 条。

        为什么不能只留最后 6 条（这是接真模型跑出来的教训）：
          真模型一轮能发 20+ 次工具调用。只留最后 6 条等于把**前面采到的证据
          从模型视野里删掉** —— 它看不到"我已经查过这一项了"，于是**重复调用
          同一个工具**。实测同一批查询被重复发了 4 次以上，最后耗尽步数也没有
          产出结论。

        Runtime 的职责是"让模型看得见它自己做过什么"，不是"省 token 省到失忆"。
        这里的取舍：

          * **先去重**（:meth:`_merge_tool_results`）：同参数重复调用只占一个名额，
            重复次数写在标记里 —— 名额是稀缺资源，不能给"内容完全一样"的记录；
          * 最近几条给全量（要引用具体数字），更早的压成轮廓（"查过、结论是劣化"）；
          * 合并后的总条数封顶 :attr:`TOOL_RESULTS_MAX_ITEMS`。
        """
        t = self.template
        results = [r for r in self._as_list(snap.get("tool_results"))
                   if isinstance(r, Mapping)]
        if not results:
            return ""
        # 先合并再封顶：顺序反过来会把"同一调用的 8 次重复"当成 8 条挤掉别人
        results = self._merge_tool_results(results)
        # 只要最近的 MAX_ITEMS 条（更早的早已被预算裁掉，留着也没意义）
        results = results[-self.TOOL_RESULTS_MAX_ITEMS:]
        tail_start = max(0, len(results) - self.TOOL_RESULTS_FULL_TAIL)

        out = [t.tool_results_header] if t.tool_results_header else []
        for idx, r in enumerate(results):
            tool = r.get("tool", "?")
            payload = r.get("payload")
            flags = []
            # 重复次数放在最前：它讲的是"这条记录本身代表了几次调用"，
            # 是模型判断"我是不是在原地打转"的唯一线索。
            repeat = int(r.get("repeat_count", 1) or 1)
            if repeat > 1 and t.repeat_label:
                flags.append(self._fill(t.repeat_label, count=repeat))
            if r.get("truncated") and t.truncated_label:
                flags.append(t.truncated_label)
            # **去重标记必须可见**：否则模型分不清"新数据"和"我刚查过的同一份"，
            # 会拿缓存结果当新证据继续推理（也是重复调用的诱因之一）。
            if r.get("deduped") and t.deduped_label:
                flags.append(t.deduped_label)
            flag = f" [{'·'.join(flags)}]" if flags else ""
            trusted = r.get("trusted", True)
            tag = "" if trusted else f" {t.untrusted_open}"
            limit = 600 if idx >= tail_start else self.TOOL_RESULTS_OLD_LIMIT
            body = self._compact(payload, limit=limit)
            out.append(f"- {tool}{flag}: {body}{tag}")
            if not trusted:
                out.append(t.untrusted_close)
        return "\n".join(out)

    @classmethod
    def _merge_tool_results(cls, results: Sequence[Mapping]) -> list[dict]:
        """把「同一个调用」的重复记录合并成一条，只保留**最新**那条。

        为什么必须合并（真模型实测）：
          deepseek-flash 在复杂排查里会拿**一模一样的参数**反复调同一个工具
          （实测 18 次 ``query_metrics``，其中多次参数与结果都完全一致）。
          每条各占一个名额时，8 次重复就挤掉 8 条**不同**的证据 —— 而它们的内容
          一个字都不差。合并后名额花在真正的新信息上。

        判据（指纹见 :meth:`_result_fingerprint`）：
          ``tool + kind + payload``（有 ``args`` 时并入 ``args``）。
          Loop 写进 Context 的记录里没有 ``args``（见 ``contracts.ToolResult.to_dict``），
          所以**相同 payload ≈ 重复调用**：同一工具、同一参数、同一份数据。

        合并语义：

          * 位置 = 该指纹**最后一次**出现的位置（顺序仍按时间新→旧排，
            "尾部给全量"的规则因此作用在最新证据上）；
          * 内容 = 最后一次的记录（payload 用最新版本：早期结果可能已被覆盖）；
          * 计数 = 总出现次数，写进 ``repeat_count``，由模板决定怎么标注。
        """
        keys = [cls._result_fingerprint(r) for r in results]
        last_index: dict[tuple, int] = {}
        counts: dict[tuple, int] = {}
        for i, key in enumerate(keys):
            last_index[key] = i
            counts[key] = counts.get(key, 0) + 1

        merged: list[dict] = []
        for i, record in enumerate(results):
            key = keys[i]
            if last_index[key] != i:
                continue          # 不是最后一次出现 → 已经被并到后面那条里
            entry = dict(record)
            entry["repeat_count"] = counts[key]
            merged.append(entry)
        return merged

    @staticmethod
    def _result_fingerprint(record: Mapping) -> tuple:
        """一条工具结果记录的判重指纹。

        用 ``json.dumps(sort_keys=True)`` 而不是 ``dict.__hash__``/``repr``：
        前者的输出与键的插入顺序无关（同一份数据的两种写法必须判等），
        后者在异常输入下会抛 ``TypeError``（不可哈希值）或混入内存地址
        —— 判重必须确定性，否则同一份 Context 两次 build 结果不同（INV-2）。

        ``payload is None``（失败/被拒的调用没有结果体）时改用 ``kind + reason``
        作指纹：两条内容不同的失败不能被并成一条，否则「失败了 3 次、三种原因」
        会被写成一次失败，排查方向直接跑偏。
        """
        tool = str(record.get("tool", "?"))
        payload = record.get("payload")
        if payload is None:
            return (tool, "!", str(record.get("kind", "")),
                    str(record.get("reason", "")))
        try:
            body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):                      # pragma: no cover - 极端输入
            body = repr(payload)
        args = record.get("args")
        args_key = ""
        if isinstance(args, Mapping):
            try:
                args_key = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
            except (TypeError, ValueError):                  # pragma: no cover
                args_key = repr(args)
        return (tool, str(record.get("kind", "")), args_key, body)

    def _render_history(self, snap: ContextSnapshot) -> str:
        history = self._as_list(snap.get("history"))
        if not history:
            return ""
        out = [self.template.history_header] if self.template.history_header else []
        for h in history[-8:]:
            role = h.get("role", "?")
            text = h.get("text", "")
            out.append(f"- {role}: {self._clip(text, 300)}")
        return "\n".join(out)

    def _render_summary(self, snap: ContextSnapshot) -> str:
        """滚动摘要（Summary Memory）—— 已经压掉的早期历史。

        没有它，history 一超预算，早期决议就真丢了：模型后面会自相矛盾。
        有了它，被裁掉的那部分是"摘要 + 原始记录仍在 Context"，不是"消失"。
        注意：它是**用少量预算换信息保真**，不是省 token
        （Builder 本来只渲染最后几条 history；摘要会让 Prompt 变长一点）。
        """
        text = snap.get("summary")
        return text.strip() if isinstance(text, str) else ""

    def _render_skills(self, snap: ContextSnapshot) -> str:
        """双层按需装载的落点：**索引常驻 + 规程只在命中后装载**。

        `skill_state` 由 SkillRuntime 产出、经 LOOP 代写进 Context（owner=SKILL）。
        形状：``{"index": str, "loaded": [{"name","body"}], "truncated": bool}``。

        Runtime 只负责把这段塞进预算框架；**技能内容本身属于 Agent**
        （见 plane/skill.py 与架构文档 §3 的 Procedural Memory 归位裁决）。
        """
        state = snap.get("skill_state")
        if not isinstance(state, dict):
            return ""
        parts: list[str] = []
        index = state.get("index")
        if isinstance(index, str) and index.strip():
            parts.append(index.strip())
        for item in state.get("loaded") or ():
            if not isinstance(item, dict):
                continue
            name = item.get("name", "?")
            body = item.get("body", "")
            if body:
                parts.append(f"--- skill:{name} ---\n{body}")
        note = self.template.skills_truncated_note
        if state.get("truncated") and note:
            parts.append(note)
        return "\n\n".join(parts)

    def _render_memory(self, snap: ContextSnapshot) -> str:
        hits = self._as_list(snap.get("memory_hits"))
        if not hits:
            return ""
        out = [self.template.memory_header] if self.template.memory_header else []
        for h in hits:
            if not isinstance(h, dict):
                continue
            kind = h.get("kind", "episodic")
            score = h.get("score", 0)
            out.append(f"- [{kind} {score:.2f}] {self._clip(h.get('text', ''), 240)}")
        return "\n".join(out)

    def _render_knowledge(self, snap: ContextSnapshot) -> str:
        hits = self._as_list(snap.get("knowledge_hits"))
        if not hits:
            return ""
        out = [self.template.knowledge_header] if self.template.knowledge_header else []
        for h in hits:
            if not isinstance(h, dict):
                continue
            ref = f"{h.get('doc_id', '?')}#{h.get('span', '-')}"
            out.append(f"- ({ref}) {self._clip(h.get('text', ''), 240)}")
        return "\n".join(out)

    # ── 预算裁剪 ─────────────────────────────────────────────
    def _fit(self, sections: dict[str, str], budget: int | None = None) -> dict[str, str]:
        """按 ``budget`` 裁剪各段。``budget=None`` 时退回 ``self.total_tokens``。

        ``budget`` 由 :meth:`build` 传入（已扣掉 tools schema 开销）；
        保留默认值是为了老调用 ``_fit(sections)`` 不至于炸。
        """
        if budget is None:
            budget = self.total_tokens
        caps = {k: int(budget * r) for k, r in self.DEFAULT_RATIO.items()}

        # 第一轮：按单项上限截断（system 永不截断）
        out: dict[str, str] = {}
        for name, text in sections.items():
            if name == "system":
                out[name] = text
                continue
            cap = caps.get(name, 0)
            if cap <= 0:
                out[name] = ""
                continue
            # 超出上限太多时整段丢弃：留下半行残句只是噪音，反而挤占其他段
            out[name] = "" if self._count(text) > cap * 3 else self._clip_to_tokens(text, cap)

        # 第二轮：总量仍超 → 从最低优先级开始整个丢弃
        order = ["knowledge", "memory", "history", "tool_results", "tools"]
        for name in order:
            if self._total(out) <= budget:
                break
            out[name] = ""
        return out

    def _total(self, sections: dict[str, str]) -> int:
        return sum(self._count(v) for v in sections.values())

    def _assemble(self, sections: dict[str, str], snap: ContextSnapshot) -> str:
        # 段顺序固定，空段跳过 —— 保证确定性
        labels = self.template.section_labels
        parts = []
        for name in SECTION_ORDER:
            body = sections.get(name, "")
            if body:
                parts.append(f"<<<{labels.get(name, name.upper())}>>>\n{body}")
        parts.append(
            f"<<<{self.template.state_label}>>>\n"
            f"phase={snap.get('phase', '-')} step={snap.get('loop_count', 0)}"
        )
        return "\n\n".join(parts)

    # ── 工具函数（全部确定性）────────────────────────────────
    def _count(self, text: str) -> int:
        """所有预算计算都走这里，保证「一套口径算到底」。"""
        if self.tokenizer is not None:
            try:
                return int(self.tokenizer.count(text))
            except Exception:                                # noqa: BLE001
                pass                                         # 计量器坏了 → 退回启发式
        return estimate_tokens(text, self.profile)

    @staticmethod
    def _as_list(v) -> list:
        return v if isinstance(v, list) else []

    @staticmethod
    def _clip(text: str, n: int) -> str:
        text = "" if text is None else str(text)
        return text if len(text) <= n else text[:n] + "…"

    def _clip_to_tokens(self, text: str, cap: int) -> str:
        if cap <= 0 or not text:
            return ""
        mark = f"\n…[{self.template.trim_label}]" if self.template.trim_label else "\n…"
        while text and self._count(text) > cap:
            keep = max(0, int(len(text) * cap / max(1, self._count(text))) - 8)
            if keep <= 0:
                return ""
            text = text[:keep] + mark
            if len(text) <= keep + 16:
                break
        return text

    @staticmethod
    def _fill(text: str, **values: object) -> str:
        """字面占位符替换（比 str.format 稳：模板里的其它花括号不会炸）。"""
        for k, v in values.items():
            text = text.replace("{" + k + "}", str(v))
        return text

    @staticmethod
    def _compact(payload, limit: int = 600) -> str:
        if isinstance(payload, str):
            s = payload
        elif isinstance(payload, dict):
            items = sorted(payload.items(), key=lambda kv: str(kv[0]))
            s = ", ".join(f"{k}={v}" for k, v in items)
        elif isinstance(payload, list):
            s = "; ".join(PromptBuilder._compact(x, 120) for x in payload[:8])
        else:
            s = str(payload)
        return s if len(s) <= limit else s[:limit] + "…"
