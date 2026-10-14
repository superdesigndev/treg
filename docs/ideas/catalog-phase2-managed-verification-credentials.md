# Idea: Catalog phase 2 — treg-managed verification credentials

> Extracted from `REFACTOR-PLAN.md` §3.3. This is an idea, not scheduled work, and nothing in it is a
> completion condition for the architecture refactor. It exists so the refactor plan stays free of
> detailed non-goals.

In phase 2, treg can become the credential channel for catalog verification:

1. A provider creates a dedicated verification team and stores the test key as a connection.
2. treg runs a basic liveness check when saving it.
3. The provider authorizes the verification machine for named tools only.
4. The pipeline calls the real API through treg without exposing plaintext credentials to scripts or
   maintainers.
5. The provider revokes access by deleting the connection or verification identity.

A side benefit: providers experience the same path their customers use.

Before implementation, all of the following must hold:

- The verification identity has tool-level least privilege and does not inherit the team's default call
  rights.
- It cannot read secrets, create local-run authorizations, or mutate tool bindings.
- Every run pins the allowed hosts, tool versions, and configuration snapshot; none can be swapped
  mid-run.
- Prefer an independent verification environment (a dedicated org inside production is a transition
  state only), with per-run and daily spending caps.
- Every call keeps a redacted audit record, and reports and credential revocations can be cross-referenced.

---

# 构想：Catalog 二期 —— treg 托管验证凭证（中文）

> 自 `REFACTOR-PLAN-CN.md` 三.3 抽出。这是构想，未排期，其中任何内容都不是架构改造的完成条件。
> 单独成文是为了让改造计划里不残留带细节的非目标。

二期可以让 treg 本身成为验证凭证通道：

1. 服务商在 treg 创建专用验证团队，把测试 key 存成连接。
2. treg 在保存时运行基础活体验证。
3. 服务商只授权验证机器调用指定工具。
4. 验证流水线通过 treg 代理调用真实 API，维护者和脚本都看不到明文 key。
5. 服务商删除连接或移除验证身份即可撤销权限。

附带的好处：服务商亲自经历客户使用 treg 的真实路径。

实现前必须满足以下安全条件：

- 验证身份只能调用指定工具，不能继承团队的默认调用权。
- 验证身份不能读取 secret、创建本地运行授权或修改工具绑定。
- 每次验证锁定允许的 host、工具版本和配置快照，不能在执行中被替换。
- 优先使用独立验证环境（生产内的独立组织仅作过渡），并设置单次和每日费用上限。
- 每次调用保留脱敏审计，凭证撤销和验证报告可以互相对应。
