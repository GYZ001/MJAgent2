# 企业生产级能力总纲（Enterprise Readiness）

> 版本 v1.0 ｜ 日期 2026-09-10 ｜ 角色分工：本文档与各 EP 由产品（主会话）出，代码由子代理实现，验收回到主会话。
> 本目录是**增补**，不替代 `PRD/README.md` 的产品主文档。

---

## 0. 结论先行

当前形态是「单机 SaaS 试验品」：有账号、有配额档位、有支付，但**企业买家在签合同前会逐条问的东西一个都没有**——
组织与角色、与企业目录打通的登录、批量开户与离职处置、按部门分账的资源治理、模型与密钥的可运营管理。

差距不在功能数量，在**可运营性**：今天所有企业级动作（改配额、换模型、发凭据、查谁用了多少）要么只有一个全局开关、
要么只能改库。本总纲把缺口拆成 6 个交付单元，其中 EP-01/03/04/05 构成「可签单最小集」。

---

## 1. 现状事实（已核实，2026-09-10 实测，全部带出处）

| 域 | 现状 | 出处 | 企业差距 |
|---|---|---|---|
| 认证 | 本地账号 + `hashlib.scrypt`，管理员开户、无自助注册；会话走 `X-Manju-Session` 头 + `user_sessions` 表，滑动过期、登录节流 | `app/auth/passwords.py`、`app/auth/sessions.py`、`app/auth/api.py:99` | 无 SSO、无密码策略、无会话策略（并发数/空闲超时/强制下线） |
| 身份接缝 | `users` 表已有 `auth_provider`（恒 `local`）、`external_subject`（恒 NULL） | `app/db.py:76-89` | 接缝在，实现为零 |
| 角色 | **两档**：`is_system_admin` / 普通用户。`Principal.all_scopes` 对任何登录用户返回全部 6 个 scope | `app/auth/principal.py` | 无组织、无团队、无自定义角色、无项目级授权 |
| 隔离 | `projects.owner_user_id` 单一判据，在 HTTP 边界统一拦截，四种解析（owner/admin_only/creator/none），不 fail-open | `app/authz/resolve.py`（275 行） | 结构正确、可复用；但天然不支持「多人协作同一项目」 |
| 权限素材 | 76 条命令（74 条带 REST 路由、10 条 `admin_only`）、风险分档 R0(1)/R1(20)/R2(38)/R3(17)、6 个 scope；另有 74 条豁免路由（每条带书面理由） | `app/capabilities/catalog.py` + `registry.py`，实测 | 素材齐备，缺的是「把它变成可编辑的角色」 |
| 服务账号 | MCP Token 有 `scopes_json`/`expires_at`/`revoked_at`/`last_used_at`，是全仓唯一真正生效的细粒度授权 | `app/db.py:859`、`app/mcp/auth.py` | 只服务 MCP，不能给企业系统集成用 |
| 配额 | 五档硬编码 `TIER_TABLE`，维度 projects/concurrency/token/video_seconds/image，30 天滚动 + 加量包 + 到期 | `app/quota_tiers.py`、`app/quota.py`(559 行)、`app/quota_addon.py`、`app/quota_expiry.py` | 无存储维度、无组织/团队聚合、无预算预警、无项目级公平调度（CLAUDE.md 已把「每项目并发配额/防饿死调度」记为已知欠账） |
| 计费 | 支付宝/微信下单，手写 RSA/AES-GCM/ASN.1 | `app/payments/`（`crypto_rsa.py` 自述「正式上线前应换成 cryptography」） | 企业采购是合同制 + 发票 + 席位，形态不匹配 |
| 模型 | 模型库 = `settings` 表两个 JSON blob（`custom_models`/`model_credentials`）；API Key 明文落库、明文写 `.env`；provider 是 6 家硬编码白名单；用途绑定是 4 个全局单选 | `app/model_registry.py`、`app/system_api.py:731,2205`、`app/monitoring.py:173-177` | 无加密、无轮换、无按组织覆盖、无故障转移、无健康度/限速、无成本分账 |
| 审计 | `operation_audit` 字段齐全（actor/source/event/target/outcome/http_status/duration/ip/UA/args），365 天保留，总线 + HTTP 双记录点 | `app/audit/store.py`、`app/audit/retention.py` | 缺导出与 SIEM 对接；缺「配置类变更」的专门视图 |
| 部署 | SQLite 单文件 62 张表 + 手写 `MIGRATIONS`（~150 条 ALTER），无 ORM/Alembic；依赖只有 6 个；无 Docker；A→B 反向隧道 + systemd | `app/db.py`、`requirements.txt`、`docs/deploy-two-servers.md` | 无一键交付物、无恢复演练、无指标端点、无许可控制 |

**两条必须记住的历史教训**（已写进 CLAUDE.md，本轮所有设计都受其约束）：
1. SQLite 写锁曾把事件循环整站冻住 30 秒——任何新增的「每请求都要写一行」的设计（用量、审计、健康）都必须走独立连接、不等锁。
2. 授权必须早于幂等缓存查询，否则无权用户可命中有权用户的缓存结果（`app/capabilities/bus.py` 的既有分工不能动）。

---

## 2. 目标形态：一套代码，两种产品形态

新增**产品形态开关** `deployment_profile`（`saas` | `enterprise`），不是 feature flag 堆叠，是决定所有取舍的根：

| 维度 | `saas`（现状，不动） | `enterprise`（本轮新增） |
|---|---|---|
| 开户 | 管理员开户 + 档位 | SSO 自动开户 / CSV 批量导入 / 邀请链接 |
| 配额来源 | 五档订阅 + 支付加量包 | 采购额度池 → 按组织/团队/人分配 |
| 支付入口 | 保留 | **不渲染、路由 403**（不是隐藏按钮，是后端拒绝） |
| 角色 | 两档 | 组织/团队/自定义角色 |
| 模型凭据 | 全局 | 全局 + 按组织覆盖 |

理由：企业客户不会用支付宝买 token；而 SaaS 侧已上线不能删。形态开关让两者共存且互不污染判定逻辑。

---

## 3. 交付单元与依赖

| 单元 | 名称 | 优先级 | 依赖 | 一句话 |
|---|---|---|---|---|
| [EP-00](./EP-00_商业化落地路径.md) | 商业化落地路径（销售与交付作业指导） | **并行** | — | 卖给谁、怎么打包定价、POC 到签单的 12 条检查表、交付 SOP、SLA 与合规 |
| [EP-01](./EP-01_权限模型与自定义角色.md) | 组织、团队、项目授权与自定义角色 | **P0** | — | 把 76 条命令变成可编辑的角色，把「1 账号 1 空间」升级成「组织内协作」 |
| [EP-02](./EP-02_企业身份接入与单点登录.md) | OIDC/企业微信/飞书/钉钉 单点登录 | **P0** | EP-01 | 员工用公司账号登录，登录即按规则落到团队与角色 |
| [EP-03](./EP-03_用户生命周期与批量导入.md) | 批量导入、邀请、密码/会话策略、离职移交 | **P0** | EP-01 | 一次开 200 个号，离职当天资产不失联 |
| [EP-04](./EP-04_资源治理与配额.md) | 额度池、三级配额、存储维度、公平调度、用量看板 | **P0** | EP-01 | 谁在用、用了多少、超了怎么办，管理员能自己解决 |
| [EP-05](./EP-05_模型管理与智能路由.md) | 模型落表、凭据加密、路由与故障转移、限速与分账 | **P0** | — | 可与 EP-01 并行，是唯一不依赖身份改造的单元 |
| [EP-06](./EP-06_部署运维与安全基线.md) | 交付物、备份恢复、指标、审计导出、许可 | P1 | — | 让客户的运维团队敢接手 |

**排期建议（R1 = 可签单最小集）**：EP-01 与 EP-05 并行起步 → EP-03 → EP-04 → EP-02 → EP-06。
EP-02 排在后面不是因为不重要，是因为它的落点（自动开户落到哪个团队/哪个角色）必须先由 EP-01 定义出来，否则会写死映射。

**R2**：SCIM 2.0、SAML 2.0、LDAP/AD 直连、审计 SIEM 推送、成本分账报表、按组织覆盖模型绑定。
**R3**：多租户（`tenant_id` 接缝已存在）、PostgreSQL、跨可用区。

---

## 4. 明确不做（防范围回潮）

- ❌ 多租户实现（本轮只留接缝，理由：单租户私有化部署是当前买家的实际形态）
- ❌ PostgreSQL 迁移（R3；触发判据写在 EP-06，不拍脑袋）
- ❌ 计费系统重做（企业形态直接关闭支付入口，不做发票/合同/对账）
- ❌ 审批流/工作流平台化（企业级不等于加流程；本项目的价值在产线）
- ❌ 前端重写、`MonitorPage.tsx` 拆分、`app/api.py` 门面重构
- ❌ 把 `admin_only` 语义改写成 RBAC 角色标记（会让 10 条命令从 Agent/MCP 排除名单漏出）

---

## 5. 冻结项（开工前一次性锁定，实施期不得改）

**依赖**：本轮**只允许**新增一个第三方依赖 `cryptography`，用途仅限三处：OIDC 的 RS256/ES256 验签、模型凭据 AES-256-GCM 加密、未来 SAML。
理由：`app/payments/crypto_*.py` 是手写实现，其自身 docstring 已写明「不是审计过的库，正式上线前应换成 cryptography」；
而 `crypto_aesgcm.py` **只有解密没有加密**，凭据加密无法直接复用。**2026-09-10 用户已拍板采纳，`cryptography==45.0.7` 已固定进 `requirements.txt` 并实测可用**。回退方案见 EP-02 §7 与 EP-05 §4——
回退可行但更慢、且 ES256 需自写，**绝不允许用「不验签只信 userinfo 端点」来绕过**。

**数据结构**：`Principal` 是唯一身份出口（未来加租户维度只改它）；`projects.owner_user_id` 保留不动，协作授权走新表 `project_grants`；
`quota_ledger` 的 `attempt_key` 幂等语义不动，只加维度与来源列。

**模块边界**：所有新模块进包，**禁止在 `app/` 根目录新增散文件**（现状 93 个文件占全后端 23.7%）；
新模块必须在 `app/LAYERS.toml` 声明层号（未声明按 fail-safe 判 L5，会制造幻影违规）。

**常量**：scope 命名空间 `manju:*` 只增不改；新增细分 scope 必须与旧 scope 保持「旧的 = 新的并集」关系，避免旧 MCP token 权限漂移。

---

## 6. 对所有子代理生效的实施约束（派单时逐条抄进去）

1. **架构闸门**：新文件 Python ≤500 行、前端 ≤300 行、测试 ≤500 行、单函数 ≤50 代码行，**一条都不许进 `[baseline.*]`**；
   **不得上调 `app/FILE_CONVENTIONS.toml` 与 `app/LAYERS.toml` 的任何阈值**（棘轮只降不升）。
2. **扇入 >100 的模块不得再加职责**：`app.db`(254)、`app.schemas`(186)、`app.evidence`(103)。新表的访问层进各自新包。
3. **禁止 `exec()` 聚合外观、禁止 `from .x import *`**；拆包必须同步补 `tests/conftest.py` 的 `patch_<pkg>_everywhere()` 与 AST 守卫测试（仓库已有 13 个可照抄）。
4. **判据从数据推导，禁止黑白名单与枚举穷举**：权限点来自能力目录、模型 provider 来自模型库、IdP 字段映射来自配置——不得再写死一张名单。
5. **拦住用户时必须给出路**，且**不得提供「强制忽略」式绕过**。
6. **所有权显式**：新增函数不留 `conn=None`；不得在调用方连接上隐式提交；回滚必须是异常处理器第一条语句。
7. **测试**：子代理**只跑定向测试**（`-k` 过滤或点名文件），禁止 `pytest -q`、`pytest tests/ -q`、`scripts/verify.py`；
   取退出码不走管道（`pytest ... > log 2>&1; echo $?`）。全量由主会话在干净 worktree 统一跑。
8. **子代理不 commit、不 push**；改完报告改了哪些文件、跑了哪些测试、结果原文。

---

## 7. 验收方法（主会话执行，不外包）

对每个 EP，验收分三层，缺一不可：

1. **行为断言**：做一次真实操作，看它是否真被挡住 / 真被放行。**不许写成「检查某字段等于某值」**——
   `tests/test_rbac_enforcement_evidence.py` 文件头已经把这条规矩写死了，本轮照用。
2. **绕过扫描**：机械列出验收过程中所有「必须改库/改配置文件才能完成」的操作，逐条裁决是真缺口还是有意为之，
   裁决理由写进对应模块的 docstring。上一轮 RBAC 用这招查出 4 处绕过，其中 1 处是真缺口。
3. **不倒退验证**：改动落在共享底层原语（`app.db`、`app.quota`、`app.capabilities.bus`）时必须跑全量——
   子集必然漏（`model_gateway` 那次子集 586 passed，全量才炸出 7 个红）。

**交付判据是原文比对，不是闸门全绿**：闸门通过只是必要条件，最终以「企业管理员能不能不改库地完成这件事」为准。
