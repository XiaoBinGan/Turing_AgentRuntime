"""auth.py — HTTP 层鉴权：把「请求头」翻译成 `Principal`（W32）。

================================================================================
它在主线里的位置
================================================================================

    client ──Authorization: ApiKey k──┐
            ──Authorization: Bearer t──┤─▶ AuthMiddleware ─▶ 端点 ─▶ Gateway
            ──X-API-Key: k─────────────┘         │
                                                 └─ request.state.principal

在 `serve.py` 之前，principal 只能由**请求体**给出（`serve.PrincipalBody`）：
客户端可以自称任何 user_id / tenant / scopes —— 那不是鉴权，是"报名"。本模块补上
「凭据 → 身份」这一段，并且**只**补这一段：

- 身份的**权限**判定（RBAC / scope）、配额、审计仍然在 Gateway（`core/gateway.py`
  ①~⑪）。两层各判一次会出现两个口径，而审计只知道其中一个 —— 本项目反复强调的纪律。
- 服务层只新增一条判断：「你说的你是谁」和「凭据证明你是谁」是否一致
  （`principal_conflict`）。这不是重新做 RBAC，而是防止**冒名**：带着自己的 token
  去操作别人的租户。一旦冲突一律 403，不做"以其中一方为准"的猜测。

================================================================================
为什么区分 401 与 403（这条边界是本模块最容易被写错的地方）
================================================================================

    401 Unauthorized   ← **没给**凭据（没 Authorization、没 X-API-Key）
                         客户端动作：去拿凭据，然后**原样重试**。
                         必须带 `WWW-Authenticate`：它告诉客户端"我要哪种凭据"
                         （RFC 9110 §15.5.2 / RFC 6750 §3）。

    403 Forbidden      ← **给了但不被接受**（key 不在表里、JWT 过期/签名错/算法不对/
                         iss/aud 不匹配、body principal 与凭据身份冲突）
                         客户端动作：换凭据或修请求，**重试同一个请求没有意义**。

    500                ← 服务端自己没配好（"密钥表为空且没配 JWT"）
                         它不是客户端的错，用 401 会让客户端反复去"取凭据"，
                         而真正该修的是部署方的环境变量。

把 401/403 混成一个码，客户端就没法区分"该刷新 token 还是该放弃"，只能统一做
"登录一次再重试"——那会在凭据确实无效时变成登录风暴。反过来把 403 报成 401，
浏览器/网关会自动弹认证框或触发重试，问题被掩盖成"用户没登录"。

================================================================================
默认策略：默认拒绝
================================================================================

- `Authenticator()` 什么都不配 → `authenticate()` 返回 `auth_not_configured`，
  **绝不**因为"没配密钥"就默默 `allow_anonymous`（那是最典型的静默降级）。
- `install_auth()` 默认保护**除 `exempt_paths` 之外的所有路径**（allowlist 风格）。
  `exempt_paths` 默认只有 `/healthz`：liveness 要由编排器无条件探活，401 会让它
  判定"进程死了"并**重启**，把最后要跑的 drain/flush 直接砍掉。
- `/readyz` 与 `/metrics` **默认要鉴权**：前者暴露依赖拓扑（信息泄露），后者暴露
  租户/任务维度的业务量（竞争情报）。要让它们公开必须**显式**写下来
  （`exempt_paths=("/healthz", "/readyz")` / `metrics_requires_auth=False`）。

================================================================================
三种凭据方式
================================================================================

1. `Authorization: ApiKey <key>` 或 `X-API-Key: <key>` → 查 `api_keys` 表。
   比较走 `hmac.compare_digest`（字节级常量时间），且**遍历全表**不提前 break：
   提前 return 会把"第几个 key 命中"泄漏成时序差。
2. `Authorization: Bearer <jwt>` → PyJWT 校验（签名 / exp / nbf / iss / aud / 算法白名单），
   从 claims 取 `sub`→user_id、`tenant`、`roles`、`scope`（空格分隔，OAuth2 惯例）。
3. 都没有 → `allow_anonymous=True` 给匿名 principal（**空 scope**，fail-closed），
   否则 `missing_credentials`（HTTP 401）。

**失败原因里绝不出现密钥/密码的任何片段**：原因串由本模块自己拼（不回显凭据），
再经 `_scrub` 兜底（把密钥本体及其前后缀抹成 `[redacted]`），长度截断。
这一条比它看起来重要：`reason` 会进 HTTP body、日志、指标 label —— 任何一处都是泄漏面。

================================================================================
依赖与边界
================================================================================

- 只读 `contracts.Principal`（身份的形状），不 import `core/*` / `plane/*`。
- PyJWT 是**懒加载**：没装时服务照样起，JWT 方式返回 `auth_not_configured`
  （API key 方式不受影响）。不静默降级成"跳过校验"。
- `clock` 可注入：它**只**用于算 `retry_after_s`（离过期还有多久）。
  时间有效性（exp/nbf）由 PyJWT 用真实时钟判定 —— 两套时钟口径只会产生
  "测试里过期、生产里没过期"的诡异差异。
"""
from __future__ import annotations

import fnmatch
import hmac
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from contracts import Principal

__all__ = [
    "AuthResult",
    "Authenticator",
    "AuthMiddleware",
    "install_auth",
    "current_principal",
    "status_for_auth_failure",
    "reason_code",
    "parse_api_keys_spec",
    "parse_bool",
    "DEFAULT_EXEMPT_PATHS",
    "SCHEME_API_KEY",
    "SCHEME_JWT",
    "SCHEME_NONE",
    "SCHEME_METRICS_TOKEN",
    "WWW_AUTHENTICATE_REALM",
]


# ══════════════════════════════════════════════════════════════
# 一、常量：scheme / reason code / 默认豁免
# ══════════════════════════════════════════════════════════════
SCHEME_API_KEY = "api_key"
SCHEME_JWT = "jwt"
SCHEME_NONE = "none"
SCHEME_METRICS_TOKEN = "metrics_token"

#: 401 响应的挑战头里的 realm。**不含任何密钥**，只是命名空间。
WWW_AUTHENTICATE_REALM = "agent-runtime"

#: 默认豁免路径。**只有**存活探针：它失败会触发重启，重启会砍掉 drain/flush。
#: `/readyz`（依赖拓扑）与 `/metrics`（业务量）默认**不在**这里，见模块 docstring。
DEFAULT_EXEMPT_PATHS: tuple[str, ...] = ("/healthz",)

#: 失败原因码（reason 里冒号前的第一段）。用它做"401/403/500"的判定与指标 label，
#: 而不是解析人类可读文本 —— 文本会改，码不该改。
REASON_MISSING_CREDENTIALS = "missing_credentials"
REASON_INVALID_API_KEY = "invalid_api_key"
REASON_INVALID_CREDENTIALS = "invalid_credentials"
REASON_UNSUPPORTED_SCHEME = "unsupported_scheme"
REASON_SCHEME_NOT_CONFIGURED = "scheme_not_configured"
REASON_AUTH_NOT_CONFIGURED = "auth_not_configured"
REASON_AUTH_INTERNAL = "auth_internal_error"
REASON_PRINCIPAL_CONFLICT = "principal_conflict"
REASON_JWT_EXPIRED = "jwt_expired"
REASON_JWT_NOT_YET_VALID = "jwt_not_yet_valid"
REASON_JWT_ALG_REJECTED = "jwt_alg_rejected"
REASON_JWT_SIGNATURE_INVALID = "jwt_signature_invalid"
REASON_JWT_ISSUER_MISMATCH = "jwt_issuer_mismatch"
REASON_JWT_AUDIENCE_MISMATCH = "jwt_audience_mismatch"
REASON_JWT_MISSING_CLAIM = "jwt_missing_claim"
REASON_JWT_MALFORMED = "jwt_malformed"
REASON_JWT_CLAIMS_INVALID = "jwt_claims_invalid"

#: reason code → HTTP 状态。**只有三种**：
#:   没给凭据 → 401；给了不认 → 403；服务端没配好 → 500。其余默认 403（fail-closed）。
_STATUS_BY_REASON: dict[str, int] = {
    REASON_MISSING_CREDENTIALS: 401,
    REASON_SCHEME_NOT_CONFIGURED: 403,
    REASON_AUTH_NOT_CONFIGURED: 500,
    REASON_AUTH_INTERNAL: 500,
}
DEFAULT_AUTH_FAILURE_STATUS = 403

#: 挑战头的 scheme 顺序：按"最可能被用"的排在前面，客户端取第一个能用的
_CHALLENGE_SCHEME = {SCHEME_JWT: "Bearer", SCHEME_API_KEY: "ApiKey"}


def reason_code(reason: Any) -> str:
    """`"jwt_expired: 已过期"` → `"jwt_expired"`。空/None → `""`。"""
    text = str(reason or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip()


def status_for_auth_failure(result: "AuthResult") -> int:
    """`AuthResult(ok=False)` → HTTP 状态码（401 / 403 / 500）。

    只有 `missing_credentials` 是 401（"你没给"），`auth_not_configured` /
    `auth_internal_error` 是 500（"我没配好/我坏了"），其余一律 403
    （"你给了，但我不认"）—— 不认识的原因码默认 403，因为 401 会诱发客户端的
    自动重试/弹框，误判成本更高。
    """
    code = reason_code(getattr(result, "reason", ""))
    return _STATUS_BY_REASON.get(code, DEFAULT_AUTH_FAILURE_STATUS)


def parse_bool(value: Any, *, default: bool = False) -> bool:
    """环境变量式的布尔解析（`1/true/yes/on` 为真，其余为假）。None → default。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


# ══════════════════════════════════════════════════════════════
# 二、AuthResult：鉴权结果（成功与失败共用一种形状）
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AuthResult:
    """一次鉴权的结果。

    为什么成功/失败用同一个类型而不是 `raise`：`authenticate()` 是**每次请求都会
    走到**的热路径，异常在 Python 里既慢又容易被 `except Exception` 顺手吞掉
    （"吞掉 = 放行"是最坏的静默失败）。返回值强制调用方**显式**判断 `ok`。

    Fields:
        ok:            是否通过。
        principal:     通过时的身份；失败时必须是 None（不给出半成品身份）。
        scheme:        `"api_key"` / `"jwt"` / `"none"` / `"metrics_token"`。
        reason:        失败原因（`"<code>: <人类可读>"`），**不含密钥任何片段**。
        retry_after_s: 建议重试秒数（例如 JWT 即将过期时 = 剩余寿命；
                       客户端据此提前刷新，而不是等着收 403）。
    """

    ok: bool
    principal: Principal | None = None
    scheme: str = ""
    reason: str = ""
    retry_after_s: float = 0.0

    @property
    def code(self) -> str:
        """失败原因码；成功时为空串。"""
        return reason_code(self.reason) if not self.ok else ""

    def to_dict(self) -> dict:
        """进日志/自述用的安全形状（principal 用它的 `to_dict`）。"""
        return {
            "ok": bool(self.ok),
            "scheme": self.scheme,
            "reason": self.reason,
            "retry_after_s": float(self.retry_after_s),
            "principal": (self.principal.to_dict()
                          if isinstance(self.principal, Principal) else None),
        }


# ══════════════════════════════════════════════════════════════
# 三、小工具：脱敏 / 常量时间比较 / claim 归一
# ══════════════════════════════════════════════════════════════
def _fragments(secret: str) -> list[str]:
    """一个密钥里"必须被抹掉"的片段：本体 + 前后缀（长度 4..12）。

    为什么要前后缀：真实事故里泄漏的往往不是完整 key，而是日志截断后的前缀
    （`sk-live-abc…`）或错误信息里拼出来的尾部。截断长度上限 12 是权衡：
    再长的前缀很少出现在一行日志里，而遍历长度会让每次失败多几百次 replace。
    """
    text = str(secret or "")
    if not text:
        return []
    out = {text}
    limit = min(len(text), 12)
    for size in range(4, limit + 1):
        out.add(text[:size])
        out.add(text[-size:])
    return sorted(out, key=len, reverse=True)


def _apply_fragments(text: Any, fragments: Iterable[str]) -> str:
    """把给定的片段从文本里抹掉（`_fragments` 的结果可直接复用，避免重复计算）。"""
    out = str(text if text is not None else "").replace("\n", " ").replace("\r", " ")
    for frag in fragments:
        if frag and frag in out:
            out = out.replace(frag, "[redacted]")
    return out[:300]


def scrub(text: Any, secrets: Iterable[str] = ()) -> str:
    """把密钥/令牌片段抹成 `[redacted]`，并截断到 300 字符。

    这是**兜底**，不是主防线：本模块拼 reason 时就不回显凭据。真正需要它的是
    "第三方异常信息里意外带了凭据"这类情况（JWT 库、注入的 authenticator）。
    """
    fragments: list[str] = []
    for secret in secrets:
        fragments.extend(_fragments(str(secret)))
    return _apply_fragments(text, fragments)


def _constant_time_equals(left: Any, right: Any) -> bool:
    """字节级常量时间比较（防时序侧信道）。

    比较的是 UTF-8 字节而不是 `str`：`hmac.compare_digest` 对非 ASCII 的 `str`
    会抛 `TypeError`，而 API key 是客户端可控输入 —— 一个 `é` 就能把它变成 500。
    """
    try:
        return bool(hmac.compare_digest(str(left).encode("utf-8"),
                                        str(right).encode("utf-8")))
    except Exception:                   # noqa: BLE001 - 比较失败 = 不相等（fail-closed）
        return False


def _header(headers: Mapping[str, str] | Any, name: str) -> str:
    """大小写不敏感地取一个请求头的值（`Mapping` / Starlette `Headers` 都认）。"""
    if not headers:
        return ""
    wanted = str(name).strip().lower()
    try:
        items = list(headers.items())
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).strip().lower() == wanted:
            return str(value or "").strip()
    return ""


def _as_str_list(raw: Any) -> tuple[str, ...]:
    """把 claims 里的 roles/scopes 归一成字符串元组。

    OAuth2 的 `scope` 是**空格分隔字符串**，而很多 IdP 的 `roles` 是数组。
    两种写法都认；裸字符串按空格/逗号切（不把 `"tool.read tool.write"` 当成
    一个 scope —— 那会让 RBAC 永远 deny，且看上去像权限配错了）。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = raw.replace(",", " ").split()
        return tuple(part for part in (p.strip() for p in parts) if part)
    if isinstance(raw, (list, tuple, set, frozenset)):
        out: list[str] = []
        for item in raw:
            if item is None:
                continue
            if isinstance(item, str):
                out.extend(p for p in (x.strip() for x in
                                       item.replace(",", " ").split()) if p)
            else:
                out.append(str(item))
        return tuple(out)
    return (str(raw),)


def _as_principal(value: Any, *, where: str = "principal") -> Principal:
    """把 `Principal` / dict 归一成 `Principal`（配置期校验，不是请求期猜测）。"""
    if isinstance(value, Principal):
        principal = value
    elif isinstance(value, Mapping):
        user_id = str(value.get("user_id") or "").strip()
        tenant = str(value.get("tenant") or "").strip()
        if not user_id or not tenant:
            raise ValueError(f"{where} 必须同时给出非空 user_id 与 tenant（fail-closed）")
        principal = Principal(user_id=user_id, tenant=tenant,
                              roles=_as_str_list(value.get("roles")),
                              scopes=frozenset(_as_str_list(value.get("scopes"))))
    else:
        raise TypeError(f"{where} 必须是 Principal 或 Mapping，收到 {type(value).__name__}")
    if not str(principal.user_id or "").strip():
        raise ValueError(f"{where}.user_id 不能为空（fail-closed）")
    if not str(principal.tenant or "").strip():
        raise ValueError(f"{where}.tenant 不能为空（fail-closed）")
    return principal


def _jwt_module() -> Any:
    """懒加载 PyJWT。没装 → None（调用方自报 `auth_not_configured`，不静默跳过）。"""
    try:
        import jwt as _jwt             # noqa: PLC0415 - 故意延迟：不装也能起服务
        return _jwt
    except Exception:                   # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════════════
# 四、API key 表解析（`key:tenant:scopes[;...]`）
# ══════════════════════════════════════════════════════════════
def parse_api_keys_spec(spec: Any) -> dict[str, dict]:
    """解析 `AGENT_RUNTIME_API_KEYS`：`key:tenant:scopes`，条目用 `;` 或换行分隔。

    **只按前两个冒号切**（`split(":", 2)`）：scope 名本身带冒号
    （`runtime:run`），按 `:` 全切会把 scope 剁碎，第 4 段还会被误当成 user_id。
    这是这类"冒号分隔配置"最常见的坑，所以这里显式钉住。

    - `scopes` 用空格或逗号分隔；`*` 是合法 scope（RBAC 认）。
    - user_id 用 `key` 的**不可逆**别名 `svc_<sha256(key)[:8]`：能进审计/指标，
      又不会把密钥本体写进日志。要固定 user_id 可以写 `key:tenant:scopes#user_id`
      （`#` 不是 scope 的合法字符，选它做二级分隔符不会歧义）。

    返回 `{key: {"user_id", "tenant", "scopes"}}`（还是 dict，方便单测）。

    错误信息**只带序号与段数**，绝不回显原始条目：一条格式错误的配置行里就有
    半个密钥，而这条错误会进日志/启动失败的堆栈。
    """
    text = str(spec or "").strip()
    if not text:
        return {}
    out: dict[str, dict] = {}
    entries = [chunk.strip() for chunk in text.replace("\n", ";").split(";")]
    for index, entry in enumerate(entries, start=1):
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) < 3:
            raise ValueError(
                f"AGENT_RUNTIME_API_KEYS 第 {index} 项格式错误：需要 key:tenant:scopes，"
                f"实际只有 {len(parts)} 段")
        key = parts[0].strip()
        tenant = parts[1].strip()
        rest = parts[2].strip()
        scopes, _, user_id = rest.partition("#")
        key, tenant = key.strip(), tenant.strip()
        if not key or not tenant:
            raise ValueError(
                f"AGENT_RUNTIME_API_KEYS 第 {index} 项格式错误：key 与 tenant 都不能为空")
        if not scopes.strip():
            raise ValueError(
                f"AGENT_RUNTIME_API_KEYS 第 {index} 项格式错误：scopes 为空"
                f"（至少要给 runtime:run，否则这把 key 进不了运行时）")
        if key in out:
            raise ValueError(
                f"AGENT_RUNTIME_API_KEYS 第 {index} 项与前面的条目重复（同一个 key 出现两次）")
        out[key] = {"tenant": tenant,
                    "scopes": list(_as_str_list(scopes)),
                    "user_id": user_id.strip() or _alias_for(key)}
    return out


def _alias_for(key: str) -> str:
    """密钥 → 稳定、不可逆的 user_id 别名（`svc_<8 hex>`）。"""
    import hashlib
    return "svc_" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:8]


# ══════════════════════════════════════════════════════════════
# 五、Authenticator：请求头 → Principal
# ══════════════════════════════════════════════════════════════
class Authenticator:
    """从请求头解析凭据 → `Principal`。

    **失败必须说清原因，不许静默放行。** 所有失败都返回 `AuthResult(ok=False, reason=...)`：
    reason 的冒号前是机器可读码（见 `REASON_*`），冒号后是人类可读解释。

    Args:
        api_keys:       `{key: Principal | dict}`。空表 = 不接受 API key 方式。
        jwt_secret:     HS*/RS* 校验密钥。空串 = 不接受 Bearer 方式。
        jwt_algorithms: 算法**白名单**（默认只有 HS256）。白名单之外一律拒绝，
                        包括 `none` —— 这是 JWT 最经典的绕过（攻击者把 alg 改成
                        none，验签端若"跟随 token 自称的算法"就直接放行）。
        jwt_issuer:     期望的 `iss`；空串 = 不校验（部署在单 IdP 下才该留空）。
        jwt_audience:   期望的 `aud`；空串 = 不校验。
        allow_anonymous: 无凭据时是否放行。**默认 False**：匿名是显式开关，
                        不是"没配密钥时的默认值"。
        anonymous_principal: 匿名的身份；None 时用 `anonymous`/`anonymous` + **空 scope**
                        （空 scope 会在 Gateway 的 RBAC 处 fail-closed）。
        clock:          `() -> float`，只用于算 `retry_after_s`。
        jwt_leeway_s:   时间容差（时钟漂移）。默认 0：宁可早拒，不要晚拒。
        expiry_warn_s:  剩余寿命小于它时，成功结果里带 `retry_after_s`。
    """

    def __init__(self, *, api_keys: dict[str, Principal] | None = None,
                 jwt_secret: str = "", jwt_algorithms: Sequence[str] = ("HS256",),
                 jwt_issuer: str = "", jwt_audience: str = "",
                 allow_anonymous: bool = False,
                 anonymous_principal: Principal | None = None,
                 clock: Callable[[], float] | None = None,
                 jwt_leeway_s: float = 0.0,
                 expiry_warn_s: float = 60.0) -> None:
        # 配置期就归一 + 校验：坏配置在启动时炸，而不是在某个请求上变成 403
        # （"某个人登不上"排查起来比"进程起不来"贵得多）。
        normalized: dict[str, Principal] = {}
        for key, principal in dict(api_keys or {}).items():
            name = str(key or "")
            if not name:
                raise ValueError("api_keys 里存在空 key（fail-closed）")
            normalized[name] = _as_principal(principal, where=f"api_keys[{_alias_for(name)}]")
        self.api_keys: dict[str, Principal] = normalized

        self.jwt_secret = str(jwt_secret or "")
        algorithms = [str(alg).strip() for alg in (jwt_algorithms or ()) if str(alg).strip()]
        self.jwt_algorithms: tuple[str, ...] = tuple(algorithms) or ("HS256",)
        self.jwt_issuer = str(jwt_issuer or "").strip()
        self.jwt_audience = str(jwt_audience or "").strip()
        self.allow_anonymous = bool(allow_anonymous)
        self.anonymous_principal = (None if anonymous_principal is None
                                    else _as_principal(anonymous_principal,
                                                       where="anonymous_principal"))
        self.clock: Callable[[], float] = clock or time.time
        self.jwt_leeway_s = float(jwt_leeway_s or 0.0)
        self.expiry_warn_s = float(expiry_warn_s or 0.0)
        # 失败原因要抹的片段**在构造期算一次**：失败是攻击者高频触发的路径，
        # 每次失败都遍历 N 把 key 的前后缀会让"拒绝"变成比"放行"更贵的事
        # —— 那样一来，拒绝本身就成了一种放大攻击面。
        self._secret_fragments: tuple[str, ...] = tuple(
            frag for secret in self._secret_material()
            for frag in _fragments(secret))

    # ── 自述 ────────────────────────────────────────────────
    @property
    def configured(self) -> bool:
        """是否配置了**任何**凭据方式（API key 或 JWT）。"""
        return bool(self.api_keys) or bool(self.jwt_secret)

    @property
    def schemes(self) -> tuple[str, ...]:
        """已启用的方式（进 /v1/info 与日志；不含任何密钥）。"""
        out: list[str] = []
        if self.api_keys:
            out.append(SCHEME_API_KEY)
        if self.jwt_secret:
            out.append(SCHEME_JWT)
        if self.allow_anonymous:
            out.append(SCHEME_NONE)
        return tuple(out)

    def default_anonymous_principal(self) -> Principal:
        """匿名身份：**空 role / 空 scope**。

        刻意不用 `Principal()` 的默认值（那是 `u_demo` / `acme` / `tool.read+write`）：
        给匿名者一份"演示用"的权限，等于把 demo 的便利变成生产漏洞。
        tenant 用 `"anonymous"` 而不是空串：空 tenant 会被 RBAC 判 `missing tenant`，
        但那条日志读起来像"配置坏了"，而事实是"这个请求本来就是匿名的"。
        """
        if self.anonymous_principal is not None:
            return self.anonymous_principal
        return Principal(user_id="anonymous", roles=(), tenant="anonymous",
                         scopes=frozenset())

    # ── 主入口 ──────────────────────────────────────────────
    def authenticate(self, headers: Mapping[str, str]) -> AuthResult:
        """解析请求头 → `AuthResult`。**不抛异常**（坏输入也是 `ok=False`）。

        为什么兜一层 try：`headers` 可能不是 Mapping、claims 可能是奇怪形状，
        而这条路径上"抛出去"最终会变成一个 500 —— 500 本身没错（不静默放行），
        但把原因说清比堆栈更有用，而且省得每个调用方各写一遍 try。
        """
        try:
            return self._authenticate(headers)
        except Exception as exc:        # noqa: BLE001 - 失败必须说清，不许静默放行
            return AuthResult(
                ok=False, scheme="",
                reason=self.scrub_reason(
                    f"{REASON_AUTH_INTERNAL}: 鉴权过程异常 {type(exc).__name__}"))

    def _authenticate(self, headers: Mapping[str, str]) -> AuthResult:
        authorization = _header(headers, "authorization")
        api_key_header = _header(headers, "x-api-key")

        # 两个头同时给：要么一致（同一个 key），要么**拒绝** —— "用哪个算数"
        # 不能由客户端通过多加一个头来决定（那是权限提升的经典手法）。
        bearer = _bearer_token(authorization)
        scheme, token = _split_authorization(authorization)

        if not authorization and not api_key_header:
            if self.allow_anonymous:
                return AuthResult(ok=True, principal=self.default_anonymous_principal(),
                                  scheme=SCHEME_NONE)
            if not self.configured:
                return self._fail(
                    REASON_AUTH_NOT_CONFIGURED,
                    "未配置任何凭据方式（api_keys 为空且没有 jwt_secret）；"
                    "拒绝所有请求，而不是默认放行匿名")
            return self._fail(
                REASON_MISSING_CREDENTIALS,
                "请求未携带凭据：需要 Authorization: Bearer <jwt>、"
                "Authorization: ApiKey <key> 或 X-API-Key: <key>")

        # ① Bearer → JWT
        if scheme == "bearer":
            if not bearer:
                # `Authorization: Bearer`（没有令牌）是**给了但无效**，不是"没给"
                return self._fail(REASON_INVALID_CREDENTIALS, "Bearer 之后没有令牌",
                                  scheme=SCHEME_JWT)
            if api_key_header:
                # 两个头指向两套凭据：只有"同一个 key"才可能一致；JWT 与 key 必然不同源
                return self._fail(
                    REASON_INVALID_CREDENTIALS,
                    "同时提供了 Bearer 与 X-API-Key 两种凭据，无法判定以哪个为准")
            return self._authenticate_jwt(bearer)

        # ② ApiKey（Authorization: ApiKey … / X-API-Key: …）
        if scheme == "apikey":
            if not token:
                return self._fail(REASON_INVALID_API_KEY, "ApiKey 凭据为空",
                                  scheme=SCHEME_API_KEY)
            if api_key_header and not _constant_time_equals(token, api_key_header):
                return self._fail(
                    REASON_INVALID_CREDENTIALS,
                    "Authorization: ApiKey 与 X-API-Key 指向不同的凭据，拒绝猜测身份")
            return self._authenticate_api_key(token)
        if api_key_header:
            return self._authenticate_api_key(api_key_header)

        # ③ Authorization 给了，但不是我们认识的 scheme（Basic / Digest / 拼错）
        if authorization:
            return self._fail(
                REASON_UNSUPPORTED_SCHEME,
                "Authorization 的 scheme 不被支持（只接受 Bearer 与 ApiKey）",
                scheme=SCHEME_NONE)
        return self._fail(REASON_MISSING_CREDENTIALS,
                          "请求未携带可用凭据（X-API-Key 为空）")

    # ── API key ────────────────────────────────────────────
    def _authenticate_api_key(self, token: str) -> AuthResult:
        if not self.api_keys:
            # 服务认可**别的**方式 → 客户端用错了 scheme（403，换凭据就能解决）；
            # 一种方式都没配 → 服务端没配好（500）。两者混在一起会把部署事故
            # 报成"你凭据无效"，运维就会去查客户端的 key。
            if self.configured:
                return self._fail(
                    REASON_SCHEME_NOT_CONFIGURED,
                    "本服务未启用 API key 方式（只接受 Bearer/JWT）",
                    scheme=SCHEME_API_KEY, secrets=(token,))
            return self._fail(
                REASON_AUTH_NOT_CONFIGURED,
                "未配置任何 API key（api_keys 为空），且服务端未配置任何凭据方式",
                scheme=SCHEME_API_KEY, secrets=(token,))
        candidate = str(token or "")
        if not candidate:
            return self._fail(REASON_INVALID_API_KEY, "ApiKey 凭据为空",
                              scheme=SCHEME_API_KEY)

        # 遍历**整张表**且不提前 break：命中位置不该反映在响应时间上。
        matched: Principal | None = None
        for key, principal in self.api_keys.items():
            if _constant_time_equals(candidate, key):
                matched = principal
        if matched is None:
            # 刻意不提示"最接近哪个 key"、不回显任何前缀
            return self._fail(REASON_INVALID_API_KEY, "API key 不在已知密钥表中",
                              scheme=SCHEME_API_KEY, secrets=(candidate,))
        return AuthResult(ok=True, principal=matched, scheme=SCHEME_API_KEY)

    # ── JWT ────────────────────────────────────────────────
    def _authenticate_jwt(self, token: str) -> AuthResult:
        if not self.jwt_secret:
            if self.configured:
                return self._fail(
                    REASON_SCHEME_NOT_CONFIGURED,
                    "本服务未启用 Bearer/JWT 方式（只接受 API key）",
                    scheme=SCHEME_JWT, secrets=(token,))
            return self._fail(REASON_AUTH_NOT_CONFIGURED,
                              "未配置 JWT 校验密钥（jwt_secret 为空），"
                              "且服务端未配置任何凭据方式",
                              scheme=SCHEME_JWT, secrets=(token,))
        jwt = _jwt_module()
        if jwt is None:
            return self._fail(REASON_AUTH_NOT_CONFIGURED,
                              "未安装 PyJWT，Bearer 凭据不可用（pip install pyjwt）",
                              scheme=SCHEME_JWT, secrets=(token,))
        if not token:
            return self._fail(REASON_INVALID_CREDENTIALS, "Bearer 之后没有令牌",
                              scheme=SCHEME_JWT)

        # ① 头部与算法白名单（**先于**任何解密/验签）
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:        # noqa: BLE001
            return self._fail(REASON_JWT_MALFORMED,
                              f"JWT 头部无法解析（{type(exc).__name__}）",
                              scheme=SCHEME_JWT, secrets=(token,))
        alg = str(header.get("alg") or "").strip()
        if not alg or alg.lower() == "none":
            return self._fail(REASON_JWT_ALG_REJECTED,
                              "拒绝 alg=none（无签名 JWT 不是凭据）",
                              scheme=SCHEME_JWT, secrets=(token,))
        if alg not in self.jwt_algorithms:
            return self._fail(
                REASON_JWT_ALG_REJECTED,
                f"JWT 算法 {alg} 不在白名单 {list(self.jwt_algorithms)} 内",
                scheme=SCHEME_JWT, secrets=(token,))

        # ② 验签 + 时间/签发者/受众/必需 claim
        options = {
            "require": ["exp"],          # exp 是**必需**：无 exp 的 token 永不过期
            "verify_signature": True,
            "verify_exp": True,
            "verify_nbf": True,
            "verify_iat": False,         # iat 只做展示，不参与判定
            "verify_iss": bool(self.jwt_issuer),
            "verify_aud": bool(self.jwt_audience),
        }
        try:
            claims = jwt.decode(
                token, self.jwt_secret,
                algorithms=list(self.jwt_algorithms),
                issuer=self.jwt_issuer or None,
                audience=self.jwt_audience or None,
                leeway=self.jwt_leeway_s,
                options=options)
        except jwt.ExpiredSignatureError:
            return self._fail(REASON_JWT_EXPIRED, "JWT 已过期（exp 已过）",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.ImmatureSignatureError:
            return self._fail(REASON_JWT_NOT_YET_VALID, "JWT 尚未生效（nbf 在未来）",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.InvalidIssuerError:
            return self._fail(REASON_JWT_ISSUER_MISMATCH, "JWT 的 iss 与本服务期望不符",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.InvalidAudienceError:
            return self._fail(REASON_JWT_AUDIENCE_MISMATCH, "JWT 的 aud 与本服务期望不符",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.MissingRequiredClaimError as exc:
            claim = _claim_name(exc)
            return self._fail(REASON_JWT_MISSING_CLAIM,
                              f"JWT 缺少必需 claim：{claim or 'exp'}"
                              f"（服务端要求 exp，否则令牌永不过期）",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.InvalidAlgorithmError:
            return self._fail(REASON_JWT_ALG_REJECTED, "JWT 算法不被接受（白名单外）",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.InvalidSignatureError:
            return self._fail(REASON_JWT_SIGNATURE_INVALID, "JWT 签名校验失败",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.DecodeError as exc:
            return self._fail(REASON_JWT_MALFORMED,
                              f"JWT 解码失败（{type(exc).__name__}）",
                              scheme=SCHEME_JWT, secrets=(token,))
        except jwt.InvalidTokenError as exc:
            return self._fail(REASON_JWT_CLAIMS_INVALID,
                              f"JWT 校验失败（{type(exc).__name__}）",
                              scheme=SCHEME_JWT, secrets=(token,))
        if not isinstance(claims, Mapping):
            return self._fail(REASON_JWT_CLAIMS_INVALID, "JWT payload 不是 JSON 对象",
                              scheme=SCHEME_JWT, secrets=(token,))

        # ③ claims → Principal（**缺 sub/tenant 就拒**：缺席不是"用默认值"）
        sub = claims.get("sub")
        user_id = sub.strip() if isinstance(sub, str) else ""
        if not user_id:
            return self._fail(REASON_JWT_CLAIMS_INVALID,
                              "JWT 缺少非空 sub（user_id）",
                              scheme=SCHEME_JWT, secrets=(token,))
        tenant = claims.get("tenant")
        tenant_text = tenant.strip() if isinstance(tenant, str) else ""
        if not tenant_text:
            return self._fail(REASON_JWT_CLAIMS_INVALID,
                              "JWT 缺少 tenant claim（多租户分区键，fail-closed）",
                              scheme=SCHEME_JWT, secrets=(token,))
        roles = _as_str_list(claims.get("roles"))
        scopes = frozenset(_as_str_list(claims.get("scope")
                                        if claims.get("scope") is not None
                                        else claims.get("scopes")))
        principal = Principal(user_id=user_id, roles=roles, tenant=tenant_text,
                              scopes=scopes)

        # ④ 剩余寿命：让客户端提前换 token，而不是等 403
        retry_after = 0.0
        try:
            remaining = float(claims.get("exp")) - float(self.clock())
        except (TypeError, ValueError):
            remaining = 0.0
        if 0.0 < remaining <= max(self.expiry_warn_s, 0.0):
            retry_after = round(remaining, 3)
        return AuthResult(ok=True, principal=principal, scheme=SCHEME_JWT,
                          retry_after_s=retry_after)

    # ── 内部 ────────────────────────────────────────────────
    def _secret_material(self) -> tuple[str, ...]:
        """需要被脱敏的素材（密钥本体）。**只用于 scrub，绝不进响应。**"""
        material = list(self.api_keys.keys())
        if self.jwt_secret:
            material.append(self.jwt_secret)
        return tuple(material)

    def scrub_reason(self, text: Any, extra: Iterable[str] = ()) -> str:
        """抹掉文本里的密钥/令牌片段（用构造期缓存的片段 + 本次额外给的）。"""
        fragments = list(self._secret_fragments)
        for item in extra:
            fragments.extend(_fragments(str(item)))
        return _apply_fragments(text, fragments)

    def _fail(self, code: str, message: str, *, scheme: str = "",
              secrets: Iterable[str] = ()) -> AuthResult:
        """造一个失败结果：`"<code>: <message>"`，并把密钥片段抹掉。"""
        return AuthResult(ok=False, scheme=scheme,
                          reason=self.scrub_reason(f"{code}: {message}", secrets))


def _bearer_token(authorization: str) -> str:
    """`Authorization: Bearer <jwt>` → `<jwt>`；不是 Bearer → 空串。"""
    text = str(authorization or "").strip()
    if not text:
        return ""
    scheme, _, rest = text.partition(" ")
    return rest.strip() if scheme.strip().lower() == "bearer" else ""


def _split_authorization(authorization: str) -> tuple[str, str]:
    """拆 Authorization → `(scheme 小写, 凭据)`。缺失时 `("", "")`。"""
    text = str(authorization or "").strip()
    if not text:
        return "", ""
    scheme, _, rest = text.partition(" ")
    return scheme.strip().lower(), rest.strip()


def _claim_name(exc: Any) -> str:
    """从 `MissingRequiredClaimError` 里取 claim 名（不同版本字段名不同）。"""
    for attr in ("claim", "claim_name"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    text = str(exc or "")
    marker = '"'
    if text.count(marker) >= 2:
        return text.split(marker, 2)[1]
    return ""


# ══════════════════════════════════════════════════════════════
# 六、中间件：鉴权 + 拒绝出口
# ══════════════════════════════════════════════════════════════
class AuthMiddleware:
    """纯 ASGI 中间件：鉴权通过就把 principal 写进 `scope["state"]`。

    为什么是**纯 ASGI** 而不是 `BaseHTTPMiddleware`：
    后者会把每个响应包成 `StreamingResponse` 再转发，SSE（`/v1/turns/stream`）
    这种长连接在它中间多一次缓冲/协程调度，断连语义（`http.disconnect`）也跟着
    变复杂。鉴权只需要读请求头、必要时直接返回一个 JSON 响应 —— 纯 ASGI 足够，
    而且对数据流零介入。

    路径策略：
        exempt_paths          命中就**完全跳过**鉴权（fnmatch 通配）。
        metrics_paths         命中时：`metrics_requires_auth or metrics_token` 决定是否鉴权；
                              配了 metrics_token 时可以用它单独走 Bearer（抓取端没有用户身份）。
        其余路径                `protect_paths=None` → 全保护；给了列表 → 只保护命中的。
    """

    def __init__(self, app: Any, authenticator: Authenticator, *,
                 protect_paths: Sequence[str] | None = None,
                 exempt_paths: Sequence[str] | None = DEFAULT_EXEMPT_PATHS,
                 metrics_requires_auth: bool = True,
                 metrics_token: str = "",
                 metrics_paths: Sequence[str] = ("/metrics",),
                 logger: Any = None,
                 on_reject: Callable[[str, int], None] | None = None) -> None:
        self.app = app
        self.authenticator = authenticator
        # `protect_paths=None` 与 `protect_paths=()` 语义**不同**：
        # None = 除豁免外全保护（默认拒绝）；() = 不保护任何路径。
        # 所以在归一化时保留 None 这个哨兵，不能把它压成空元组。
        self.protect_paths = (None if protect_paths is None
                              else _as_patterns(protect_paths))
        self.exempt_paths = _as_patterns(exempt_paths)
        self.metrics_paths = _as_patterns(metrics_paths)
        self.metrics_token = str(metrics_token or "")
        self.metrics_requires_auth = bool(metrics_requires_auth)
        self.logger = logger
        self.on_reject = on_reject

    # ── 路径判定 ────────────────────────────────────────────
    def _is_exempt(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.exempt_paths)

    def _is_metrics(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.metrics_paths)

    def requires_auth(self, path: str) -> bool:
        """这个路径是否需要鉴权（**唯一的策略判定点**，测试可以直接问它）。"""
        path = str(path or "")
        if self._is_exempt(path):
            return False
        if self._is_metrics(path):
            return bool(self.metrics_requires_auth or self.metrics_token)
        if self.protect_paths is None:
            return True             # 默认拒绝：除豁免外全部保护
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.protect_paths)

    # ── ASGI ────────────────────────────────────────────────
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # websocket/lifespan 不在这里处理：lifespan 是进程生命周期，不是请求。
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not self.requires_auth(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)

        # metrics 专用令牌：抓取端没有用户身份，给它一条独立通道（也是最小权限）
        if self._is_metrics(path) and self.metrics_token:
            token = _bearer_token(_header(headers, "authorization"))
            if token and _constant_time_equals(token, self.metrics_token):
                self._mark(scope, AuthResult(ok=True, scheme=SCHEME_METRICS_TOKEN))
                await self.app(scope, receive, send)
                return

        try:
            result = self.authenticator.authenticate(headers)
        except Exception as exc:        # noqa: BLE001
            # **不许静默放行**：鉴权器自己炸了，请求就不能通过。500 而不是 403 ——
            # 这是服务端故障，客户端改凭据也没用。异常文本只留类型名：
            # 注入的 authenticator 完全可能把 token 拼进 message。
            await self._reject(scope, receive, send, 500,
                               f"{REASON_AUTH_INTERNAL}: 鉴权器抛出 "
                               f"{type(exc).__name__}")
            return

        if not isinstance(result, AuthResult):
            await self._reject(scope, receive, send, 500,
                               f"{REASON_AUTH_INTERNAL}: authenticator.authenticate "
                               f"返回 {type(result).__name__}，不是 AuthResult")
            return

        if not result.ok or not isinstance(result.principal, Principal):
            # ok=True 却没有 principal 是**实现错误**，按内部错误处理（不静默放行）
            if result.ok:
                await self._reject(scope, receive, send, 500,
                                   f"{REASON_AUTH_INTERNAL}: ok=True 但 principal 缺失")
                return
            status = status_for_auth_failure(result)
            await self._reject(scope, receive, send, status, result.reason,
                               scheme=result.scheme,
                               retry_after_s=result.retry_after_s)
            return

        self._mark(scope, result)
        await self.app(scope, receive, send)

    # ── 内部 ────────────────────────────────────────────────
    def _mark(self, scope: dict, result: AuthResult) -> None:
        """把结果写进 `scope["state"]`（`request.state` 直接可见）。"""
        state = scope.setdefault("state", {})
        state["principal"] = result.principal
        state["auth"] = result
        state["auth_scheme"] = result.scheme

    def _challenge(self) -> str:
        """401 的 `WWW-Authenticate`：告诉客户端**我们接受哪种**凭据。

        顺序按"最可能被用"排：JWT 在前（浏览器/网关认 Bearer），ApiKey 在后。
        一条 header 里可以并列多个 challenge（逗号分隔），客户端按自己的能力挑。
        """
        challenges: list[str] = []
        for scheme in (SCHEME_JWT, SCHEME_API_KEY):
            if scheme == SCHEME_JWT and not self.authenticator.jwt_secret:
                continue
            if scheme == SCHEME_API_KEY and not self.authenticator.api_keys:
                continue
            word = _CHALLENGE_SCHEME[scheme]
            challenges.append(f'{word} realm="{WWW_AUTHENTICATE_REALM}"')
        if not challenges:
            # 一个凭据方式都没配：仍然给一个 challenge，形状对客户端才可解析
            challenges.append(f'Bearer realm="{WWW_AUTHENTICATE_REALM}"')
        return ", ".join(challenges)

    async def _reject(self, scope: dict, receive: Any, send: Any, status: int,
                      reason: str, *, scheme: str = "",
                      retry_after_s: float = 0.0) -> None:
        """统一拒绝出口：JSON body（形状与 serve.py 的错误出口一致）。"""
        path = str(scope.get("path") or "")
        # 二次脱敏：注入的 authenticator 可能把凭据拼进 reason
        scrubber = getattr(self.authenticator, "scrub_reason", None)
        detail = (scrubber(reason) if callable(scrubber)
                  else scrub(reason, _secrets_of(self.authenticator)))
        payload: dict[str, Any] = {
            "error": _error_name(status),
            "detail": detail,
            "status": int(status),
            "path": path,
            "scheme": scheme,
            "reason_code": reason_code(detail),
        }
        headers: dict[str, str] = {}
        if int(status) == 401:
            headers["WWW-Authenticate"] = self._challenge()
        if retry_after_s and retry_after_s > 0:
            headers["Retry-After"] = str(max(1, int(round(float(retry_after_s)))))

        if self.on_reject is not None:
            try:
                self.on_reject(path, int(status))
            except Exception:           # noqa: BLE001 - 观测面 fail-open
                pass
        if self.logger is not None:
            try:
                self.logger.warn("auth.rejected", path=path, status=int(status),
                                 scheme=scheme, reason=detail)
            except Exception:           # noqa: BLE001
                pass
        await JSONResponse(payload, status_code=int(status),
                           headers=headers)(scope, receive, send)


def _secrets_of(authenticator: Any) -> tuple[str, ...]:
    """取"需要脱敏的素材"。注入的 authenticator 不一定有这个方法 —— 那就无可脱敏。

    为什么不用 `authenticator._secret_material()` 直接调：`authenticator` 是**可注入**的
    （测试假件、将来的别的实现）。在拒绝出口上因为"少了私有方法"而抛 AttributeError，
    结果会是一个 500 HTML 而不是 500 JSON —— 最不该出问题的路径反而先坏。
    """
    getter = getattr(authenticator, "_secret_material", None)
    if not callable(getter):
        return ()
    try:
        return tuple(getter() or ())
    except Exception:                   # noqa: BLE001
        return ()


def _as_patterns(patterns: Sequence[str] | str | None) -> tuple[str, ...]:
    """归一成模式元组：str → (str,)，None → ()，其它可迭代 → tuple[str,...]。"""
    if patterns is None:
        return ()
    if isinstance(patterns, str):
        text = patterns.strip()
        return (text,) if text else ()
    out: list[str] = []
    for item in patterns:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return tuple(out)


def _error_name(status: int) -> str:
    """状态码 → 错误名（与 serve.py 的 JSON 错误出口对齐）。"""
    return {401: "unauthorized", 403: "forbidden", 500: "internal_error",
            503: "unavailable"}.get(int(status), "http_error")


def install_auth(app: Any, authenticator: Authenticator, *,
                 protect_paths: Sequence[str] | None = None,
                 exempt_paths: Sequence[str] | None = DEFAULT_EXEMPT_PATHS,
                 metrics_requires_auth: bool = True,
                 metrics_token: str = "",
                 metrics_paths: Sequence[str] = ("/metrics",),
                 logger: Any = None,
                 on_reject: Callable[[str, int], None] | None = None) -> None:
    """把鉴权装到 FastAPI 应用上（幂等：同一个 app 重复装会抛错，不静默叠加）。

    默认值说明（与任务书里的签名片段有一处**故意**不同）：
        片段写的是 `metrics_requires_auth=False`，同一段文字又要求
        "`/metrics` 默认要鉴权（`metrics_requires_auth=True`）"。两句话矛盾时取
        **安全的那一侧**：默认 True。要公开就显式 `metrics_requires_auth=False`。

    401 的 `WWW-Authenticate` 由中间件按"已启用的方式"生成；403 不带挑战头
    （凭据已给过，再挑战只会让客户端重复提交同一个坏凭据）。
    """
    if getattr(app.state, "authenticator", None) is not None:
        raise RuntimeError("鉴权已经安装过：同一个 app 上重复 install_auth 会让"
                           "拒绝语义变成两套，请只装一次")
    middleware = AuthMiddleware(
        app, authenticator,
        protect_paths=protect_paths,
        exempt_paths=exempt_paths,
        metrics_requires_auth=metrics_requires_auth,
        metrics_token=metrics_token,
        metrics_paths=metrics_paths,
        logger=logger,
        on_reject=on_reject,
    )
    app.state.authenticator = authenticator
    # state 里留一个**同配置**的实例：策略判定（`requires_auth`）与自述用它，
    # 真正跑请求的是 middleware_stack 里的那一个（两者配置相同，行为一致）。
    app.state.auth_middleware = middleware
    app.state.auth_config = {
        "protect_paths": (None if middleware.protect_paths is None
                          else list(middleware.protect_paths)),
        "exempt_paths": middleware.exempt_paths,
        "metrics_requires_auth": middleware.metrics_requires_auth,
        "metrics_token": bool(middleware.metrics_token),   # 只记"有没有"，不记值
        "schemes": list(authenticator.schemes),
        "allow_anonymous": bool(authenticator.allow_anonymous),
    }
    app.add_middleware(AuthMiddleware, authenticator=authenticator,
                       protect_paths=protect_paths,
                       exempt_paths=exempt_paths,
                       metrics_requires_auth=metrics_requires_auth,
                       metrics_token=metrics_token,
                       metrics_paths=metrics_paths,
                       logger=logger,
                       on_reject=on_reject)


# ══════════════════════════════════════════════════════════════
# 七、Depends 入口
# ══════════════════════════════════════════════════════════════
def current_principal(request: Request) -> Principal:
    """FastAPI 依赖：取本次请求的 `Principal`（由 `AuthMiddleware` 注入）。

    用法::

        @app.get("/v1/whoami")
        async def whoami(principal: Principal = Depends(current_principal)):
            return principal.to_dict()

    三种出口，**都不静默**：

    - 有 principal → 返回它（鉴权已通过）。
    - 没 principal 且没装鉴权 → 500 `auth_not_configured`：
      "这个端点要身份，但进程没配鉴权方式"是**部署错误**。若在这里返回一个
      默认 Principal，端点就会以 `u_demo` 的身份干活 —— 这是最坏的一种静默降级。
    - 没 principal 但装了鉴权 → 401：正常路径下中间件已经挡掉了，走到这里说明
      该路径被 `exempt_paths` 豁免却又声明了 `Depends(current_principal)`。
    """
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal

    authenticator = getattr(getattr(request, "app", None), "state", None)
    configured = getattr(authenticator, "authenticator", None)
    if configured is None:
        raise StarletteHTTPException(
            status_code=500,
            detail=(f"{REASON_AUTH_NOT_CONFIGURED}: 该端点需要 principal，"
                    f"但本进程没有安装鉴权（install_auth 未调用）"),
        )
    raise StarletteHTTPException(
        status_code=401,
        detail=f"{REASON_MISSING_CREDENTIALS}: 本次请求没有通过鉴权（路径被豁免？）",
        headers={"WWW-Authenticate": f'Bearer realm="{WWW_AUTHENTICATE_REALM}"'},
    )
