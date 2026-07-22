# Phase 9B：用户、组织、RBAC 与生产数据模型

状态：**已冻结**

冻结日期：2026-07-22

基线：`origin/master` at `ed50fb27186f8627bd67a260fb22c501671bd31e`

任务分支：`codex/phase9b-identity-rbac`

## 目标

把 Week 7 的单一静态 Bearer、单租户仓库注册表和启动时 SQLite 建表，升级为可替换身份边界、
组织隔离、仓库授权、可追责审计和正式版本迁移支撑的 Review 服务基础。交付必须：

1. 为所有生产 Review 数据建立明确的 `organization_id`、`repository_id` 和主体 lineage；
2. 以 `org_admin`、`maintainer`、`reviewer`、`viewer` 实施默认拒绝的 RBAC；
3. 让 REST、MCP 和内部服务读取/修改都通过同一 principal + repository 授权路径；
4. 以不可逆摘要保存短期访问凭据，并允许立即吊销；
5. 提供组织成员/角色、仓库、凭据和审计记录的最小管理 API；
6. 以 Postgres 为生产数据库目标，以 SQLite 为本地开发/单机测试兼容模式；
7. 使用显式、版本化 migration 支持空库初始化和 Week 7 SQLite 升级，并在 schema 未到目标版本
   或迁移失败时拒绝服务启动；
8. 保持既有 Week 7 REST、MCP、Webhook 和协议安全语义兼容，且不调用真实 GitHub、OAuth、
   模型或其他外部服务。

## 非目标

- 不实现完整前端、登录页、OAuth authorization flow、OIDC discovery/JWKS 网络获取或 GitHub
  App 安装流程；标准 OIDC/JWT 只通过可替换 `AuthBackend` 和经外部验证的 claims 映射接入；
- 不实现真实 GitHub 发布、自动发布、Repair 远程审批、真实 provider 调用或云部署；
- 不把 GitHub Webhook HMAC 映射为用户 principal，也不允许 Webhook 创建用户审批；
- 不实现分布式 durable queue、跨区域复制、在线 schema migration 或数据库自动 failover；
- 不修改 Finder/Verifier prompt、sentinel、评测判据、`eval/` 或 `eval/holdout/`；
- 不引入用户个人记忆；反馈只绑定组织/仓库/Finding 版本；
- 不声称完成生产 OAuth、容量验证、业务 KPI 或 guarded publish 生产安全评审。

## 用户、组织、仓库和角色模型

### 主体与租户

- `organizations` 是租户根；其主键本身就是 organization identity。
- `users` 是组织内身份，必须包含 `organization_id`；同一外部 subject 在不同组织映射为不同
  user，避免全局用户行成为跨租户旁路。
- `memberships` 为用户在组织内的唯一角色快照，角色只能是
  `org_admin|maintainer|reviewer|viewer`。
- `repositories` 必须包含 `organization_id`；GitHub `owner/repo` 为外部稳定别名，数据库只保存
  operator 注册别名，不接收调用方文件系统路径。
- `repository_access` 把非 `org_admin` membership 显式绑定到可访问仓库；`org_admin` 在本组织
  内拥有仓库管理和读取权限，但不因管理员身份自动获得 Finding 发布审批权。
- `Principal` 至少包含 principal/user/organization identity、role、auth method 和 credential
  identity；资源查询不信任请求体中的 organization identity。

### 生产实体

下列表全部进入正式 migration；除 `organizations` 自身外，每条生产业务记录必须直接携带
`organization_id`，并通过复合查询或外键 lineage 绑定同组织资源：

- `organizations`
- `users`
- `memberships`
- `repository_access`
- `repositories`
- `access_credentials`
- `review_sessions`
- `review_jobs`
- `findings`
- `finding_feedback`
- `approvals`
- `audit_events`
- `webhook_deliveries`
- `provider_usage`

`review_jobs` 取代 Week 7 `jobs`；`webhook_deliveries` 取代 Week 7 `deliveries`。迁移保留历史 job
identity、状态、结果和 delivery 幂等映射，并归入显式的本地 legacy organization/repository；
不把 inline diff、Token、Authorization header、Cookie、Webhook secret 或原始 provider secret
写入任一表。

## 权限矩阵

`read` 始终还要求同组织且已获目标 repository access；随机资源 ID 命中其他组织时返回 not
found，避免泄露资源存在性。

| 操作 | viewer | reviewer | maintainer | org_admin |
| --- | --- | --- | --- | --- |
| 获取当前 principal | 是 | 是 | 是 | 是 |
| 读取获权仓库的 job/trace/Finding/feedback/approval | 是 | 是 | 是 | 是 |
| 提交 Review | 否 | 是 | 是 | 否 |
| 提交 Finding feedback | 否 | 是 | 是 | 否 |
| 批准/拒绝 Finding 发布 | 否 | 否 | 是 | 否 |
| 查询组织成员和角色 | 否 | 否 | 否 | 是 |
| 新增成员、修改角色和仓库授权 | 否 | 否 | 否 | 是 |
| 注册/查询仓库，管理预算和策略字段 | 否 | 否 | 否 | 是 |
| 创建/吊销自己的短期凭据 | 是 | 是 | 是 | 是 |
| 吊销组织内其他凭据 | 否 | 否 | 否 | 是 |
| 查询受控审计记录 | 否 | 否 | 否 | 是 |

角色不是“高角色自动包含全部低角色”的单调层级：组织管理员不能代替 maintainer 进行具体
Finding 审批。任何用户都不能通过修改自身 membership 提权；membership 修改只接受另一名
`org_admin`，且目标角色变化必须审计。

## 身份与安全边界

- `AuthBackend` 是可替换协议；HTTP bearer、测试 fake principal、外部已验证 OIDC/JWT claims
  映射共享同一 `Principal` 输出，授权层不解析 provider-specific token。
- 数据库 API token 使用至少 256-bit 随机值；响应只在创建时返回明文，数据库仅保存
  SHA-256 摘要、非敏感 prefix、到期时间和吊销时间。高熵 token 摘要用常量时间比较；每次请求
  查询当前 credential 状态，所以吊销立即生效。
- 标准 OIDC/JWT 适配器只接受由部署方验证签名、issuer、audience、expiry 后传入的 claims；
  本阶段不联网下载 key，也不把 JWT 保存到数据库或 trace。
- local-development 静态 token 仅作为显式兼容模式：必须设置 opt-in，必须使用 loopback bind，
  并映射到确定的本地 principal；非 loopback 地址与该模式组合时服务拒绝启动。
- 测试使用确定性 `FakeAuthBackend`；不得依赖真实 OAuth、GitHub 或模型。
- Authorization、Cookie、API token、Webhook secret/header 和原始请求凭据不得写入数据库、
  canonical trace、异常响应或应用日志。审计只记录 credential ID/auth method，不记录 secret。
- Webhook HMAC 仅证明 delivery 来源；Webhook 使用 system actor 创建 job，不产生用户 principal，
  且审批 API 永不接受 Webhook 身份。
- 跨组织资源访问使用同一 `organization_id` predicate，并在资源存在性未知和跨租户两种情况下
  返回相同 not-found 形状。

## 数据库和 migration 策略

- 生产配置使用 `CRAG_DATABASE_URL=postgresql+psycopg://...`；SQLAlchemy 2 作为方言/事务边界，
  Psycopg 3 作为 Postgres driver，Alembic 作为唯一 schema 版本来源。
- migration 是独立运维步骤（`crag-db upgrade`）；HTTP/MCP worker 启动只执行只读 revision
  检查，绝不调用 `upgrade`，因此多个 worker 不会竞争 DDL。
- 迁移失败必须回滚当前 revision（在后端支持 transactional DDL 的范围内），并使命令非零；
  schema 不是 Alembic head 时 service factory 在创建 executor/MCP session 前失败。
- 首个 revision 创建 Phase 9B schema；升级 revision 检测 Week 7 `jobs`/`deliveries`，在单事务内
  复制到带 organization/repository lineage 的新表后再移除旧表。升级测试覆盖状态、结果、
  delivery 幂等 identity 和失败回滚/未达 head 拒绝启动。
- migration 不由 Web 请求触发；不使用 `CREATE TABLE IF NOT EXISTS` 掩盖 drift；不允许每个
  worker 在 startup 自动修改 schema。
- 数据访问用显式事务；状态迁移、Finding 落库和审计事件在同一事务内完成。授权失败也写入
  限定字段的审计事件，但跨租户 not-found 审计不能包含另一租户资源内容。

## SQLite 开发兼容策略

- SQLite 只支持本地开发和单机测试，使用现有 state directory 文件锁保证一个进程拥有数据库
  与 trace；不声称支持多 worker/多主机生产并发。
- `JobStore(state_dir)` 的既有直接构造保留为本地/测试兼容入口：持有 state lock 后可对空库运行
  migration；`create_review_service_from_env` 的生产路径默认只检查 revision。
- 显式 `CRAG_DATABASE_URL=sqlite:///...` 必须配合本地模式；公网/非 loopback bind 禁止启用
  local static token。
- SQLite 开启 foreign keys、busy timeout 和 WAL；测试覆盖空库 migration、Week 7 schema
  upgrade、pending migration 拒绝启动和单进程锁。

## 允许新增的依赖

仅允许以下运行时依赖及其解析后的必要传递依赖：

- `SQLAlchemy>=2.0,<3`
- `alembic>=1.13,<2`
- `psycopg[binary]>=3.1,<4`

不新增 OAuth client、远程 JWKS client、密码哈希框架、Web UI 或数据库服务。访问 token 是高熵
机器凭据而非用户密码；本阶段不使用快速密码哈希处理低熵密码。`requirements.lock` 必须随
`pyproject.toml` 更新并通过安装/`pip check`；`requirements.txt` 继续只指向 editable project。

## 公共 API 兼容边界

- 保留 `crag`、`crag-service`、`crag-mcp` 入口及现有 CLI 参数；新增 `crag-db`，不改变已有入口。
- 保留 REST 路径和主要响应字段：`/v1/reviews/diff`、`/v1/reviews/pr`、
  `/v1/reviews/{id}`、`/v1/reviews/{id}/trace`、`/webhooks/github`、`/mcp`、`/healthz`。
- 既有 Review schema version、job 状态、Webhook HMAC/幂等、body/diff/result/trace 上限、
  Host/Origin 防护和稳定错误形状保持兼容；新增 principal/tenant 校验不能回显 secret。
- `ReviewService`/`JobStore` 的 Week 7 直接构造和调用在显式本地测试模式保持工作；新生产调用
  必须提供 principal。新代码不得让无 principal 的远程请求落入隐式管理员。
- MCP 保留原工具、resources 和 prompt 名称；HTTP MCP principal 通过请求上下文注入，stdio
  只在显式本地 principal 模式工作。跨租户 MCP get/resource/trace 与 REST 使用同一授权函数。
- 新增最小 API：当前 principal；组织 membership 查询/管理；仓库注册/查询；凭据创建/吊销；
  Finding feedback；Finding 批准/拒绝；受控 audit 查询。

## Audit Event 合同

每条 `audit_events` 至少包含：

```text
id, principal_id, organization_id, action, resource_type, resource_id,
decision, policy_version, occurred_at_utc, correlation_id
```

另可保存 `repository_id`、`credential_id`、`auth_method` 和稳定 reason code。`decision` 只能是
`allow|deny|error`；`occurred_at_utc` 由服务端生成；correlation ID 来自受限请求/run context，
缺失时服务端生成。不得保存 header、Cookie、token、diff、prompt、trace body、异常消息或路径。
审计查询仅限本组织 `org_admin`，按 UTC 时间/稳定 ID 分页并设置硬上限。

## Single Writer 文件清单

Codex 在本任务中仅拥有以下路径的写权限：

- `docs/plans/phase9b-identity-rbac.md`（本合同）；
- `README.md`、`docs/protocol-service.md`（配置、迁移、身份和当前能力说明）；
- `pyproject.toml`、`requirements.lock`、`requirements.txt`（仅上述依赖/入口及必要说明）；
- `Dockerfile`、`Dockerfile.service`（仅复制已批准的 Alembic 配置/migration 资源，保证现有
  package/container smoke 可安装 `crag-db`）；
- `alembic.ini`、`migrations/**`（正式 schema migration）；
- `src/code_review_agent/database.py`、`src/code_review_agent/identity.py`（新增）；
- `src/code_review_agent/service_core.py`、`src/code_review_agent/service.py`、
  `src/code_review_agent/mcp_server.py`（tenant-aware service/adapters）；
- `tests/test_phase9b_identity_rbac.py`、`tests/test_phase9b_migrations.py`（新增）；
- `tests/test_week7_service.py`、`tests/test_week7_service_core.py`、`tests/test_week7_mcp.py`
  （仅在兼容构造、principal 注入或 migration fixture 必需时修改）。

其他所有路径只读。尤其禁止读取或修改 `eval/`、`eval/holdout/`。若实现发现必须触碰清单外
路径，必须先向用户说明并获批，再修订本合同；不得先改后报。

## 验证命令

全部命令使用项目虚拟环境，不调用外部模型/GitHub/OAuth/Postgres 服务，不读取 `eval/` 或
`eval/holdout/`：

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $repoRoot "src"

# 定向身份/RBAC/迁移与 Week 7 回归
& $python -m unittest -v tests.test_phase9b_identity_rbac `
  tests.test_phase9b_migrations tests.test_week7_service `
  tests.test_week7_service_core tests.test_week7_mcp

# 全量离线门禁（禁止 --eval-assets）
& $python -m ruff check .
& $python -m mypy src/code_review_agent
& $python scripts\verify.py

# 依赖、迁移 CLI 和 diff 自审
& $python -m pip check
& $python -m code_review_agent.database --help
git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

迁移测试必须在临时目录内构造空 SQLite 与最小 Week 7 legacy SQLite，不读取已有 state 或
评测资产。Postgres 仅做 SQLAlchemy/Alembic 配置和方言单元边界验证；本阶段不连接真实服务。

提交前还必须逐文件通读 `git diff`，确认：改动集合是 Single Writer 子集；lock 与声明一致；
没有 token/key/header/cookie/绝对私密路径；没有付费/真实外部调用；没有调试残留。

## 验收标准

- 核心表由 Alembic 空库 migration 创建，Week 7 job/delivery 可升级且 identity/状态/结果/幂等
  关系保留；schema pending/失败时服务不创建 worker 或部分启动；
- 生产配置明确推荐并支持 Postgres URL，SQLite 明确限于本地/单机测试；worker 不执行 DDL；
- 所有业务表具备 organization lineage；review/Finding/feedback/approval/trace/job 查询使用
  principal organization + repository access predicate；
- A 组织 principal 无法通过 REST、MCP 或 service API 读取/修改 B 组织 job、trace、Finding、
  feedback 或 approval，并获得与不存在资源一致的响应；
- viewer 只能读，不能提交 Review、feedback、approval 或发布；reviewer 能提交 Review/feedback，
  不能批准、管理仓库或修改自身角色；maintainer 能对获权仓库批准/拒绝；org_admin 能管理成员、
  仓库、预算/策略和审计，但不能替代 maintainer 审批 Finding；
- API credential 只落不可逆摘要，创建明文只返回一次，吊销后下一请求立即失败；header、Cookie、
  token 和 secret 不出现在数据库、trace、响应或审计 payload；
- Webhook HMAC 只能产生 system-attributed Review job，不能创建用户 approval；重复 delivery 不
  新增 job；
- 最小管理 API 能获取当前 principal、查询/管理成员角色、注册/查询仓库、创建/吊销凭据并查询
  受组织限制的 audit；
- audit 至少具备冻结字段，allow/deny 决策均有稳定 correlation ID 和 UTC 时间；
- 既有 Week 7 REST/MCP/Webhook 测试与全量离线门禁通过；Ruff、mypy、`scripts/verify.py`、
  `pip check`、`git diff --check` 全通过；
- 任务分支产生本地稳定提交；只有全部验收通过才可 push、创建 Draft PR、等待 CI、转 Ready、
  复核 mergeability、通过 PR 合并 master，并在合并后确认 master CI 全绿。

## 回滚和 migration 风险

- 结构性迁移的安全回滚优先采用数据库备份/恢复和应用版本回退；已把 legacy 表复制并删除后，
  downgrade 只用于开发验证，不承诺在生产无损重建旧 schema。上线前必须备份并在副本演练。
- SQLite 的 DDL 事务能力和锁语义弱于 Postgres；legacy 升级必须在独占 state lock 下执行，失败
  后服务不得启动，并保留原数据库备份供人工恢复。
- Postgres DDL 通常可事务回滚，但扩展、驱动断连和运维权限仍可能留下外部状态；migration
  command 必须串行、先备份、先 staging 演练，worker 只接受 exact head。
- legacy Week 7 数据没有真实组织主体；迁移只能诚实归入隔离的 `local-legacy` tenant，不能
  推断历史用户。上线前由管理员显式认领/导出，不允许自动并入生产组织。
- 用户按组织存储简化了隔离但不支持一个全局 user row 跨组织；未来若引入全局 identity，必须
  新阶段设计 subject linking、隐私删除和跨租户防关联，不能原地删除 organization predicate。
- SHA-256 适用于本阶段 256-bit 随机 token，不适用于用户密码；若未来接收低熵密码，必须新增
  专门密码哈希方案和迁移。
- 当前队列仍是单进程 executor，Postgres 化数据模型不等于 durable worker；进程崩溃恢复、
  多 worker claim/lease 和容量测试仍是后续风险。

## 变更控制

本合同自创建后冻结。仅允许修正可验证的事实错误，或在用户明确批准范围变化后先更新合同再
实施。任何修订必须在最终报告中列为偏差；不得以实现便利为由静默扩大依赖、公共 API、写路径
或外部调用范围。

### 已批准修订

- 2026-07-22：容器式打包验证证明现有两个 Dockerfile 只复制 `src/`，新增的 Alembic data
  files 因缺少 `alembic.ini`/`migrations/` 而使 wheel build 失败。用户明确批准把
  `Dockerfile`、`Dockerfile.service` 加入 Single Writer；修改仅限复制 migration 资源，不改变
  镜像入口、用户、网络、系统包或运行权限。
