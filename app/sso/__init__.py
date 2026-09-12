"""EP-02 企业身份接入与单点登录（PRD/enterprise/EP-02_企业身份接入与单点登录.md）。

``schema.py``（L2，lazy 建表）/ ``store.py``（L2，纯 db 读写）/ ``profiles.py``
（L1，纯配置模板）/ ``oidc_verify.py``（L1，纯 JWT 解析与签名校验，零 app 内部
依赖）/ ``oidc.py``（L3，出网 discovery/token/userinfo/JWKS）/ ``provision.py``
（L2，JIT 自动开户与角色映射）各自独立；本 ``__init__.py`` 故意不做聚合
re-export、不 import 子模块——与 ``app/orgs/__init__.py`` 同一条理由：
``api.py``/``admin_api.py``（L5，需要 ``app.authz``/``app.orgs.service`` 等
更高层依赖）由 ``app.main`` 直接引用，不经包 ``__init__``，这样 ``app.sso``
包前缀本身不需要承担任何真实 import 边，层号声明
（``app/LAYERS.toml::app.sso`` = 2）不会被 L3/L5 子模块污染。
"""
from __future__ import annotations
