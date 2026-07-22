# Phase 9A：产品定位、业务闭环和指标合同

状态：**已冻结**

冻结日期：2026-07-22

基线：`origin/master` at `acc0dcce077113dcbbde2478abd53cbb09a4ef2e`
任务分支：`codex/phase9a-product-kpi`

## 目标

把项目收敛为一条可陈述、可度量但尚未声称生产验证的产品主线：

> 面向中小型 Python 团队的 GitHub PR Review Agent。系统自动分析 PR，默认处于
> shadow 模式；Finding 只有经有权限的仓库维护者批准后才可发布到 GitHub。系统收集开发者
> accept/reject 反馈，并用冻结的指标合同衡量审查噪声、人工复核时间、可靠性和成本。

本阶段交付：

1. 定义组织管理员、仓库维护者、Reviewer、普通开发者四类用户及权限边界；
2. 定义从 PR Webhook 到仓库规则/反馈记忆更新的完整业务闭环；
3. 为指定业务 KPI 给出机器可计算的事件、分子、分母、来源、排除规则、窗口、空分母语义和
   禁止外推结论；
4. 将 Review 固定为产品主线，将 Repair 和 Verifier Training 分别降为后续高风险增强与研发
   附录；
5. 用当前代码与实际验证修正文档中的测试、覆盖率、master CI、阶段状态和 synthetic 证据
   边界；
6. 提供 30 秒和 3 分钟两种一致的产品陈述，以及一张覆盖生产闭环的 Mermaid 架构图。

## 非目标

- 不实现用户、组织、RBAC、OAuth 或 GitHub App 身份系统；
- 不重构 SQLite、数据库 schema、队列或持久化层；
- 不部署服务，不调用模型，不发送 webhook，不发布 GitHub review/comment，不执行外部写操作；
- 不新增或修改生产代码、测试、依赖、锁文件、打包入口或 CI workflow；
- 不修改 Finder/Verifier prompt、sentinel、阈值或评测判据；
- 不实现 Repair 远程审批或自动修复；
- 不训练或评估 Verifier 模型；
- 不做聊天机器人、用户个人记忆或为展示而增加多 Agent；
- 不读取或修改 `eval/`、`eval/holdout/` 及其中任何资产；
- 不把 Week 4 预注册、Week 6/8 synthetic 结果或 Week 7.5 单次链路证据表述为生产业务收益。

## Single Writer 文件清单

Codex 在本任务中仅拥有以下路径的写权限：

- `docs/plans/phase9a-product-kpi.md`（新建，本合同）；
- `docs/product-brief.md`（新建）；
- `docs/business-metrics.md`（新建）；
- `docs/production-architecture.md`（新建）；
- `README.md`（修正产品主线和当前事实）；
- `docs/interview-defense.md`（改写产品陈述和事实边界）。

其他所有路径只读。若实施需要扩大清单，必须先取得用户明确批准并更新本合同；不得先改后报。

## 冻结的产品范围

- **主线**：GitHub PR Review；默认 shadow；维护者审批后发布；开发者反馈进入仓库级聚合。
- **后续增强**：Repair。它具有写代码、跑命令和提交变更的更高风险，必须独立授权和审计，
  不进入 Phase 9A 主业务闭环。
- **研发附录**：Verifier Training。它服务于离线研究，不是用户产品面，也没有真实质量收益
  结论。
- **明确排除**：聊天机器人、用户个人记忆、以 Agent 数量为卖点的编排。

## 事实边界

1. 当前仓库是 alpha 工程，不存在已验证的生产部署、生产用户、远程 OAuth 或大规模真实 PR
   运行证据。
2. Week 4 只完成可信 Review 的预注册、数据合同和统计仪器；没有 materialized 30-PR
   reporting 数据或最终效果数字。
3. Week 7.5 只证明一个私有 draft PR 的有界真实链路、幂等重放和 HMAC 拒绝；首次投递曾触发
   GitHub 10 秒超时。该证据不等于生产可用性或普遍可靠性。
4. Week 8B/8D 已冻结真实公开来源快照、Finder 队列和真人盲标包，但尚无真人标签；Week 8C
   和 8D 的已闭环训练/标注结果是 synthetic，`quality_claim_allowed=false` 或
   `trainable=false`，不得声称模型提升。
5. 历史人工植入基准、fake-clock 安全套件和 synthetic 训练指标只能作为工程回归/协议证据，
   不能替代真实业务 accepted rate、节省时间、成本或生产时延。
6. 测试数、跳过数、覆盖率和源文件数只写入本任务实际运行命令得到的结果，并注明日期、环境
   和命令。CI 状态只写入可核验的 GitHub Actions run/commit，不用“最新”替代精确锚点。
7. 产品文档中的 KPI 是**未来采集合同**；除非已有对应生产事件，否则只给定义，不填虚构
   基线、目标或收益。

## KPI 最小合同

`docs/business-metrics.md` 必须覆盖下列指标，并为每项固定：精确定义、分子/分母、数据来源、
排除规则、聚合窗口、空分母语义和不能声称的结论。

- Finding accepted/actionable rate；
- Finding rejection rate；
- 每 PR 人工复核时间；
- Agent completion rate；
- p50/p95 Review latency；
- webhook acknowledgement latency；
- 每 PR 成本；
- 每个 accepted Finding 成本；
- fail-open/degraded/error rate；
- duplicate webhook 新增任务数；
- unauthorized publish 数量。

所有比率必须能从稳定事件和 ID 去重后计算；货币使用整数最小单位并绑定价格版本；百分位必须
固定算法；空分母统一输出 `null` 并同时输出分子、分母和排除数。

## 验证命令

本阶段为文档变更。为获得当前事实基线，允许并要求运行一次不带 `--eval-assets` 的离线验证；
它不调用模型或外部服务。运行前先只读检查 `scripts/verify.py`，确认默认路径不读取
`eval/`/`eval/holdout/`。

```powershell
# 当前代码事实：不得添加 --eval-assets
$repoRoot = git rev-parse --show-toplevel
$env:PYTHONPATH = Join-Path $repoRoot "src"
python scripts\verify.py

# 当前 master CI 事实：只读查询；若 gh 未认证则如实报告，不能猜测
gh run list --branch master --limit 10
gh run view <selected-run-id>

# 文档自审与所有权检查
git diff --check
git diff -- README.md docs/interview-defense.md docs/product-brief.md `
  docs/business-metrics.md docs/production-architecture.md `
  docs/plans/phase9a-product-kpi.md
git diff --name-only
```

提交后再运行：

```powershell
git diff --check origin/master...HEAD
git diff --name-only origin/master...HEAD
git status --short --branch
```

禁止运行任何付费评测、模型调用、live webhook、发布命令或带 `--eval-assets` 的验证。

## 验收标准

- 30 秒和 3 分钟版本都能讲清业务痛点、四类用户、shadow/审批/反馈闭环、价值与事实边界；
- 业务流程完整覆盖：PR Webhook → 异步 Review → Finding → Maintainer 审核 → 发布或拒绝 →
  开发者反馈 → 指标聚合 → 仓库规则/反馈记忆更新；
- 全部 KPI 均有机器可计算定义，明确时间口径、去重、排除和空分母；
- 架构图覆盖 GitHub、API、worker、数据库、审批、发布、反馈、上下文和可观测链路；
- 明确 Review/Repair/Verifier Training 的产品层级，不引入聊天、个人记忆或无业务必要的多
  Agent；
- 不把 synthetic、单项目植入基准、单次 live probe 或预注册仪器写成真实业务收益；
- README 与答辩文档不再保留过期测试数、覆盖率、master CI 或阶段状态；
- 实际改动路径是 Single Writer 清单的子集，且没有生产代码、依赖、CI 或评测资产改动；
- 文档 diff 已逐文件通读，`git diff --check` 通过；
- 在任务分支创建本地稳定提交，不 push、不建 PR、不 merge、不修改 `master`。

## 变更控制

本合同自创建后冻结。仅允许在以下两种情况下修改：修正合同内部可验证的事实错误，或用户明确
批准范围变更。任何合同修订都必须在实施前说明原因，并在最终报告中列为偏差；正常措辞润色不
作为扩大任务范围的理由。
