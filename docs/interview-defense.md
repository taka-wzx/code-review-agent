# 面试答辩手册 — code-review-agent

> 用途：用一致、可核验的口径说明产品、工程和证据边界。回答顺序统一为：业务问题 → 用户与
> 决策链 → 可计算指标 → 当前证据 → 未完成项。不得用 synthetic、单项目植入基准或单次 live
> probe 替代生产收益。

> 当前事实锚点：2026-07-22，`origin/master`
> `acc0dcce077113dcbbde2478abd53cbb09a4ef2e`；本机 Python 3.13.12 运行
> `scripts/verify.py`（未带 `--eval-assets`）通过 646 个测试、6 个环境跳过、分支覆盖率 86%、
> mypy 26 个源文件；对应 master Actions run `29894645345` 的 7 个 job 全部成功。

## 一、项目陈述

### 30 秒版

这是一个面向中小型 Python 团队的 GitHub PR Review Agent。它解决的不是“让 LLM 多提意见”，
而是减少无效审查噪声，同时保留人的发布责任：PR Webhook 异步触发 Finder + Verifier 分析，
系统默认只在 shadow 模式生成 Finding；只有有权限的仓库维护者批准后，未来的 guarded-publish
链路才允许发布到 GitHub。开发者再对已发布 Finding accept/reject，团队用接受率、人工复核
时间、完成率、p50/p95、成本和未授权发布等指标决定是否扩大使用。当前 Review 引擎、Webhook、
幂等 job 和 trace 已实现，但远程身份、审批、反馈和生产 KPI 尚未实现，所以我不声称已经产生
真实业务收益。

### 3 分钟版

**问题与用户。** 中小型 Python 团队没有专门的平台团队，Reviewer 一方面要反复补上下文，
另一方面又不能承受 LLM 把低价值意见直接灌进 PR。组织管理员关心接入范围、预算和风险；仓库
维护者对发布负责；Reviewer 要更快获得有证据的候选；普通开发者需要能明确 accept/reject，
而不是被机器人单向教育。

**产品闭环。** GitHub PR Webhook 先验签、检查仓库白名单和 delivery 幂等，再快速持久化异步
任务。worker 获取精确 PR/head diff，构建 Python 仓库上下文，通过 Finder 双跑召回候选、
结构化去重/scope 和 Verifier 双 pass 过滤，形成带证据和稳定版本的 Finding。仓库默认是
shadow：Finding 只进入内部审核，不对 GitHub 发言。未来显式启用 guarded publish 后，每次
发布仍要绑定维护者身份、repo/PR/head SHA、Finding 内容哈希和一次性批准；拒绝就不发布。
开发者只对实际发布的 Finding 反馈，成熟反馈进入仓库级聚合；达到样本门槛并再次由维护者批准，
才能形成版本化、可回滚的仓库规则。这里没有用户个人记忆，也没有自动在线改 prompt。

**如何判断有价值。** 我不以 Finding 数量为成功。accepted/actionable rate 和 rejection rate
只使用开发者的最终结果，不把维护者批准混进去；人工复核时间用前台活动 heartbeat 的非重叠
区间计算；completion、review p50/p95、webhook ack、fail-open/degraded/error 衡量可靠性；
每 PR 成本和每 accepted Finding 成本包含失败与重试；duplicate webhook 新增任务和
unauthorized publish 是强安全计数。所有空分母输出 `null`，样本、排除和成熟期同时报告。
没有同仓基线/对照，就不能说节省了多少时间。

**当前证据与边界。** 当前 master 有 646-test / 86% coverage 的离线回归和 7-job CI 成功证据；
Week 7.5 有且只有一个私有 draft PR 的有界 live 链路，初次 webhook 还发生过 GitHub 10 秒
超时。历史 precision/recall 来自单项目人工植入缺陷。Phase 8D 已有 137 条真实来源 Finder
候选和真人盲标包，但还没有真人标签；已跑通的训练和双标/仲裁是 synthetic，明确禁止模型
质量结论。因此这个阶段交付的是产品与指标合同，不是生产部署或收益报告。

**范围取舍。** Review 是唯一产品主线。Repair 涉及写代码、跑命令和提交，是后续独立授权的
高风险增强；Verifier Training 是研发附录。不做聊天机器人、不做用户个人记忆，也不为了讲
故事增加多 Agent。下一个真正的产品阶段应先补 GitHub App/OAuth、RBAC、审批/反馈数据合同和
shadow 试点，而不是扩功能面。

## 二、产品角色与权限

| 角色 | 负责什么 | 关键权限边界 |
| --- | --- | --- |
| 组织管理员 | 仓库注册、shadow/guarded-publish 模式、预算和组织指标 | 可以启用仓库，但不能代替维护者批准某条 Finding |
| 仓库维护者 | 审核 Finding、批准/拒绝发布、确认仓库规则 | 权限和 head/Finding 绑定不匹配时必须 fail closed |
| Reviewer | 使用 Finding 证据辅助审查、标记重复/低价值 | 没有维护者角色就不能发布或改全仓规则 |
| 普通开发者 | 处理自己 PR 上已发布的 Finding，accept/reject | 反馈只绑定 Finding 版本，不形成个人画像 |

当前 FastAPI 服务只有 operator-controlled static Bearer，不具备上述远程 RBAC。答辩时要把“目标
角色”与“当前身份实现”分开说。

## 三、业务状态机

```text
signed PR webhook
  → accepted / ignored / rejected
  → queued → running → succeeded | degraded | fail_open | error
  → finding generated → pending approval
  → maintainer rejected
    或 maintainer approved → shadow retained
    或 maintainer approved → guarded publish → published
  → developer accepted | rejected | unresolved
  → metrics aggregation
  → repository rule proposed → maintainer approved → activated / rolled back
```

当前实现的持久任务状态是 `queued -> running -> succeeded|failed`。更细的终态、审批、发布、反馈
和规则状态是生产目标合同，不是已经存在的数据库 schema。

## 四、指标快答

### accepted/actionable rate 是什么？

只对已发布并经过 14 天反馈成熟期的 Finding 计算：

```text
accepted / (accepted + rejected)
```

shadow、仅被维护者批准、发布失败和 unresolved 不进分母，但全部单列。它不是 defect precision，
也不能证明 prevented bug。

### rejection rate 是什么？

和 accepted rate 使用完全相同的 cohort 和分母：

```text
rejected / (accepted + rejected)
```

同一 cohort 两者应加总为 1。reject 可能是 invalid、duplicate、out-of-scope 或 low-value，
不能不分原因地当作模型误报。

### 人工复核时间怎么测？

审核界面聚焦且有活动时每 15 秒 heartbeat，每条贡献最近 15 秒；跨 tab/Reviewer 取非重叠区间
并集。每 PR 报 mean/p50/p95。没有人工基线或阶梯/随机对照，只能报告“用了多久”，不能报告
“节省多久”。

### completion 和 latency 怎么避免幸存者偏差？

一旦 `review_queued` 成功就进入 completion 分母，失败、重启或超时不能删除。完成时延只对有
持久化结果的 Review 计算，但错误必须同时进入 error rate；不能只展示成功样本 p95 来隐藏失败。

### 成本怎么计算？

每 PR 成本把同一逻辑 Review 的全部 attempts、失败和重试计入分子。缺 pricing revision 或任一
Review 成本缺失时 headline 为 `null`。每 accepted Finding 成本的分子仍包含无 Finding、被拒绝
和无反馈 Review 的成本，防止只算赢家。

### 两个强安全指标是什么？

- `duplicate webhook 新增任务数`：重复 delivery 或相同 PR/head/policy 幂等键错误新增的逻辑
  Review 行数，期望为 0；0 也只证明观测窗口和保留期内的行为。
- `unauthorized publish 数量`：实际 publish succeeded 但缺少有效 RBAC/一次性绑定的次数。被
  正确阻断的尝试另计，不能混进分子。当前发布审批链未实现，所以当前值是“未采集”，不是 0。

完整机器口径见 [`business-metrics.md`](business-metrics.md)。

## 五、技术架构与取舍

### 当前 Review 引擎

```text
diff / commit / uncommitted / PR
→ context：约定文档 + 改动文件 + import + 调用方，预算封顶
→ Finder temp=0 + temp=0.7 阶段内并行
→ 去重 + 文件级 scope
→ Verifier A/B 阶段内并行
→ confirmed / dropped / uncertain + sentinel
→ JSON / Markdown / trace / GitHub review payload
```

Finder 负责召回，Verifier 负责证据过滤；两个目标的 prompt 姿态不同。分歧进入 uncertain，
fail-open/degraded 显式留在结果和 trace。整个 Review 有 300 秒 monotonic 软截止：截止后不再发
新请求，但不能强杀同步 SDK 已在途请求，所以不是硬实时保证。

### 为什么默认 shadow？

因为误报不仅是质量问题，也是外部写入和团队信任问题。shadow 把“能生成 Finding”和“有权对
GitHub 发言”拆开，使团队先得到接受率、复核负担、可靠性和成本分布。没有 RBAC、一次性审批
绑定和反馈数据时，自动发布是越权扩大，不是产品成熟。

### 为什么不是聊天机器人？

聊天把身份、上下文、权限和成功标准变得模糊。产品对象是 PR version、Finding、approval、
publish receipt 和 feedback，它们都有稳定身份和状态机，才能幂等、审计和计算 KPI。通用聊天
不会改善这条闭环。

### 为什么不做用户个人记忆？

个人记忆带来隐私、画像、纠错和权限迁移问题，而且一次 reject 很可能只是具体上下文。产品只
允许仓库级聚合规则：达到样本门槛、离线回放、维护者批准、版本化和可回滚后才生效。

### 为什么不增加更多 Agent？

Finder/Verifier 分离有明确的召回/精度目标和独立失败语义。新增 Agent 只有在可测收益、权限
隔离或故障域成立时才合理；否则会增加调用、延迟、成本和审计面。产品价值由 KPI 证明，不由
Agent 数量证明。

### Repair 为什么不是主线？

Repair 会写文件、执行命令、跑测试和创建提交，风险等级高于只读 Review。仓库已有本地一次性
审批、Docker 隔离、checkpoint 和 pilot 证据，但它仍需要独立权限、失败恢复和生产安全评审。
把它塞进 Review 首发会扩大可信计算基，不利于先证明 Finding 产品价值。

### Verifier Training 为什么是研发附录？

Phase 8 建立了数据、split、防泄漏、训练和 artifact 合同，也物化了真实来源 Finder 候选；但
真人标签/仲裁和真实跨仓质量结果尚未完成。Phase 8C/8D 的闭环结果是 synthetic，分别禁止
quality claim 或保持 non-trainable。它可以支撑未来研发，不是当前用户面或业务 KPI。

目标生产图和差距表见 [`production-architecture.md`](production-architecture.md)。

## 六、事实证据台账

| 事实 | 当前可说 | 不能外推 |
| --- | --- | --- |
| Phase 9A 本机验证 | Python 3.13.12；646 tests passed，6 skipped；branch coverage 86%；Ruff、mypy 26 files、双 CLI 通过 | 生产稳定性、真实模型质量 |
| 当前 master CI | SHA `acc0dcc…`；Actions run `29894645345`；Linux 3.10–3.13、Windows 3.11、lock-check、container-smoke 共 7 job 成功 | live 协议在容器中通过、生产部署成功 |
| Week 7.5 | 一个私有 draft PR 产生一个成功 Review；重复 delivery 未新增 job/provider work；无效 HMAC 401 | completion/p95、长期 exactly-once、生产可用；初次 delivery 曾 GitHub 10 秒超时 |
| Week 4 trusted review | 30-PR reporting 的选择、标注、freeze、统计合同已实现 | 真实 30-PR precision/recall；数据尚未 materialize |
| 历史 Review 评测 | 单项目人工植入缺陷上有可复现实验和 trace | 跨仓泛化、accepted rate、时间节省 |
| Week 6 安全 | 48-case recording-fake 控制面回归和有限 GLM synthetic probe | 生产攻击抵抗力、unauthorized publish rate |
| Phase 8 | 9 仓/29 PR 来源、137 条净化候选、真人盲标包；synthetic 训练/仲裁闭环 | 真人标签一致率、模型提升、业务收益 |
| Repair pilot | 10 个本地提交工作流完成，后 4 个有严格 red-to-green；两次受控恢复 | 10-run pass@1、生产自动修复收益 |

历史阶段文档中的测试数是当时快照；当前回归基线只引用上表的 2026-07-22 精确命令结果。

## 七、高频追问

### Q1「这还是个人工程项目，为什么叫产品？」

我没有把“代码多”当产品。Phase 9A 明确了一条用户决策链：中小 Python 团队、四类角色、
shadow 默认、维护者发布权、开发者反馈和单位经济指标。当前只是 alpha 产品基础，还缺身份、
审批、反馈和部署；准确说法是“产品方向已经收敛，生产闭环尚未实现”。

### Q2「维护者 approve 就算 accepted 吗？」

不算。approve 是发布权限决策，accepted 是开发者对已发布 Finding 的结果反馈。混在一起会把
内部筛选当用户价值，虚高 KPI，所以事件、分母和文案都分开。

### Q3「无反馈 Finding 为什么不进 accepted/rejected 分母？」

因为不能猜标签。但 unresolved 必须按成熟 cohort 单列并报告占比；如果只展示已反馈子集，会有
选择偏差。产品决策要同时看 accepted rate 和 feedback coverage/unresolved count。

### Q4「怎么证明真的节省人工时间？」

先统一 active-time 采集，再在同一仓库按 PR 规模/类型建立历史基线，最好做阶梯启用或随机
shadow 对照。只有差值和置信区间能支持“节省”；单组均值只能说用了多久。

### Q5「为什么不是自动发评论？」

当前没有生产 RBAC 和绑定审批。自动发布会把模型质量风险升级为外部写入风险，也破坏团队信任。
guarded publish 需要独立实现和安全评审；Phase 9A 明确不存在自动发布模式。

### Q6「fail-open 还允许维护者发布吗？」

fail-open 的含义是 Verifier 失败时保留 Finder 结果，避免静默漏报；它不是普通成功。目标策略中
它可以进入 shadow 审核，但 guarded publish 默认不具备发布资格。任何 override 都应是显式、
逐次、强审计的新策略，而不是隐藏降级。

### Q7「Week 7.5 不是已经跑通生产链路了吗？」

只跑通一个有界私有 draft PR：唯一任务成功、重放幂等、无效 HMAC 401。但初次 GitHub delivery
记录了 10 秒超时，只有一个 Windows/Python 3.13 样本，没有 OAuth、审批、反馈、远程 MCP 或
容器内 live 链路。这是集成证据，不是生产可用声明。

### Q8「历史 precision/recall 很高，为什么不能当 accepted rate？」

它来自单项目人工植入缺陷和 LLM judge，测的是冻结 benchmark 上的 finding/gold 匹配；accepted
rate 来自真实开发者对实际发布 Finding 的反馈。数据来源、分母和偏差完全不同，不能换名字。

### Q9「Phase 8 不是已经训练四个模型了吗？」

训练路径跑过，但 test 规模极小且是 synthetic；合同明确
`quality_claim_allowed=false`。Phase 8D 的 synthetic 双标/仲裁也保持
`trainable=false`。真实来源候选已准备好，真人盲标还没完成，因此不能说后训练提升。

### Q10「当前测试和覆盖率能与早期阶段直接比较吗？」

不能。早期阶段文档记录的是当时较小代码面的历史快照；之后增加 Repair、observability、协议
服务和训练数据工具，分母和分支结构大幅变化。当前可说的是 6,655 statements、2,314 branches
的全包覆盖结果为 86%，仍过 85% 门禁，646 个测试通过、6 个环境跳过。覆盖率是回归防护指标，
不是业务质量。

### Q11「master CI 的准确状态是什么？」

不要说“某周最新”。精确说：2026-07-22 查询时，`origin/master`
`acc0dcce077113dcbbde2478abd53cbb09a4ef2e` 对应 Actions run `29894645345`，push event，
7 个 job 全部 success。它覆盖 Ubuntu Python 3.10–3.13、Windows 3.11、lock-check 和
container-smoke；不覆盖 live provider 或 live webhook。

### Q12「为什么成本缺失就把 headline 设 null？」

只对有账单的成功任务求均值会系统性低估失败和缺数。headline 要求 cohort 成本完整；provider
没返回账单时可以给绑定 pricing revision 的 estimated 系列，但不能和 settled 混合。

### Q13「duplicate job 指标为 0 就是 exactly-once 吗？」

不是。它只证明观测服务、数据库和 idempotency retention 内没有新增逻辑 Review。跨区域复制、
保留期外重放和数据库灾难恢复都需要另外验证。Week 7.5 也只是一次重放样本。

### Q14「unauthorized publish 为 0 就能说明安全吗？」

不能。首先当前链路还没实现，所以值是未采集。未来 0 也只表示审计到的成功发布都带有效绑定；
还要证明审计覆盖、RBAC 快照、nonce 原子消费和 GitHub receipt 对账本身可靠。

### Q15「仓库规则会不会把模型调成迎合开发者？」

规则不直接在线改 prompt。至少 20 个已解决 Finding、跨 5 个 PR 才产生候选；高严重度类别有
保护；离线回放后由维护者批准；规则版本化、只影响未来 Review、可以回滚。单次 reject 不更新。

### Q16「下一阶段最重要的工程是什么？」

先做最小生产闭环，不扩 Agent：GitHub App/OAuth 和 RBAC、approval/finding/feedback 的持久化
合同、紧凑 webhook ack 埋点、shadow dashboard、成本完整性和数据质量门禁。shadow 取得真实
基线后，再单独评审 guarded publish。

## 八、不能说的话

- 不能说“已经为团队减少 86% 噪声”；历史 noise 数字来自人工植入 benchmark。
- 不能说“developer acceptance 已达到某值”；当前没有生产 feedback 数据。
- 不能说“平均每 PR 节省 X 分钟”；当前没有统一 active-time 基线或对照。
- 不能说“p95 是 254 秒”；254 秒只是 Week 7.5 的单个 Review wall-clock。
- 不能说“webhook 可靠”；单次 live 首投曾触发 GitHub 10 秒超时。
- 不能说“重复 webhook exactly-once”；只验证过本地测试和一次 live 重放。
- 不能说“Phase 8 提升了 Verifier 质量”；现有训练/标注结果是 synthetic。
- 不能说“30 个真实 PR 评测已完成”；Week 4 只完成预注册和仪器。
- 不能把 Repair 的 10-run pilot 写成 pass@1；严格 red-to-green 只覆盖后 4 个。
- 不能说“生产可用”；身份、审批、反馈、durable deployment 和运营 SLO 均未完成。

## 九、一页速记

| 项 | 当前口径 |
| --- | --- |
| 产品 | 中小 Python 团队的 GitHub PR Review Agent |
| 默认模式 | shadow，不发布 GitHub comment |
| 发布 | 目标为维护者逐次、版本绑定、fail-closed 批准；当前未实现服务端审批链 |
| 反馈 | 已发布 Finding 的 developer accept/reject；当前未采集 |
| 用户 | 组织管理员、仓库维护者、Reviewer、普通开发者 |
| 闭环 | Webhook → Review → Finding → 审核 → 发布/拒绝 → 反馈 → 指标 → 仓库规则 |
| 业务 KPI | accepted/rejected、人工复核时间、成本/PR、成本/accepted Finding |
| 可靠性 KPI | completion、review p50/p95、webhook ack、fail-open/degraded/error |
| 安全 KPI | duplicate webhook 新增任务数、unauthorized publish 数量 |
| 当前本机验证 | 646 passed、6 skipped、86% branch coverage、mypy 26 files |
| 当前 master CI | `acc0dcc…` / run `29894645345` / 7 jobs success |
| live 证据 | 1 个 Week 7.5 draft PR；成功但初投曾 10 秒超时 |
| Phase 8 | 137 条真实来源候选；无真人标签；synthetic 不准作质量结论 |
| 范围 | Review 主线；Repair 高风险后续；Verifier Training 研发附录 |
| 明确不做 | 聊天机器人、用户个人记忆、为展示增加多 Agent |
