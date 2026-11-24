"""plane/router.py — W20：Router（Agent 选择 + confidence）。

════════════════════════════════════════════════════════════════════════
架构位置（agent-runtime-core-arch.md §0 核心等式 / §1 主线图）
════════════════════════════════════════════════════════════════════════

    Gateway → **Router** → Planner → Loop ⇄ Context → LLM

    「Gateway 把关，**Router 选人**，Planner 会返工，Loop 有边界。」

Router 是主线上的第一个「判断」：请求进来时，Runtime 必须先回答
**「这件事该由谁办」**，Planner 才谈得上排计划。它读三样东西：

    请求（goal） + Agent 描述（AgentSpec）+ 权限（Principal.scopes）
    + 业务标签（AgentSpec.tags / priority）

════════════════════════════════════════════════════════════════════════
为什么必须有这个模块：没有 Router 的"单 Agent 假象"
════════════════════════════════════════════════════════════════════════

demo 里曾经是 `loop.run(goal)` —— 代码里根本没有"选择"这一步，于是
**"只有一个 Agent" 变成了一个没人验证过的隐含前提**。它的失效方式不是报错，
而是更糟的那种：

    注册了 coder / researcher / sql_analyst 三个 Agent
    → 用户问 "把这份报表的 SQL 优化一下"
    → 谁都不会被选中，因为根本没有选择逻辑
    → 实现者"顺手"在调用点写 `agent = agents[0]`
    → 全流量打进 coder，researcher 的 prompt / 工具 / 权限策略形同虚设

**硬选一个错的 Agent，比明确说"我不确定，请澄清"危险得多**：错选会继承错的
工具集、错的系统提示、错的权限范围，错误在 Loop 里被放大好几轮才暴露。

所以本模块的核心语义只有一条：

    分数不够高 → 回澄清（__clarify__），不许硬选。
    两个人分数差不多 → 也回澄清，不许掷硬币。

`RouteDecision.agent == "__clarify__"` 就是"我不选"的显式表达 —— 它是一个
**和 Agent 名字同形的信号**，上游无论是谁拿到它，都只能走澄清分支，
不可能"忘了处理"然后拿 `"__clarify__"` 当 Agent 名去查表（查不到，立刻炸）。

════════════════════════════════════════════════════════════════════════
打分公式（确定性、可手算复现 —— 这是刻意设计的）
════════════════════════════════════════════════════════════════════════

    score = 0.55 * text_match(goal, spec)
          + 0.20 * tag_match(goal, spec.tags)
          + 0.15 * scope_ok(principal, spec)      # 1 或 0；0 时该 spec 直接剔除
          + 0.10 * priority_norm(spec.priority)

四个分项都在 0..1，权重和为 1.00，所以 score ∈ [0, 1]，和 confidence 同一量纲
（这就是为什么阈值能直接拿 0.45 这种人类可读的数字当参数）。

**为什么权重是这个比例**（不是拍脑袋，是从失效模式倒推的）：

    0.55 text  文本相似度是唯一直接看"这件事是什么"的信号，必须过半，否则
               一个描述恰好撞词的 Agent 就能靠 tag/priority 抢走路由。
    0.20 tag   业务标签是人工维护的强信号，但它是"粗分类"（一个 Agent 常挂
               5~8 个 tag），精度不如文本，所以给次高权重。
    0.15 scope 权限是**闸门**不是分数：不满足就整个 spec 出局（见下）。这里的
               0.15 只表达"满足权限是加分项"的表层观感，真实作用是让
               "无权但描述高度相关"的 Agent 无法靠文本分爬到阈值以上。
    0.10 prio  人工排序是最后的微调，权重最小；它只在**同分竞争**时说话。

**权限的语义是剔除，不是扣分**（INV 级要求）：

    spec.required_scopes ⊄ principal.scopes → 该 spec 从候选集里**消失**

如果降级成"扣 0.15"，那一个 text_match=1.0、tag_match=1.0 但需要
`sql.write` 的 Agent 仍能拿到 0.85，照样被选中 —— 权限校验就成了一句空话。
**能力不足 ≠ 意愿不足**，不能靠分数打折来表达。

**priority_norm 是相对序（min-max 归一化到 0..1），依赖"当前可选集"**：

    可选集里 priority 最高 → 1.0，最低 → 0.0，落在中间按线性插值。
    可选集所有 priority 相同（含只有一个 Agent）→ 全部记 1.0（不参与区分）。

选相对序而不是"固定刻度 priority/100"的原因：priority 在企业里是"运营手工
调的相对排序"（谁的活更该先接），量纲没有绝对意义。固定刻度会让 0.10 的权重
实际上退化成常数偏移 —— 两个 Agent 写 10 和 20，固定刻度下都趋近 1.0，
排序自然消失。min-max 保证"只要 priority 不同，就必然拉开 0.10 的分差"，
恰好越过下面的 0.08 接近线，于是 **priority 真的能决定同描述 Agent 的胜负**。

代价（诚实写明）：**注册表里增删一个 Agent 会改变别人的 priority_norm**，
因此分数是"相对于当前可选集"的。这是有意的 —— 路由本来就是竞争性判断。
为了不让权限过滤污染这种竞争，min-max 只在**通过权限闸门之后**的集合上算。

════════════════════════════════════════════════════════════════════════
手算示例（照抄即可复现，`router.explain()` 会给出同一组数）
════════════════════════════════════════════════════════════════════════

    goal : "重构 python 代码"
    spec : AgentSpec(name="coder", description="写代码 重构 调试 python",
                     tags=("code", "python"), required_scopes=("tool.write",))
    principal.scopes = {"tool.read", "tool.write"}      # 权限满足 → scope_ok=1.0
    可选集 priority = [0]                                # 只有一个 → pn=1.0

    ① 归一化 goal        "重构python代码"（9 个 bigram）
       重构 构p py yt th ho on n代 代码
    ② 归一化 spec 文本    "coder写代码重构调试pythoncodepython"（19 个 bigram）
    ③ 交集 7 个：重构 代码 py yt th ho on ；并集 21 → text_match = 7/21 = 0.3333
    ④ tag_match：tags=("code","python")，goal 里只出现 "python" → 1/2 = 0.5
    ⑤ 代入：
         0.55 × 0.3333 = 0.18333
         0.20 × 0.5    = 0.10000
         0.15 × 1.0    = 0.15000     ← 权限满足
         0.10 × 1.0    = 0.10000     ← priority 唯一 → pn=1.0
         ───────────────────────
         score         = 0.53333  ≥ 0.45 → 选中 coder

    换成 "今天天气怎么样"：text_match=0、tag_match=0 → 0.15+0.10 = 0.25 < 0.45
    → __clarify__，candidates 里带着 0.25 这个数，用户一眼能看出"还差 0.20"。

这个例子里没有任何"随机 / 学习 / 隐式权重"：同样的注册表加同样的 goal，
任何人拿纸笔都能算出同一个 0.5333。**Router 的可解释性不是附加卖点，
它是本模块唯一的验收口径** —— 选错了必须能指着某一行说"这 0.20 给错了"。

════════════════════════════════════════════════════════════════════════
置信度与澄清（核心语义）
════════════════════════════════════════════════════════════════════════

    ① goal 归一化后为空（含纯空白）              → __clarify__，confidence=0.0
    ② 无任何可选 spec（空注册表 / 全被权限剔除） → __clarify__，confidence=0.0
    ③ top < confidence_threshold                 → __clarify__，reason 写明"低于阈值"
    ④ top ≥ 阈值 且 top - second < 0.08          → __clarify__，reason 写明"分数接近"
    ⑤ 其余                                        → 选中 top

规则的顺序是刻意的：**先看够不够格（③），再看赢没赢透（④）**。
只有 ④ 是"防模棱两可时硬选"的关键，它必须排在 ③ 之后 —— 两个都很差的
Agent 不该被报成"分数接近"，那会误导用户以为"再澄清就能选出来"，而真实
情况是**该补 Agent / 补描述**。两种澄清在 reason 里用词不同，就是为了让人
一眼分清："低于阈值" = 没人能干这事；"分数接近" = 好几个人都能干。

`0.08` 这个数字的来源：它约等于 priority 全权重（0.10）的 80%，也约等于
tag 权重（0.20）的 40% —— 换句话说，两个 Agent 的差距必须**大于一次人工
排序调整的绝大部分**才算"赢透"。窄于这个缝，人类的一句话补充（"我说的是
生产库不是测试库"）就能翻盘，此时选哪个都是在赌。

其余行为细节：

  * 只有一个候选且 ≥ 阈值 → 直接选（唯一候选不存在"接近"问题）；
  * `candidates` 永远给前 3 名（按分数降序），让人**看见差多少**，
    而不是只拿到一句"我不确定"；
  * 同分时的确定性排序：score 降序 → priority 降序 → name 升序。
    最后一个键是 name，保证**完全同分也不会因注册顺序不同而结果漂移**。

════════════════════════════════════════════════════════════════════════
与 plane/memory.py::similarity 的口径关系
════════════════════════════════════════════════════════════════════════

`text_match` 的字符 bigram Jaccard **与 memory 同口径**，但本模块**刻意不
import memory**，自己实现（十几行）。理由不是洁癖：

    1. BRIEF 硬约束 7：模块要么只依赖 contracts，要么自带 fake。plane/memory
       是另一个 worker 的文件，它在并行施工中随时可能被改；把 Router 的分数
       绑到别人文件的私有函数上，等于让路由结果随别人的重构漂移。
    2. 路由是主线第一跳，必须能被单独 import（`from plane.router import
       AgentRouter`）而不拖起 Memory Plane 的整套运行时。
    3. 口径一致性靠**测试钉住**，不靠 import 复用：tests 里有一条用例直接用
       与 memory 相同的归一化 + Jaccard 公式重算一遍，确保两边口径不漂移。

口径定义（与 memory.normalize_text / _bigrams / similarity 逐字一致）：

    归一化：小写 + 去掉所有 `\\s` / `\\W`（含下划线）→ 只留 CJK 与字母数字
    切分  ：连续两字符为一个 bigram；长度为 1 时退化为单个字符
    相似度：|A ∩ B| / |A ∪ B|，任一为空 → 0.0

中文没有空格分词，字符二元组是最便宜的、不依赖词典的近似语义单元 ——
这正好是 Router 需要的：它只要"谁更像"，不要"谁最像"。

可替换点：`embedder`。默认就是上面的 bigram；企业已经有向量模型时，
可以注入 `Callable[[str], set[str]]`（返回 token 集合），Jaccard 口径不变、
token 粒度由调用方决定（例如按词切、或把同义词归并）。**注意 embedder
自己负责归一化**，本模块不再对它做二次处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

from contracts import AgentSpec, Owner, Patch, Principal, RouteDecision

__all__ = [
    "CLARIFY_AGENT",
    "W_TEXT",
    "W_TAG",
    "W_SCOPE",
    "W_PRIORITY",
    "GAP_MIN",
    "ScoreBreakdown",
    "AgentRouter",
    "KeywordRouter",
    "normalize_text",
    "bigrams",
    "jaccard",
    "text_similarity",
    "priority_norm",
    "route_patches",
]

# ══════════════════════════════════════════════════════════════
# 常量：把"魔法数字"全部提到台面上，方便手算与调参
# ══════════════════════════════════════════════════════════════

#: "我不选"的显式信号。与 Agent 名同形，但注册表里永远查不到它 ——
#: 上游把 `__clarify__` 当 Agent 名用的那一刻就会失败，不会被静默吞掉。
CLARIFY_AGENT = "__clarify__"

W_TEXT = 0.55        # 文本相似度
W_TAG = 0.20         # 业务标签命中
W_SCOPE = 0.15       # 权限满足（不满足则剔除，不是 0 分参与排序）
W_PRIORITY = 0.10    # 人工排序（相对序）

#: 第一名与第二名的分差小于它 → 视为"模棱两可"，回澄清。
#: 见模块 docstring：约等于一次人工排序调整的 80%，窄于此缝人类一句话就能翻盘。
GAP_MIN = 0.08

#: candidates 里最多列几个（让人看见差多少，但不刷屏）
MAX_CANDIDATES = 3

#: 分数保留位数。只用于对外展示与分差比较 —— 消除浮点尾差，
#: 保证 `0.1 * (1.0 - 0.0) >= 0.08` 这种边界判断不会因 1e-17 翻转。
_ROUND = 6


# ══════════════════════════════════════════════════════════════
# 文本口径（与 plane/memory.py 同口径，故意不 import）
# ══════════════════════════════════════════════════════════════
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """归一化：小写 + 去空白/标点，保留 CJK 与字母数字。

    与 `plane/memory.py::normalize_text` 逐字同口径（正则也相同）。
    大小写、全角空格、多余标点因此都不影响路由结果。
    """
    return _PUNCT_RE.sub("", (text or "").lower())


def bigrams(text: str) -> set[str]:
    """字符 bigram 集合（默认 embedder）。

    中文无空格，字符二元组是最便宜的近似语义单元；英文也照样能用
    （"python" → {py, yt, th, ho, on}）。长度为 1 的串退化为它自己，
    否则单字 goal（"写"）会得到空集 → 恒 0 分。
    """
    s = normalize_text(text)
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard：|A∩B| / |A∪B|。任一为空 → 0.0（空集不相似于任何东西）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


def text_similarity(a: str, b: str) -> float:
    """两段文本的 bigram Jaccard（便捷函数，测试与手算用）。"""
    return jaccard(bigrams(a), bigrams(b))


def priority_norm(priorities: Sequence[int]) -> list[float]:
    """把一组 priority 线性归一化到 0..1（min-max）。

    全部相同（含只有 1 个元素）→ 全部 1.0：没有区分度时，priority 这个维度
    不提供信息，但仍给满分，避免"单人场景反而被扣 0.10"的荒谬结果。
    返回列表与入参**同序**，便于调用方 zip 回去。
    """
    vals = list(priorities)
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [1.0] * len(vals)
    span = float(hi - lo)
    return [(v - lo) / span for v in vals]


# ══════════════════════════════════════════════════════════════
# 分项明细：让"为什么选了它"可审计，而不是只给一个总分
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ScoreBreakdown:
    """一个 spec 在当前 goal / principal 下的四项分数 + 总分。

    `eligible=False` 表示它被权限闸门剔除。此时 `scope_ok=0.0`、`priority_norm=0.0`
    （priority 的 min-max 只在可选集上算），所以**它的 score 不可与 eligible 的
    score 直接比大小** —— 它少了 0.15+0.10 的底分。要看"它差在哪"，看 `text_match`
    与 `tag_match`；要看"它若有权会怎样"，把这两项底分加回去再比。
    这个分数只作诊断，永远不参与排序，也永远不会被选中。
    """
    agent: str
    text_match: float
    tag_match: float
    scope_ok: float
    priority_norm: float
    score: float
    eligible: bool = True
    tag_hits: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "text_match": round(self.text_match, _ROUND),
            "tag_match": round(self.tag_match, _ROUND),
            "scope_ok": self.scope_ok,
            "priority_norm": round(self.priority_norm, _ROUND),
            "score": round(self.score, _ROUND),
            "eligible": self.eligible,
            "tag_hits": list(self.tag_hits),
        }


# ══════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════
def _scopes_ok(principal: Principal, spec: AgentSpec) -> bool:
    """权限闸门：required_scopes 必须是 principal.scopes 的子集。"""
    if not spec.required_scopes:
        return True
    scopes = principal.scopes or frozenset()
    return all(s in scopes for s in spec.required_scopes)


def _spec_text(spec: AgentSpec) -> str:
    """参与 text_match 的文本：name + description + tags。

    name 也算进去是因为企业里 Agent 名往往是业务名（"billing_agent"），
    本身就是最强的描述性信号；用空格拼接后归一化会去掉分隔符，
    跨字段的 bigram 也会自然生成 —— 这与 memory 对整段文本的处理一致。
    """
    return " ".join([spec.name, spec.description, " ".join(spec.tags)])


def _tag_ratio(goal_norm: str, tags: Sequence[str]) -> tuple[float, tuple[str, ...]]:
    """tag 命中比例：tag 是 goal 的子串即算命中（归一化后比较）。

    归一化后比较，而不是原样 `tag in goal`：
      * 否则 `Python` 这个 tag 匹配不到 `python`、`DEBUG` 匹配不到 `debug`；
      * 否则 tag 里的空格/下划线会让它永远匹配不上。
    分母是**去重后的 tag 数**，重复 tag（复制粘贴留下的）不该稀释比例。
    """
    uniq: list[str] = []
    for t in tags:
        n = normalize_text(t)
        if n and n not in uniq:
            uniq.append(n)
    if not uniq:
        return 0.0, ()
    hits = tuple(t for t in uniq if t in goal_norm)
    return len(hits) / float(len(uniq)), hits


# ══════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════
class AgentRouter:
    """按请求 / 描述 / 权限 / 业务标签选 Agent。**不确定就澄清，不硬选。**

    典型用法：

        router = AgentRouter(confidence_threshold=0.45)
        for spec in (coder, researcher, analyst):
            router.register(spec)
        d = router.route("把这份报表的 SQL 优化一下", principal)
        if d.agent == CLARIFY_AGENT:
            ...  # 走澄清：把 d.candidates 摊给用户 / 让 Planner 追问
        else:
            loop.run(goal, agent=d.agent)

    线程安全：不是线程安全的。注册表按注册顺序保存（`specs()` 保序），
    排序键里带 name 兜底，所以**只要注册顺序稳定，结果就完全确定**。
    """

    def __init__(self, *, confidence_threshold: float = 0.45,
                 embedder: Callable[[str], set[str]] | None = None) -> None:
        if not isinstance(confidence_threshold, (int, float)) or isinstance(confidence_threshold, bool):
            raise TypeError("confidence_threshold 必须是数字")
        if not 0.0 <= float(confidence_threshold) <= 1.0:
            raise ValueError(f"confidence_threshold 必须落在 0..1，收到 {confidence_threshold!r}")
        if embedder is not None and not callable(embedder):
            raise TypeError("embedder 必须是 Callable[[str], set[str]]")
        self.confidence_threshold = float(confidence_threshold)
        self._embed: Callable[[str], set[str]] = embedder or bigrams
        self._specs: dict[str, AgentSpec] = {}
        #: spec 名 → 它的描述向量。AgentSpec 是 frozen 的，描述文本不会变，
        #: 所以按名缓存是安全的；注入向量模型后这一步省掉的是每轮 N 次编码。
        self._vec_cache: dict[str, set[str]] = {}

    def _spec_vec(self, spec: AgentSpec) -> set[str]:
        """spec 描述文本的向量（懒缓存）。注销时清掉，避免同名重注册拿到旧向量。"""
        vec = self._vec_cache.get(spec.name)
        if vec is None:
            vec = self._embed(_spec_text(spec))
            self._vec_cache[spec.name] = vec
        return vec

    # ----------------------------------------------------------
    # 注册表
    # ----------------------------------------------------------
    def register(self, spec: AgentSpec) -> None:
        """注册一个 Agent。**同名不静默覆盖** —— 抛 ValueError。

        静默覆盖的代价：两个 worker / 两个插件各自注册了一个叫 `coder` 的
        Agent，后注册的把前一个的描述、权限、工具集全盖掉，而调用方毫不知情。
        路由结果会变成"另一个 Agent"，排查时却看不见任何痕迹。
        """
        if not isinstance(spec, AgentSpec):
            raise TypeError(f"register 需要 AgentSpec，收到 {type(spec).__name__}")
        if not normalize_text(spec.name):
            raise ValueError("AgentSpec.name 不能为空")
        if spec.name in self._specs:
            raise ValueError(f"Agent {spec.name!r} 已注册（拒绝静默覆盖）")
        self._specs[spec.name] = spec
        self._vec_cache.pop(spec.name, None)      # 同名重注册 → 旧向量作废

    def unregister(self, name: str) -> bool:
        """按名注销。存在→True，不存在→False（不抛错：注销是幂等意图）。"""
        self._vec_cache.pop(name, None)
        return self._specs.pop(name, None) is not None

    def specs(self) -> list[AgentSpec]:
        """当前注册的 spec，**按注册顺序**（不排序，排序由 route 内部做）。"""
        return list(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    # ----------------------------------------------------------
    # 打分
    # ----------------------------------------------------------
    def _terms(self, goal_norm: str, goal_vec: set[str], principal: Principal,
               eligible_names: set[str] | None = None) -> list[ScoreBreakdown]:
        """算出所有 spec 的四项分数。

        `eligible_names=None` 时在"全部通过权限闸门的 spec"上做 min-max；
        传入集合则限定在该集合上归一化（explain 用，保证与 route 同口径）。
        """
        specs = self.specs()
        eligible = [s for s in specs if _scopes_ok(principal, s)]
        norm_pool = ([s for s in eligible if s.name in eligible_names]
                     if eligible_names is not None else eligible)
        pn_by_name = dict(zip([s.name for s in norm_pool],
                              priority_norm([s.priority for s in norm_pool])))

        out: list[ScoreBreakdown] = []
        for spec in specs:
            ok = _scopes_ok(principal, spec)
            tag_ratio, hits = _tag_ratio(goal_norm, spec.tags)
            tm = jaccard(goal_vec, self._spec_vec(spec))
            pn = pn_by_name.get(spec.name, 0.0) if ok else 0.0
            scope = 1.0 if ok else 0.0
            score = W_TEXT * tm + W_TAG * tag_ratio + W_SCOPE * scope + W_PRIORITY * pn
            out.append(ScoreBreakdown(
                agent=spec.name,
                text_match=tm,
                tag_match=tag_ratio,
                scope_ok=scope,
                priority_norm=pn,
                score=round(score, _ROUND),
                eligible=ok,
                tag_hits=hits,
            ))
        return out

    def explain(self, goal: str, principal: Principal) -> list[ScoreBreakdown]:
        """审计视图：所有 spec 的分项明细，按 (分数降序, priority 降序, name 升序)。

        这是"可手算复现"的兑现方式 —— `sum(0.55*tm + ...)` 必须等于 `score`。
        """
        goal_norm = normalize_text(goal)
        goal_vec = self._embed(goal) if goal_norm else set()
        rows = self._terms(goal_norm, goal_vec, principal)
        prio = {s.name: s.priority for s in self.specs()}
        rows.sort(key=lambda r: (-r.score, -prio.get(r.agent, 0), r.agent))
        return rows

    # ----------------------------------------------------------
    # 路由
    # ----------------------------------------------------------
    def route(self, goal: str, principal: Principal) -> RouteDecision:
        """选一个 Agent，或明确回澄清。**保证返回的 agent 一定是注册过的。**"""
        goal_norm = normalize_text(goal)

        # ① 空 goal：没有可比对的信号，直接澄清（不让空串"命中所有 tag"）。
        if not goal_norm:
            return RouteDecision(
                agent=CLARIFY_AGENT, confidence=0.0,
                reason="goal 为空（归一化后无内容），无从路由",
                candidates=(),
            )

        # ② 权限闸门 + 空注册表
        eligible = [s for s in self.specs() if _scopes_ok(principal, s)]
        if not eligible:
            total = len(self._specs)
            reason = ("无可用 Agent" if total == 0
                      else f"无可用 Agent：{total} 个 Agent 全部因权限不足被剔除"
                           f"（principal.scopes={sorted(principal.scopes)}）")
            return RouteDecision(agent=CLARIFY_AGENT, confidence=0.0,
                                 reason=reason, candidates=())

        goal_vec = self._embed(goal)
        # 与 explain() 共用同一套打分：审计视图与真实决策不可能漂移
        rows = [r for r in self._terms(goal_norm, goal_vec, principal) if r.eligible]
        prio = {s.name: s.priority for s in self.specs()}
        # 确定性排序：score 降 → priority 降 → name 升（完全同分也不漂移）
        rows.sort(key=lambda r: (-r.score, -prio.get(r.agent, 0), r.agent))

        candidates = tuple((r.agent, r.score) for r in rows[:MAX_CANDIDATES])
        top = rows[0]
        top_score = top.score

        # ③ 够不够格
        if top_score < self.confidence_threshold:
            return RouteDecision(
                agent=CLARIFY_AGENT, confidence=top_score,
                reason=(f"最高分 {top_score:.4f}（{top.agent}）低于阈值 "
                        f"{self.confidence_threshold:.2f}，没有 Agent 足够匹配"),
                candidates=candidates,
            )

        # ④ 赢没赢透（唯一候选不存在"接近"问题）
        if len(rows) >= 2:
            second_score = rows[1].score
            gap = round(top_score - second_score, _ROUND)
            if gap < GAP_MIN:
                return RouteDecision(
                    agent=CLARIFY_AGENT, confidence=top_score,
                    reason=(f"两个 Agent 分数接近（{top.agent}={top_score:.4f} vs "
                            f"{rows[1].agent}={second_score:.4f}，差距 {gap:.4f} < "
                            f"{GAP_MIN}），需要澄清具体要哪一个"),
                    candidates=candidates,
                )
            return RouteDecision(
                agent=top.agent, confidence=top_score,
                reason=(f"{top.agent} 最高分 {top_score:.4f}，领先 "
                        f"{rows[1].agent}={second_score:.4f}（差距 {gap:.4f} ≥ "
                        f"{GAP_MIN}）"),
                candidates=candidates,
            )

        # ⑤ 唯一候选且过阈值 → 直接选
        return RouteDecision(
            agent=top.agent, confidence=top_score,
            reason=(f"唯一候选 {top.agent}，分数 {top_score:.4f} ≥ 阈值 "
                    f"{self.confidence_threshold:.2f}"),
            candidates=candidates,
        )


# ══════════════════════════════════════════════════════════════
# 便利实现：单 Agent 场景兜底
# ══════════════════════════════════════════════════════════════
class KeywordRouter:
    """无需注册的极简 Router：**永远返回同一个 agent**。

    它的存在价值是"没有选择"本身要显式化：单 Agent 场景不该被迫注册一个
    假注册表，也不该在自己实现里写死 `agent = "default"`。用 `KeywordRouter`
    的话，调用点的形状和 `AgentRouter` 完全一致（都返回 RouteDecision），
    将来接第二个 Agent 只需换一个对象，不用改调用点。

    confidence 固定 1.0 是诚实的：它没有判断能力，所以它不表达"不确定"。
    真正的歧义处理必须由 `AgentRouter` 承担 —— 这里假装有 confidence 反而危险。
    """

    def __init__(self, agent: str = "default") -> None:
        if not normalize_text(agent):
            raise ValueError("KeywordRouter.agent 不能为空")
        self.agent = agent

    def route(self, goal: str, principal: Principal) -> RouteDecision:
        return RouteDecision(agent=self.agent, confidence=1.0,
                             reason="single-agent fallback", candidates=())

    def __repr__(self) -> str:
        return f"KeywordRouter(agent={self.agent!r})"


# ══════════════════════════════════════════════════════════════
# 与 Context 的接缝：Router 是 agent_state / route_confidence 的唯一 writer
# ══════════════════════════════════════════════════════════════
def route_patches(decision: RouteDecision) -> tuple[Patch, ...]:
    """把 RouteDecision 落成两条 patch（`by=Owner.ROUTER`）。

    `contracts.OWNER_TABLE` 已经把 `agent_state` / `route_confidence` 指派给
    ROUTER，所以这里只负责产生 patch，**不碰 Context 的 apply 逻辑**（那是
    core/context.py 的地盘）。没有这两条 patch，Router 的判定就只活在内存里，
    patch_log 里看不见"为什么换了 Agent" —— 而 patch_log 同时是 WAL 和评测轨迹。
    """
    return (
        Patch(op="set", path="agent_state", by=Owner.ROUTER,
              value={"agent": decision.agent,
                     "reason": decision.reason,
                     "candidates": [[n, round(s, 4)] for n, s in decision.candidates]}),
        Patch(op="set", path="route_confidence", by=Owner.ROUTER,
              value=round(decision.confidence, 4)),
    )
