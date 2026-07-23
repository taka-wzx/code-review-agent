# Phase 9C：持久任务队列、并发、限流、故障恢复和部署基础

状态：**已冻结**

冻结日期：2026-07-22

基线：`origin/master` at `9670fae0ddd90d180b6fbf50bf4280527ca8b4fb`

任务分支：`codex/phase9c-durable-service`

## 目标

把 Phase 9B 的单进程 `ThreadPoolExecutor` 服务升级为 API 与 worker 分离的持久 Review
服务基础。生产路径以 Postgres 为队列和租约的唯一协调者，支持多个 API、多个 worker、原子
claim、lease/heartbeat、崩溃恢复、幂等提交、组织/仓库级背压与模型调用预算，并提供可实际
启动的容器编排基础。

本阶段只执行现有只读 Review，并最多推进到 `awaiting_approval`。Phase 9D 才实现完整审批、
guarded publish 和外部 GitHub 写入。

## 非目标与授权边界

- 不做云部署、跨区域复制、数据库自动 failover、在线无停机 migration 或生产 SLO 声明；
- 不调用真实模型、真实 GitHub、真实 OAuth/OIDC/JWKS，也不发送外部评论或任何写操作；
- 不实现 Phase 9D 的审批发布，不把 `approved` 当作本阶段可达的自动状态；
- 不新增 Redis、Celery、RabbitMQ 或其他队列框架；Postgres 已能提供本阶段需要的事务、行锁和
  `SKIP LOCKED`；
- 不修改 Finder/Verifier prompt、sentinel、评测判据、Repair 或 Verifier Training；
- 不读取或修改 `eval/`、`eval/holdout/`；验证不得带 `--eval-assets`；
- 不把 fake-run、SQLite 或单机负载结果表述为生产容量或 exactly-once 证明；
- 不把 secret、Authorization/Cookie、Webhook/provider token、原始 provider header、异常原文、
  diff 正文或宿主绝对路径写入日志、trace、Compose 文件或镜像层。

## 依赖与技术选择

Phase 9C 不新增运行时第三方依赖。继续使用已经批准并锁定的：

- SQLAlchemy 2：事务和 Postgres/SQLite 方言边界；
- Psycopg 3：Postgres driver；
- Alembic：唯一 schema 版本来源；
- FastAPI/Uvicorn：API；
- Python 标准库线程、signal、文件原子操作：worker runtime 和有界 artifact 管理。

新增 `crag-worker` 包入口。`crag`、`crag-service`、`crag-mcp`、`crag-db` 的既有调用保持兼容；
`review_pr` 只做向后兼容的可选 `head_sha`/`idempotency_key` 扩展，既有两参数 MCP 调用由适配器
派生稳定的、只落摘要的兼容幂等键。`requirements.lock` 不应变化；若实现证明必须新增依赖，必须
先给出证据、取舍和用户批准，再修订本合同。

## 冻结状态机

公开 job 状态集合固定为：

```text
received
  -> queued
  -> leased
  -> running
  -> awaiting_approval
  -> approved
  -> published | declined | failed | dead_letter
```

本阶段合法转换为：

```text
received -> queued | failed
queued -> leased
leased -> running | queued | failed | dead_letter
running -> awaiting_approval | queued | failed | dead_letter
awaiting_approval -> approved | declined        # schema/interface frozen，Phase 9D 实现
approved -> published | failed                  # schema/interface frozen，Phase 9D 实现
```

- `received` 表示逻辑 job 行已创建但 durable payload 尚未完成；API 不得在该状态返回 202。
- `queued` 表示 payload/reference 已持久化且可被 claim。
- `leased` 表示数据库已授予有期限的独占执行权；`running` 表示获租 worker 已开始 runner。
- Review 成功、结果/Findings/usage 原子落库后进入 `awaiting_approval`，不再使用新 job 的
  `succeeded` 状态。
- 可重试类别耗尽总尝试次数后进入 `dead_letter`；明确不可重试类别进入 `failed`。
- 0003 migration 将历史 `succeeded` 映射为 `awaiting_approval`；有稳定 reference 的历史
  `pull_request/running` 映射回 `queued`。Phase 9B 未持久化 inline diff payload，因此其非终态
  inline job 必须 fail closed 为 `legacy_payload_unavailable`，不能伪装成 PR 重跑；保留既有
  `failed`，不得把历史成功结果丢失或重复执行。

## Job lease 与 fencing

每个 `review_jobs` 行至少新增并持久化：

```text
submission_key, idempotency_key_hash, request_fingerprint,
payload_key, queued_at, available_at,
lease_owner, lease_token, lease_expires_at, heartbeat_at,
attempt_count, max_attempts, last_error_category,
model_call_limit, model_calls_reserved, final_trace_key, updated_at
```

- 默认 lease 60 秒、heartbeat 10 秒；均可由有界环境配置覆盖，且必须满足
  `heartbeat < lease / 2`。测试使用注入时钟，不靠长时间 sleep。
- `lease_token` 是每次 claim 新生成的 fencing token。owner 名相同也不能复用旧 token。
- 所有同时涉及 quota/job 的事务统一使用
  `organization quota -> repository quota -> review_job` 锁序。claim 先无锁读取一小批候选 scope，
  再按该顺序锁 quota，最后用 `FOR UPDATE SKIP LOCKED` 锁定并重查 candidate；候选已变化就回滚
  并重试，不能反向先锁 job 再等 quota。
- SQLite 只用于本地/测试，使用数据库写事务串行化 claim；不声称具备生产多主机语义。
- 成功 claim 才把 `attempt_count` 加一。所有 `leased -> running`、heartbeat、retry、success、
  fail 更新都必须同时匹配 job ID、lease owner、lease token 和未过期 lease；rowcount 为 0 即
  丢失租约，旧 worker 不得写 Finding、usage、终态或最终 trace 指针。
- worker 死亡后不做启动扫库判失败。lease 到期后另一个 worker 可 claim 同一逻辑 job，并获得
  新 token；旧 worker 即使恢复，也只能产生被 fencing 拒绝的过期尝试。
- 一个逻辑 job 最多有一个可见结果，但 attempt 执行语义是 **at-least-once**。外部副作用仍被
  禁止；本阶段不声称任意进程/网络分区下 exactly-once 模型调用。

## Heartbeat 与 graceful shutdown

- worker 启动时在 `worker_instances` 注册稳定 worker ID、capacity、版本、状态和 UTC 时间；
  不记录主机路径、环境变量或 secret。
- worker 主循环定期更新实例 heartbeat，并批量延长它当前持有的 job lease。
- API `/healthz` 保持纯进程 liveness；新增 `/readyz`，仅在 schema 为 head、数据库 `SELECT 1`
  成功且至少一个 `ready` worker heartbeat 新鲜时返回 200，否则返回稳定 503。
- 收到 SIGTERM/SIGINT 后，API 立即停止接受新提交并完成在途 HTTP；API 从不等待 Review 完成。
- worker 先停止 claim、把实例标为 `draining`，在有界 grace 内继续 heartbeat 并等待当前任务；
  grace 到期后停止续租并退出，未完成 job 由 lease 超时恢复，不能先释放 lease 后继续写结果。

环境配置冻结为：

| 变量 | 默认 | 有效范围 |
| --- | --- | --- |
| `CRAG_JOB_LEASE_SECONDS` | 60 | `1..3600` 秒 |
| `CRAG_JOB_HEARTBEAT_SECONDS` | 10 | `0.1..600` 秒且 `< lease/2` |
| `CRAG_WORKER_POLL_SECONDS` | 1 | `0.05..60` 秒 |
| `CRAG_WORKER_STALE_SECONDS` | 30 | `1..3600` 秒 |
| `CRAG_SHUTDOWN_GRACE_SECONDS` | 30 | `0..3600` 秒 |
| `CRAG_CONTAINER_STOP_GRACE_PERIOD` | `35s` | Compose duration，且必须大于 worker drain grace |
| `CRAG_RECEIVED_TIMEOUT_SECONDS` | 60 | `1..3600` 秒 |
| `CRAG_WORKER_CONCURRENCY` | 2 | `1..8` |

短 lease 仅用于注入时钟/容器故障测试，不形成生产建议。

## Retry 合同

默认最多 3 次总 attempt（包含第一次）。基础 backoff 为 1 秒，按 `2^(attempt-1)` 增长并加由
job ID 派生的确定性小 jitter；测试可注入为 0。

| 类别 | 是否重试 | 最终状态 | 规则 |
| --- | --- | --- | --- |
| `transient_network` | 是，有限 | 耗尽后 `dead_letter` | timeout、connection/reset 等；按 backoff |
| `provider_5xx` | 是，有限 | 耗尽后 `dead_letter` | 只接受可验证的 HTTP 5xx/provider unavailable |
| `rate_limit` | 是，有限 | 耗尽后 `dead_letter` | `available_at=max(backoff, Retry-After/reset)`；只保存规范化时间，不保存原 header |
| `authentication` | 否 | `failed` | 401、provider credential/configuration |
| `authorization` | 否 | `failed` | 403、tenant/repository/permission deny |
| `schema_policy` | 否 | `failed` | 400/422、payload/result schema、policy reject |
| `budget_exhausted` | 否 | `failed` | 组织、仓库或单 job 模型调用预算耗尽 |
| `external_command` / `internal` | 否 | `failed` | 默认 fail closed；不把未知错误伪装成 transient |

`Retry-After` 支持秒数和 HTTP-date；受支持的 reset 支持 epoch/相对时长。无效值忽略，超过 7 天
按 7 天封顶并记录稳定类别，不记录 header 内容。重试调度、error category、attempt usage 和
状态改变必须在同一事务内。

## 幂等语义与 durable payload

- `submission_key` 是服务端派生的逻辑 Review 键，覆盖 organization、repository、policy、
  source kind、稳定 source identity 和 source hash/head SHA；数据库唯一约束为
  `(organization_id, submission_key)`。
- REST 可接受有界 `Idempotency-Key`，数据库只保存其 SHA-256，不保存或 trace 原始 header。
  同 key + 同 request fingerprint 返回原 job 和 `duplicate=true`；同 key + 不同 fingerprint
  稳定返回 409 `idempotency_conflict`。
- Webhook delivery ID 的摘要作为请求幂等键；PR/head SHA/policy 派生的 submission key 还保证
  不同 delivery 对同一逻辑 head 的重放只产生一个 job。新的 head SHA 或 policy version 才是
  新逻辑 job。
- 幂等 replay 不重复扣提交速率、排队容量或模型预算。
- Phase 9B 禁止把 inline diff 存入业务表。Phase 9C 使用共享、私有的
  `CRAG_JOB_DATA_DIR` durable artifact：数据库只保存相对 opaque key；API 以 0600 临时文件 +
  原子 rename 写入，worker 按 fingerprint 验证后读取。Compose 中 API/worker 共享 named volume，
  不使用 state-directory 全局锁。
- job 行先进入 `received`，artifact 原子落盘后 CAS 为 `queued`，只有随后才返回 202。崩溃后
  replay 可完成同一 job；reconciler 对超时 `received` 行按 artifact 是否完整决定继续 queue 或
  稳定失败。终态提交后清理 payload；孤儿清理由 opaque key 和数据库 lineage 驱动。
- PR job 保存不可变 head SHA（Webhook 必须提供）；worker 在取 diff 前后核对该 SHA，不能拿 PR
  number 的后来版本冒充原提交。没有 head SHA 的 REST/直接手工 PR 请求只在显式 idempotency
  key 下接受，且不作精确 snapshot 声明。为保持冻结的两参数 MCP 工具兼容，MCP 适配器在调用方
  未提供 head/key 时派生组织内稳定兼容键；该路径同样不作精确 snapshot 声明。

跨数据库/文件系统不声称一个 ACID 事务。提交顺序固定为：创建/复用 `received` 行 -> 同卷
0600 temp write + flush/fsync + atomic replace -> 数据库 fencing/CAS 为 `queued`。terminal trace 使用
`job/attempt/lease_token` 独立的 O_EXCL 文件；runner 关闭 trace 后，获胜的 fenced 数据库事务才
写 `final_trace_key`。数据库事务失败时文件只是不可见 orphan，由数据库 lineage 清理，绝不通过
覆盖旧 trace 来“回滚”。Phase 9C 的 `JobStore` 在 Postgres 和 SQLite 路径都不得创建或获取
`.service.lock`；SQLite 仅靠数据库写事务串行化兼容测试。

## 配额、模型调用预算和背压

`service_quotas` 同时保存 organization scope 和 repository scope。worker/submit 事务按固定顺序
先锁 organization，再锁 repository；有效限制取两者中更严格者。默认值可由 migration 给出，
管理员 API 可显式更新：

- `max_queued_jobs`：`received|queued` 的最大数量；
- `max_concurrent_jobs`：未过期 `leased|running` 的最大数量；
- `submission_rate_limit` + `submission_window_seconds`：持久固定窗口提交速率；
- `monthly_model_call_budget`：UTC 月内已结算 calls + 活跃 reservations；
- `model_call_limit_per_job`：runner 调 provider 前的硬调用上限。

默认与边界冻结为：

| scope/字段 | organization 默认 | repository 默认 | 可配置边界 |
| --- | ---: | ---: | --- |
| `max_queued_jobs` | 1000 | 100 | `1..100000` |
| `max_concurrent_jobs` | 16 | 2 | `1..64` |
| `submission_rate_limit` | 600 | 60 | `1..100000` |
| `submission_window_seconds` | 60 | 60 | `1..86400` |
| `monthly_model_call_budget` | 100000 | 10000 | `1..1000000000` 或 `null` |
| `model_call_limit_per_job` | 64 | 64 | `1..256` |

管理 API 固定为组织 `org_admin` 可写的
`GET|PATCH /v1/organizations/{organization_id}/service-quota` 与
`GET|PATCH /v1/organizations/{organization_id}/repositories/{repository_id}/service-quota`；普通
提交者只能观察稳定 429，不能读取其他租户 quota。

模型 client 使用每 job 并发安全计数器，在发起下一次 provider 请求前原子消费调用额度；达到
上限抛出 `budget_exhausted`，不重试。提交事务为首 attempt 同时在组织/仓库 quota 预留有效
per-job limit；每个 retry 事务先结算当前 reservation，再原子预留下次 attempt，若余额不足则
直接 `failed/budget_exhausted` 而不是重新排队。terminal 时从 canonical trace 结算到
`provider_usage.llm_calls`；trace 缺失或 worker lease 死亡时保守按 reservation 结算，避免崩溃
绕过预算。预算是模型调用次数门禁，不声称等同最终账单；既有
`budget_microusd` 继续作为成本配置/审计字段，不在无法预知 provider 最终费用时伪装成硬成本
上限。

稳定拒绝：

| 条件 | HTTP | error code |
| --- | --- | --- |
| organization/repository 排队已满 | 429 | `queue_full` |
| 提交窗口超限 | 429 | `submission_rate_limited` |
| 月度模型调用预算无可用 reservation | 429 | `model_budget_exhausted` |

429 必须有稳定小响应；速率限制返回规范化 `Retry-After`，不得回显 quota 行、tenant 数据或
请求内容。并发 claim 达上限不把 job 标失败，只保持 queued 并让 worker 有界退避。

## 数据库事务合同

1. **提交事务**：先无锁查幂等 fast path；对新提交按 org quota -> repo quota 加锁后再次检查
   idempotency，再检查 queue/rate/budget；创建或复用逻辑 job、submission event、Webhook mapping
   和首 attempt reservation。重复提交不重复计数。
2. **payload 完成事务**：核对 fingerprint 后只允许 `received -> queued`；API ack 在提交之后。
3. **claim 事务**：无锁选 candidate scope，按 org quota -> repo quota -> candidate job 加锁，
   最后用 `SKIP LOCKED` 重查 job/并发/预算，创建新 fencing lease 并增加 attempt。
4. **heartbeat 事务**：只延长当前 owner/token 的未过期 lease；worker instance 与 job heartbeat
   不依赖文件锁。
5. **terminal/retry 事务**：先读 scope identity，再按 org quota -> repo quota -> job 加锁；fencing
   CAS、Review result、Finding、provider usage、audit/event、trace key、当前 reservation 结算和
   （若 retry）下次 reservation 在同一事务；事务失败不得出现部分 Finding 或假终态。
6. **migration**：只由 `crag-db upgrade` 显式执行；API/worker 只检查 exact Alembic head，绝不在
   startup 或请求中执行 DDL。

Postgres 使用数据库时钟判断 lease。SQLite 兼容测试可以用应用 UTC 时钟，但不能据此声称跨主机
时钟安全。

## 部署边界与 secrets

- 生产多 worker 只支持 Postgres；SQLite 是本地/单机测试兼容，不作为 Compose 默认数据库。
- API 容器只提供 HTTP/MCP 和 durable submit/read；不创建 executor，不 claim job。
- worker 容器运行 `crag-worker`，可水平扩展；默认 Compose 提供至少两个 worker 实例的扩展
  命令和一个 Postgres 服务。反向代理本阶段可省略。
- Compose 提供单独 `migrate` one-shot service/命令，但 API/worker 不依赖其自动运行；运维顺序
  固定为 Postgres ready -> 显式 migration -> API/worker。
- API、worker 共享只包含 job artifacts/traces 的私有 volume；注册 checkout 只读挂载；不挂载
  Docker socket、宿主 credential directory 或 `.env`。
- secret 内容只通过运行时 secret file/secret manager 注入。冻结支持
  `CRAG_DATABASE_PASSWORD_FILE`、`CRAG_WEBHOOK_SECRET_FILE`、`CRAG_SERVICE_TOKEN_FILE`、
  `DEEPSEEK_API_KEY_FILE`、`GLM_API_KEY_FILE`/`ZHIPUAI_API_KEY_FILE`。Compose 只引用 `_FILE` 路径或 secret
  名称，不包含示例明文；镜像不 COPY secret。数据库 password、Webhook secret、local test token
  和 provider key不得出现在命令行、日志、trace 或健康响应。
- fake-run 是显式测试 runner，不读取 provider key、不调用网络；生产默认始终是真实 runner，
  不能因缺 key 静默切 fake。
- Compose 的 API healthcheck 使用 `/healthz`，避免 worker 尚未启动造成编排死锁；部署/流量入口
  才使用 `/readyz`。worker healthcheck 检查数据库与自身新鲜 heartbeat，不把 API readiness 当
  worker 启动前置条件。

## Single Writer 文件清单

Codex 团队在本任务中仅拥有以下路径的写权限；每个具体文件同时只由一个 agent 修改：

- `docs/plans/phase9c-durable-service.md`（本合同）；
- `README.md`、`docs/protocol-service.md`、`docs/production-architecture.md`（状态、运行、部署和
  风险说明）；
- `pyproject.toml`（只新增 `crag-worker` 入口，不新增 dependency）；
- `migrations/README.md`、`migrations/versions/0003_phase9c_durable_queue.py`；
- `src/code_review_agent/database.py`；
- `src/code_review_agent/service_core.py`、`src/code_review_agent/service.py`、
  `src/code_review_agent/mcp_server.py`；
- `src/code_review_agent/llm.py`（仅支持运行时 `_FILE` provider secret，并禁用 durable worker
  路径的 SDK 内部重试，使模型调用预算与实际 HTTP attempt 一致）；
- `src/code_review_agent/service_queue.py`、`src/code_review_agent/worker.py`（新增）；
- `Dockerfile.service`、`compose.service.yml`（服务镜像与 API/worker/Postgres/migrate 编排）；
- `.github/workflows/ci.yml`（移除现有 `--eval-assets`，并新增无真实外部调用的
  durable/container 门禁，确保本阶段远端 CI 也不读取禁止目录）；
- `tests/test_phase9c_durable_service.py`、`tests/test_phase9c_postgres.py`（新增）；
- `tests/test_week7_service.py`、`tests/test_week7_service_core.py`、`tests/test_week7_mcp.py`；
- `tests/test_phase9b_identity_rbac.py`、`tests/test_phase9b_migrations.py`（仅 schema/兼容断言）；
- `scripts/phase9c_load_test.py`、`scripts/phase9c_container_test.py`（新增，均为 fake/offline）。

其他路径只读。尤其禁止访问或修改 `eval/**`。当前只读审计期间出现的未跟踪
`%SystemDrive%/` 不属于本任务所有权，不得纳入 diff/提交或擅自删除。发现必须修改清单外路径时，
先向用户说明原因并修订本合同，不能先改后报。

## 验收与验证命令

全部测试使用 fake runner，不调用真实模型/GitHub/OAuth，不读取 `eval/`：

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $repoRoot "src"

& $python -m unittest -v tests.test_phase9c_durable_service `
  tests.test_phase9c_postgres tests.test_phase9b_identity_rbac `
  tests.test_phase9b_migrations tests.test_week7_service `
  tests.test_week7_service_core tests.test_week7_mcp

& $python scripts\phase9c_load_test.py --submissions 50 --workers 2
& $python scripts\phase9c_container_test.py

& $python -m ruff check .
& $python -m mypy src/code_review_agent
& $python scripts\verify.py
& $python -m pip check
& $python -m code_review_agent.database --help
& $python -m code_review_agent.worker --help

git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

验收必须机器验证：

- 50 个并发、不同逻辑提交全部落库，无丢失、无重复，Webhook/API handler 不等待 Review 完成；
- 两个独立 worker 对同一时刻的 job 不会得到相同 lease token，且不会同时持有同一 job；
- 杀死/停止一个 worker 的 heartbeat 后，lease 到期由另一 worker 恢复，旧 token 不能提交结果；
- 同 delivery 以及不同 delivery 的同 PR/head 重放都只产生一个逻辑 job；key/payload 冲突稳定 409；
- organization 和 repository 的 queue/rate/model budget 超限均稳定返回 429；并发上限不超卖；
- 多个 API/worker `JobStore` 可共享 state/artifact/trace 目录，不依赖独占文件锁；
- `/healthz`、`/readyz`、DB 失败、无新鲜 worker heartbeat 和 graceful drain 行为符合合同；
- Compose 必须显式 migration，启动 API + 至少两个 worker + Postgres，fake-run 完成一项 Review；
- Ruff、mypy、`scripts/verify.py`、定向测试、负载测试、容器测试、`pip check` 和
  `git diff --check` 全通过。

Postgres 定向测试在本地没有 Docker/Postgres 时可以按明确环境条件 skip，但容器验收和 CI 必须
实际覆盖 Postgres claim/recovery；不能以 SQLite 结果替代。`scripts/verify.py` 不得添加
`--eval-assets`。

## 回滚

- 应用回滚先停止 API 新提交，再让 worker drain，备份数据库和 artifact/trace volume，最后回退
  应用；不能让旧 Phase 9B worker 连接已经产生 Phase 9C 状态的数据库。
- 0003 是结构性 migration。生产回滚以 migration 前备份恢复为主；downgrade 只供空库/测试演练，
  不承诺把 attempt/lease/quota 历史无损压回 Phase 9B 四状态。
- migration 前必须确认没有活跃旧 worker；可恢复的旧 `pull_request/running` 会回到 `queued`，
  可能再次执行，因此外部写入仍保持禁用；缺少 durable payload 的旧 inline work 会稳定失败。
- 若 queue 或 worker 出现异常，可停止 worker 保留 API 只读；若排队继续增长则关闭提交/返回 503
  或 429。不得删除 job 行来“恢复”。
- artifact 清理失败只产生稳定运维告警，不回滚已经原子提交的 Review；清理器必须只按数据库
  opaque key 操作，绝不接受调用方路径。

## 交付控制

用户已明确授权在本任务分支创建稳定提交、push、创建 Draft PR、等待 CI、转 Ready，并只通过
PR 合并；禁止直接 push/merge/rebase `master`。合并后必须核验 merge SHA 和 master CI。任何
真实外部评论、模型、GitHub 内容写入或云部署仍未授权。

## 变更控制

本合同创建后冻结。只允许修正可验证的合同错误，或在用户明确批准范围变化后先修订再实施。
任何新增依赖、状态语义、secret 通道、可写路径或真实外部调用都必须视为范围变化。

### 可验证合同修订

- 2026-07-22：原合同同时要求无 head 的手工 PR 必须带显式幂等键、保持既有两参数 MCP
  `review_pr` 语义，却未把 MCP 适配器列入 writable paths。修订为：REST/直接调用继续 fail
  closed；MCP 保持两参数兼容并派生稳定兼容键，同时允许调用方提供可选 head/key。
- 2026-07-22：实际 OpenAI-compatible client 默认在一次逻辑 `create` 内重试两次，能绕过按逻辑
  调用计数的硬预算；授权仅在 `llm.py` 增加 `_FILE` secret 读取，并在 durable runner 包装处把
  SDK retry 固定为零。job retry 仍由本合同的持久状态机统一执行。
- 2026-07-23：审计确认 Phase 9B inline diff job 没有可供新 worker 恢复的 durable payload。修正
  0003 的迁移规则：只有有稳定 reference 的历史 PR work 可以重新排队；非终态 inline work 以
  `legacy_payload_unavailable/schema_policy` fail closed，避免把 `inline` 错当 PR reference 执行。
- 2026-07-23：原 Compose 把 Docker stop deadline 与 worker 内部 drain deadline 设为相同值，
  Docker 可能在 worker 写入最终状态并退出前发送 SIGKILL。新增独立外层 stop grace，默认比内部
  deadline 多 5 秒；容器验收检查 worker 在该边界内以退出码 0 停止。
- 2026-07-23: Compose implementations ignore secret `uid`/`gid`/`mode` long-syntax fields.
  The deployment contract therefore uses short-syntax mounts plus a minimal root-only image
  bootstrap. It copies only allow-listed runtime secrets to a `0600` tmpfs directory and
  execs the service command as UID/GID `1000:1000` with empty capability bounding,
  inheritable, and ambient sets. Secret values remain absent from image layers, Compose
  output, argv, logs, and traces.
