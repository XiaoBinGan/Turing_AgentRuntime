"""tests/test_auth.py — HTTP 层鉴权（auth.py + serve.py 的接线）验收用例。

设计原则：
1. **不启真实端口**：全部走 `fastapi.testclient.TestClient`（进程内 ASGI）。
2. **不连外部服务**：gateway / metrics / logger / shutdown / deps 都是最小假件。
3. **断言安全属性，不断言实现细节**：
   - 401 与 403 的边界（"没给" vs "给了不认"）；
   - 失败原因里**没有**密钥/JWT 原文的任何片段；
   - `alg=none` / 算法白名单 / exp 必需 / iss·aud 校验确实生效；
   - 请求体 principal 不能越过凭据身份（不许自己给自己升权限）。
4. 每一条都对应一个**真实会出事的场景**，不是为覆盖率凑数。

覆盖清单（任务书 ①~㉖ 逐条对应，见每个用例名后的编号）：
    ㉑ 状态码边界逐项        ① 无凭据 401+WWW-Authenticate  ② 匿名放行
    ③ X-API-Key  ④ ApiKey scheme  ⑤ 无效 key 403  ⑥ compare_digest
    ⑦ JWT claims→principal  ⑧ 过期  ⑨ alg=none  ⑩ 算法白名单  ⑪ 签名错
    ⑫ iss/aud  ⑬ 缺 exp  ⑭ reason 不含密钥  ⑮ reason 不含 JWT
    ⑯ /healthz 豁免  ⑰ /readyz 默认鉴权  ⑱ /metrics 默认鉴权  ⑲ metrics 公开
    ⑳ 身份冲突 403   ㉑ 身份一致通过  ㉒ 中间件异常 500
    ㉓ 多 key 多租户  ㉔ 匿名默认空 scope  ㉕ scope 展开  ㉖ 端到端 RBAC
另加：metrics 专用令牌、current_principal、protect_paths、nbf、retry_after_s、
      配置期校验、环境变量装配、脱敏函数本身。
"""
from __future__ import annotations

import hmac
import json
import time
import unittest
from unittest import mock

import jwt
from fastapi import Depends
from fastapi.testclient import TestClient

import auth as authmod
import serve
from contracts import Principal
from core.gateway import TurnResult
from core.observability import JsonLogger, Metrics, ShutdownCoordinator

# ══════════════════════════════════════════════════════════════
# 〇、常量与假件
# ══════════════════════════════════════════════════════════════
SILENT = lambda _line: None  # noqa: E731

#: 长度都 ≥ 32 字节：PyJWT 对短 HMAC 密钥会告警（那是真实的风险提示）
API_KEY = "sk-live-acme-0123456789abcdef0123456789"
API_KEY_BETA = "sk-live-beta-fedcba9876543210fedcba98"
JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789abc"
METRICS_TOKEN = "metrics-scrape-token-0123456789abcdef"

ACME_SCOPES = ("runtime:run", "tool.read", "tool.write")
PRINCIPAL_ACME = Principal(user_id="u_alben", roles=("sre",), tenant="acme",
                           scopes=frozenset(ACME_SCOPES))
PRINCIPAL_BETA = Principal(user_id="u_boris", roles=("dev",), tenant="beta",
                           scopes=frozenset({"runtime:run"}))

#: 请求体里的 principal（与 ACME 凭据身份一致：字段相同、scope 是子集/等集）
PRINCIPAL_BODY_ACME = {"user_id": "u_alben", "tenant": "acme", "roles": ["sre"],
                       "scopes": list(ACME_SCOPES)}
PRINCIPAL_BODY_BETA = {"user_id": "u_boris", "tenant": "beta", "roles": ["dev"],
                       "scopes": ["runtime:run"]}
#: 匿名身份的请求体（默认匿名 principal 是 anonymous/anonymous + 空 scope）
PRINCIPAL_BODY_ANON = {"user_id": "anonymous", "tenant": "anonymous",
                       "roles": [], "scopes": []}

#: 用于"丢弃某个 claim"（例如验证"缺 exp"时真的没有 exp）
_DROP = object()


def make_result(**kwargs) -> TurnResult:
    base = dict(ok=True, text="已排查完成", phase="DONE", term_reason="DONE",
                task_id="task_fake", steps=2)
    base.update(kwargs)
    return TurnResult(**base)


class FakeGateway:
    """最小 Gateway 替身：记录拿到的 request（鉴权测试只关心 principal 有没有透对）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.stream_requests: list[dict] = []

    def handle(self, request):
        self.requests.append(request)
        return make_result()

    def handle_stream(self, request):
        self.stream_requests.append(request)
        yield {"type": "accepted", "data": {"task_id": "task_fake"}}
        yield {"type": "completed", "data": {"ok": True, "text": "ok", "steps": 1}}


class GoodDep:
    """健康依赖（/readyz 要 200 才能验证"鉴权放行后真的通"）。"""

    def info(self):
        return {"kind": "fake", "available": True, "reason": "ok"}


class RecordingMetrics:
    """只记录 counter 调用的指标假件（用来验证 label 取值，不验证导出格式）。"""

    def __init__(self) -> None:
        self.counters: list[tuple] = []

    def render(self):
        return ""

    def counter(self, name, labels=None, value=1.0):
        self.counters.append((name, dict(labels or {}), value))

    def gauge(self, *_args, **_kwargs):
        pass

    def observe(self, *_args, **_kwargs):
        pass

    def describe(self, *_args, **_kwargs):
        pass


class ExplodingAuthenticator:
    """`authenticate` 直接抛异常：验证中间件**不静默放行**。

    异常文本里故意塞进 API key —— 验证 500 出口也会脱敏。
    """

    schemes: tuple[str, ...] = ("api_key",)

    def __init__(self) -> None:
        self.api_keys: dict[str, Principal] = {}
        self.jwt_secret = ""
        self.jwt_algorithms: tuple[str, ...] = ("HS256",)
        self.jwt_issuer = ""
        self.jwt_audience = ""
        self.allow_anonymous = False

    def authenticate(self, _headers):
        raise RuntimeError(f"boom: 内部炸了，顺手带出 {API_KEY}")


def make_api_key_authn(**kwargs) -> authmod.Authenticator:
    """默认：一张两租户的 API key 表。"""
    keys = kwargs.pop("api_keys", None)
    if keys is None:
        keys = {API_KEY: PRINCIPAL_ACME, API_KEY_BETA: PRINCIPAL_BETA}
    return authmod.Authenticator(api_keys=keys, **kwargs)


def make_jwt_authn(**kwargs) -> authmod.Authenticator:
    kwargs.setdefault("jwt_secret", JWT_SECRET)
    return authmod.Authenticator(**kwargs)


def make_jwt(claims: dict | None = None, *, secret: str = JWT_SECRET,
             algorithm: str = "HS256") -> str:
    """造一个 JWT：默认是 ACME 的合法令牌（1 小时后过期）。"""
    payload: dict = {
        "sub": "u_alben",
        "tenant": "acme",
        "roles": ["sre"],
        "scope": " ".join(ACME_SCOPES),      # OAuth2 惯例：空格分隔
        "exp": int(time.time()) + 3600,
    }
    for key, value in (claims or {}).items():
        if value is _DROP:
            payload.pop(key, None)
        else:
            payload[key] = value
    return jwt.encode(payload, secret, algorithm=algorithm)


def make_app(gateway=None, *, authenticator=None, metrics=None, logger=None,
             shutdown=None, deps=None, **auth_kwargs):
    """造带/不带鉴权的 app。

    `authenticator=None` + 不传任何 auth 参数 = **历史行为**（principal 由请求体给）：
    这条路径同时证明"加鉴权没有改变未启用时的语义"。
    """
    return serve.create_app(
        gateway=gateway if gateway is not None else FakeGateway(),
        metrics=metrics if metrics is not None else Metrics(),
        logger=logger if logger is not None else JsonLogger(sink=SILENT),
        shutdown=shutdown if shutdown is not None else ShutdownCoordinator(),
        deps=deps if deps is not None else {"store": GoodDep()},
        authenticator=authenticator,
        **auth_kwargs,
    )


def post_turn(client, *, principal=None, goal="排查 api 超时", **extra):
    body = {"goal": goal, "principal": principal or PRINCIPAL_BODY_ACME}
    body.update(extra)
    return client.post("/v1/turns", json=body)


def detail_of(response) -> str:
    """取错误体里的 detail（失败原因必须在这里，且要能断言"不含什么"）。"""
    try:
        return str(response.json().get("detail", ""))
    except Exception:                   # noqa: BLE001 - 非 JSON 时返回原文
        return response.text


# ══════════════════════════════════════════════════════════════
# 一、Authenticator 单元：三种凭据方式
# ══════════════════════════════════════════════════════════════
class TestApiKeyAuthenticator(unittest.TestCase):
    def test_x_api_key_header_authenticates(self):
        """③ X-API-Key 有效 → 通过且 principal 正确。"""
        result = make_api_key_authn().authenticate({"x-api-key": API_KEY})
        self.assertTrue(result.ok)
        self.assertEqual(result.scheme, authmod.SCHEME_API_KEY)
        self.assertEqual(result.principal.user_id, "u_alben")
        self.assertEqual(result.principal.tenant, "acme")
        self.assertEqual(result.reason, "")

    def test_authorization_apikey_scheme_authenticates(self):
        """④ `Authorization: ApiKey <key>` 有效 → 通过。"""
        result = make_api_key_authn().authenticate(
            {"authorization": f"ApiKey {API_KEY}"})
        self.assertTrue(result.ok)
        self.assertEqual(result.principal.tenant, "acme")

    def test_header_name_is_case_insensitive(self):
        """HTTP 头大小写不敏感；用 `Authorization` 原样大小写也必须认。"""
        result = make_api_key_authn().authenticate(
            {"Authorization": f"ApiKey {API_KEY}"})
        self.assertTrue(result.ok)

    def test_invalid_api_key_is_rejected(self):
        """⑤ 无效 key → 不通过，reason 是 `invalid_api_key`。"""
        result = make_api_key_authn().authenticate({"x-api-key": "sk-nope"})
        self.assertFalse(result.ok)
        self.assertIsNone(result.principal)
        self.assertEqual(result.code, authmod.REASON_INVALID_API_KEY)

    def test_near_miss_key_is_rejected(self):
        """只差最后一个字符也必须拒（前缀相同不是凭据）。"""
        result = make_api_key_authn().authenticate({"x-api-key": API_KEY[:-1]})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_INVALID_API_KEY)

    def test_comparison_uses_compare_digest(self):
        """⑥ API key 比较必须走 `hmac.compare_digest`（防时序侧信道）。"""
        calls: list[tuple] = []
        real = hmac.compare_digest

        def spy(left, right):
            calls.append((left, right))
            return real(left, right)

        with mock.patch.object(authmod.hmac, "compare_digest", side_effect=spy):
            result = make_api_key_authn().authenticate({"x-api-key": API_KEY})
        self.assertTrue(result.ok)
        self.assertTrue(calls, "API key 比较没有走 hmac.compare_digest")
        flattened = [value for pair in calls for value in pair]
        self.assertTrue(
            any(str(value) in (API_KEY, API_KEY.encode()) or
                (isinstance(value, bytes) and value == API_KEY.encode())
                for value in flattened),
            "compare_digest 的入参里没有出现待比较的 key")

    def test_wrong_key_does_not_leak_any_fragment(self):
        """⑭ 失败 reason 里不许出现密钥的任何片段（哪怕前缀/尾缀）。"""
        candidate = API_KEY[:-1] + "X"          # 与真 key 只差一个字符
        result = make_api_key_authn().authenticate({"x-api-key": candidate})
        self.assertFalse(result.ok)
        for fragment in (candidate, candidate[:4], candidate[:8], candidate[-8:],
                         API_KEY, API_KEY[:6], API_KEY[-6:]):
            self.assertNotIn(fragment, result.reason)

    def test_keys_map_to_different_tenants(self):
        """㉓ 多 key 表：各自映射到不同租户（不是"一把 key 全租户通吃"）。"""
        authenticator = make_api_key_authn()
        acme = authenticator.authenticate({"x-api-key": API_KEY})
        beta = authenticator.authenticate({"x-api-key": API_KEY_BETA})
        self.assertEqual(acme.principal.tenant, "acme")
        self.assertEqual(beta.principal.tenant, "beta")
        self.assertEqual(beta.principal.user_id, "u_boris")

    def test_both_headers_pointing_to_different_keys_is_rejected(self):
        """两个头给出不同凭据 → 拒绝（不能由客户端决定"以哪个为准"）。"""
        result = make_api_key_authn().authenticate(
            {"authorization": f"ApiKey {API_KEY}", "x-api-key": API_KEY_BETA})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_INVALID_CREDENTIALS)

    def test_unsupported_scheme_is_rejected(self):
        """`Authorization: Basic ...` 不是支持的 scheme → 403 侧（给了但不认）。"""
        result = make_api_key_authn().authenticate(
            {"authorization": "Basic dXNlcjpwdw=="})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_UNSUPPORTED_SCHEME)
        self.assertEqual(authmod.status_for_auth_failure(result), 403)

    def test_bad_config_raises_at_construction(self):
        """配置期就炸：key 指向的 principal 缺 tenant 不许等到请求时才变成 403。"""
        with self.assertRaises(ValueError):
            authmod.Authenticator(api_keys={API_KEY: {"user_id": "u", "tenant": ""}})


class TestJwtAuthenticator(unittest.TestCase):
    def test_valid_jwt_maps_claims_to_principal(self):
        """⑦ JWT 有效 → sub/tenant/roles/scope 解析正确。"""
        result = make_jwt_authn().authenticate(
            {"authorization": f"Bearer {make_jwt()}"})
        self.assertTrue(result.ok)
        self.assertEqual(result.scheme, authmod.SCHEME_JWT)
        self.assertEqual(result.principal.user_id, "u_alben")
        self.assertEqual(result.principal.tenant, "acme")
        self.assertEqual(tuple(result.principal.roles), ("sre",))
        self.assertEqual(result.principal.scopes, frozenset(ACME_SCOPES))

    def test_scope_claim_expands_to_set(self):
        """㉕ `scope: "tool.read tool.write"` → {"tool.read", "tool.write"}。"""
        token = make_jwt({"scope": "tool.read tool.write", "sub": "u2"})
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertTrue(result.ok)
        self.assertEqual(result.principal.scopes, frozenset({"tool.read", "tool.write"}))
        # 不用空格切的话，RBAC 会永远 deny —— 这里把两种写法都钉住
        token = make_jwt({"scope": ["tool.read", "tool.write"]})
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertEqual(result.principal.scopes, frozenset({"tool.read", "tool.write"}))

    def test_expired_jwt_rejected_with_reason_mentioning_expired(self):
        """⑧ 过期 → 拒，reason 里要看得出是"过期"（排障不能靠猜）。"""
        token = make_jwt({"exp": int(time.time()) - 30})
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertIn("expired", result.reason)
        self.assertEqual(result.code, authmod.REASON_JWT_EXPIRED)
        self.assertEqual(authmod.status_for_auth_failure(result), 403)

    def test_alg_none_is_rejected(self):
        """⑨ `alg=none` 必须拒（JWT 最经典的签名绕过）。"""
        token = jwt.encode({"sub": "u_alben", "tenant": "acme",
                            "exp": int(time.time()) + 3600},
                           key="", algorithm="none")
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_JWT_ALG_REJECTED)
        self.assertIn("none", result.reason)

    def test_algorithm_outside_whitelist_is_rejected(self):
        """⑩ 白名单外的算法（这里用 HS512）→ 拒。"""
        # HS512 的推荐密钥 ≥ 64 字节：用长密钥是为了不触发 PyJWT 的 InsecureKeyLengthWarning
        long_secret = JWT_SECRET + "-padding-to-64-bytes-0123456789abcdefghij"
        token = make_jwt(secret=long_secret, algorithm="HS512")
        result = make_jwt_authn(jwt_algorithms=("HS256",)).authenticate(
            {"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_JWT_ALG_REJECTED)
        # 白名单里给了 HS512 就应当通过（证明拒绝的是"算法不在白名单"，不是"算法新")
        token512 = make_jwt(secret=long_secret, algorithm="HS512")
        ok = make_jwt_authn(jwt_algorithms=("HS256", "HS512"),
                            jwt_secret=long_secret).authenticate(
            {"authorization": f"Bearer {token512}"})
        self.assertTrue(ok.ok)

    def test_wrong_signature_is_rejected(self):
        """⑪ 用别的密钥签 → 签名校验失败。"""
        token = make_jwt(secret="some-other-secret-0123456789abcdefghij")
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_JWT_SIGNATURE_INVALID)

    def test_issuer_and_audience_mismatch_are_rejected(self):
        """⑫ iss / aud 不匹配 → 各自一个明确原因（不是笼统的"无效"）。"""
        token = make_jwt({"iss": "https://evil.example", "aud": "other-api"})
        authenticator = make_jwt_authn(jwt_issuer="https://idp.example",
                                       jwt_audience="agent-runtime")
        result = authenticator.authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertIn(result.code, (authmod.REASON_JWT_ISSUER_MISMATCH,
                                    authmod.REASON_JWT_AUDIENCE_MISMATCH))

        right_iss = make_jwt({"iss": "https://idp.example", "aud": "other-api"})
        result = authenticator.authenticate({"authorization": f"Bearer {right_iss}"})
        self.assertEqual(result.code, authmod.REASON_JWT_AUDIENCE_MISMATCH)

        right_both = make_jwt({"iss": "https://idp.example", "aud": "agent-runtime"})
        result = authenticator.authenticate({"authorization": f"Bearer {right_both}"})
        self.assertTrue(result.ok)

    def test_missing_exp_is_rejected(self):
        """⑬ 缺 exp → 拒（`options={"require": ["exp"]}`：无 exp 的令牌永不过期）。"""
        token = make_jwt({"exp": _DROP})
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_JWT_MISSING_CLAIM)
        self.assertIn("exp", result.reason)

    def test_nbf_in_future_is_rejected(self):
        """nbf 在未来 → 尚未生效。"""
        token = make_jwt({"nbf": int(time.time()) + 600})
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_JWT_NOT_YET_VALID)

    def test_missing_sub_or_tenant_is_rejected(self):
        """缺 sub / tenant → 拒（缺席不是"用默认值"）。"""
        for claims, code in (({"sub": _DROP}, authmod.REASON_JWT_CLAIMS_INVALID),
                             ({"tenant": _DROP}, authmod.REASON_JWT_CLAIMS_INVALID),
                             ({"sub": "   "}, authmod.REASON_JWT_CLAIMS_INVALID)):
            with self.subTest(claims=list(claims)):
                token = make_jwt(claims)
                result = make_jwt_authn().authenticate(
                    {"authorization": f"Bearer {token}"})
                self.assertFalse(result.ok)
                self.assertEqual(result.code, code)

    def test_reason_never_contains_the_token(self):
        """⑮ 失败 reason 里不许出现 JWT 原文（整串或前 20/40 字符）。"""
        token = make_jwt({"exp": int(time.time()) - 30})
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        for fragment in (token, token[:20], token[:40], token[-20:]):
            self.assertNotIn(fragment, result.reason)

        garbage = "not-a-jwt.at-all.invalid"
        result = make_jwt_authn().authenticate({"authorization": f"Bearer {garbage}"})
        self.assertFalse(result.ok)
        self.assertNotIn(garbage, result.reason)

    def test_expiring_soon_reports_retry_after(self):
        """即将过期 → 成功结果里带 retry_after_s（客户端提前换 token，而不是等 403）。"""
        token = make_jwt({"exp": int(time.time()) + 10})
        result = make_jwt_authn(expiry_warn_s=60).authenticate(
            {"authorization": f"Bearer {token}"})
        self.assertTrue(result.ok)
        self.assertGreater(result.retry_after_s, 0)
        self.assertLessEqual(result.retry_after_s, 11)

    def test_far_future_token_has_no_retry_after(self):
        result = make_jwt_authn().authenticate(
            {"authorization": f"Bearer {make_jwt()}"})
        self.assertEqual(result.retry_after_s, 0.0)


class TestNegotiationAndAnonymous(unittest.TestCase):
    def test_no_credentials_without_anonymous_is_missing_credentials(self):
        """① 无凭据 + allow_anonymous=False → `missing_credentials`（HTTP 401 侧）。"""
        result = make_api_key_authn().authenticate({})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_MISSING_CREDENTIALS)
        self.assertEqual(authmod.status_for_auth_failure(result), 401)

    def test_anonymous_allowed_returns_anonymous_principal(self):
        """② allow_anonymous=True → 无凭据也通过，身份是匿名的。"""
        result = authmod.Authenticator(api_keys={API_KEY: PRINCIPAL_ACME},
                                       allow_anonymous=True).authenticate({})
        self.assertTrue(result.ok)
        self.assertEqual(result.scheme, authmod.SCHEME_NONE)
        self.assertEqual(result.principal.tenant, "anonymous")

    def test_anonymous_principal_defaults_to_empty_scopes(self):
        """㉔ 没配 anonymous_principal 时，默认匿名身份**不带任何 scope**。

        给匿名者一份 demo 默认权限（`tool.read` + `tool.write`），等于把 demo 的
        便利变成生产漏洞 —— 这条断言就是防止那种"顺手复用 Principal() 默认值"。
        """
        result = authmod.Authenticator(allow_anonymous=True).authenticate({})
        self.assertTrue(result.ok)
        self.assertEqual(result.principal.scopes, frozenset())
        self.assertEqual(result.principal.roles, ())

    def test_explicit_anonymous_principal_is_used(self):
        anon = Principal(user_id="guest", tenant="public", scopes=frozenset({"tool.read"}))
        result = authmod.Authenticator(allow_anonymous=True,
                                       anonymous_principal=anon).authenticate({})
        self.assertEqual(result.principal.user_id, "guest")
        self.assertEqual(result.principal.scopes, frozenset({"tool.read"}))

    def test_nothing_configured_is_explicit_not_silent_allow(self):
        """密钥表为空且没配 JWT → 明确 `auth_not_configured`，**不**默默放行。"""
        result = authmod.Authenticator().authenticate({})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_AUTH_NOT_CONFIGURED)
        self.assertIn("未配置任何凭据方式", result.reason)
        self.assertIsNone(result.principal)
        # 服务端没配好不是客户端的错：不能是 401（那会让客户端反复去取凭据）
        self.assertEqual(authmod.status_for_auth_failure(result), 500)

    def test_api_key_presented_when_no_keys_configured(self):
        """只配了 JWT、却递上 ApiKey → `scheme_not_configured`（403，换凭据就能解决）。

        注意它**不是** `auth_not_configured`（500）：服务端配置是好的，只是没开这种方式。
        把这两者混起来，运维会照着一个正常的部署去查"为什么没配"。
        """
        result = make_jwt_authn().authenticate({"x-api-key": API_KEY})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_SCHEME_NOT_CONFIGURED)
        self.assertEqual(authmod.status_for_auth_failure(result), 403)

    def test_bearer_presented_when_no_secret_configured(self):
        token = make_jwt()
        result = make_api_key_authn().authenticate(
            {"authorization": f"Bearer {token}"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_SCHEME_NOT_CONFIGURED)
        self.assertEqual(authmod.status_for_auth_failure(result), 403)
        self.assertNotIn(token, result.reason)


class TestGarbageInput(unittest.TestCase):
    """坏输入也是"拒绝 + 说清原因"，不是异常穿透。"""

    def test_none_headers_is_missing_credentials(self):
        result = make_api_key_authn().authenticate(None)      # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_MISSING_CREDENTIALS)

    def test_headers_object_that_raises_becomes_internal_error(self):
        class Hostile:
            def items(self):
                raise RuntimeError("boom: " + API_KEY)

        result = make_api_key_authn().authenticate(Hostile())
        self.assertFalse(result.ok)
        self.assertEqual(result.code, authmod.REASON_AUTH_INTERNAL)
        self.assertNotIn(API_KEY, result.reason)             # 异常文本也要脱敏

    def test_empty_scheme_tokens_are_rejected_with_clear_reasons(self):
        """`Bearer` 后面没令牌 / `ApiKey` 后面没 key：是"给了但无效"（403），不是"没给"。"""
        bearer = make_jwt_authn().authenticate({"authorization": "Bearer "})
        self.assertFalse(bearer.ok)
        self.assertEqual(bearer.code, authmod.REASON_INVALID_CREDENTIALS)
        self.assertEqual(authmod.status_for_auth_failure(bearer), 403)

        apikey = make_api_key_authn().authenticate({"authorization": "ApiKey "})
        self.assertFalse(apikey.ok)
        self.assertEqual(apikey.code, authmod.REASON_INVALID_API_KEY)

    def test_scrub_reason_masks_secret_fragments(self):
        authenticator = make_api_key_authn()
        text = authenticator.scrub_reason(f"oops {API_KEY} / {API_KEY[:9]}")
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(API_KEY[:9], text)


class TestStatusBoundary(unittest.TestCase):
    """㉑ 401/403/500 的判定表（边界就是契约，逐项钉住）。"""

    def test_reason_code_to_status(self):
        cases = {
            authmod.REASON_MISSING_CREDENTIALS: 401,
            authmod.REASON_INVALID_API_KEY: 403,
            authmod.REASON_UNSUPPORTED_SCHEME: 403,
            authmod.REASON_JWT_EXPIRED: 403,
            authmod.REASON_JWT_ALG_REJECTED: 403,
            authmod.REASON_JWT_SIGNATURE_INVALID: 403,
            authmod.REASON_JWT_ISSUER_MISMATCH: 403,
            authmod.REASON_JWT_AUDIENCE_MISMATCH: 403,
            authmod.REASON_JWT_MISSING_CLAIM: 403,
            authmod.REASON_PRINCIPAL_CONFLICT: 403,
            authmod.REASON_SCHEME_NOT_CONFIGURED: 403,
            authmod.REASON_AUTH_NOT_CONFIGURED: 500,
            authmod.REASON_AUTH_INTERNAL: 500,
            "something_new": 403,          # 陌生原因码默认 403（fail-closed）
            "": 403,
        }
        for code, expected in cases.items():
            with self.subTest(code=code or "<empty>"):
                result = authmod.AuthResult(ok=False, reason=f"{code}: x" if code else "")
                self.assertEqual(authmod.status_for_auth_failure(result), expected)

    def test_reason_code_split(self):
        self.assertEqual(authmod.reason_code("jwt_expired: 过期了"), "jwt_expired")
        self.assertEqual(authmod.reason_code(None), "")
        self.assertEqual(authmod.reason_code(""), "")

    def test_scrub_masks_key_fragments(self):
        """脱敏函数本身：整串、前缀、尾缀都要被抹掉。"""
        text = f"key={API_KEY} prefix={API_KEY[:10]} suffix={API_KEY[-10:]}"
        cleaned = authmod.scrub(text, [API_KEY])
        self.assertNotIn(API_KEY, cleaned)
        self.assertNotIn(API_KEY[:10], cleaned)
        self.assertNotIn(API_KEY[-10:], cleaned)
        self.assertIn("[redacted]", cleaned)


# ══════════════════════════════════════════════════════════════
# 二、中间件：路径策略与 401/403 出口
# ══════════════════════════════════════════════════════════════
class TestHttpAuthBoundary(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.authenticator = make_api_key_authn()
        self.app = make_app(self.gateway, authenticator=self.authenticator)
        self.client = TestClient(self.app)

    # ① 无凭据 → 401 + WWW-Authenticate
    def test_missing_credentials_returns_401_with_challenge(self):
        response = self.client.get("/v1/info")
        self.assertEqual(response.status_code, 401)
        self.assertIn("www-authenticate", {k.lower() for k in response.headers})
        challenge = response.headers["www-authenticate"]
        self.assertIn("ApiKey", challenge)
        self.assertIn(authmod.WWW_AUTHENTICATE_REALM, challenge)
        body = response.json()
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(body["reason_code"], authmod.REASON_MISSING_CREDENTIALS)

    # ⑤ 无效 key → 403（"给了但不认"），**不带**挑战头
    def test_invalid_key_returns_403_without_challenge(self):
        response = self.client.get("/v1/info", headers={"x-api-key": "sk-nope"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("www-authenticate", {k.lower() for k in response.headers})
        self.assertEqual(response.json()["error"], "forbidden")
        self.assertNotIn("sk-nope", response.text)

    # ③ 有效 key → 通过且 principal 正确
    def test_valid_x_api_key_passes_and_principal_is_used(self):
        response = self.client.post(
            "/v1/turns",
            json={"goal": "排查 api 超时", "principal": PRINCIPAL_BODY_ACME},
            headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)
        sent = self.gateway.requests[-1]["principal"]
        self.assertEqual(sent["user_id"], "u_alben")
        self.assertEqual(sent["tenant"], "acme")
        self.assertEqual(sorted(sent["scopes"]), sorted(ACME_SCOPES))

    # ④ Authorization: ApiKey → 通过
    def test_authorization_apikey_scheme_passes(self):
        response = self.client.get(
            "/v1/info", headers={"authorization": f"ApiKey {API_KEY}"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["auth"]["enabled"])

    # ⑯ /healthz 免鉴权
    def test_healthz_is_exempt(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "alive")

    # ⑰ /readyz 默认要鉴权（暴露依赖拓扑 = 信息泄露）
    def test_readyz_requires_auth_by_default(self):
        self.assertEqual(self.client.get("/readyz").status_code, 401)
        self.assertEqual(
            self.client.get("/readyz", headers={"x-api-key": API_KEY}).status_code, 200)

    def test_readyz_can_be_exempted_explicitly(self):
        """允许豁免，但必须**写下来**（默认不豁免）。"""
        app = make_app(self.gateway, authenticator=self.authenticator,
                       exempt_paths=("/healthz", "/readyz"))
        client = TestClient(app)
        self.assertEqual(client.get("/readyz").status_code, 200)
        self.assertEqual(client.get("/v1/info").status_code, 401)

    # ⑱ /metrics 默认要鉴权
    def test_metrics_requires_auth_by_default(self):
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        response = self.client.get("/metrics", headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])

    # ⑲ metrics_requires_auth=False → 公开
    def test_metrics_can_be_public(self):
        app = make_app(self.gateway, authenticator=self.authenticator,
                       metrics_requires_auth=False)
        client = TestClient(app)
        response = client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        # 公开的只是 metrics：其它路径仍然要鉴权
        self.assertEqual(client.get("/v1/info").status_code, 401)

    def test_metrics_token_is_its_own_channel(self):
        """抓取端没有用户身份：给它一条独立 Bearer，而不是共用某把用户 key。"""
        app = make_app(self.gateway, authenticator=self.authenticator,
                       metrics_token=METRICS_TOKEN)
        client = TestClient(app)
        self.assertEqual(client.get("/metrics").status_code, 401)
        ok = client.get("/metrics",
                        headers={"authorization": f"Bearer {METRICS_TOKEN}"})
        self.assertEqual(ok.status_code, 200)
        wrong = client.get("/metrics",
                          headers={"authorization": "Bearer not-the-token"})
        self.assertEqual(wrong.status_code, 403)
        # 正常用户凭据也能抓（metrics_token 是**额外**通道，不是替代）
        self.assertEqual(
            client.get("/metrics", headers={"x-api-key": API_KEY}).status_code, 200)

    def test_protect_paths_narrows_the_surface(self):
        """protect_paths 给了就只保护命中的路径（其余按公开处理）。"""
        app = make_app(self.gateway, authenticator=self.authenticator,
                       protect_paths=("/v1/turns",), metrics_requires_auth=False)
        client = TestClient(app)
        self.assertEqual(client.get("/v1/info").status_code, 200)
        self.assertEqual(client.post("/v1/turns",
                                     json={"goal": "x",
                                           "principal": PRINCIPAL_BODY_ACME}
                                     ).status_code, 401)

    def test_requires_auth_policy_is_queryable(self):
        """策略判定是一个纯函数式入口，运维/测试可以直接问它。"""
        middleware = self.app.state.auth_middleware
        self.assertFalse(middleware.requires_auth("/healthz"))
        self.assertTrue(middleware.requires_auth("/readyz"))
        self.assertTrue(middleware.requires_auth("/metrics"))
        self.assertTrue(middleware.requires_auth("/v1/turns"))
        self.assertTrue(middleware.requires_auth("/anything-else"))

    def test_repeated_install_is_rejected(self):
        """重复装鉴权会让拒绝语义变成两套：必须炸，而不是静默叠加。"""
        with self.assertRaises(RuntimeError):
            authmod.install_auth(self.app, self.authenticator)

    # ㉒ 中间件异常 → 500（不静默放行）
    def test_authenticator_exception_returns_500_not_pass_through(self):
        app = make_app(self.gateway, authenticator=ExplodingAuthenticator())
        client = TestClient(app)
        response = client.get("/v1/info")
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error"], "internal_error")
        self.assertEqual(body["reason_code"], authmod.REASON_AUTH_INTERNAL)
        # 异常文本里塞的 key 也必须被抹掉
        self.assertNotIn(API_KEY, response.text)
        self.assertNotIn(API_KEY[:10], response.text)

    def test_missing_principal_on_ok_result_is_500(self):
        """`ok=True` 但没给 principal 是**实现错误**：按 500 处理，不放行。"""

        class HalfBaked:
            schemes = ("api_key",)
            api_keys: dict = {}
            jwt_secret = ""
            jwt_algorithms = ("HS256",)
            jwt_issuer = ""
            jwt_audience = ""
            allow_anonymous = False

            def authenticate(self, _headers):
                return authmod.AuthResult(ok=True, scheme="api_key")

        app = make_app(self.gateway, authenticator=HalfBaked())
        self.assertEqual(TestClient(app).get("/v1/info").status_code, 500)

    def test_challenge_lists_both_schemes_when_both_configured(self):
        """两种方式都启用时，401 的挑战头要能同时告诉客户端"我接受什么"。"""
        authenticator = authmod.Authenticator(api_keys={API_KEY: PRINCIPAL_ACME},
                                              jwt_secret=JWT_SECRET)
        client = TestClient(make_app(self.gateway, authenticator=authenticator))
        challenge = client.get("/v1/info").headers["www-authenticate"]
        self.assertIn("Bearer", challenge)
        self.assertIn("ApiKey", challenge)

    def test_rejected_unknown_path_does_not_explode_metric_labels(self):
        """拒绝发生在路由之前：随机 URL 不许直接变成指标 label（基数打爆 = DoS）。

        prometheus_client 会为每个新 label 组合建一条时间序列；把攻击者可控的
        path 原样塞进去，几千个随机路径就能把进程内存吃掉。
        """
        metrics = RecordingMetrics()
        client = TestClient(make_app(self.gateway, authenticator=self.authenticator,
                                     metrics=metrics))
        for suffix in ("/random-1", "/random-2", "/deep/nested/zzz"):
            self.assertEqual(client.get(suffix).status_code, 401)
        paths = {labels.get("path") for name, labels, _value in metrics.counters
                 if name == "http_requests_total"}
        self.assertIn("<other>", paths)
        self.assertFalse([p for p in paths if p and str(p).startswith("/random")])


# ══════════════════════════════════════════════════════════════
# 三、请求体 principal 与凭据身份的一致性
# ══════════════════════════════════════════════════════════════
class TestBodyPrincipalConsistency(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.client = TestClient(make_app(
            self.gateway, authenticator=make_api_key_authn()))

    # ⑳ 冲突 → 403
    def test_user_id_conflict_is_403(self):
        body = dict(PRINCIPAL_BODY_ACME, user_id="u_mallory")
        response = self.client.post("/v1/turns", json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 403)
        self.assertIn("principal_conflict", detail_of(response))
        self.assertEqual(self.gateway.requests, [])   # 冲突请求**没有**到 Gateway

    def test_tenant_conflict_is_403(self):
        """带 acme 的 key 去操作 beta 租户 → 403（跨租户是最危险的一种冒名）。"""
        body = dict(PRINCIPAL_BODY_ACME, tenant="beta")
        response = self.client.post("/v1/turns", json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.gateway.requests, [])

    def test_scope_escalation_in_body_is_403(self):
        """请求体声明凭据没有的 scope → 403（自己给自己升权限）。"""
        body = dict(PRINCIPAL_BODY_ACME, scopes=list(ACME_SCOPES) + ["*"])
        response = self.client.post("/v1/turns", json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 403)
        self.assertIn("scopes", detail_of(response))

    def test_role_escalation_in_body_is_403(self):
        body = dict(PRINCIPAL_BODY_ACME, roles=["sre", "admin"])
        response = self.client.post("/v1/turns", json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 403)
        self.assertIn("roles", detail_of(response))

    # ㉑ 一致 → 通过，且**以凭据身份为准**
    def test_consistent_body_passes_and_header_wins(self):
        body = dict(PRINCIPAL_BODY_ACME, roles=[], scopes=[])   # 少声明是合法的
        response = self.client.post("/v1/turns", json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)
        sent = self.gateway.requests[-1]["principal"]
        self.assertEqual(sent["scopes"], sorted(ACME_SCOPES))    # 用凭据的，不用 body 的
        self.assertEqual(sent["roles"], ["sre"])

    def test_empty_identity_in_body_means_unspecified(self):
        """user_id/tenant 留空 = **未声明** → 用凭据身份。

        这条路径是必需的：API key 的默认 user_id 是不可逆别名（`svc_xxxxxxxx`），
        客户端无从回填；强求"必须一致"会把鉴权变成"客户端得先猜自己的别名"。
        """
        body = {"user_id": "", "tenant": "", "roles": [], "scopes": []}
        response = self.client.post("/v1/turns", json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)
        sent = self.gateway.requests[-1]["principal"]
        self.assertEqual(sent["user_id"], "u_alben")
        self.assertEqual(sent["tenant"], "acme")

    def test_conflict_on_stream_endpoint_is_403_before_streaming(self):
        """流式端点也在**开流之前**拒：不能让客户端先收到 accepted 再失败。"""
        body = dict(PRINCIPAL_BODY_ACME, user_id="u_mallory")
        response = self.client.post("/v1/turns/stream",
                                    json={"goal": "x", "principal": body},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.gateway.stream_requests, [])

    def test_stream_endpoint_requires_credentials(self):
        response = self.client.post("/v1/turns/stream",
                                    json={"goal": "x",
                                          "principal": PRINCIPAL_BODY_ACME})
        self.assertEqual(response.status_code, 401)

    def test_stream_endpoint_passes_with_credentials(self):
        response = self.client.post("/v1/turns/stream",
                                    json={"goal": "x",
                                          "principal": PRINCIPAL_BODY_ACME},
                                    headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: accepted", response.text)
        self.assertEqual(self.gateway.stream_requests[-1]["principal"]["tenant"], "acme")


# ══════════════════════════════════════════════════════════════
# 四、鉴权关闭时：历史行为不变
# ══════════════════════════════════════════════════════════════
class TestAuthDisabledKeepsLegacyBehaviour(unittest.TestCase):
    def test_principal_still_comes_from_body_without_authenticator(self):
        gateway = FakeGateway()
        client = TestClient(make_app(gateway))
        response = post_turn(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(gateway.requests[-1]["principal"]["user_id"], "u_alben")
        self.assertEqual(client.get("/v1/info").status_code, 200)   # 全公开

    def test_info_reports_auth_disabled(self):
        body = TestClient(make_app()).get("/v1/info").json()
        self.assertFalse(body["auth"]["enabled"])
        self.assertEqual(body["auth"]["schemes"], [])

    def test_current_principal_without_auth_is_500_not_fake_identity(self):
        """没装鉴权却声明 `Depends(current_principal)` → 500（部署错误），
        绝不返回一个默认 Principal 让端点以别人的身份干活。"""
        app = make_app()

        @app.get("/whoami")
        async def whoami(principal: Principal = Depends(authmod.current_principal)):
            return principal.to_dict()

        response = TestClient(app).get("/whoami")
        self.assertEqual(response.status_code, 500)
        self.assertIn("auth_not_configured", detail_of(response))


# ══════════════════════════════════════════════════════════════
# 五、current_principal（Depends）
# ══════════════════════════════════════════════════════════════
class TestCurrentPrincipalDependency(unittest.TestCase):
    def test_dependency_returns_authenticated_principal(self):
        app = make_app(authenticator=make_api_key_authn())

        @app.get("/whoami")
        async def whoami(principal: Principal = Depends(authmod.current_principal)):
            return principal.to_dict()

        client = TestClient(app)
        self.assertEqual(client.get("/whoami").status_code, 401)   # 中间件先挡
        body = client.get("/whoami", headers={"x-api-key": API_KEY_BETA}).json()
        self.assertEqual(body["user_id"], "u_boris")
        self.assertEqual(body["tenant"], "beta")

    def test_whoami_endpoint_only_exists_when_auth_is_enabled(self):
        """`/v1/whoami`：启用鉴权时给调用方看**自己**的身份；没启用就 404。

        为什么要它：`/v1/turns` 要求请求体声明身份并与凭据一致，而 API key 的默认
        user_id 是别名 —— 客户端需要一个"我是谁"的合法来源。它只回调用方自己的
        身份，不构成越权信息泄露。
        """
        client = TestClient(make_app(authenticator=make_api_key_authn()))
        self.assertEqual(client.get("/v1/whoami").status_code, 401)   # 无凭据
        response = client.get("/v1/whoami", headers={"x-api-key": API_KEY})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["principal"]["user_id"], "u_alben")
        self.assertEqual(body["principal"]["tenant"], "acme")
        self.assertEqual(body["scheme"], "api_key")
        self.assertEqual(body["retry_after_s"], 0.0)
        # 鉴权关闭：能力不存在（404），而不是 500 或返回一个默认身份
        self.assertEqual(TestClient(make_app()).get("/v1/whoami").status_code, 404)


# ══════════════════════════════════════════════════════════════
# 六、匿名模式的 HTTP 语义
# ══════════════════════════════════════════════════════════════
class TestAnonymousHttp(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.client = TestClient(make_app(
            self.gateway, authenticator=authmod.Authenticator(allow_anonymous=True)))

    def test_anonymous_can_read_info(self):
        """② 无凭据 + allow_anonymous=True → 放行（不是 401）。"""
        response = self.client.get("/v1/info")
        self.assertEqual(response.status_code, 200)

    def test_anonymous_principal_reaches_gateway_when_body_matches(self):
        response = self.client.post("/v1/turns",
                                    json={"goal": "x",
                                          "principal": PRINCIPAL_BODY_ANON})
        self.assertEqual(response.status_code, 200)
        sent = self.gateway.requests[-1]["principal"]
        self.assertEqual(sent["user_id"], "anonymous")
        self.assertEqual(sent["scopes"], [])      # 空 scope：Gateway 会 fail-closed

    def test_anonymous_cannot_claim_a_named_user(self):
        """匿名身份声明成 u_alben 就是冒名 → 403。"""
        response = self.client.post("/v1/turns",
                                    json={"goal": "x",
                                          "principal": PRINCIPAL_BODY_ACME})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.gateway.requests, [])


# ══════════════════════════════════════════════════════════════
# 七、端到端：真 Gateway + 真鉴权 → RBAC 仍然说话
# ══════════════════════════════════════════════════════════════
class TestEndToEndWithRealGateway(unittest.TestCase):
    def _client(self, scopes):
        gateway = serve.build_gateway_from_env(env={"AGENT_RUNTIME_LLM": "scripted"})
        key = "sk-e2e-0123456789abcdef0123456789abcdef"
        principal = Principal(user_id="u_e2e", roles=("sre",), tenant="acme",
                              scopes=frozenset(scopes))
        app = serve.create_app(
            gateway=gateway, logger=JsonLogger(sink=SILENT), metrics=Metrics(),
            shutdown=ShutdownCoordinator(), deps={"store": GoodDep()},
            authenticator=authmod.Authenticator(api_keys={key: principal}))
        return TestClient(app), key

    def test_valid_credentials_reach_the_real_runtime(self):
        client, key = self._client({"runtime:run"})
        response = client.post("/v1/turns",
                               json={"goal": "帮我看看连接池",
                                     "principal": {"user_id": "u_e2e", "tenant": "acme",
                                                   "scopes": ["runtime:run"]}},
                               headers={"x-api-key": key})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["vertex"]["tenant"], "acme")

    def test_scope_from_credentials_is_enforced_by_rbac(self):
        """㉖ 凭据只给 tool.read → Gateway 的准入判 `forbidden` 403。

        注意这里**请求体声称有 runtime:run**：它被 `_principal_conflict` 拦下（403）
        或被执行侧拦下（403）。两种都必须 403 —— 不允许"body 多声明就多权限"。
        """
        client, key = self._client({"tool.read"})
        response = client.post("/v1/turns",
                               json={"goal": "帮我看看连接池",
                                     "principal": {"user_id": "u_e2e", "tenant": "acme",
                                                   "scopes": ["runtime:run"]}},
                               headers={"x-api-key": key})
        self.assertEqual(response.status_code, 403)
        self.assertIn("scopes", detail_of(response))

        # 只声明凭据真实拥有的 scope → 放行到 Gateway，由它判 scope 不足
        response = client.post("/v1/turns",
                               json={"goal": "帮我看看连接池",
                                     "principal": {"user_id": "u_e2e", "tenant": "acme",
                                                   "scopes": ["tool.read"]}},
                               headers={"x-api-key": key})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["error"].startswith("forbidden"))


# ══════════════════════════════════════════════════════════════
# 八、环境变量装配（main() 的接线路径）
# ══════════════════════════════════════════════════════════════
class TestEnvBuilder(unittest.TestCase):
    def test_nothing_configured_returns_none(self):
        self.assertIsNone(serve.build_authenticator_from_env(env={}))

    def test_api_keys_spec_parsed_with_colon_scopes(self):
        """`runtime:run` 自带冒号：只按前两个冒号切，scope 不能被剁碎。"""
        authenticator = serve.build_authenticator_from_env(env={
            "AGENT_RUNTIME_API_KEYS":
                f"{API_KEY}:acme:runtime:run tool.read#{'u_alben'};"
                f"{API_KEY_BETA}:beta:runtime:run"})
        self.assertIsNotNone(authenticator)
        self.assertEqual(authenticator.schemes, ("api_key",))
        acme = authenticator.api_keys[API_KEY]
        self.assertEqual(acme.user_id, "u_alben")
        self.assertEqual(acme.tenant, "acme")
        self.assertEqual(sorted(acme.scopes), ["runtime:run", "tool.read"])
        beta = authenticator.api_keys[API_KEY_BETA]
        self.assertTrue(beta.user_id.startswith("svc_"))   # 不可逆别名
        self.assertEqual(beta.tenant, "beta")

    def test_jwt_env_is_wired(self):
        authenticator = serve.build_authenticator_from_env(env={
            "AGENT_RUNTIME_JWT_SECRET": JWT_SECRET,
            "AGENT_RUNTIME_JWT_ALGORITHMS": "HS256, HS512",
            "AGENT_RUNTIME_JWT_ISSUER": "https://idp.example",
            "AGENT_RUNTIME_JWT_AUDIENCE": "agent-runtime"})
        self.assertEqual(authenticator.schemes, ("jwt",))
        self.assertEqual(authenticator.jwt_algorithms, ("HS256", "HS512"))
        self.assertEqual(authenticator.jwt_issuer, "https://idp.example")
        token = make_jwt({"iss": "https://idp.example", "aud": "agent-runtime"})
        self.assertTrue(authenticator.authenticate(
            {"authorization": f"Bearer {token}"}).ok)

    def test_allow_anonymous_env(self):
        self.assertIsNone(serve.build_authenticator_from_env(
            env={"AGENT_RUNTIME_ALLOW_ANONYMOUS": "0"}))
        authenticator = serve.build_authenticator_from_env(
            env={"AGENT_RUNTIME_ALLOW_ANONYMOUS": "true"})
        self.assertTrue(authenticator.allow_anonymous)

    def test_malformed_spec_raises_without_echoing_the_key(self):
        with self.assertRaises(ValueError) as ctx:
            serve.build_authenticator_from_env(env={
                "AGENT_RUNTIME_API_KEYS": f"{API_KEY}:acme"})
        message = str(ctx.exception)
        self.assertIn("第 1 项", message)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn(API_KEY[:8], message)

    def test_duplicate_key_raises(self):
        with self.assertRaises(ValueError):
            authmod.parse_api_keys_spec(
                f"{API_KEY}:acme:runtime:run;{API_KEY}:beta:runtime:run")

    def test_empty_scopes_rejected_at_parse_time(self):
        with self.assertRaises(ValueError):
            authmod.parse_api_keys_spec(f"{API_KEY}:acme:")


if __name__ == "__main__":
    unittest.main()
