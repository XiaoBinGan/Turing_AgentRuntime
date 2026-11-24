"""plane/wiring.py — 把各平面的能力接到主线上的最小粘合层。

这些能力都是「模块有了、主线还没用」的那些：

* `SkillRuntime.render()` 的产出要进 `Context.skill_state`，Builder 才看得见
* `ResourceRuntime.read()` 要能当一个 read_only 工具被 Loop 调用
* `HistoryCompressor` 要在 history 变长时自动压缩

放在独立模块而不是塞进各平面，是为了让「能力」和「接线」分开：
平面模块不依赖 Context / ToolRegistry（它们只吃 contracts），
接线需要两者都知道，所以单独一层。**这一层没有自己的状态和策略。**
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from contracts import (
    ExecCtx, Owner, Patch, ResourceRef, SideEffect, ToolManifest, ToolResult,
    RiskTier,
)


# ══════════════════════════════════════════════════════════════
# ① Skill → Context（双层装载的落点）
# ══════════════════════════════════════════════════════════════
def sync_skill_state(ctx, skill_rt, goal: str, k: int = 3) -> dict:
    """把技能渲染结果写进 Context.skill_state（owner=SKILL）。

    **索引常驻，规程按需**：`render()` 返回的 `index` 每次都写，
    `loaded` 只含命中的那几个 —— 这正是省预算的机制。

    `skill_state` 的 owner 是 `Owner.SKILL`，这里由 Loop 侧代写
    （与 Memory 的「经 LOOP 代写」同一模式）。
    """
    state = skill_rt.render(goal, k=k)
    ctx.apply(Patch("set", "skill_state", Owner.SKILL, state))
    return state


# ══════════════════════════════════════════════════════════════
# ② Resource → Tool（MCP Resources 变成可调用的 read_only 工具）
# ══════════════════════════════════════════════════════════════
def resource_tool_manifest(ref: ResourceRef, *, tool_name: str | None = None) -> ToolManifest:
    """把一个 Resource 声明成 read_only 工具。

    参数固定为 `{"uri": "<该资源的 uri>"}`，`additionalProperties=False` ——
    模型不需要"猜路径"，路径就是资源清单里的那一份。
    """
    name = tool_name or "read_" + ref.uri.replace("://", "_").replace("/", "_")[:40]
    return ToolManifest(
        name=name,
        side_effect=SideEffect.READ_ONLY,
        risk_tier=RiskTier.T0_SAFE,
        idempotent=True,
        #: 必须与 ResourceRuntime 的授权口径一致：资源走 `resource.*`，
        #: 不借 `tool.read` 的通道（否则两层的权限模型会各说各话）。
        required_scope="resource.read",
        description=f"读取资源 {ref.uri}（{ref.mime}）",
        input_schema={
            "type": "object",
            "properties": {"uri": {"type": "string", "const": ref.uri}},
            "required": ["uri"],
            "additionalProperties": False,
        },
    )


def register_resources(
    registry,
    runtime,
    refs: Sequence[ResourceRef],
    *,
    name_for: Callable[[ResourceRef], str] | None = None,
) -> list[ToolManifest]:
    """把若干 Resource 注册成 read_only 工具，返回最终 manifest 列表。

    返回值会被 Builder 看到（`registry.manifests()`），所以模型能"看见"这些资源。
    """
    out: list[ToolManifest] = []
    for ref in refs:
        manifest = resource_tool_manifest(ref, tool_name=name_for(ref) if name_for else None)

        def handler(args: dict, e: ExecCtx, _ref=ref) -> dict:
            return runtime.read(args.get("uri") or _ref.uri, e)

        registry.register(manifest, handler)
        out.append(manifest)
    return out


# ══════════════════════════════════════════════════════════════
# ③ Summary → Context（历史变长时自动压缩）
# ══════════════════════════════════════════════════════════════
def maybe_compress_history(ctx, compressor, *, min_messages: int = 0):
    """history 超过阈值才压缩；返回 SummaryResult 或 None。

    `min_messages=0` 时完全交给 compressor 的 `max_keep` 判断。
    """
    history = ctx.snapshot().get("history") or []
    if not isinstance(history, list) or len(history) < max(1, min_messages):
        return None
    from core.summary import apply_to_context
    return apply_to_context(ctx, compressor)


# ══════════════════════════════════════════════════════════════
# ④ 一站式装配
# ══════════════════════════════════════════════════════════════
class RuntimeWiring:
    """把可选能力包成一次装配，给 Gateway / demo / 生产入口共用。

    任何一项为 None 就跳过对应接线（渐进启用，不给最小用法加负担）。
    """

    def __init__(self, *, skill_rt=None, resource_rt=None, resources=(),
                 compressor=None, router=None, memory=None, recall_k: int = 5):
        self.skill_rt = skill_rt
        self.resource_rt = resource_rt
        self.resources = tuple(resources)
        self.compressor = compressor
        self.router = router
        #: Memory Plane。Runtime 管"什么时候读/写"（形状），
        #: 抽什么内容由 `Extractor`（Agent 层）决定 —— 见 contracts.Extractor。
        self.memory = memory
        self.recall_k = int(recall_k)
        self.resource_tools: list[ToolManifest] = []

    def attach_tools(self, registry) -> list[ToolManifest]:
        """把 resources 注册成工具（幂等：重复 attach 不会重复注册）。"""
        if not self.resource_rt or not self.resources or self.resource_tools:
            return self.resource_tools
        self.resource_tools = register_resources(registry, self.resource_rt, self.resources)
        return self.resource_tools

    def before_turn(self, ctx, goal: str, *, principal=None) -> dict:
        """回合开始前：路由 + 技能装载。返回可观测的摘要。"""
        info: dict[str, Any] = {}
        if self.router is not None and principal is not None:
            decision = self.router.route(goal, principal)
            ctx.apply(Patch("set", "agent_state", Owner.ROUTER, decision.agent))
            ctx.apply(Patch("set", "route_confidence", Owner.ROUTER,
                            round(decision.confidence, 4)))
            info["route"] = decision.to_dict()
        # Memory 召回：把命中写进 `ctx.memory_hits`（owner=MEMORY），Builder 会渲染它。
        # **放在技能装载之前**：技能渲染要看"以前遇到过什么"，召回得先有。
        # 失败不倒扣（fail-open）：记忆是增强，读不到不该让回合失败。
        if self.memory is not None and principal is not None:
            try:
                from contracts import Subject
                subject = Subject(tenant=getattr(principal, "tenant", "") or "",
                                  user_id=getattr(principal, "user_id", "") or "",
                                  team=getattr(principal, "team", "") or "",
                                  project=getattr(principal, "project", "") or "")
                hits = self.memory.recall(goal, subject, k=self.recall_k)
                if hits:
                    ctx.apply(Patch("set", "memory_hits", Owner.MEMORY, [
                        {"kind": getattr(h, "kind", ""), "text": getattr(h, "text", ""),
                         "score": round(float(getattr(h, "score", 0.0)), 4)}
                        for h in hits]))
                info["memory"] = {"hits": len(hits)}
            except Exception as exc:                           # noqa: BLE001
                info["memory"] = {"hits": 0, "error": f"{type(exc).__name__}"}
        if self.skill_rt is not None:
            state = sync_skill_state(ctx, self.skill_rt, goal)
            info["skills"] = {
                "index_lines": len(str(state.get("index", "")).splitlines()),
                "loaded": [x.get("name") for x in (state.get("loaded") or [])],
                "truncated": bool(state.get("truncated")),
            }
        return info

    def after_turn(self, ctx, *, turn: dict | None = None, principal=None):
        """回合结束后：记忆写回 + 历史压缩。

        顺序有讲究：**先写记忆再压缩**。压缩会裁掉旧消息，先压缩就等于
        把要抽取的原文丢了 —— 抽出来的东西会比实际少，而且不报错。
        """
        out: dict[str, Any] = {}
        summary_result = None
        if self.memory is not None and turn and principal is not None:
            try:
                from contracts import Subject
                subject = Subject(tenant=getattr(principal, "tenant", "") or "",
                                  user_id=getattr(principal, "user_id", "") or "",
                                  team=getattr(principal, "team", "") or "",
                                  project=getattr(principal, "project", "") or "")
                recs = self.memory.write(turn, subject)
                out["memory_written"] = len(recs)
            except Exception as exc:                           # noqa: BLE001 - fail-open
                out["memory_written"] = 0
                out["memory_error"] = f"{type(exc).__name__}"
        if self.compressor is not None:
            summary_result = maybe_compress_history(ctx, self.compressor)
            if summary_result is not None:
                out["compressed"] = True

        # **后向兼容**：没有装 memory 时返回原契约（`SummaryResult | None`）。
        # 早期调用方（`demo/scenario_fullchain.py` 场景 10）读的是
        # `sr.compressed_count` 这种属性；改成一律返回 dict 会让它 AttributeError。
        # 装了 memory 才返回 dict（多了一条"写了几条记忆"的信息）。
        if self.memory is None:
            return summary_result
        if out:
            out["summary"] = summary_result
        return out or summary_result


# ══════════════════════════════════════════════════════════════
# ⑤ 语义召回接进 Memory（把 bigram 换成真 embedding）
# ══════════════════════════════════════════════════════════════
def semantic_memory(memory_cls, *, provider=None, **kw):
    """返回一个「把相似度换成语义向量」的 MemoryRuntime 子类实例。

    为什么用子类而不是改 `plane/memory.py`：
    那个模块是纯 bigram 的稳定实现（45 个用例在跑），而语义召回依赖外部网关。
    用组合保持两边独立：网关挂了降级回 bigram，召回不中断。

    关键细节（不这么做就是每条记忆一次 HTTP）：
    `_rank` 里先用 `prefetch()` 把「查询 + 本主体全部候选」一次批量预热，
    之后 `_score` 的逐条 `sim()` 全部命中缓存。
    """
    from plane import memory as mem_mod
    from plane.semantic import SemanticSimilarity

    class SemanticMemory(memory_cls):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.sim = SemanticSimilarity(provider)
            self._query = ""

        def _rank(self, query, subject, k, budget_tokens, activate):
            self._query = query or ""
            try:
                texts = [self._query] + [r.text for r in self.provider.scan(subject)]
                self.sim.prefetch(texts)
            except Exception:                                   # noqa: BLE001
                pass                                            # 预热失败就走逐条降级
            return super()._rank(query, subject, k, budget_tokens, activate)

        def _score(self, rec, query_bg, t):
            base = super()._score(rec, query_bg, t)
            # 把 bigram 那一项换成语义项，其余（重要度/时效）保持原样
            old = mem_mod.W_SIMILARITY * mem_mod.similarity(query_bg, mem_mod._bigrams(rec.text))
            new = mem_mod.W_SIMILARITY * self.sim.sim(self._query, rec.text)
            return base - old + new

        def semantic_stats(self) -> dict:
            return self.sim.stats()

    return SemanticMemory(**kw)


# ══════════════════════════════════════════════════════════════
# ⑥ 沙箱后端接进 Tool Plane
# ══════════════════════════════════════════════════════════════
def guarded_sandbox_handler(*, selector=None, policy=None, cwd: str | None = None):
    """产出 `register_sandbox` 能用的 handler，但执行走 BackendSelector。

    这样「隔离强度」变成可配置的能力：本机只有 subprocess 就跑 process 级，
    企业环境有 nsjail 就自动升到 kernel 级；策略要求更高时 fail-closed。
    """
    from plane.sandbox_backends import BackendSelector, run_guarded
    from contracts import SandboxPolicy

    sel = selector or BackendSelector()
    pol = policy or SandboxPolicy()

    def handler(args: dict, e: ExecCtx) -> dict:
        argv = (args or {}).get("argv")
        backend = sel.pick()
        return run_guarded(backend, argv, policy=pol,
                           cwd=(args or {}).get("cwd") or cwd)

    handler.backend_report = sel.report        # 便于审计"当前实际隔离强度"
    return handler


# ══════════════════════════════════════════════════════════════
# ⑦ 持久化选型（FileStore / SQLiteStore 同一协议）
# ══════════════════════════════════════════════════════════════
def make_store(kind: str = "sqlite", *, path: str = "", root: str = ""):
    """按名字造一个 `ContextStore`。

    * `sqlite` —— **默认**：事务、WAL、并发安全，能按租户/时间查询。
    * `file`   —— 零依赖兜底：原子写，但无并发保护、无查询能力。
    * `memory` —— 测试用。
    """
    if kind == "sqlite":
        from core.sqlite_store import SQLiteStore
        return SQLiteStore(path or "agent_runtime.db")
    if kind == "file":
        from core.store import FileStore
        return FileStore(root or "/tmp/agent-runtime-store")
    if kind == "memory":
        from core.store import MemoryStore
        return MemoryStore()
    raise ValueError(f"未知 store 类型 {kind!r}（可选 sqlite / file / memory）")
