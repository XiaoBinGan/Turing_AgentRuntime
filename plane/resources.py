"""plane/resources.py — MCP Resources：**只读数据，URI 寻址**。

================================================================================
为什么需要这一层：`read_only` 需要一个标准载体，而不是每个工具各自约定参数
================================================================================

以前「只读工具」靠 `ToolManifest.side_effect == read_only` 手工标注：
`read_file(path=...)`、`read_db(sql=...)`、`read_git(diff=...)` 三套参数形状，
缓存要写三份，授权要写三份，跨实现无法互认。MCP 的 Resources 恰好提供了
标准载体：**有 URI 就能寻址、缓存、授权**，标注成本归零。

所以本模块只有两个东西：

    ResourceRegistry   uri → (ResourceRef, reader)   登记与查询，纯数据结构
    ResourceRuntime    read(uri, ExecCtx) → dict     执行顺序即安全语义

--------------------------------------------------------------------------------
`read` 的执行顺序为什么是这个顺序（**不许调换**）
--------------------------------------------------------------------------------

    1. URI 形状校验      scheme://path，否则 ResourceError
    2. 查注册表          未注册 → ResourceError("unknown resource")
    3. 授权（fail-closed） principal 需要 `resource.read` 或 `resource.<scheme>`
    4. 缓存命中          命中 → cached=True（**仍然过第 8 步护栏**）
    5. 调 reader         抛异常 → 包成 ResourceError（消息带 uri）
    6. 大小上限          超限 → 截断 + truncated=True（**不抛错**）
    7. 写缓存            LRU，最多 cache_size 条
    8. 输出护栏          governance.before_output → 掩码后的内容

逐条说明「为什么不能换」：

* **1 必须在 2 之前**：形状校验是授权模型的前提。没有 scheme 的 URI
  （`/etc/passwd`、`users/schema`）没有 scope 挂载点，先查表就会让「形状非法」
  和「未注册」混成同一个错误码，运维拿不到可区分的信号。
* **2 必须在 3 之前**：授权需要知道 `scheme`。查不到 ref 就取不到 scheme，
  也就无法判定该要哪个 scope —— 顺序反过来只能猜 scheme，猜就等于白送权限。
* **3 必须在 4 之前**：**缓存不是权限的旁路**。已经缓存过的 URI 对无权限主体
  同样必须拒绝；否则「先借别人的身份读一次预热，再用低权限身份拿缓存」就是
  一条现成的越权链。
* **3 必须在 5 之前**：授权先于任何真实副作用。reader 可能真的去打数据库、
  读文件、发 HTTP —— 先授权再读，未授权主体连一次 reader 调用都不该触发。
* **4 必须在 5 之前**：缓存的唯一目的就是省掉 reader。反过来毫无意义。
* **5 必须在 6 之前**：不知道内容就无从截断；而截断是读取后处理，不是读取前的
  准入条件（否则大资源会变成「读不到」，把可恢复的降级变成硬失败）。
* **6 必须在 7 之前**：缓存要存**截断后**的内容。若先缓存再截断，缓存里会留着
  全量内容，后面每次命中都要重复截断，且 `cache_size` 的字节成本失控。
* **6 不抛错、7/8 继续**：截断是**可恢复降级**（模型拿到头部仍然有用），
  与「未注册 / 无权限」的硬失败不同级。标 `truncated=True` 让上层决定是否
  换一种取数方式（分页读取），而不是替它决定「干脆不给」。
* **8 最后**：护栏必须作用在**最终出口字符串**上。若在 5 之前包 reader 结果，
  第 6 步截断可能把掩码截成半截（`***RED…`），反而制造出新的可疑片段；
  若把护栏放到缓存之后就别想了 —— 缓存会把未脱敏的原文发给下一个调用方。
* **8 对缓存命中也生效**：第 4 步命中后跳过 5~7，但**不跳过 8**。缓存存的是
  「已截断、未脱敏」的原文，脱敏**每次出口都重算**。否则「第一次读被掩码，
  第二次读从缓存拿到原文」就是自建的泄漏通道。

--------------------------------------------------------------------------------
fail-closed 的落点
--------------------------------------------------------------------------------

第 3 步的任何内部异常（`principal` 是 None、`scopes` 属性抛错、形状不可识别）
都收敛成**拒绝**，绝不 `return True`。同理第 8 步：护栏不可用、返回 None、
`allowed=False`，一律拒绝读取（`ResourceError`）。

设计约束：
  * 只读 `contracts.py`，不 import 其他 plane 模块（并行施工互不依赖）；
  * `ResourceRef` / `NamedPrompt` 的形状归契约，本模块只做 Registry + Runtime；
  * 缓存与统计全部进程内，不做持久化（v1 的 Context 已够重，资源缓存属瞬时）。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterator

from contracts import ExecCtx, GuardVerdict, Principal, ResourceRef

#: 默认读上限（与 `ResourceRef.max_bytes` 的默认值一致）
DEFAULT_MAX_BYTES = 262144
#: 默认缓存条数
DEFAULT_CACHE_SIZE = 128

#: URI 形状：`scheme://path`，scheme 走 RFC 3986 的字符集
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://\S+$")


# ══════════════════════════════════════════════════════════════
# 错误
# ══════════════════════════════════════════════════════════════
class ResourceError(Exception):
    """资源读取失败（形状非法 / 未注册 / reader 失败 / 护栏拦截）。"""


class ResourcePermissionError(ResourceError):
    """授权不通过。**是 ResourceError 的子类**：调用方只捕获基类也不会漏掉拒绝。"""


# ══════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════
def is_valid_uri(uri: Any) -> bool:
    """URI 必须是 `scheme://path` 形式。

    刻意不接受裸路径（`/etc/passwd`）与相对路径（`users/schema`）：没有 scheme
    就没有 scope 的挂载点，第 3 步的授权无从谈起 —— 形状校验不是格式洁癖，
    它是授权模型的前提。
    """
    if not isinstance(uri, str):
        return False
    return bool(_URI_RE.match(uri))


def scheme_of(uri: str) -> str:
    """取 scheme；形状非法时返回空串（**不抛错**：本函数只做解析）。"""
    if not isinstance(uri, str) or "://" not in uri:
        return ""
    return uri.split("://", 1)[0]


def _scope_satisfies(scopes: set[str], required: str) -> bool:
    """`required` 是否被 `scopes` 覆盖。覆盖关系由宽到窄：`*` > `<家族>.*` > 精确。

    与 plane/gate.py / plane/governance.py 同一套语义（本模块不 import 它们，
    并行施工要求互不依赖；语义保持一致靠测试，不靠耦合）。
    """
    if not required:
        return True
    if "*" in scopes or required in scopes:
        return True
    if "." in required:
        family = required.split(".", 1)[0]
        if f"{family}.*" in scopes:
            return True
    return False


def _as_text(raw: Any) -> str:
    """reader 的返回值归一成 `str`。bytes 按 UTF-8 解，其它类型 `str()`。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return str(raw)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    """按**字节**截断，且不切断多字节字符。

    `errors="ignore"` 让被切断的半个汉字安静消失，而不是变成 U+FFFD ——
    模型看到的应该是「内容变短」，不是「文件坏了」。
    """
    if limit <= 0:
        return text, False
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", "ignore"), True


# ══════════════════════════════════════════════════════════════
# Registry：登记与查询（纯数据，无 IO、无授权）
# ══════════════════════════════════════════════════════════════
class ResourceRegistry:
    """uri → (ResourceRef, reader)。

    **重复注册直接 ValueError，不静默覆盖**：两个模块都想拥有 `db://schema/users`
    时，静默覆盖的后果是「谁后启动谁说话」—— 测试环境里接着跑，生产里就是
    一方的数据被另一方的实现悄悄替换，且没有任何痕迹。宁可启动即炸。

    reader 的签名刻意只有 `(uri) -> str`：**不传 ExecCtx**。
    授权、缓存、截断、护栏全部在 Runtime 侧收口；reader 是无知的取数函数，
    这样第三方实现（文件 / git / db / http）不需要认识本 Runtime 的任何类型。
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[ResourceRef, Callable[[str], str]]] = {}
        #: W25 并发加固：`register()` 的「查重 → 插入」是经典的 check-then-act。
        #: 少了这把锁，两个线程可以同时通过查重，然后互相覆盖 —— 恰好绕过本类
        #: 「重复注册直接 ValueError，不静默覆盖」这条唯一的纪律。
        #: 用 RLock 是因为 `list()` 会在持锁时再次读 `_items`（语义上无嵌套需求，
        #: 但可重入能防住将来有人把 `__iter__` 塞进 `list()` 的实现里）。
        self._lock = threading.RLock()

    # ── 写 ──────────────────────────────────────────────────
    def register(self, ref: ResourceRef, reader: Callable[[str], str]) -> None:
        if not isinstance(ref, ResourceRef):
            raise TypeError(
                f"ref 必须是 contracts.ResourceRef，收到 {type(ref).__name__}")
        if not callable(reader):
            raise TypeError(f"reader 必须可调用，收到 {type(reader).__name__}")
        if not is_valid_uri(ref.uri):
            # fail fast：坏 URI 不许进表，否则 Runtime 第 1 步的形状校验形同虚设，
            # 而 list()/list_visible() 还会把一个永远读不到的条目报给上层。
            raise ValueError(
                f"非法 resource uri {ref.uri!r}：必须是 scheme://path 形式")
        with self._lock:                     # 查重与插入必须在同一临界区
            if ref.uri in self._items:
                raise ValueError(
                    f"resource 已注册，拒绝覆盖：{ref.uri}")
            self._items[ref.uri] = (ref, reader)

    def unregister(self, uri: str) -> bool:
        """注销成功返回 True，本来就没有返回 False（不抛错：幂等清理）。"""
        with self._lock:
            return self._items.pop(uri, None) is not None

    # ── 读 ──────────────────────────────────────────────────
    def get(self, uri: str) -> tuple[ResourceRef, Callable[[str], str]] | None:
        with self._lock:
            return self._items.get(uri)

    def list(self, scheme: str | None = None) -> list[ResourceRef]:
        """按 uri 排序（确定性）；`scheme` 给定时只返回该 scheme 的条目。

        持锁只到「把 ref 抄出来」为止，排序在锁外做：`sorted()` 会调
        `ref.uri` 比较（可能是任意实现），把它关进临界区只会扩大锁的
        影响面，而不会让它更正确。
        """
        with self._lock:
            refs = [ref for ref, _reader in self._items.values()]
        if scheme is not None:
            refs = [r for r in refs if r.scheme() == scheme]
        return sorted(refs, key=lambda r: r.uri)

    def schemes(self) -> list[str]:
        """已登记的 scheme 清单（排序、去重）。给上层做「有哪些面可读」的展示用。"""
        with self._lock:
            refs = [ref for ref, _ in self._items.values()]
        return sorted({r.scheme() for r in refs if r.scheme()})

    # ── 协议糖 ──────────────────────────────────────────────
    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, uri: object) -> bool:
        with self._lock:
            return isinstance(uri, str) and uri in self._items

    def __iter__(self) -> Iterator[ResourceRef]:
        return iter(self.list())


# ══════════════════════════════════════════════════════════════
# Runtime：执行顺序即安全语义
# ══════════════════════════════════════════════════════════════
class ResourceRuntime:
    """读资源：形状 → 注册表 → 授权 → 缓存 → reader → 截断 → 缓存 → 护栏。

    构造参数：
      registry           登记表
      governance         可选；有 `before_output` / `output_guard.scan` / `scan`
                         任一时启用输出护栏。**注入即启用，未注入即不过滤**
                         （v1 的取舍：本模块不自己造第二条脱敏清单，
                         否则 Runtime 和 Governance 会出现两份会漂移的规则）。
      cache_size         LRU 上限条数；0 = 关闭缓存
      max_bytes_default  全局上限；与 `ref.max_bytes` 取 **min**，
                         即「声明较小的那个说话」，全局上限只能更严，不能更松
    """

    def __init__(self, registry: ResourceRegistry, *, governance: Any = None,
                 cache_size: int = DEFAULT_CACHE_SIZE,
                 max_bytes_default: int = DEFAULT_MAX_BYTES) -> None:
        if cache_size < 0:
            raise ValueError(f"cache_size 不能为负：{cache_size}")
        self.registry = registry
        self.governance = governance
        self.cache_size = cache_size
        self.max_bytes_default = max_bytes_default
        #: LRU：OrderedDict 头冷尾热
        self._cache: OrderedDict[str, tuple[str, bool]] = OrderedDict()
        #: 观测计数（Quality/Trace 可读）。拒绝是安全信号，必须可观测。
        self.stats: dict[str, int] = {
            "reads": 0, "hits": 0, "misses": 0, "truncated": 0,
            "invalid_uri": 0, "unknown": 0, "denied": 0, "reader_errors": 0,
            "redacted": 0, "blocked": 0,
        }
        #: W25 并发加固：保护 `_cache`（LRU）与 `stats`（计数）。
        #:
        #: 为什么必须锁：`OrderedDict.get` + `move_to_end` 是两步；两步之间被别的
        #: 线程 `popitem(last=False)` 掉，`move_to_end` 就会抛 `KeyError` —— 一个有
        #: 缓存的读接口在并发下随机 500，且只在 cache_size 被打满时出现（最难复现
        #: 的那一类）。`stats["hits"] += 1` 同理是读-改-写，会丢计数；而 `hits`
        #: 与 `misses` 合计必须等于 `reads`，否则运维从计数里读出来的命中率是假的。
        #:
        #: 刻意**不**把 reader / governance 护栏放进临界区：它们可能打数据库、
        #: 发 HTTP、跑正则。缓存锁只保护"记账"和"表结构"，保护越少死锁面越小。
        self._lock = threading.RLock()

    # ── 主入口 ──────────────────────────────────────────────
    def read(self, uri: str, e: ExecCtx) -> dict:
        """读一个资源。返回 `{"uri","mime","content","bytes","cached","truncated"}`。"""
        self._bump("reads")

        # 1) URI 形状校验 —— 授权模型的前提，必须最先
        if not is_valid_uri(uri):
            self._bump("invalid_uri")
            raise ResourceError(
                f"非法 resource uri {uri!r}：必须是 scheme://path 形式")

        # 2) 查注册表 —— 取 ref（也就取到了 scheme）才能谈授权
        entry = self.registry.get(uri)
        if entry is None:
            self._bump("unknown")
            raise ResourceError(f"unknown resource: {uri}")
        ref, reader = entry

        # 3) 授权 —— fail-closed；先于缓存、先于 reader
        principal = getattr(e, "principal", None)
        if not self._authorized(principal, ref):
            self._bump("denied")
            user = getattr(principal, "user_id", "?") if principal is not None else "None"
            raise ResourcePermissionError(
                f"principal {user} 缺少读取 {uri} 的 scope"
                f"（需要 resource.read 或 resource.{ref.scheme()}）")

        # 4) 缓存命中 —— 命中则跳过 5~7，但**不跳过 8**（护栏每次出口都算）
        if self.cache_size > 0:
            with self._lock:                 # get + move_to_end 必须原子（见 _lock 注释）
                cached = self._cache.get(uri)
                if cached is not None:
                    self._cache.move_to_end(uri)
                    self._bump("hits")
                else:
                    cached = None
            if cached is not None:
                content, truncated = cached
                return self._deliver(ref, content, truncated, cached=True)

        self._bump("misses")

        # 5) 调 reader —— 异常包一层，消息必须带 uri（否则 N 个资源共用一条日志）
        try:
            raw = reader(uri)
        except Exception as exc:
            self._bump("reader_errors")
            raise ResourceError(
                f"reader failed for resource {uri}: {type(exc).__name__}: {exc}") from exc
        content = _as_text(raw)

        # 6) 大小上限 —— 超限截断，**不抛错**（可恢复降级 ≠ 硬失败）
        limit = self._limit_for(ref)
        content, truncated = _truncate_bytes(content, limit)
        if truncated:
            self._bump("truncated")

        # 7) 写缓存（存截断后的原文；脱敏在出口算，不进缓存）
        self._cache_put(uri, content, truncated)

        # 8) 输出护栏
        return self._deliver(ref, content, truncated, cached=False)

    # ── 可见性 ──────────────────────────────────────────────
    def list_visible(self, principal: Principal) -> list[ResourceRef]:
        """按 scope 过滤后的资源清单（排序）。principal 为 None 时返回空列表。

        这是「模型/子 Agent 能看到什么」的唯一入口：**看不见就不该被寻址**，
        否则模型会拿一个读不了的 URI 反复试错，把失败次数耗在权限而不是任务上。
        """
        refs = self.registry.list()          # 已按 uri 排序，过滤不破坏顺序
        return [ref for ref in refs if self._authorized(principal, ref)]

    # ── 缓存管理 ────────────────────────────────────────────
    def invalidate(self, uri: str) -> bool:
        """清掉某个 uri 的缓存。返回是否真的清掉了。"""
        if self.cache_size <= 0:
            return False
        with self._lock:
            return self._cache.pop(uri, None) is not None

    def clear_cache(self) -> int:
        """清空缓存，返回清掉的条数。"""
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
        return n

    def is_cached(self, uri: str) -> bool:
        with self._lock:
            return uri in self._cache

    def cache_len(self) -> int:
        with self._lock:
            return len(self._cache)

    # ── 内部 ────────────────────────────────────────────────
    def _bump(self, name: str, n: int = 1) -> None:
        """计数 +n（锁内）。`stats[name] += 1` 是读-改-写，并发下会丢计数。"""
        with self._lock:
            self.stats[name] = self.stats.get(name, 0) + n

    def _limit_for(self, ref: ResourceRef) -> int:
        """min(ref.max_bytes, max_bytes_default)；非正数按「不限」处理。"""
        declared = ref.max_bytes if isinstance(ref.max_bytes, int) else DEFAULT_MAX_BYTES
        if declared <= 0 or self.max_bytes_default <= 0:
            return 0
        return min(declared, self.max_bytes_default)

    def _cache_put(self, uri: str, content: str, truncated: bool) -> None:
        if self.cache_size <= 0:
            return
        with self._lock:
            # 写入 + 移到表尾 + 淘汰，三步是一件事：中间被别的线程插进来，
            # 淘汰就会按过期的长度做，cache_size 变成「大约」的上限。
            self._cache[uri] = (content, truncated)
            self._cache.move_to_end(uri)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)     # LRU：淘汰最久未用（表头）

    def _authorized(self, principal: Any, ref: ResourceRef) -> bool:
        """scope 判定。**任何内部异常 → False**（fail-closed）。"""
        try:
            if principal is None:
                return False
            raw_scopes = getattr(principal, "scopes", None)
            if raw_scopes is None:
                return False
            scopes = {str(s) for s in raw_scopes}
            if not scopes:
                return False
            scheme = ref.scheme()
            for required in ("resource.read", f"resource.{scheme}"):
                if _scope_satisfies(scopes, required):
                    return True
            return False
        except Exception:
            # 异常路径里绝不 return True
            return False

    def _deliver(self, ref: ResourceRef, content: str, truncated: bool,
                 *, cached: bool) -> dict:
        """最终出口：过护栏 → 组装返回结构。**所有**出口都走这里。"""
        text = self._guard(ref, content)
        return {
            "uri": ref.uri,
            "mime": ref.mime,
            "content": text,
            "bytes": _byte_len(text),
            "cached": cached,
            "truncated": truncated,
        }

    def _guard(self, ref: ResourceRef, content: str) -> str:
        """输出护栏。未注入 governance → 原样返回；注入了但形状不认识 → 拒绝。

        认不出形状就拒绝，绝不「没有护栏就当干净」：读出来的数据同样是外泄面
        （数据库里存着别人的 `sk-`，读一次就等于把密钥搬进 Prompt）。
        """
        g = self.governance
        if g is None:
            return content
        try:
            verdict = self._scan(g, content)
            if verdict is None:
                raise ResourceError(
                    f"governance 返回 None，拒绝放行 {ref.uri}（fail-closed）")
            if not verdict.allowed:
                self._bump("blocked")
                raise ResourceError(
                    f"governance 拒绝放行 {ref.uri}：{verdict.reason}")
            payload = verdict.payload
            if isinstance(payload, str) and payload != content:
                self._bump("redacted")
                return payload
            return content
        except ResourceError:
            raise
        except Exception as exc:
            self._bump("blocked")
            raise ResourceError(
                f"governance failed closed for {ref.uri}: "
                f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _scan(governance: Any, text: str) -> GuardVerdict:
        """兼容三种护栏形状：`before_output` / `output_guard.scan` / `scan`。"""
        hook = getattr(governance, "before_output", None)
        if callable(hook):
            return hook(text)
        guard = getattr(governance, "output_guard", None)
        scan = getattr(guard, "scan", None)
        if callable(scan):
            return scan(text)
        scan = getattr(governance, "scan", None)
        if callable(scan):
            return scan(text)
        raise TypeError(
            f"governance 形状不可识别（{type(governance).__name__}）："
            "需要 before_output / output_guard.scan / scan 之一")
