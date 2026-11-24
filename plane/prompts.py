"""plane/prompts.py — MCP Prompts：**工程化模板**，版本化、可注入参数。

================================================================================
为什么 Prompt 要独立成一层，而不是散在代码里的 f-string
================================================================================

散装 Prompt 的四个典型现场问题，全是「没有注册表」造成的：

  1. **改不了**：一句措辞埋在 300 行 loop 代码里，改文案要发版；
     → 注册表让 Prompt 成为**配置**，进配置中心。
  2. **对不齐**：同一个「代码评审」模板在三个 Agent 里各写一份，慢慢漂移；
     → 同名寻址（`prompt://review`），一处生效。
  3. **测不了**：模板渲染结果没有稳定入口，只能跑整条链路看输出像不像；
     → `render(name, **kw)` 是纯函数，可快照测试。
  4. **接不上工具契约**：模板里的 `{var}` 和调用方传的 kwargs 对不上，
     运行时才发现少了一个 `{language}`，模型拿到一个字面量花括号。
     → `validate(prompt)` 在装载期就把「未声明的占位符」列出来。

--------------------------------------------------------------------------------
三条刻意的设计取舍
--------------------------------------------------------------------------------

**(1) 只替换声明的 arguments，其余 `{...}` 原样保留**

模板里出现 JSON 是常态：

    {"tool": "search", "query": "{query}", "limit": 10}

用 `str.format(**kw)` 会在 `{"tool"` 上直接炸（`KeyError: '"tool"'`），
或者更糟：调用方为了让 format 通过而塞一堆假 key，把真实占位符淹掉。
所以渲染规则是**白名单替换**：只有 `arguments` 里登记的名字会被换成值，
`{"tool"`、`{ }`、`{1,2}` 这些一律原样留着 —— 它们是内容，不是参数。

**(2) 未传的 argument → 空串，不抛错**

模板容错优先。理由是故障模式不对称：
  * 少一个参数的后果：模型看到 `Language: `（空），仍然能干活，人也能看出缺了什么；
  * 抛错的后果：整条链路 TermReason=UNRECOVERABLE，用户什么也拿不到。
一次可降级的输入缺失，不该升级成一次任务失败。**要严格，用 `validate` 在装载期
拦，而不是在每轮渲染时炸。**

**(3) 同名注册即覆盖，「版本」用 description 记录，不硬加字段**

`NamedPrompt` 是冻结契约，没有 `version` 字段。硬加一个字段意味着：
契约漂移、所有构造点要改、序列化格式要分叉。而「后注册的生效」这一条
已经足够表达配置中心的语义（配置中心本身就是「最后一次发布的版本说话」）。
版本信息往 `description` 里写（"v2: 加入 diff 上下文"），既能审计，又不改契约。

设计约束：
  * 只读 `contracts.py`，不 import 任何其他 plane 模块（并行施工互不依赖）；
  * `render` / `validate` 共用**一条**占位符正则（`_PLACEHOLDER_RE`），
    所以「validate 报未声明」与「render 不替换」永远是同一个判断；
    渲染语义与 `contracts.NamedPrompt.render` 保持兼容（只替换声明项、缺参补空串），
    额外认 `{name!r}` / `{name:>8}` 这类格式化形态。
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from contracts import NamedPrompt

#: 占位符语法：`{name}` / `{user.name}`，可选 `!r` 转换与 `:spec` 格式说明。
#: 刻意**不**匹配 `{"key": 1}`（后面紧跟引号）与 `{}`（空）—— 它们是内容。
#:
#: **渲染与校验共用这一条正则**：`validate` 说「这个占位符没声明」，
#: `render` 就一定不会替换它；反过来，声明过的 `{name:>8}` 也一定会被替换。
#: 两处语法分叉过一次就会永远分叉，所以只留一份定义。
_PLACEHOLDER_RE = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:(![rsa]))?"
    r"(?::([^{}]*))?\}"
)


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class PromptError(Exception):
    """Prompt 不存在 / 渲染失败。"""


# ══════════════════════════════════════════════════════════════
# 渲染（白名单替换）
# ══════════════════════════════════════════════════════════════
def _render_template(template: str, arguments: tuple[str, ...] | list[str],
                     kwargs: dict[str, Any]) -> str:
    """只替换 `arguments` 里登记的名字；其余 `{...}` 是内容，原样保留。

    未传的 argument → `str(kwargs.get(name, ""))` 即空串（与
    `contracts.NamedPrompt.render` 同语义），不抛错。
    """
    declared = {str(a) for a in (arguments or ())}
    if not declared:
        # 没声明任何参数 → 一个占位符都不动（与契约 render 行为一致）
        return template

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in declared:
            return m.group(0)          # 白名单外：原样保留（JSON 花括号靠这条活下来）
        value = kwargs.get(name, "")
        conv, spec = m.group(2), m.group(3)
        if conv == "!r":
            return repr(value)
        if conv == "!a":
            return ascii(value)
        if spec:
            return format(value, spec)
        return str(value)

    return _PLACEHOLDER_RE.sub(_sub, template)


# ══════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════
class PromptRegistry:
    """name → NamedPrompt。装载期校验、运行期渲染、任意时刻列举。

    与 `ResourceRegistry` 的差异值得说清：
      * Resource 的 key 是 **URI**（外部世界的地址），重复注册是冲突 → ValueError；
      * Prompt 的 key 是 **名字**（本地的配置项），重复注册是**发版** → 覆盖。
    同一个动作（register），两种语义，因为一个是「发现」一个是「发布」。
    """

    def __init__(self) -> None:
        self._items: dict[str, NamedPrompt] = {}

    # ── 写 ──────────────────────────────────────────────────
    def register(self, prompt: NamedPrompt) -> None:
        """注册。同名即覆盖（后注册的生效）；相同对象重复注册是幂等空操作。"""
        if not isinstance(prompt, NamedPrompt):
            raise TypeError(
                f"prompt 必须是 contracts.NamedPrompt，收到 {type(prompt).__name__}")
        if not isinstance(prompt.name, str) or not prompt.name.strip():
            raise ValueError(f"prompt 必须有非空 name，收到 {prompt.name!r}")
        if not isinstance(prompt.template, str):
            raise ValueError(
                f"prompt {prompt.name!r} 的 template 必须是 str，"
                f"收到 {type(prompt.template).__name__}")
        self._items[prompt.name] = prompt

    def unregister(self, name: str) -> bool:
        return self._items.pop(name, None) is not None

    # ── 读 ──────────────────────────────────────────────────
    def get(self, name: str) -> NamedPrompt | None:
        return self._items.get(name)

    def list(self) -> list[NamedPrompt]:
        """按 name 排序（确定性 —— 注入 Prompt 清单时顺序不能随哈希抖动）。"""
        return [self._items[name] for name in sorted(self._items)]

    def names(self) -> list[str]:
        return sorted(self._items)

    # ── 渲染 / 校验 ─────────────────────────────────────────
    def render(self, name: str, **kwargs: Any) -> str:
        """渲染。不存在 → PromptError；参数缺失 → 空串（不抛错）。"""
        prompt = self.get(name)
        if prompt is None:
            raise PromptError(f"unknown prompt: {name}")
        try:
            return _render_template(prompt.template, prompt.arguments, kwargs)
        except PromptError:
            raise
        except Exception as exc:      # 格式化说明非法之类的输入错误
            raise PromptError(
                f"prompt {name!r} 渲染失败：{type(exc).__name__}: {exc}") from exc

    def validate(self, prompt: NamedPrompt) -> list[str]:
        """返回**未声明的占位符**列表（排序去重，空列表＝通过）。

        只查「模板要、声明没有」这一个方向。反向（声明了但模板没用）不报错：
        多声明一个参数是常见的向下兼容手法，不该被判成错误。
        """
        try:
            declared = {str(a) for a in (prompt.arguments or ())}
            found = [m.group(1) for m in _PLACEHOLDER_RE.finditer(prompt.template or "")]
        except Exception as exc:
            raise PromptError(
                f"prompt {getattr(prompt, 'name', '?')!r} 无法校验："
                f"{type(exc).__name__}: {exc}") from exc
        return sorted({name for name in found if name not in declared})

    def validate_registered(self, name: str) -> list[str]:
        """便捷版：直接校验注册表里的 prompt。不存在 → PromptError。"""
        prompt = self.get(name)
        if prompt is None:
            raise PromptError(f"unknown prompt: {name}")
        return self.validate(prompt)

    # ── 协议糖 ──────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._items

    def __iter__(self) -> Iterator[NamedPrompt]:
        return iter(self.list())
