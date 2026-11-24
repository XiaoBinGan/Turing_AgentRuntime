"""plane/skill.py — Skill Runtime（W21）：Procedural Memory 的唯一权威源。

================================================================================
归属裁决：SOP 的内容归 Skill 库，不归 Memory Plane
================================================================================
架构文档（agent-runtime-core-arch.md）§3 把专栏「七层记忆」重新归位，其中
Procedural Memory 被**移出** Memory Plane：

    维度            Memory（Episodic/Semantic/Profile）      Skill 库
    ─────────────────────────────────────────────────────────────────────────
    变更流程        运行时回写，随写随生效                    code review / 单测 / 灰度 / 回滚
    淘汰机制        TTL + 确定性保留矩阵（180 天未激活归档）  显式 retire，不做时间衰减
    加载单位        记忆条目（recall 一条条取）               文档（索引 + 规程全文）
    可测试性        无验收标准                               verification 进 CI
    失败后果        召回污染 → 毒化后续 turn                 规程陈旧 → 单次做错，可纠正

三条理由里最硬的是第一条：**用「180 天未激活即归档」去淘汰一份排错规程是灾难**。
排错规程恰恰是低频使用、高价值的东西 —— 它天生「不活跃」，但删了就要重新踩坑。
所以本模块**没有 TTL、没有衰减、没有分数淘汰**，只有 `retire()` 这一条显式下线路径。

对应的代码事实：`contracts.py` 里 `"skill_state": Owner.SKILL` 这个字段，
此前**没有任何模块写它** —— 本模块是它的唯一写者（通过 `SkillLibrary`）。
`plane/memory.py::consolidate()` 已经能产出 Skill 候选，但按裁决它只产出**候选**、
不落库；接收方就是本模块的 :meth:`SkillRuntime.ingest_candidates`。

================================================================================
双层按需装载：索引常驻、规程按需
================================================================================
这是本模块存在的**主要工程价值**（专栏第 08 篇）：

    index()  ── 每条技能一行（name + 触发词 + 一句话）→ 常驻 Prompt，永远在
    load()   ── 命中技能的 body_text() 全文 → 只在 match() 命中后装载

为什么必须分两层：把所有 SOP 全文塞进系统提示，10 个技能就是几万 token，
而且 90% 的轮次根本用不到其中 9 个 —— 这是「能力越强、上下文越废」的死循环。
分层后常驻成本随技能**数量**线性增长（每行 ~60~100 字），
按需成本只随**本次命中数**增长（默认 k=3），且 `max_body_chars` 硬兜底。

    render(goal) 的返回 = {"index": 常驻索引, "loaded": [命中规程], "truncated": bool}

`render()` 是 Builder / 调用方**唯一需要的方法**：它内部完成 match → load → 预算裁剪，
调用方不需要知道匹配算法，也不需要自己算预算。测试里有量化断言：
`len(index) < 全文的 1/3`、`len(render) < index + 全部 body`（见 tests ⑧ ⑯）。

================================================================================
匹配：触发词命中 + 字符 bigram Jaccard（与 memory 严格同口径）
================================================================================
    最终分 = 0.6 * 触发词命中(0/1) + 0.4 * Jaccard(bigrams(goal), bigrams(表层文本))

- **触发词是精确信号**：`trigger=("部署", "rollback")` 命中即强证据，权重给 0.6。
  它是作者显式声明的「这个技能管这类活」，比任何相似度都可信。
- **bigram 是模糊信号**：中文没有空格，字符二元组是最便宜的近似语义单元。
  它兜住「作者没想到的措辞」—— 没有它，技能库就退化成一张关键词表。
- **不 import memory**：这里**复制**了 `plane/memory.py::similarity` 的口径
  （小写 + 去空白/标点 + 字符 bigram + Jaccard），但不 import 它。理由是平面之间
  只许依赖 `contracts.py`（BRIEF 硬约束 7）：Skill 库将来可能被单独抽出成服务，
  它不该因为 Memory Plane 换了向量后端而跟着变。同口径是**契约**，
  由测试（`TestSimilarityParity`）在两份实现之间钉住，而不是由 import 钉住。

阈值的两处刻意设计：
- `MIN_TRIGGER_CHARS = 2`：单字符触发词（如 "a"）会命中一切，直接不参与匹配。
- `SIM_FLOOR = 0.08`：纯相似度命中要过一道地板，否则任意两个中文句子偶然共用一个
  bigram 就能挤进 Top-K（实测噪声 0.06 vs 同义改写 0.21，见常量处标定）。

================================================================================
自演进闭环（本模块最有价值的能力）
================================================================================
    Memory Plane.consolidate()  ──候选 dict──▶  SkillRuntime.ingest_candidates()
                                                        │
                                          name/version/steps/trigger/source
                                                        ▼
                                                  SkillLibrary.add()
                                                        ▼
                                              index() 下一轮就看得见

闭环能成立的关键是**候选 → SkillSpec 的字段映射是确定性的**：
- `name` 由 `title` slug 化（纯中文标题退化为内容哈希，见 `slugify`）；
- `steps` 从 `draft`（首选）或 `reason` 中切分，都没有则记 `参考候选来源：<记忆 id>`；
- `source = "memory-consolidate"`，与手工编写的技能区分开（评审时一眼能看出来源）；
- **同名再次摄入 → 版本号 +1，历史版本保留**。这里刻意选择「宁可版本膨胀，
  也不静默覆盖」：自动沉淀的结论可能是错的，覆盖掉人工校订过的 v1 是不可接受的。
  去重责任在**调用方**（按 `skill_id` 去重后再送进来），不在本模块 —— 因为
  「同一个 skill_id 隔了三周再来一次，算不算新版本」是运营判断，不是机制判断。

================================================================================
版本与退休
================================================================================
- `add` 同名同版本 → **幂等**（首写胜出，不重复、不改内容）。
  理由：重放一个部署脚本不该改变已发布的 SOP 内容。
- `add` 同名新版本 → 成为 current；旧版本仍可 `get(name, version="1")` 取到。
- `retire(name)` → 从 `all()` / `index()` / `load()` 消失，但 `get(name, version)`
  仍可取。这是审计通道：三个月前的 v1 到底写了什么，必须查得到。

================================================================================
Provider SPI（与 Memory 一致的做法）
================================================================================
    InMemorySkillProvider: put / get / list_latest / retire

`SkillLibrary(provider=None)` 默认用它；换文件系统 / DB 只改这一层，
`SkillLibrary` 与 `SkillRuntime` 一行不动。注入时会检查四个方法是否齐全
（fail fast，而不是在第一次 add 时才炸）。

================================================================================
设计约束
================================================================================
1. 只依赖 `contracts.py`（SkillSpec / SkillMatch），不 import core/*、不 import 其他 plane。
2. 一切排序都显式带 name 兜底（`sorted(key=(‑score, name))`）：**确定性不是可选项**，
   技能装载顺序变化会让同一个 goal 的 Prompt 变化，从而让评测不可复现。
3. `body_text()` 是唯一的正文来源，本模块**不解析 Markdown**、不自己拼正文格式 ——
   格式归契约（`SkillSpec.body_text`），预算归本模块，两者不混。
4. `load()` 只从**在役**技能里取：退休技能不进 Prompt，只走审计通道。
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

# 允许在任意工作目录下 import（demo/run.py 可能从别处启动）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from contracts import SkillMatch, SkillSpec  # noqa: E402

__all__ = [
    "InMemorySkillProvider",
    "SkillLibrary",
    "SkillRuntime",
    "PROVIDER_METHODS",
    "TRIGGER_WEIGHT",
    "SIM_WEIGHT",
    "SIM_FLOOR",
    "MIN_TRIGGER_CHARS",
    "DESC_MAX_CHARS",
    "slugify",
    "normalize_text",
    "similarity",
    "text_similarity",
]

# ══════════════════════════════════════════════════════════════
# 常量：匹配权重与阈值（手算可验证，不藏在公式里）
# ══════════════════════════════════════════════════════════════
#: 触发词命中的权重（精确信号，作者显式声明）
TRIGGER_WEIGHT = 0.6
#: 字符 bigram 相似度的权重（模糊信号，兜住作者没想到的措辞）
SIM_WEIGHT = 0.4
#: 纯相似度命中的地板：低于它的偶然重合不算命中。
#: 取值来自实测标定（字符 bigram Jaccard）：同义改写 ≈ 0.21（"数据库连接被占满一直报超时"
#: vs "连接池耗尽导致数据库超时"），而「两个无关句偶然共用一个 bigram」≈ 0.06
#: （"线上部署失败了" vs "CI 流水线失败定位"）。0.08 把两者分开：
#: 作者没写出的近义措辞进得来，纯噪声进不来。
SIM_FLOOR = 0.08
#: 触发词最短有效长度（归一化后）：单字符会命中一切
MIN_TRIGGER_CHARS = 2
#: 候选 → description 的截断长度（description 会进常驻索引，必须短）
DESC_MAX_CHARS = 120
#: Provider SPI 的四个必需方法（注入时校验）
PROVIDER_METHODS = ("put", "get", "list_latest", "retire")


# ══════════════════════════════════════════════════════════════
# 文本工具：与 plane/memory.py **同口径**的独立实现（刻意不 import）
# ══════════════════════════════════════════════════════════════
#: 归一化：与 memory.py::normalize_text 完全一致（小写 + 去空白/标点，保留 CJK）
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """小写 + 去空白/标点，保留 CJK 与字母数字。

    与 `plane/memory.py::normalize_text` 同口径。复制而 share 的原因见模块 docstring：
    平面之间只依赖契约，同口径由测试钉住。
    """
    return _PUNCT_RE.sub("", (text or "").lower())


def _bigrams(text: str) -> set[str]:
    """字符 bigram 集合（中文无空格，字符二元组是最便宜的近似语义单元）。"""
    s = normalize_text(text)
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度。与 `plane/memory.py::similarity` 同口径（零依赖、可手算）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b)) if inter else 0.0


def text_similarity(a: str, b: str) -> float:
    return similarity(_bigrams(a), _bigrams(b))


# ══════════════════════════════════════════════════════════════
# 版本工具：版本号只做「可读 + 可比较」，不做语义版本解析
# ══════════════════════════════════════════════════════════════
_DIGITS_RE = re.compile(r"^\d+$")
_NUMS_RE = re.compile(r"\d+")


def _version_key(version: str) -> tuple[int, ...]:
    """版本排序键：把 "10" 排在 "9" 之后（字符串比较会把它们颠倒）。

    非数字版本（"v1" / "1.2"）按其中的数字序列比较；没有任何数字则视为 (0,)。
    """
    nums = _NUMS_RE.findall(str(version))
    return tuple(int(n) for n in nums) if nums else (0,)


def _next_version(current: str) -> str:
    """v1 → v2；"1.2" → "1.3"；无法解析 → "2"。

    刻意只做「最后一段数字 +1」：Skill 版本不是语义化版本，没有兼容性承诺，
    它只需要 ① 单调递增 ② 人类可读 ③ 旧版本地址不变。
    """
    cur = str(current or "").strip()
    if _DIGITS_RE.match(cur):
        return str(int(cur) + 1)
    nums = _NUMS_RE.findall(cur)
    if nums:
        last = nums[-1]
        idx = cur.rfind(last)
        return cur[:idx] + str(int(last) + 1) + cur[idx + len(last):]
    return "2"


# ══════════════════════════════════════════════════════════════
# slug：title → skill name
# ══════════════════════════════════════════════════════════════
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """标题 → 技能名：小写、空白转 `-`、去非字母数字。

    **纯中文 / 日韩标题会削成空串**，这不是「丢内容」，而是标识符的硬要求
    （文件名、URL、`skills/<name>/SKILL.md` 路径、CLI 参数都要它）。
    空串时退化为**内容哈希**：`skill-<sha1(归一化文本)[:8]>`。

    为什么用 sha1 而不是内置 `hash()`：字符串哈希受 `PYTHONHASHSEED` 影响，
    跨进程不可复现 —— 而「同一份内容产出同一个技能名」必须是稳定的，
    否则每次重启都会多出一份同名不同 id 的技能。行为明确：同标题同内容 → 同名；
    不同中文标题 → 不同哈希名（可读性差，但可用、可审计、不撞车）。
    """
    raw = (text or "").strip().lower()
    slug = _NON_SLUG_RE.sub("-", raw).strip("-")
    if slug:
        return slug
    material = normalize_text(text) or (text or "").strip()
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
    return "skill-" + digest


# ══════════════════════════════════════════════════════════════
# Provider SPI
# ══════════════════════════════════════════════════════════════
class InMemorySkillProvider:
    """Skill 存储 SPI 的默认实现（内存字典）。

    存储形状刻意拆成三层：
        `_versions[name][version] -> SkillSpec`   全部历史版本
        `_latest[name] -> version`                当前版本指针
        `_retired: set[str]`                      退休名单（**不删版本**）

    为什么退休不删版本：`retire()` 的语义是「不再出现在索引和装载里」，
    不是「这段历史没发生过」。审计要求能回答「三个月前那个 v1 的 SOP 写了什么」，
    所以版本数据永远保留，只有可见性被摘掉。

    `put()` 在版本已存在时**首写胜出**（返回 False），这是 `add` 幂等语义的底座：
    重放部署脚本 / 重跑数据迁移，不该改变一份已发布 SOP 的内容。
    """

    #: SPI 名称（`SkillMatch.provider` 取它，便于多 provider 混用时溯源）
    name = "InMemorySkillProvider"

    def __init__(self) -> None:
        self._versions: dict[str, dict[str, SkillSpec]] = {}
        self._latest: dict[str, str] = {}
        self._retired: set[str] = set()

    # ── SPI 四方法 ──────────────────────────────────────
    def put(self, spec: SkillSpec) -> bool:
        """写入一个版本。返回 True = 新增，False = 该 (name, version) 已存在。"""
        if not isinstance(spec, SkillSpec):
            raise TypeError("put() 只接受 SkillSpec")
        name = spec.name
        version = str(spec.version)
        bucket = self._versions.setdefault(name, {})
        if version in bucket:
            return False                      # 幂等：首写胜出，不改已落库内容
        bucket[version] = spec
        cur = self._latest.get(name)
        if cur is None or _version_key(version) > _version_key(cur):
            self._latest[name] = version
        # 重新 add 一个已退休技能 = 复出（新版本发布即恢复上线）
        self._retired.discard(name)
        return True

    def get(self, name: str, version: str | None = None) -> SkillSpec | None:
        """取技能。`version=None` 取当前版本；指定版本可绕过退休状态（审计通道）。"""
        bucket = self._versions.get(name)
        if not bucket:
            return None
        if version is None:
            cur = self._latest.get(name)
            return bucket.get(cur) if cur is not None else None
        return bucket.get(str(version))

    def list_latest(self) -> list[SkillSpec]:
        """所有**在役**技能的最新版本，按 name 排序（确定性）。"""
        out: list[SkillSpec] = []
        for name in sorted(self._versions):
            if name in self._retired:
                continue
            spec = self.get(name)
            if spec is not None:
                out.append(spec)
        return out

    def retire(self, name: str) -> bool:
        """下线一个技能。True = 本次确实摘掉；False = 不存在或已退休。"""
        if self._latest.get(name) is None:
            return False
        if name in self._retired:
            return False
        self._retired.add(name)
        return True

    # ── 便利方法（非 SPI 必需；换 provider 时可缺）──────────
    def versions(self, name: str) -> list[str]:
        """某技能的全部历史版本，按版本号升序。"""
        return sorted(self._versions.get(name, {}), key=_version_key)

    def is_retired(self, name: str) -> bool:
        return name in self._retired

    def __len__(self) -> int:
        return len(self._latest)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"InMemorySkillProvider(names={len(self._latest)}, "
                f"versions={sum(len(b) for b in self._versions.values())}, "
                f"retired={len(self._retired)})")


# ══════════════════════════════════════════════════════════════
# SkillLibrary：内容权威源（唯一写者）
# ══════════════════════════════════════════════════════════════
class SkillLibrary:
    """技能库：版本化存储 + 退休，**不做匹配、不做预算**。

    职责切分的理由：技能库是「内容权威源」，可能被 CI、评审流程、其他 Agent
    直接读写；匹配与预算属于「每次 turn 的运行时决策」，只服务当前 goal。
    两者混在一个类里，将来把库换成远端服务时会连匹配逻辑一起搬走。
    """

    def __init__(self, provider: InMemorySkillProvider | None = None) -> None:
        prov = provider if provider is not None else InMemorySkillProvider()
        missing = [m for m in PROVIDER_METHODS
                   if not callable(getattr(prov, m, None))]
        if missing:
            raise TypeError(f"Skill provider 缺少 SPI 方法: {missing}")
        self.provider = prov

    @property
    def provider_name(self) -> str:
        """provider 的可读名（用于 `SkillMatch.provider` 溯源）。"""
        return str(getattr(self.provider, "name", "") or type(self.provider).__name__)

    # ── 写 ──────────────────────────────────────────────
    def add(self, spec: SkillSpec) -> None:
        """落库一个技能版本。

        - 同名同版本 → 幂等（首写胜出，内容不变）；
        - 同名新版本 → 成为 current，旧版本保留（`get(name, version)` 可查）；
        - 同名且已退休 → 新版本发布即复出（provider 负责摘掉退休标记）。
        """
        if not isinstance(spec, SkillSpec):
            raise TypeError("add() 只接受 SkillSpec")
        cleaned = _clean_spec(spec)
        if not cleaned.name:
            raise ValueError("SkillSpec.name 不能为空（它是版本与退休的主键）")
        # 幂等检查放在**库**这一层，而不是只依赖 provider 自觉：
        # 换文件系统 / DB provider 时，"同名同版本不覆盖内容" 仍是库的承诺。
        if self.provider.get(cleaned.name, str(cleaned.version)) is not None:
            return
        self.provider.put(cleaned)

    # ── 读 ──────────────────────────────────────────────
    def get(self, name: str, version: str | None = None) -> SkillSpec | None:
        """取技能：`version=None` 取 current；指定版本可读历史（含已退休）。"""
        return self.provider.get(name, None if version is None else str(version))

    def all(self) -> list[SkillSpec]:
        """所有在役技能的最新版本（**不含**退休；**不含**历史版本），按 name 排序。"""
        specs = [s for s in self.provider.list_latest() if s is not None]
        return sorted(specs, key=lambda s: s.name)

    def versions(self, name: str) -> list[str]:
        """某技能的全部历史版本（升序）。provider 不提供时退化为只报告 current。"""
        fn = getattr(self.provider, "versions", None)
        if callable(fn):
            return list(fn(name))
        cur = self.get(name)
        return [cur.version] if cur is not None else []

    # ── 下线 ────────────────────────────────────────────
    def retire(self, name: str) -> bool:
        """显式下线（**唯一的淘汰路径**：没有 TTL、没有衰减、没有分数淘汰）。"""
        return bool(self.provider.retire(name))


def _clean_spec(spec: SkillSpec) -> SkillSpec:
    """归一化外部传入的 spec：version 转字符串、元组字段去空项。

    只在需要时 `replace`（frozen dataclass 不改内容语义，只改脏形状）。
    空字符串条目必须去掉：一个空 trigger 会命中一切，一条空 step 会在正文里留空洞。
    """
    fields: dict[str, Any] = {}
    if not isinstance(spec.version, str):
        fields["version"] = str(spec.version)
    name = spec.name.strip()
    if name != spec.name:
        fields["name"] = name
    for field in ("trigger", "steps", "pitfalls", "verification", "tools"):
        value = getattr(spec, field)
        if not isinstance(value, tuple):
            value = tuple(value)
        cleaned = tuple(x for x in (str(v).strip() for v in value) if x)
        if cleaned != getattr(spec, field):
            fields[field] = cleaned
    return replace(spec, **fields) if fields else spec


# ══════════════════════════════════════════════════════════════
# 匹配工具
# ══════════════════════════════════════════════════════════════
def _match_surface(spec: SkillSpec) -> str:
    """参与 bigram 相似度的**表层文本**：title + description + trigger。

    刻意**不含 steps/pitfalls/verification**：Jaccard 的分母是并集，
    把 2000 字的规程全文塞进来会让任何短 goal 的分母爆炸、相似度恒趋近 0 ——
    「全文越长越匹配不上」，那是反直觉的 bug，不是特性。
    规程全文的作用是**命中后装载**，不是参与打分（这正是双层装载的分界）。
    """
    parts = [spec.title, spec.description, " ".join(spec.trigger)]
    return " ".join(p for p in parts if p)


def _trigger_hit(triggers: Sequence[str], goal_norm: str) -> bool:
    """触发词是否命中（goal 已归一化）。

    归一化后做**子串包含**判断：这样 "CI/构建" 与 "ci 构建" 视为同一个词，
    作者不必为标点和空格写变体。
    """
    if not goal_norm:
        return False
    for trig in triggers:
        nt = normalize_text(trig)
        if len(nt) >= MIN_TRIGGER_CHARS and nt in goal_norm:
            return True
    return False


def _unique(names: Sequence[str]) -> list[str]:
    """保序去重（重复装载同一个技能只会浪费预算）。"""
    seen: set[str] = set()
    out: list[str] = []
    for name in names or ():
        key = str(name or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ══════════════════════════════════════════════════════════════
# SkillRuntime：双层按需装载
# ══════════════════════════════════════════════════════════════
class SkillRuntime:
    """双层按需装载：索引常驻进 Prompt，规程只在命中后装载。

    三个入口的职责边界（不要混用）：
        `index()`  常驻层：全量技能各一行，**永远付得起**
        `match()`  决策层：goal → 命中的 (name, score)，不产生正文
        `load()`   装载层：name 列表 → 正文，**受 max_body_chars 硬约束**
        `render()` 组合层：match + load 一次做完 —— **Builder 只需要这一个**
    """

    def __init__(self, library: SkillLibrary, *, max_body_chars: int = 4000) -> None:
        for method in ("all", "get"):
            if not callable(getattr(library, method, None)):
                raise TypeError(f"SkillLibrary 缺少方法: {method}")
        self.library = library
        #: 单次装载的正文总预算。4000 字符 ≈ 一个 3 步 SOP 的全文 + 余量；
        #: 超过它就该怀疑 k 设大了，而不是该调大预算。
        self.max_body_chars = max(0, int(max_body_chars))

    # ── ① 常驻索引 ──────────────────────────────────────
    def index(self) -> str:
        """常驻索引：每行一条 `spec.index_text`，按 name 排序（确定性）。

        行格式归契约（`SkillSpec.index_text`），本模块只保证：一行一条、不重复、
        顺序稳定 —— 顺序变化会让同一个 goal 的 Prompt 变化，评测就不可复现。
        """
        return "\n".join(spec.index_text for spec in self.library.all())

    # ── ② 匹配 ─────────────────────────────────────────
    def match(self, goal: str, k: int = 3) -> list[SkillMatch]:
        """goal → 命中的技能（按 score 降序，最多 k 条）。

        打分：`0.6 * 触发词命中 + 0.4 * bigram Jaccard`。
        命中条件：触发词命中，**或**相似度 ≥ `SIM_FLOOR`（纯模糊信号也能进，
        但要有地板，否则偶然共用一个 bigram 就算命中）。
        """
        try:
            limit = int(k)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return []
        goal_norm = normalize_text(goal)
        if not goal_norm:
            return []
        goal_bg = _bigrams(goal)
        provider = self.library.provider_name if hasattr(self.library, "provider_name") else ""
        scored: list[SkillMatch] = []
        for spec in self.library.all():
            trig = _trigger_hit(spec.trigger, goal_norm)
            sim = similarity(goal_bg, _bigrams(_match_surface(spec)))
            if not trig and sim < SIM_FLOOR:
                continue
            score = TRIGGER_WEIGHT * (1.0 if trig else 0.0) + SIM_WEIGHT * sim
            if score <= 0.0:
                continue
            # 排序键带 name：同分时按 name 升序，避免集合遍历顺序渗透进 Prompt
            scored.append(SkillMatch(name=spec.name, score=round(score, 6), provider=provider))
        scored.sort(key=lambda m: (-m.score, m.name))
        return scored[:limit]

    # ── ③ 按需装载 ──────────────────────────────────────
    def load(self, names: Sequence[str],
             *, scores: Mapping[str, float] | None = None) -> list[SkillSpec]:
        """按需装载：**只返回请求里仍在役的技能**，受 `max_body_chars` 总和约束。

        预算裁剪策略是**按优先级贪心装、装不下就跳过**（而不是截断正文）：
        - 截断正文会交付一份「说了半截的 SOP」—— 半截规程比没有规程更危险
          （模型会以为自己看全了，跳过被截掉的验收步骤）；
        - 跳过则明确表达「这次装不下，未装载」，`render()` 用 `truncated=True` 上报。

        优先级来源：给了 `scores` 就按 score 降序（render 就是这么调的），
        否则按传入顺序。同一技能的重复名字自动去重。
        返回顺序 = 实际装载顺序（最相关的排最前，直接决定 Prompt 里的先后）。
        """
        ordered = _unique(names)
        if scores:
            ordered.sort(key=lambda n: (-float(scores.get(n, 0.0)), n))
        # 只从在役技能里取：退休技能不进 Prompt（审计通道是 get(name, version)）
        active = {spec.name: spec for spec in self.library.all()}
        budget = self.max_body_chars
        used = 0
        out: list[SkillSpec] = []
        for name in ordered:
            spec = active.get(name)
            if spec is None:
                continue
            body = spec.body_text()
            if len(body) > budget - used:
                continue                      # 装不下 → 跳过，不截断
            used += len(body)
            out.append(spec)
        return out

    # ── ④ 组合入口（Builder 唯一需要的方法）──────────────
    def render(self, goal: str, k: int = 3) -> dict:
        """`{"index": str, "loaded": [{"name","body"}], "truncated": bool}`。

        纯函数（同样输入 → 同样的 dict，逐字节可复现），因为它直接进 Prompt：
        Prompt 不可复现，评测与回归就都无从谈起（测试 ㉒ 钉住这一点）。
        """
        matches = self.match(goal, k)
        specs = self.load([m.name for m in matches],
                          scores={m.name: m.score for m in matches})
        loaded = [{"name": spec.name, "body": spec.body_text()} for spec in specs]
        return {
            "index": self.index(),
            "loaded": loaded,
            # 命中但被预算挡掉 → 明确上报，调用方可以降级（问用户 / 缩小 k / 换检索）
            "truncated": len(specs) < len(matches),
        }

    # ── ⑤ 自演进闭环：Memory 候选 → 技能库 ────────────────
    def ingest_candidates(self, candidates: Sequence[dict]) -> list[SkillSpec]:
        """接收 `MemoryRuntime.consolidate()` 的产出，转成 SkillSpec 落库。

        候选形状（由 memory plane 定义，本模块只读不管写）：
            {"skill_id", "title", "from_memory", "reason", "draft"}

        **容错是硬要求**：候选来自离线批处理，可能缺字段、带脏类型、甚至整条是 `{}`。
        坏一条不能炸整批 —— 但也不能悄悄吞掉，所以只捕获「数据形状错误」四类异常
        （TypeError/ValueError/KeyError/AttributeError），程序性错误照常抛出。

        返回**实际落库**的 spec 列表（调用方据此写审计日志 / 通知评审）。
        """
        written: list[SkillSpec] = []
        for cand in candidates or ():
            data: Mapping[str, Any] = cand if isinstance(cand, Mapping) else {}
            try:
                spec = self._spec_from_candidate(data)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue                      # 脏候选：跳过这一条，继续后面的
            self.library.add(spec)
            written.append(spec)
        return written

    def _spec_from_candidate(self, cand: Mapping[str, Any]) -> SkillSpec:
        """候选 dict → SkillSpec（字段映射见模块 docstring 的「自演进闭环」）。

        完全没有可落库内容的候选（`{}` / 全 None）→ `ValueError`，由调用方跳过。
        刻意**不**为它编一个 "candidate" 一样的占位名：技能库里塞进无名技能，
        比少一条候选危险得多 —— 它会出现在索引里，却没人能评审它。
        """
        title = _as_text(cand.get("title")) or _as_text(cand.get("skill_id"))
        reason = _as_text(cand.get("reason"))
        draft = _as_text(cand.get("draft"))
        if not (title or reason or draft):
            raise ValueError("候选缺少 title/skill_id/draft/reason，无法落库")
        name = slugify(title or reason or draft)
        from_memory = _as_str_list(cand.get("from_memory"))

        # 同名已存在 → 升版本，历史保留（宁可版本膨胀，也不静默覆盖）
        current = self.library.get(name)
        version = "1" if current is None else _next_version(current.version)

        steps = _split_steps(draft) or _split_steps(reason)
        if not steps:
            source_ids = ", ".join(from_memory) if from_memory else "（候选未带来源 id）"
            steps = [f"参考候选来源：{source_ids}"]

        # description 会进常驻索引（`index_text`），必须短；来源信息放这里做审计线索
        desc = reason or f"由记忆巩固产出的技能候选：{title or name}"
        if from_memory:
            shown = ", ".join(from_memory[:3]) + ("…" if len(from_memory) > 3 else "")
            desc = f"{desc}（源自 {len(from_memory)} 条记忆: {shown}）"
        desc = _clip(desc, DESC_MAX_CHARS)

        spec = SkillSpec(
            name=name,
            title=title or name,
            description=desc,
            trigger=_derive_triggers(title),
            steps=tuple(steps),
            version=version,
            source="memory-consolidate",
        )
        # 走一次与手工 add 相同的归一化，避免两条写入路径慢慢漂移
        return _clean_spec(spec)


# ── 候选解析的小工具（纯函数，便于单独验证）────────────────────
def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    """`from_memory` 可能是 list / tuple / 逗号分隔字符串 / None。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw: Sequence[Any] = re.split(r"[,;；\s]+", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        return []
    return [str(v).strip() for v in raw if str(v).strip()]


#: 步骤切分符：候选的 draft 是「；」拼接的记忆文本
_STEP_SPLIT_RE = re.compile(r"[；;。\n]+")
#: 步骤前缀噪声（"1. " / "1) " / "- " / "•"）
_STEP_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)、]|[-*•·])\s*")


def _split_steps(value: Any) -> list[str]:
    """把 draft / reason 切成步骤列表；切不出内容返回空列表（由调用方兜底）。"""
    text = _as_text(value)
    if not text:
        return []
    out: list[str] = []
    for part in _STEP_SPLIT_RE.split(text):
        part = _STEP_PREFIX_RE.sub("", part.strip()).strip()
        if part:
            out.append(part)
    return out


def _derive_triggers(title: str) -> tuple[str, ...]:
    """从标题派生触发词：去掉「自动沉淀：」这类前缀后按分隔符切。

    派生而不是留空的理由：没有 trigger 的技能只能靠 bigram 命中（权重 0.4），
    落库后基本等于「装着但永远不匹配」。标题里的关键词就是作者能拿到的最强信号。
    """
    cleaned = re.sub(r"^\s*(?:自动沉淀|沉淀|skill|candidate)\s*[:：\s]*", "",
                     title or "", flags=re.IGNORECASE)
    out: list[str] = []
    for part in re.split(r"[/、,，;；:：\s]+", cleaned):
        part = part.strip()
        if len(part) >= MIN_TRIGGER_CHARS and part not in out:
            out.append(part)
    return tuple(out[:6])


def _clip(text: str, limit: int) -> str:
    """截断并加省略号（索引预算：一行技能必须短）。"""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit - 1] + "…"
