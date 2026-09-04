# treg 架构改造设计

[English version](REFACTOR-PLAN.md)

> treg 是一个替调用方保管凭证、代为调用外部 API，并在需要时计费的服务。
> 这次改造不改变产品能力，而是把 1.2 万行的 `api.py` 拆成边界清楚的模块，
> 让多人开发、新服务商接入和调用量增长互不拖累。

保持不变：继续使用 Python；继续发布成一个可自托管的安装包；托管版继续使用 Postgres；
代理只做必要的凭证注入和透明转发，不理解或改写外部 API；所有余额变化都经过账本。

## 〇、不可协商清单

本文其余内容是指导；下面六条是合同。任何段落与它们冲突时，以这六条为准。

1. 一笔预留最终恰好结算或释放一次——包括超时、取消和异常路径。
2. 上游请求在途期间，数据库连接占用为 0。
3. 转发保真：只允许凭证注入和传输头改写。
4. 余额只经 money 五入口变化：grant · topup · reserve · settle · release。
5. 数据面写白名单（一.6）是穷举的；白名单外的写是缺陷，不是自由裁量。
6. 每个 PR 只承担一种风险：移动、行为变更或数据库迁移，不混批。
   *修订（2026-08-26 批准）：* 重构 PR 可另外携带至多三个 `fix:` commit 修复既有小缺陷 - 永不折进移动或
   抽取 commit、排在触碰同区域的移动之前、爆炸半径有界、不改客户端解析的 API 表面、同 commit 带回归测试
   （涉并发或钱邻接的必须 E2E + Postgres）、每个 fix 落地后重解析行号锚点。钱语义、API 表面、并发架构的
   改动仍须独立 PR。每个 PR 描述含"刻意行为变更"一节穷举列出 fix commit（或声明为零）；清单之外一律
   行为恒等。

## 一、目标结构

采用四层模块化单体。四层是代码边界，不是四个部署服务。默认仍作为一个进程运行，
未来只有调用量确实需要时，才把调用运行时独立部署。

### 1. 接口层 `routers/`

只负责把 HTTP 或 MCP 请求转换成应用层输入，再把结果转换成响应。路由里不新增业务规则。

| 模块 | 作用 |
|---|---|
| `web/` | 落地页、SEO、教程等展示页面 |
| `auth/` | 用户登录、CLI 配对、treg 的 OAuth 授权服务器入口 |
| `orgs` `resources` `connections` `billing` `catalog` `admin` `onboard` | 各功能的 HTTP 入口 |
| `call` | 接收 `/call/` 请求，建立调用输入，交给 `application.call` |
| `mcp` | MCP 协议入口 |

一期不要求所有路由全面改成 DTO，也不重写现有序列化。完成标准是路由足够薄，
并且迁移过程中不把新的判断、查询编排或金额逻辑写回路由。

### 2. 应用层 `application/`

负责一个完整用例的执行顺序、事务边界和失败补偿。这里是原方案缺少的一层。

| 模块 | 作用 |
|---|---|
| `call` | 解析目标、检查权限、预留金额、转发、按结果结算 |
| `signup` | 首次登录、创建团队、发放赠送金额 |
| `connect` | OAuth/key 接入、验证、发现资源、创建工具 |
| `billing` | 创建充值、处理 Stripe webhook、触发入账 |
| `onboard` | 落地页沙盒、demo 团队与引导种子数据（现 sandbox.py 与 onboard 路由段） |

只有具备多步编排、事务补偿或跨域顺序的用例才进入 application；单域的 CRUD 写操作由
domain 的公开写命令直接承接，不为它们制造空壳用例。

`application.call` 在拆文件前先定义阶段合同：每个阶段接收什么、返回什么、可能怎样失败、
是否开启事务，以及失败后如何释放或结算预留金额。必须保留三个现有硬约束：

- 外部 API 调用期间不占用数据库连接。
- 无论超时、取消还是异常，一笔预留金额最终只能结算或释放一次。
- call 用例的产出是流式响应，合同定为一个框架无关的小对象
  `UpstreamResponse(status, raw_headers, body_stream, close)`：infra 产生它，
  application 在其上做计费与失败补偿，router 最后包装成 Starlette StreamingResponse，
  `close` 保证上游连接一定释放；计量调用缓冲后仍包装成同一种对象。application 因此
  不依赖 FastAPI，透明转发与流式关闭语义不变。

会话与提交纪律适用于每个用例，不只 call：application 用例开启会话，并且是唯一 commit 的地方；
domain 函数永不 commit 或 rollback；session factory 由 infra 提供。中途提交的函数会静默破坏
失败补偿，import 规则查不出来，这条线只能靠纪律、代码审查和架构测试共同守住。

### 3. 领域层 `domain/`

存放可以独立说明和测试的业务规则。

| 模块 | 作用 |
|---|---|
| `identity` | 调用者身份、token、角色和权限判断 |
| `governance` | 团队、成员、项目、拒绝规则、用量和预算限制 |
| `connections` | 凭证状态、OAuth token 刷新规则、连接可用性 |
| `tools` | 自有工具、凭证绑定、skill/bundle 规则 |
| `catalog` | 目录端点、能力分类、定价和凭证选择规则 |
| `capacity` | 平台自有服务商账户的容量：策略、快照、燃烧预测与 exhausted 状态 |
| `money` | 余额、预留、结算、释放、充值和对账规则 |

`ledger` 放在 money 包内部。调用流程只能使用 money 对外提供的 `grant()`、`topup()`、
`reserve()`、`settle()`、`release()` 五个入口，不能直接操作账本表。Stripe SDK 不属于 money，它只出现在 infra 的
Stripe 适配器中。

domain 模块之间默认互不 import。跨域组合属于 application 层；跨域取数走已被允许的共享表读取，
或把值作为参数传入纯规则函数，让每个域保持可独立解释和测试。仅枚举三条有向例外：
`governance → identity`、`tools → connections`、`capacity → catalog`（只读）。`identity` 与 `money`
是叶子，不 import 任何兄弟域。该矩阵自阶段 1 起由 import-linter 固定，并决定阶段 3 的迁移顺序：先搬叶子。

### 4. 基础设施层 `infra/`

负责和数据库及外部服务打交道。

| 模块 | 作用 |
|---|---|
| `db` | 数据库引擎、会话和 Alembic 迁移 |
| `crypto` | 本地 Fernet 或托管 KMS 加密 |
| `upstream` | 共享 httpx 客户端、透明流式转发、SSRF 防护 |
| `ratestore` | 本地数据库或 Redis 的限流和短期状态 |
| `email` `stripe` | Resend 和 Stripe 的适配器 |

依赖倒置只用在确实存在外部实现或双实现的窄边界：`crypto`、`ratestore`、`email`、
`stripe` 和 `upstream`。一期不为整个数据库再包一层 repository。现有领域和应用函数
可以继续接收 `AsyncSession`，避免把这次拆分变成数据访问层重写。

### 5. 组装与共享代码

- `bootstrap/` 是唯一知道具体实现的地方，负责 `create_app(role=...)`、依赖组装和后台任务。
- `config` 只提供经过校验的配置。
- `audit` 是允许丢失的运营审计，绝不承担金额正确性。
- `analytics` 是只读统计，不反向控制业务流程。
- 数据库继续使用一个物理 schema，ORM 模型也可以共享读取。

共享读取不等于共享写入。重构开始前建立“表归属清单”，每张表只有一个模块可以写；
跨域报表和查询可以读多张表。对于无法通过 import-linter 精确检查的写入规则，
用公开写入入口、架构测试和代码审查共同保证。

归属清单必须直接写明三个已知例外，避免执行时争论：`org` 表归 governance，但
`org.balance_micro` 与自动充值字段只有 money 可写（归属降到列级）；`secret` 表归
connections，但调用运行时的 OAuth token 刷新允许回写；`callrecord` 由横切 audit 写入，
其余域只读。

### 6. 调用运行时边界

“数据面不依赖控制面”具体指：调用运行时可以读取成员权限、拒绝规则、凭证、目录价格和
余额；它的写操作限于一份枚举白名单：计费命令（reserve/settle/release）、幂等占位与释放、
OAuth token 刷新回写、审计与遥测、首次调用标记、标签预算记账、平台账户容量标记。白名单之外不得有任何写。
它不依赖管理路由、登录页面、OAuth 授权同意流程或 Stripe 充值流程。这份白名单本身就是
dataplane 的合同，架构测试按它执行（“只读 + 计费”不符合热路径事实，不能作为定义）。

这份白名单约束的是请求路径；启动序列里的写入（schema 迁移、`_backfill_provider_extra_tools`
这类数据回填）单独列在各角色的启动清单中，不受本白名单约束。该豁免是过渡性的，到期于阶段 5：
执行切换到 Alembic 后，启动期数据回填移入发布流水线，各角色启动清单不再包含任何写入，豁免随之
作废。在那之前，不得以这条豁免为由向启动清单新增任何写入。

`create_app(role="all" | "dataplane" | "control")` 必须为每个角色固定三张清单：

1. 挂载哪些路由。
2. 启动哪些后台任务。
3. 执行哪些启动检查。

同一代码包换角色只是未来独立部署的准备，不代表本次必须拆成多个服务。

`/run`（服务端 CLI 执行）**不属于 dataplane**：它起子进程、吃 CPU、需要沙箱，与薄转发
的伸缩和安全画像完全不同。最危险的工作负载不该放在最暴露的实例上。一期只做名分上的
区分：dataplane 的路由清单不含 `/run`，runner 挂在 control（或 all），role 分类中预留
独立的 `runner` 角色；真正的独立部署与容器级隔离留待二期（现有部署运行时不支持）。
它与 call 共享的 deny/上限/ACL 闸门是 domain 函数，不受此影响。

### 7. 自动检查的规则

1. routers 只调用 application 用例或 domain 的公开入口（只读查询与公开写命令）；
   路由里不得内联业务判断、查询编排或金额逻辑。
2. application 可以调用 domain，并通过窄接口使用外部能力。
3. domain 不依赖 routers、application 或具体的外部 SDK。
4. 只有 money 包内部可以写账本表。
5. 只有表的所有者模块可以写该表，跨域读取允许。
6. CLI 基础安装不能 import 服务端重依赖。
7. 每个 app role 的路由、后台任务和启动行为由测试固定。
8. 转发保真、数据库连接纪律和结算正确性由端到端测试固定，不能只依赖 import-linter。
9. domain 模块之间只允许枚举过的依赖边；其余跨域组合一律发生在 application。

### 8. 横向约定

- **读模型。** 跨域的只读查询有明确的家：域可以对外提供公开的只读查询入口（read model），
  允许跨表 join、永不写表（reconcile 是先例）。router 仍然不做查询编排。
- **treg 自产错误。** application 与 domain 抛出框架无关的语义错误类型，只有 router 把它们
  翻译成 HTTP 形状；错误词汇表进入 OpenAPI 快照，402/429/503 保持机器可读的统一结构，
  不允许各模块自造 dict。
- **域间异步反应。** 一个域对另一个域发生之事的反应，同步的一律由 application 编排；
  必须在重启后仍然完成的走显式 outbox（adsconv 先例）。不建立隐式的进程内事件总线，
  也绝不借道 audit——它会丢行。

## 二、系统拓扑与长期演进原则

四层解决代码怎么组织；下面的拓扑解决系统如何承载不同来源和不同风险的工作负载。

### 1. 一个统一的 Call Kernel

团队自有工具和平台 Catalog 是两种供给来源，不应发展成两套调用系统。二者都进入同一个
Call Kernel，统一完成身份识别、权限和预算检查、凭证选择与注入、透明转发、计费结算、
幂等和审计。差异只存在于工具来源、凭证归属和定价策略，不存在于调用主流程。

### 2. 按工作负载划分部署角色

保持同一个代码包和同一个模块化单体，但为四类工作负载建立清楚的启动角色：

- `control`：登录、团队、工具、连接、Catalog 管理和充值。
- `call`（当前代码中的 `dataplane`）：HTTP/MCP 调用、转发和结算。
- `runner`：受控的 CLI 子进程执行，使用独立沙箱、并发上限和资源限制。
- `worker`：必须可靠完成但不应阻塞请求的后台任务，例如验证、同步和 outbox 投递。

一期可以只运行 `all`、`dataplane` 和 `control`，但模块归属和启动清单从一开始按上述四种
画像设计。未来拆部署是配置和容量决策，不再需要重画领域边界。

任何 role 独立部署或副本数超过 1 之前，必须先满足三个前置条件：后台任务幂等、单例任务经
DB/ratestore 锁声明、每个进程内缓存写明失效策略。满足之前，多实例问题不算已解决。

### 3. Catalog 是一条供应链

Catalog 的正式数据流固定为：提交配置 -> 静态校验 -> 真实验证 -> 生成证据 -> 发布版本化
快照 -> Call Kernel 只消费已发布快照。验证中的草稿不能直接进入调用热路径；回滚以快照
版本为单位，不依赖在线热改配置。

### 4. Call Kernel 的平台级约束

- 公平性与准入控制（按组织/token/服务商限并发、速率、费用并背压）属于长期能力；本次各阶段均不实现。
- 客户自有凭证与平台托管凭证是两个安全域：分别设置预算、配额、轮换、熔断和事故隔离，
  不能因为底层都存放在 secret 表里就混成同一风险池。
- 后台结果分三级：金额、授权、凭证和幂等要求强一致；验证、同步和关键通知要求持久化且可
  重试；通用分析和运营审计允许丢失。允许丢失的内存队列不能承担前两类工作。

这些是长期边界，不要求一期引入 Kafka、拆数据库或拆微服务。

## 三、Catalog 接入与验证

目标是：接入常规服务商时主要提交配置；验证时运行同一套自动流程，避免依赖维护者记忆。
更准确的说法是“常规情况由配置驱动，非标准协议使用经过审核的代码插件”，而不是
“全部声明式、零 Python”。

### 1. 目录和 provider 配置

- 每个服务商一个 `catalog/<service>.yaml`，记录端点、能力分类、参数、价格、价格来源和
  复核时间。没有来源的价格不能上架。
- `capabilities.yaml` 是跨服务商比较的共同分类，`fx.yaml` 记录汇率和 credit 单价。
- provider 配置迁到 `providers/<service>.yaml` 时，必须有版本化 schema、严格字段白名单
  和启动时校验。
- 少数特殊流程通过具名插件实现。YAML 只能引用允许列表中的插件名，不能嵌入任意代码或
  任意网络行为。

### 2. 自动验证流程

一条命令依次完成：确认无效 key 会被真实 API 拒绝、用测试凭证运行安全样例、核对价格和
实际计量、生成脱敏报告。验证结果是流水线产出的带时间戳报告，不是服务商可以在 PR 中
自行填写的 `verified: true`。

每个 `test_request` 必须声明：

- 是否只读。
- 最坏费用和超时。
- 允许访问的 host。
- 使用哪个隔离测试账户。

会写数据、发消息、投广告或费用无法封顶的端点不能由通用流水线自动执行。

持续检查包括：

- 进程内调用状态矩阵，覆盖按调用计费、成功才计费、按返回量计费、失败、幂等和并发。
- 每个服务商的费用响应解析测试。
- 每晚使用独立测试凭证调用最便宜的安全端点，并设置总预算、单服务商预算和熔断。
- 价格超过 90 天未复核时明确标记为过期，不继续当成可信报价。

### 3. Catalog 二期：让 treg 保管验证凭证

一期优先让维护者自行注册测试账户，再由服务商给该账户少量测试额度。这样密钥不需要换手。
确需传递密钥时，使用私下通道，只放入环境变量，验证后立即撤销，绝不进入来自 fork 的 CI。

二期——服务商把验证 key 存成 treg 连接、流水线经 treg 调真实 API 而不暴露明文——是一个构想，
不属于本次重构。其流程与安全前置条件见
`docs/ideas/catalog-phase2-managed-verification-credentials.md`，其中任何内容都不是本次的完成条件。

本次仍不做目录热加载和自动选择服务商。treg 提供比较信息，最终选择由调用方决定。

## 四、改造顺序

原则是先建立安全网，再按完整用例逐段迁移。所谓“纯移动”只是没有改变行为的意图，
并不表示不需要验证。FastAPI 路由顺序、启动副作用、依赖覆盖和后台任务都可能在搬文件时改变。

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| 0. 安全网 | 调用状态矩阵、关键用户旅程、Postgres CI | 当前测试全绿；记录现有路由、OpenAPI 和 app 启动行为 |
| 1. 组装边界 | 引入 `create_app()`、bootstrap、import-linter；生成 Alembic 基线 | 默认 app 行为不变；各 role 清单有测试；基线与现有 schema 校验一致；HEAD 方法改写与 OpenAPI 定制收进工厂 |
| 2. 展示与接口 | 搬出展示页、catalog API、admin/reconcile router | 路由快照（含注册顺序，Starlette 按序匹配，`/catalog/search` 必须先于 `/catalog/{slug}` 注册）不变；router 没有新增业务规则 |
| 3. 身份与控制功能 | 提取 identity，再迁移 auth、团队治理、资源和连接用例 | 每次只迁一个完整用例；迁移前后同一组 E2E 保持全绿 |
| 4. 调用主流程 | 先定义 CallContext 和窄接口，再按阶段拆 `application.call` | 26 场景预期不变；转发时 DB 连接为 0；没有悬挂预留 |
| 5. 数据库迁移 | 单独切换到 Alembic 执行，替换启动期手写迁移 | 新库、旧 SQLite、旧 Postgres、滚动部署和回滚均验证；切换前声明客户端兼容窗口（旧 CLI ↔ 新服务端，N-1）；启动期数据回填移入发布流水线，任何角色的启动清单不再写数据（一.6 的启动期豁免作废） |
| 6. Catalog 一期 | provider 配置、验证报告和持续检查 | schema、安全限制、真实调用预算和证据留档完整；配置迁移期间设置接入冻结窗口或双读期；vendor 三个对外面（docs/VENDORS.md、/vendor-listing 托管页、dashboard 弹窗）同步更新 |

阶段 1 只建立 Alembic 基线和结构校验，不直接对未知生产库盲目 `stamp`。真正改变迁移执行
方式仍放在阶段 5，作为独立行为变更。托管部署由流水线单独迁移；自托管继续保留一条命令
升级的体验。

阶段 1 至阶段 5 之间不冻结 schema 变更，但立一条防漂移规则：每次 schema 变更必须同时
更新旧启动迁移和对应的 Alembic revision，并由 CI 在全新数据库上验证两条路径产生完全
相同的最终 schema。基线因此始终新鲜，阶段 5 的切换只是换执行器。若未来 control 与
dataplane 真正分开部署，还需补 expand/contract 迁移规则（相邻两个版本可同时使用同一
数据库）；在此之前，role 只是“代码可分开启动”，不宣称完整的独立部署能力。

每阶段结束后在真实环境运行一次小额检查，确认登录、连接、调用、扣费和充值主路径仍然工作。

## 五、附录：现有服务端模块的完整归宿表

每个现有服务端文件的去向。列含义：目标（层/模块）· 主要调用方 · 所属 role ·
后台任务 · 写入的表或字段（“-”= 只读或无）。api.py 本体按第一节的分区拆解，不再单列。

| 现有文件 | 目标（层/模块） | 主要调用方 | role | 后台任务 | 写入 |
|---|---|---|---|---|---|
| `proxy.py` | infra/upstream（产出 UpstreamResponse） | application.call 经 port | dataplane | 无 | - |
| `injectors.py` | infra/upstream（凭证注入） | relay | dataplane | 无 | - |
| `ledger.py` | domain/money 包内 | 仅 money 内部 | 两者 | 无 | creditblock · hold · ledgerentry · org.balance_micro |
| `billing.py` | 拆两半：Stripe SDK → infra/stripe；充值编排 → application.billing | routers.billing · webhook | control | 无 | 经 money 入口；org 自动充值字段 |
| `reconcile.py` | domain/money（只读报表） | routers.admin | control | 无 | - |
| `catalog_store.py` | domain/catalog | catalog 路由 · application.call | 两者 | 无 | - |
| `endpoint_stats.py` | domain/catalog（观测成功率/时延） | catalog 视图 | 两者 | 无 | -（读 callrecord） |
| `oauth_providers.py` | 阶段 6 迁 providers/*.yaml + 具名插件（domain/connections） | connections · call（绑定信息只读） | 两者 | 无 | - |
| `oauth.py`（token 刷新） | domain/connections | application.call · connect | 两者 | 无 | secret（刷新回写，白名单项） |
| `mcp.py` | routers/mcp | - | dataplane | 无 | - |
| `mcp_oauth.py` | domain/identity（授权服务器 grant/refresh 族） | routers.auth（发 token，control）· mcp 验 token（dataplane，只读） | 两者 | 无 | oauthclient · oauthgrant · oauthrefresh |
| `session.py` | domain/identity（浏览器会话） | auth · web；MCP token 验证共用其签名密钥 | 两者 | 无 | - |
| `runner.py` | application/run（服务端 CLI 执行；与 call 同享 deny/上限闸门） | routers（/run） | control（一期）；预留独立 runner 角色，不进 dataplane | 每次执行起子进程 | runrecord（经 audit） |
| `sandbox.py` `demo.py` `pubfeed.py` | application/onboard 下三个独立子模块：sandbox（合成响应）· seed（demo 团队/种子）· pubfeed（落地页 feed），不合并成一个文件，防杂物化 | routers.onboard · web | control（sandbox 判定在 call 内为只读） | pubfeed 的 SSE 内存态 | demo org/user 标记 |
| `adsconv.py` | application/adsconv（广告归因 outbox + 上传器） | lifespan 启动 | 仅 control 启动 worker | 有：约 300s 一轮 drain | adconversion · org 归因字段 |
| `referrals.py` | domain/referrals（小而独立，不并入 governance） | routers · signup | control | 无 | referral · user.referral_code；发credit经 money.grant |
| `health.py` | 拆两半：凭证健康 → domain/connections；host_is_public（SSRF）→ infra/upstream | routers · relay | 两者 | 无 | secret.last_error |
| `agent_pages.py` | routers/web 的渲染助手 | 仅 web | control | 无 | - |
| `audit.py` | 横切（操作日志，忙时可丢） | 全部（fire-and-forget） | 两者 | 进程内写队列 | callrecord · runrecord · searchmiss |
| `analytics.py` | 横切（外发统计） | 全部 | 两者 | flush 队列 | -（外发 PostHog） |
| `ratestore.py` | infra/ratestore（接口 + 双实现） | identity · governance 经接口 | 两者 | 无 | ephemeral |
| `db.py` | infra/db | bootstrap | 两者 | 无 | schema（阶段 5 后归 Alembic） |
| `crypto.py` | infra/crypto（接口 + 双实现） | connections · tools · call 经接口 | 两者 | 无 | - |
| `email.py` | infra/email | signup · connect · governance 经接口 | control | 无 | - |
| `models.py` | 横切（共享 schema；写权按归属清单） | - | 两者 | 无 | - |
| `config.py` | 横切 | 全部 | 两者 | 无 | - |
| `__main__.py` | bootstrap | - | - | - | - |
| `cli.py` `convert.py` `skills.py` `providers.py` `localrun.py` `localproxy.py` `shell.py` `agents.py` `egress.py` `fsjail.py` | 轻装 CLI，一律不属服务端四层；保持“不 import 重依赖”铁律 | 终端用户 | - | - | - |

登录/CLI 配对/OAuth 授权服务器的多步编排归 application 侧的 auth 用例（signup 只覆盖
“首登建队赠额”那一段）；表中未列的 api.py 内部辅助函数族按第一节层规则就近归位。
本表在阶段 0 记录路由与启动行为时一并核对，发现遗漏先补表再动代码。

## 六、词语对照

| 原词 | 更容易理解的说法 |
|---|---|
| modular monolith / 模块化单体 | 一个部署单元，代码内部按清楚边界分模块 |
| wheel | Python 安装包 |
| faithful relay | 除凭证和传输必需项外，不改请求与响应的透明转发 |
| control plane | 登录、配置、管理、充值等管理功能 |
| data plane | 接收调用、检查规则、转发并结算的运行时功能 |
| pipeline / callpipe | 一次调用从进入到结算的处理流程 |
| ledger | 记录每笔余额变化的账本 |
| gateway / adapter | 把内部接口接到 Stripe、Redis、KMS 等外部系统的实现 |
| schema | 数据库表结构，或配置文件允许的字段规则 |
| bogus-key test | 用明确无效的测试 key，确认外部 API 确实拒绝它 |
| canary / 金丝雀 | 用极小流量和严格预算做一次真实调用检查 |
| hot path / 热路径 | 每次调用都会经过、对稳定性和延迟最敏感的代码 |
| pure move / 纯移动 | 只改变代码位置，不打算改变对外行为 |
