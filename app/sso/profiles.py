"""EP-02 IdP 协议差异的纯配置模板（L1，零 app 内部依赖、零 db、零网络）。

PRD EP-02 §4 把企业微信/飞书/钉钉与通用 OIDC 的差异收进四个维度：默认
scope、默认 ``claim_map``、token 响应里承载访问令牌的字段名（三家国内 IdP
都不会返回 ``id_token``，识别身份必须调 userinfo 端点，因此都要有一个"拿到
的令牌怎么喂给 userinfo"的适配点）、以及把该令牌交给 userinfo 端点时的携带
方式（query 参数 / ``Authorization: Bearer`` 头 / 自定义 header 名）。

新增一家走既有协议族的 IdP——比如又一个基于标准 OIDC 的 Okta/Keycloak/
Authing 租户，或又一个企业微信自建应用——只需要在 ``identity_providers``
表里插入一行、按需覆盖 ``scopes``/``claim_map_json``，不需要在这里加新分支
或新代码（验收方式见 ``tests/test_sso_oidc_flow.py::
test_new_idp_of_existing_kind_needs_no_new_branch_in_git_diff``）。

四个 ``kind`` 本身是闭集（PRD EP-02 §3 的字段说明：``kind
'oidc|wecom|feishu|dingtalk'``），对应四种**协议家族**的差异，不是"每来一个
客户写一条 if"的黑名单式分支——同一协议家族内部差异全部靠配置字段吸收。

**互通验证状态是诚实标注,不是宣传**（``IdpProfile.interop_verified``）：
本仓库开发环境没有可用的真实企业微信/飞书/钉钉沙箱账号，``wecom``/
``feishu``/``dingtalk`` 三个 profile 的字段名、端点角色划分（``token_url``
在这里被当作"换发一个可复用的 app/corp 级 access_token"的端点、
``userinfo_url`` 是"用 code + 该 token 换用户身份"的端点）只经过通用 OAuth2
抽象与单元测试验证，**没有对接过任何一家真实服务器**——尤其企业微信真实
流程是先用 corpid/corpsecret 换一个与用户 code 无关的 app 级 token（通常要
做进程内缓存 + 并发去重，见 PRD EP-02 §10 陷阱 6），与本模块"每次登录都走
token_url 换新 token"的简化实现是否完全兼容未经验证。**只有 ``oidc``
（标准 OIDC，覆盖 Microsoft Entra ID / Okta / Keycloak / Authing）经过完整
验证**：``tests/test_sso_oidc_flow.py`` 用真实生成的 RSA 密钥对签发/篡改
id_token，走真实的签名验证 + 五项声明校验代码路径，不是打桩绕过。

界面/API 不许在没验证的地方看起来像验证过——``app.sso.admin_api`` 的 IdP
配置读接口把 ``interop_verified``/``interop_note`` 一并返回，管理员配置
企业微信/飞书/钉钉时应该能在界面上看到"未经真实互联验证"这行字，而不是
被"支持"两个字误导成"已经跑通"。
"""
from __future__ import annotations

from dataclasses import dataclass

_UNVERIFIED_NOTE = (
    "未经真实互联验证：本环境没有可用的企业微信/飞书/钉钉沙箱账号，"
    "此 profile 只做过通用 OAuth2 抽象的单元测试，从未对接过真实服务器，"
    "接入前请先用真实测试账号跑通一次完整登录再上生产。"
)
_VERIFIED_NOTE = (
    "已验证：id_token 签名（RS256/ES256）+ iss/aud/exp/nonce/sub 五项声明校验"
    "走真实代码路径测试（RSA 密钥对现场生成签发/篡改），非打桩绕过。"
)


@dataclass(frozen=True)
class IdpProfile:
    kind: str
    label: str
    default_scopes: str
    default_claim_map: dict[str, str]
    # 只有标准 OIDC 才签发 id_token；其余三家是纯 OAuth2 + 私有 userinfo 端点，
    # 身份的真源是 userinfo 响应本身（这不是"只信 userinfo 端点"的安全捷径——
    # 那条禁令针对的是"OIDC 场景下跳过 id_token 验签、只信 userinfo"，这三家
    # 协议本来就没有 id_token 可验）。
    requires_id_token: bool
    userinfo_token_location: str  # "header" | "query"
    userinfo_token_param: str  # header 名（如 "Authorization"/自定义头）或 query 参数名
    interop_verified: bool  # 是否对接过真实 IdP 服务器（见模块文档）
    interop_note: str  # 给管理面展示的诚实说明，不是宣传文案


PROFILES: dict[str, IdpProfile] = {
    "oidc": IdpProfile(
        kind="oidc",
        label="通用 OIDC（Microsoft Entra ID / Okta / Keycloak / Authing 等）",
        default_scopes="openid profile email",
        default_claim_map={
            "subject": "sub", "username": "preferred_username",
            "display_name": "name", "email": "email", "dept": "department",
            "groups": "groups",
        },
        requires_id_token=True,
        userinfo_token_location="header",
        userinfo_token_param="Authorization",
        interop_verified=True,
        interop_note=_VERIFIED_NOTE,
    ),
    "wecom": IdpProfile(
        kind="wecom",
        label="企业微信",
        default_scopes="snsapi_base",
        default_claim_map={
            "subject": "userid", "username": "userid", "display_name": "name",
            "email": "email", "dept": "department", "groups": "groups",
        },
        requires_id_token=False,
        userinfo_token_location="query",
        userinfo_token_param="access_token",
        interop_verified=False,
        interop_note=_UNVERIFIED_NOTE,
    ),
    "feishu": IdpProfile(
        kind="feishu",
        label="飞书",
        default_scopes="",
        default_claim_map={
            "subject": "open_id", "username": "en_name", "display_name": "name",
            "email": "enterprise_email", "dept": "department_ids", "groups": "department_ids",
        },
        requires_id_token=False,
        userinfo_token_location="header",
        userinfo_token_param="Authorization",
        interop_verified=False,
        interop_note=_UNVERIFIED_NOTE,
    ),
    "dingtalk": IdpProfile(
        kind="dingtalk",
        label="钉钉",
        default_scopes="openid",
        default_claim_map={
            "subject": "unionId", "username": "nick", "display_name": "nick",
            "email": "email", "dept": "department", "groups": "department",
        },
        requires_id_token=False,
        userinfo_token_location="header",
        userinfo_token_param="x-acs-dingtalk-access-token",
        interop_verified=False,
        interop_note=_UNVERIFIED_NOTE,
    ),
}


def profile_for(kind: str) -> IdpProfile:
    try:
        return PROFILES[kind]
    except KeyError as exc:
        raise ValueError(f"未知的 IdP kind：{kind!r}，仅支持 {sorted(PROFILES)}") from exc
