"""agents/planner.py —— 两个「规划器」实现（Agent 层能力，Runtime 不含任何规划逻辑）。

===============================================================================
为什么规划器在 agents/ 而不是 core/
===============================================================================
Runtime 判断的是**形状**：Patch 合法吗、owner 对不对、该不该终止。
「发布失败了下一步该回滚还是先扩容」「日志查不到该换指标还是换时间窗」这些是
**内容** —— 换成电商客户、法务 Agent、代码 Agent，答案完全不同，而
`core/loop.py` 的三级死锁判定、`Patch` 校验、`TermReason` 一行都不用改。

所以 Runtime 只冻结了 `contracts.PlannerProtocol`（一个 `replan(snapshot, reason)`
签名），实现留在这里。`agents/demo_template.py` 是另一个方向的示例（模板），
本文件专管规划。

===============================================================================
契约（违反会**静默出错**，所以逐条对着写）
===============================================================================
1. **返回 `Patch`，不碰 `Context`**。Runtime 校验 `by` 与 `OWNER_TABLE` 后自己
   `ctx.apply()`。Planner 若能直接写 Context，就等于绕开归属校验与审计轨迹
   （INV-3 就白设了）。
2. **返回 `None` = 「我也想不出」**。Loop 据此判 `NO_PROGRESS` 收尾；返回空计划
   或抛异常拖着重试，会把「补救路径」变成死循环。
3. **抛异常不致命，但不主动抛**。Runtime 会包住 `replan` 并记 warning；可那只是
   兜底。主动抛等于把「模型今天胡说」升级成「回合异常」，还丢掉可观测的失败原因。
   本模块的失败一律**返回 None**，并把原因记在 `self.notes` / `self.last_error`。
4. **`snapshot` 是只读快照**（实际传的是 `ContextSnapshot`，带 `.data`）。
   改了不影响真实状态 —— 所以干脆不依赖它的任何可变性。
5. **只能写 `OWNER_TABLE` 里属于 `Owner.PLANNER` 的字段**：`plan` /
   `current_step` / `step_status`。
   注意 `observations` 归 `Owner.LOOP`（观测事实的权威源），Planner 只读 ——
   写它会被 `ctx.apply()` 以 `OwnershipViolation` 拒掉，Loop 只记一条 warning，
   于是「Patch 生成了但什么都没发生」，这是最难查的一类静默故障。
6. `plan` 的惯用形状是 `{"steps": [...]}`（`ContextSnapshot.get("plan")` 的消费方
   与 Runtime 测试都按这个读）。
7. **一次只能回一条 Patch**（签名如此）。所以本模块只写 `plan`；想同时把
   `current_step` 归零只能再调一次 `replan`，而 Loop 只强制调用一次 ——
   故新计划把 `current_step` 的语义收进 `plan["steps"]` 的序号里，
   不复用 Context 顶层的游标字段，避免出现「plan 换了、游标还停在旧计划第 3 步」
   这种两处状态不一致。

===============================================================================
两个实现的分工
===============================================================================
* `StaticDAGPlanner`：流程本来就确定（**发布 / 回滚 / 扩容 / 告警处置**这类
  有 SOP 的场景）。按预定义 DAG 的拓扑序出计划，同输入同输出 —— 可复现、可审计。
* `LLMPlanner`：开放式排障（「服务 p99 抖了，但日志里什么都没有」）。让模型看
  快照重写计划，输出仍必须是 `Patch`，且必须被严格解析。

===============================================================================
一条刻意的边界：**危险工具不在这里过滤**
===============================================================================
模型如果在计划里写了 `drop_database` / `restart_all_nodes`，本模块**照抄进 `plan`**，
不做任何拦截。理由不是「懒得做」，而是分层：

  * **Governance 才是闸门**（`plane/governance.py` 的 RBAC / Policy / 审批）。
    它在**执行时刻**按工具清单、`risk_tier`、principal 的 scope、租户策略判定，
    拒绝还能进 `denied_log` 供审计与回放。
  * Planner 手上只有一份只读快照，**没有工具清单、没有 principal、没有租户策略**。
    在这里过滤只能靠「猜名字黑名单」，那是把策略抄到错误的层：名单永远不全，
    而且会与 Governance 的判定**不一致**——读者无法判断「计划里没有这个工具」
    是因为模型没写，还是 Planner 偷偷删了。
  * 更糟的是**计划被悄悄改写**：模型写了 6 步、Planner 删掉 2 步，`plan` 与实际
    意图不符，事后复盘看到的是一份「谁也没批准过、也没人写过」的计划。
  * 规划阶段的正确职责是**建议**；能不能干由 Governance 在执行期判。计划里出现
    高危动作本身是有价值的信息（说明模型在往危险方向想），应当被记录、被审查，
    而不是被 Planner 抹掉。
"""
from __future__ import annotations

from typing import Any, Mapping

from contracts import ExecCtx, Owner, Patch
from plane.dag import DAG

try:                                     # 包内正常路径
    from .planner_llm_prompt import build_replan_prompt, parse_plan
except ImportError:                      # pragma: no cover - 直接按脚本运行时
    from planner_llm_prompt import build_replan_prompt, parse_plan

__all__ = ["LLMPlanner", "PLANNER_FIELDS", "StaticDAGPlanner"]


#: `OWNER_TABLE` 里归属 `Owner.PLANNER` 的字段（本模块**只许**写这些）。
#: 刻意从常量写死而不是运行时反查：反查会让「Runtime 改了表」这件事静默传染到
#: Agent 层；写死则会在 Runtime 变更时于 `tests/test_agent_planner.py` 里炸出来。
PLANNER_FIELDS: tuple[str, ...] = ("plan", "current_step", "step_status")

#: 快照里可以「点名用哪张 DAG」的顶层字段（上层编排器指定，优先于自动匹配）
_HINT_KEYS = ("plan_hint", "dag", "plan_name", "scenario")


def _as_mapping(snapshot: Any) -> Mapping[str, Any]:
    """接住两种快照：`Mapping`，或 Runtime 的 `ContextSnapshot`（有 `.data`）。"""
    if isinstance(snapshot, Mapping):
        return snapshot
    data = getattr(snapshot, "data", None)
    if isinstance(data, Mapping):
        return data
    return {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ══════════════════════════════════════════════════════════════
# ① 静态 DAG 规划器
# ══════════════════════════════════════════════════════════════
class StaticDAGPlanner:
    """按预定义 DAG 规划（适合流程确定的场景，如发布/回滚）。

    实现 `contracts.PlannerProtocol`。

    选择顺序（**确定性**，同快照同 reason 必定同结果）：

      1. 快照里的显式提示：`plan_hint` / `dag` / `plan_name` / `scenario`，
         或 `plan.dag`（上一次计划里记着的 DAG 名，保证「重规划不换 SOP」）；
      2. `reason` 文本里出现的 DAG 名（死锁详情通常带动作摘要，里面常含场景词）；
      3. `default`；
      4. 都没有 → `None`（Loop 判 NO_PROGRESS，不重试）。

    不做的事：不判风险、不查工具清单、不改 Context —— 见模块头的分层说明。
    """

    def __init__(self, dags: Mapping[str, Any], *, default: str = ""):
        # 拷贝一份：外部之后改 dict 不该改变已构造规划器的行为
        self._dags: dict[str, Any] = dict(dags or {})
        self._default: str = _text(default)
        #: 最近若干次判定的理由（「为什么选它 / 为什么没选出来」），供日志追溯
        self.notes: list[str] = []
        #: 最近一次返回的 Patch（测试与排障用；不参与决策）
        self.last_patch: Patch | None = None

    # ── 只读视图 ────────────────────────────────────────────
    @property
    def names(self) -> list[str]:
        """已注册的 DAG 名（排序，确定性）。"""
        return sorted(self._dags)

    def __contains__(self, name: object) -> bool:
        return name in self._dags

    def __len__(self) -> int:
        return len(self._dags)

    # ── 契约唯一入口 ────────────────────────────────────────
    def replan(self, snapshot: Mapping[str, Any], reason: str) -> Patch | None:
        """按 DAG 出计划。返回 `Patch`（写 `plan`）或 `None`（想不出）。"""
        snap = _as_mapping(snapshot)
        name, why = self._select(snap, reason or "")
        if not name:
            self._note(f"未选中任何 DAG（{why}）；已注册 {len(self._dags)} 张")
            self.last_patch = None
            return None

        steps = self._steps_of(name)
        if not steps:
            self.last_patch = None
            return None

        plan = {
            "source": "static_dag",
            "dag": name,
            # 能追溯「为什么 re-plan」：死锁原因原样留档
            "reason": reason or "",
            # 以及「为什么选这张 DAG」
            "why": why,
            "steps": steps,
        }
        patch = Patch("set", "plan", Owner.PLANNER, plan)
        self._note(f"命中 DAG {name!r}（{why}），{len(steps)} 步")
        self.last_patch = patch
        return patch

    # ── 内部：选哪张 DAG ────────────────────────────────────
    def _select(self, snap: Mapping[str, Any], reason: str) -> tuple[str, str]:
        for key in _HINT_KEYS:
            value = snap.get(key)
            if isinstance(value, str) and value in self._dags:
                return value, f"快照字段 {key} 指定"

        plan = snap.get("plan")
        if isinstance(plan, Mapping):
            for key in ("dag", "name", "template"):
                value = plan.get(key)
                if isinstance(value, str) and value in self._dags:
                    return value, f"旧计划 plan.{key} 指定"

        # reason 里出现的 DAG 名：按名字排序逐个试，保证命中结果与 dict 顺序无关
        for name in sorted(self._dags):
            if name and name in reason:
                return name, "死锁原因里提到了该场景"

        if self._default and self._default in self._dags:
            return self._default, "回落到 default"
        if self._default:
            return "", f"default={self._default!r} 未注册"
        return "", "无匹配且未设 default"

    # ── 内部：DAG → 步骤 ────────────────────────────────────
    def _steps_of(self, name: str) -> list[dict]:
        """拓扑序展开为步骤列表。非法 DAG（成环/重名/悬空依赖）→ 空列表 + 记因。"""
        spec = self._dags.get(name)
        if spec is None:
            self._note(f"DAG {name!r} 不在表里")
            return []
        try:
            dag = DAG(spec)                    # 只读拓扑视图，不执行
            errors = dag.validate()
            if errors:
                self._note(f"DAG {name!r} 不合法：{'; '.join(errors[:3])}")
                return []
            order = dag.topo_order()
        except Exception as exc:               # noqa: BLE001 - 非法输入不该拖垮死锁补救
            self._note(f"DAG {name!r} 展开失败：{type(exc).__name__}: {exc}")
            return []

        steps: list[dict] = []
        for index, node_name in enumerate(order):
            node = dag.node(node_name)
            steps.append({
                "index": index + 1,
                "name": node_name,
                "agent": _text(getattr(node, "agent", "")),
                "goal": _text(getattr(node, "goal", "")),
                "depends_on": list(dag.deps(node_name)),
            })
        if not steps:
            self._note(f"DAG {name!r} 是空图（没有节点），不产生计划")
        return steps

    def _note(self, text: str) -> None:
        """记一条可追溯的判定理由。只保留最近 32 条，避免长跑进程无限增长。"""
        self.notes.append(text)
        if len(self.notes) > 32:
            del self.notes[:-32]


# ══════════════════════════════════════════════════════════════
# ② LLM 规划器
# ══════════════════════════════════════════════════════════════
class LLMPlanner:
    """让模型根据快照重新出计划（适合开放式排障）。

    实现 `contracts.PlannerProtocol`。**输出必须是 Patch，不能直接写 Context。**

    失败策略（契约第 3 条的具体落地）：模型抛异常 / 吐坏 JSON / 给空计划，
    一律 **返回 None**，不向上抛。选择 None 而不是 re-raise 的原因：

      * `None` 是契约里**有语义**的返回值（"我也想不出"），Loop 据此判
        `NO_PROGRESS` 并给出确定文案；异常只会变成一条 warning，然后继续循环，
        下一次死锁还要再烧一次模型调用，直到撞上硬上限；
      * 失败原因不丢：记在 `self.last_error` / `self.notes` / `self.last_prompt`，
        排查时能直接看到「模型吐了什么、Prompt 长什么样」。

    模型输出的解析在 `agents/planner_llm_prompt.py`（严格：坏 JSON → None）。
    """

    def __init__(self, llm, *, max_steps: int = 6):
        if llm is None:
            # 构造期就炸，别等到死锁那一刻（那时已经在补救路径上了）
            raise ValueError("LLMPlanner 需要一个 LLM（实现 complete 或可调用）")
        if not (callable(getattr(llm, "complete", None)) or callable(llm)):
            raise TypeError(
                "llm 必须实现 contracts.LLMProtocol.complete(prompt, tools, e)，"
                f"或本身可调用；收到 {type(llm).__name__}")
        self._llm = llm
        #: 步数上限：模型给 20 步也只留 max_steps 步（防「计划本身超窗」）
        self.max_steps: int = max(1, int(max_steps))
        #: 最近一次失败原因（None = 最近一次成功或尚未调用）
        self.last_error: str | None = None
        #: 最近一次送给模型的 prompt（排障只读）
        self.last_prompt: str = ""
        #: 判定理由，供日志追溯（保留最近 32 条）
        self.notes: list[str] = []
        self.last_patch: Patch | None = None

    # ── 契约唯一入口 ────────────────────────────────────────
    def replan(self, snapshot: Mapping[str, Any], reason: str) -> Patch | None:
        """让模型重写计划；返回 `Patch`（写 `plan`）或 `None`。"""
        snap = _as_mapping(snapshot)
        prompt = build_replan_prompt(snap, reason or "", max_steps=self.max_steps)
        self.last_prompt = prompt

        try:
            raw = self._ask(prompt)
        except Exception as exc:               # noqa: BLE001 - 契约第 3 条：失败返回 None
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._note(f"模型调用失败，返回 None：{self.last_error}")
            self.last_patch = None
            return None

        parsed = parse_plan(raw, max_steps=self.max_steps)
        if parsed is None:
            self.last_error = "模型输出无法解析为计划（坏 JSON / 空 / 无步骤）"
            self._note(f"{self.last_error}；原始输出前 200 字：{_preview(raw)}")
            self.last_patch = None
            return None

        self.last_error = None
        plan = {
            "source": "llm",
            # 能追溯「为什么 re-plan」
            "reason": reason or "",
            "steps": parsed.as_dicts(),
            "truncated": parsed.truncated,
            "dropped": parsed.dropped,
        }
        patch = Patch("set", "plan", Owner.PLANNER, plan)
        self._note(f"模型给出 {len(parsed.steps)} 步"
                   f"{'（已按 max_steps 截断）' if parsed.truncated else ''}")
        self.last_patch = patch
        return patch

    # ── 内部：调模型 ────────────────────────────────────────
    def _ask(self, prompt: str) -> str:
        """统一两种 LLM 形状，都收敛成「文本」。"""
        complete = getattr(self._llm, "complete", None)
        if callable(complete):
            # 预算：Planner 拿不到 Loop 的 `e`（契约只有 snapshot + reason），
            # 所以这次调用用一个默认 ExecCtx。代价是它不进 Loop 的预算账 ——
            # 想要精确记账得扩契约，而契约已冻结，故此处如实注明而不自作主张。
            response = complete(prompt, (), ExecCtx())
            text = getattr(response, "text", None)
            if text is None and isinstance(response, str):
                text = response
            return text if isinstance(text, str) else ""
        response = self._llm(prompt)           # 可调用对象：直接当「prompt -> 文本」
        text = getattr(response, "text", response)
        return text if isinstance(text, str) else ""

    def _note(self, text: str) -> None:
        self.notes.append(text)
        if len(self.notes) > 32:
            del self.notes[:-32]


def _preview(raw: Any) -> str:
    """原始输出的一小段（单行化），只用于日志。"""
    text = raw if isinstance(raw, str) else str(raw)
    squeezed = " ".join(text.split())
    return squeezed[:200] + ("…" if len(squeezed) > 200 else "")
