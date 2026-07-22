# 业务指标合同

## 目的与状态

本合同把产品问题变成可由事件计算的指标，防止把离线评测、synthetic 演练或单次 live probe
误写成业务收益。它定义未来生产事件和算法；Phase 9A 不实现采集链路，也不填没有真实数据的
基线或目标值。

当前 canonical trace 已能提供部分 review 状态、时延、token 和降级信息，但远程身份、维护者
审批、GitHub 发布回执、开发者反馈和统一业务指标表尚不存在。因此下列 KPI 的当前值均为
**未采集**，不是 0。

## 统一计算约定

### 稳定身份与事件

每个事件至少包含：

```text
schema_version, event_id, event_type, occurred_at, ingested_at,
organization_id, repository_id, pull_request_id, head_sha,
delivery_id?, review_id?, finding_id?, principal_id?,
mode, policy_version, source, payload
```

- `pull_request_id` 使用 GitHub 不随编号展示变化的稳定身份；展示时可附 `owner/repo#number`。
- 逻辑 Review 的唯一键是
  `(repository_id, pull_request_id, head_sha, review_policy_version)`；重试是同一 `review_id` 下的
  `attempt_id`，不能变成新的逻辑 Review。
- `finding_id` 绑定 `review_id + canonical finding fingerprint + content_sha256`。内容变化必须
  产生新版本，反馈不能漂移到另一版本。
- `delivery_id` 是 GitHub `X-GitHub-Delivery`。同一 delivery 的重放只允许映射到原逻辑 Review。
- `principal_id` 只用于最小权限审计和去重，不建立个人画像；产品记忆只按仓库聚合。
- 时间使用 UTC，持续时间来自同一进程的 monotonic clock 后以整数微秒落盘；跨服务链路同时
  保留 wall-clock 时间和 trace/span 关联。

必要事件类型：

```text
webhook_received, webhook_acknowledged, webhook_rejected,
review_queued, review_started, review_terminal,
finding_generated, maintainer_decision,
publish_attempted, publish_succeeded, publish_failed,
developer_feedback,
review_session_heartbeat,
repository_rule_proposed, repository_rule_approved, repository_rule_activated
```

事件必须 append-only；更正通过带 `supersedes_event_id` 的新事件完成。聚合器按 `event_id` 去重，
并对 superseded 事件只保留最终有效版本。

### 窗口、成熟期与迟到数据

- 窗口都是左闭右开 `[window_start, window_end)`。
- 业务质量与单位成本默认使用 UTC 自然月，并同时提供滚动 28 天诊断视图。
- 可靠性与时延默认提供 UTC 日、滚动 7 天；月度业务报告再汇总自然月。
- Review cohort 按 `review_queued.occurred_at` 入窗，窗口结束后等待 24 小时成熟；仍未终态的
  Review 派生 `primary_terminal_class=error` 和 stable category `observation_timeout`，不能从
  分母消失；原 job 状态不被聚合器反向改写。
- Finding 反馈 cohort 按 `publish_succeeded.occurred_at` 入窗，窗口结束后等待 14 天成熟；
  到期仍无有效 accept/reject 的 Finding 进入 `unresolved_count`，不进入 accepted/rejected
  比率分母，但必须单列，防止静默隐藏无反馈样本。
- 迟到或更正事件到达时重算受影响窗口，报告增加 `revision` 和 `recomputed_at`，不覆盖原报告。

### 空分母、百分位与货币

- 比率在分母为 0 时输出 JSON `null`，同时输出 `numerator=0`、`denominator=0` 和排除计数；
  不输出 0%、100% 或“无问题”。
- 计数指标在窗口内无事件时可输出整数 `0`，但其配套 rate 仍遵守空分母为 `null`。
- 百分位：将整数微秒升序排列，`p` 分位取 nearest-rank
  `x[ceil(p*n)-1]`；`n=0` 时为 `null`。p95 在 `n<20` 时仍可计算，但必须带
  `small_sample=true`，不得作为稳定尾延迟结论。
- 成本使用整数 `micro_usd`。provider 原币种账单与估算成本分开；换汇必须绑定汇率来源、
  `fx_revision` 和生效日期。`settled` 与 `estimated` 不混成同一 headline。
- 每个输出都携带 `sample_count`、全部分子/分母、`excluded_by_reason`、窗口、成熟期、模式、
  仓库集合、schema/policy/pricing revision 和数据完整率。

## Finding 结果口径

维护者的 `approved/rejected` 是**发布决策**；开发者的 `accepted/rejected` 是**结果反馈**，二者
不可混用。accepted/actionable 只来自已发布 Finding 的有效开发者反馈：

- `accepted`：开发者明确确认 Finding 可行动（包含已修复或计划修复的结构化子原因）；
- `rejected`：开发者明确认为 Finding 不成立、重复、无关或不值得行动；
- `unresolved`：14 天内没有有效结果反馈；
- 撤回、内容版本变化、机器人反馈、无权主体反馈和测试流量不能进入结果分母。

一个 Finding 只取成熟期内最后一条有效、未被 supersede 的反馈。若同一 Finding 被多位有权
开发者反馈，优先级固定为 PR author 的最新反馈；没有 author 反馈时取最早的 maintainer-confirmed
团队反馈。冲突数必须单列，不能挑对产品有利的标签。

## KPI 定义

### 1. Finding accepted/actionable rate

- **精确定义**：成熟 Finding cohort 中，得到有效 `accepted` 开发者结果的唯一 Finding 占
  所有已得到有效 `accepted` 或 `rejected` 结果的唯一 Finding 比例。
- **分子**：`count(distinct finding_id where final_feedback = accepted)`。
- **分母**：`count(distinct finding_id where final_feedback in {accepted, rejected})`。
- **公式**：`accepted_rate = accepted_count / (accepted_count + rejected_count)`。
- **数据来源**：`publish_succeeded`、`developer_feedback`、Finding 版本表、PR author/仓库角色
  快照。
- **排除规则**：shadow 未发布、维护者仅批准但未发布、发布失败、撤回、内容已换版、测试仓库/
  测试 PR、机器人或无权主体反馈、成熟期内 unresolved；每类分别计数。
- **聚合窗口**：按 `publish_succeeded` 的 UTC 自然月入组，14 天成熟；按组织、仓库、模式和
  Finding severity/type 切片。
- **空分母语义**：`null`；不能把没有反馈写成 0% 或 100%。
- **不能声称**：不能等同缺陷 precision、recall、bug prevented、代码质量提升或人工时间节省；
  accept 仍可能受团队习惯、严重度和反馈选择偏差影响。

### 2. Finding rejection rate

- **精确定义**：与 accepted rate 同一成熟 cohort 中，最终有效结果为 `rejected` 的比例。
- **分子**：`count(distinct finding_id where final_feedback = rejected)`。
- **分母**：`accepted_count + rejected_count`，必须与 accepted rate 完全相同。
- **公式**：`rejection_rate = rejected_count / (accepted_count + rejected_count)`；在同一有效
  cohort 上应满足 `accepted_rate + rejection_rate = 1`。
- **数据来源**：同上，并记录结构化原因 `invalid/duplicate/out_of_scope/low_value/other`。
- **排除规则**：与 accepted rate 完全一致；不得为降低 rejection rate 单独丢弃某类原因。
- **聚合窗口**：UTC 自然月 + 14 天成熟，另报原因分布。
- **空分母语义**：`null`。
- **不能声称**：reject 不自动等于模型误报；它也可能代表重复、时机不合适或产品呈现问题，
  更不能直接推出离线 precision。

### 3. 每 PR 人工复核时间

- **精确定义**：维护者/Reviewer 在 Finding 审核界面处于前台且有活动时，对同一逻辑 Review
  消耗的非重叠主动秒数。
- **分子**：对每个 `review_id`，合并全部有效 heartbeat 区间后的 `active_microseconds` 总和。
- **分母**：有至少一个有效审核 session 且完成维护者决策的 distinct `review_id` 数；headline
  输出 `sum(active_microseconds) / review_count`，并同时输出 p50/p95。
- **采集算法**：前端在窗口聚焦且检测到键盘/鼠标活动时每 15 秒发 heartbeat；每条只贡献
  `[occurred_at-15s, occurred_at]`，跨 tab/Reviewer 区间取集合并集，失焦、断连或超过 30 秒
  的空档不补计。
- **数据来源**：`review_session_heartbeat`、`maintainer_decision`、Review/Finding 身份表。
- **排除规则**：机器人/API 自动动作、后台 tab、没有最终决策的调试 session、测试仓库；
  多人同时审核的重叠时间只计一次 wall-clock，并额外报告 person-seconds 诊断值。
- **聚合窗口**：按 `maintainer_decision.occurred_at` 的 UTC 自然月；按仓库和模式切片。
- **空分母语义**：均值和百分位均为 `null`。
- **不能声称**：没有同仓库、同 PR 规模口径的人工基线或随机/阶梯对照时，不能声称“节省了
  X 分钟”；active UI 时间也不是 Reviewer 的全部认知成本。

### 4. Agent completion rate

- **精确定义**：进入队列的成熟逻辑 Review 中，在 24 小时内持久化可展示终态结果的比例。
- **分子**：`count(review_id where terminal_status in {succeeded, degraded, fail_open}
  and result_persisted=true and terminal_at-queued_at <= 24h)`。
- **分母**：`count(distinct review_id with review_queued in window)`；同一 delivery/head SHA 的
  重放与 attempt 不增加分母。
- **数据来源**：幂等 job store 的 `review_queued/review_terminal`、结果持久化回执、canonical
  trace 交叉校验。
- **排除规则**：已验签但被策略忽略的事件、无效签名、未注册仓库、draft 策略不触发的事件
  不算 attempted；一旦 `review_queued` 成功就不得因失败、取消或重启从分母删除。
- **聚合窗口**：UTC 日和滚动 7 天，24 小时成熟；月报再汇总自然月。
- **空分母语义**：`null`。
- **不能声称**：完成不代表 Finding 正确、被接受、在 SLO 内或成功发布；degraded/fail-open 必须
  另报，不能藏在完成率中。

### 5. p50/p95 Review latency

- **精确定义**：完成逻辑 Review 从 `review_queued` 持久化成功到 `review_terminal` 结果持久化
  成功的 wall-clock 微秒分布。
- **分子**：非比率；样本向量为每个完成 Review 的
  `terminal_persisted_at - queued_persisted_at`。
- **分母**：向量中的完成 Review 数 `n`；attempt 内部重试不单独成为样本。
- **数据来源**：job store 时间戳和 root trace duration；两者差异超过预设容差时标记
  `timing_inconsistent` 并排除 headline、单列计数。
- **排除规则**：未完成/错误 Review 不进入完成延迟分布但必须进入 completion/error rate；测试
  流量排除。`degraded`、`fail_open` 分别出切片，不能只保留最快成功样本。
- **聚合窗口**：按 `review_queued` 的 UTC 日、滚动 7 天，24 小时成熟；nearest-rank p50/p95。
- **空分母语义**：两个百分位都为 `null`。
- **不能声称**：单次 Week 7.5 的约 254 秒不是 p50/p95；完成样本的尾延迟也不能代表 webhook
  ack、GitHub 网络或用户等待体验的全部。

### 6. Webhook acknowledgement latency

- **精确定义**：服务从收到请求体第一个字节到最后一个响应字节交给 ASGI server 的微秒数；
  headline 只看验签通过、受支持且返回 2xx 的 GitHub delivery。
- **分子**：非比率；样本向量为 `webhook_acknowledged_at - webhook_first_byte_at`。
- **分母**：满足 headline 条件的 distinct `delivery_id` 数；重复 delivery 仍是独立 HTTP ack
  样本，但不应新增 Review。
- **数据来源**：API edge/ASGI 中间件 monotonic 时间、HMAC/路由结果、HTTP status、delivery ID。
- **排除规则**：健康检查、非 GitHub 请求、缺 delivery ID；无效签名/超限/不支持事件另做 status
  切片，不混入 headline。客户端/隧道往返不在服务端值中，若采集则作为另一指标。
- **聚合窗口**：UTC 日和滚动 7 天；nearest-rank p50/p95，同时报告 max 和 status 分布。
- **空分母语义**：百分位为 `null`。
- **不能声称**：ack 快不代表任务已启动或完成；服务端 ack 也不能解释 Week 7.5 初次 GitHub
  10 秒超时的端到端根因。

### 7. 每 PR 成本

- **精确定义**：窗口内所有 attempted 逻辑 Review 的完整 Agent 运行成本除以 attempted Review
  数，失败与重试成本都归入其逻辑 Review。
- **分子**：`sum(review_total_cost_microusd)`，包括该 Review 的全部 provider attempts；可归因的
  托管/沙箱成本若未来纳入，必须作为独立组件和版本化分摊规则。
- **分母**：`count(distinct review_id with review_queued in window)`。
- **数据来源**：provider 账单或 token trace + 冻结 pricing revision、attempt ledger、成本归因表。
- **排除规则**：测试流量单列；duplicate delivery 未新增 attempt 时成本为 0。任何 Review 成本
  缺失、币种不可换算或 pricing revision 缺失时，headline 为 `null`，并报告 cost coverage，
  不只对有成本的成功样本求均值。
- **聚合窗口**：按 `review_queued` 的 UTC 自然月，24 小时成熟；settled 和 estimated 分开。
- **空分母语义**：`null`；总成本计数仍为 0。
- **不能声称**：模型 API 成本不等于总拥有成本，也不能单独证明 ROI、节省人力或预算可预测。

### 8. 每个 accepted Finding 成本

- **精确定义**：成熟 Review cohort 的全部 Agent 成本，除以这些 Review 最终产生的 accepted
  Finding 数；没有 Finding、发布失败、被拒绝和无反馈 Review 的成本仍留在分子。
- **分子**：`sum(review_total_cost_microusd)` for all distinct reviews queued in the cohort。
- **分母**：这些 `review_id` 关联、在 14 天反馈成熟期内最终为 accepted 的 distinct
  `finding_id` 数。
- **数据来源**：Review/attempt 成本账、Finding lineage、`publish_succeeded`、最终
  `developer_feedback`。
- **排除规则**：测试流量排除；成本不完整时 headline 为 `null`；同一 accepted Finding 不因
  多次反馈重复计数；被 supersede 的 Finding 版本不计 accepted。
- **聚合窗口**：按 `review_queued` 的 UTC 自然月，等待 Review 24 小时和反馈 14 天后结算；
  settled/estimated 分开。
- **空分母语义**：accepted Finding 为 0 时输出 `null`，同时保留总成本和 `accepted_count=0`。
- **不能声称**：该值不是“修复一个 bug 的成本”；accepted 不保证 Finding 正确或产生业务
  收益，也不含维护者人工成本，除非另行定义并展示。

### 9. fail-open / degraded / error rate

- **精确定义**：每个 attempted 逻辑 Review 只派生一个 primary terminal class：
  `error > fail_open > degraded > succeeded`。若多个信号同时出现，按此前置顺序归类并保留原始
  flags。分别计算三个互斥比率。
- **分子**：
  - `fail_open_count = count(review_id where primary_terminal_class=fail_open)`；
  - `degraded_count = count(...=degraded)`；
  - `error_count = count(...=error)`。
- **分母**：三者均为窗口内 `review_queued` 的 distinct `review_id` 数。
- **数据来源**：job store 终态、canonical trace 的 fail-open/degraded/error 事件、provider/
  worker stable error category；不保存原始异常消息。
- **排除规则**：队列前策略忽略和无效 webhook 不进入 Review 分母，另入 webhook 指标；排队后
  的超时、重启、认证、限流、结果持久化失败均不得排除。重复 attempt 归并到原 Review。
- **聚合窗口**：UTC 日、滚动 7 天和自然月；同时按 stable error category 切片。
- **空分母语义**：三个 rate 均为 `null`，count 均为 0。
- **不能声称**：零比率只描述已观测窗口；不能证明不会失败。fail-open 是保留输出的风险状态，
  不能作为普通成功隐藏；fake suite 的 0 率不是生产 rate。

### 10. Duplicate webhook 新增任务数

- **精确定义**：被判定为重复的 delivery 到达后，错误新增的逻辑 Review 行数。既检查同一
  `delivery_id` 重放，也检查同一 PR/head/policy 幂等键的重复事件。
- **分子**：`count(distinct review_id newly inserted where creation_receipt_id belongs to a
  duplicate receipt)`；headline 是整数 `duplicate_created_job_count`。`creation_receipt_id` 必须由
  幂等事务审计写入，不能靠相邻查询的行数差推断。
- **分母**：`count(distinct duplicate HTTP receipt event)`；辅助率为
  `duplicate_created_job_count / duplicate_delivery_count`。
- **数据来源**：webhook receipt ledger、delivery→review 映射、job store 事务审计、
  `review_queued` 事件。
- **排除规则**：新的 head SHA 或新的 policy version 是新逻辑 Review，不算 duplicate；超过明确
  idempotency retention 后的事件单列，不能默认为重复或新任务。
- **聚合窗口**：UTC 日、滚动 7 天和自然月，按 receipt 时间入窗。
- **空分母语义**：新增任务**数量**为 0；duplicate rate 为 `null`。
- **不能声称**：数量为 0 只证明已观测服务和保留窗口内没有重复插入；不能证明跨区域、跨库或
  任意故障下 exactly-once。Week 7.5 的一次重放证据也不是长期生产率。

### 11. Unauthorized publish 数量

- **精确定义**：`publish_succeeded` 发生时，没有同时满足以下全部条件的发布次数：主体在审批
  时和发布时均具备仓库维护者权限；审批绑定相同 repository/PR/head SHA/Finding 内容哈希；
  nonce 在本次 publish attempt 原子消费前未过期且未被其他 attempt 消费、本 attempt 是唯一
  消费者；仓库处于 guarded publish；发布回执对应同一 payload hash。
- **分子**：`count(publish_succeeded where authorization_binding_valid=false)`；这是整数
  `unauthorized_publish_count`。被成功阻断的尝试另报 `blocked_unauthorized_attempt_count`，
  不能混入分子制造“发生过发布”的误解。
- **分母**：辅助率使用窗口内全部 `publish_succeeded` 数；安全 headline 始终同时展示绝对数量。
- **数据来源**：不可变审批记录、RBAC/仓库权限快照、policy version、发布 attempt/payload hash、
  GitHub publish receipt 和审计 trace。
- **排除规则**：shadow 内部展示、发布前被阻断、GitHub 返回失败都不是 unauthorized publish；
  但必须作为 blocked/failed attempt 单列。审计字段缺失时按 invalid binding，不能假设授权。
- **聚合窗口**：实时告警 + UTC 日/自然月报告；事件按 `publish_succeeded.occurred_at` 入窗。
- **空分母语义**：数量为 0；rate 在没有成功发布时为 `null`。
- **不能声称**：观测到 0 不等于系统安全、RBAC 正确或不存在绕过；只有覆盖完整、独立核验的
  发布链路才能支持更强结论。当前产品尚未实现该远程发布链路，当前值是未采集而不是 0。

## 仓库规则/反馈记忆更新门槛

反馈聚合不能直接在线改 prompt。规则候选必须满足：

1. 只使用同一仓库、已成熟、非测试的 Finding 结果；
2. 携带窗口、样本量、accepted/rejected/unresolved、原因分布和生成算法版本；
3. 默认至少 20 个已解决 Finding 且跨至少 5 个 PR；未达门槛只展示，不自动建议；
4. 由仓库维护者明确批准，形成不可变 `rule_version` 和回滚父版本；
5. 激活前离线回放，确认不会隐藏安全/高严重度类别；
6. 规则只对后续 Review 生效，不改写历史指标或已有 Finding；
7. 不保存或推断某位开发者的个人偏好。

这些是目标合同，Phase 9A 不实现规则引擎。阈值本身不是业务效果保证，试点后如需调整必须版本化。

## 数据质量门禁

每份 KPI 报告必须先通过：

- event ID 唯一、必填字段完整、时间非负且状态转换合法；
- delivery/review/finding/publish/feedback lineage 可追溯；
- 终态互斥、成本账和 attempt 汇总守恒；
- unauthorized publish 与 duplicate-job 数可从原始审计事件重新派生；
- 数据完整率低于 100% 时，对受影响的成本、时间或授权 headline 输出 `null`，不得插值；
- shadow 与 guarded publish 分开报告；仓库或 cohort 过小时不做组织泛化；
- 报告保留 schema、代码提交、查询版本和输入哈希，允许复算。

## 当前证据映射

| 证据 | 可以支持 | 不能支持 |
| --- | --- | --- |
| 当前离线 646-test / 86% coverage 验证 | 代码回归门在本机通过 | 任何业务 KPI 或生产可靠性 |
| Week 7.5 单个 draft PR live chain | 该次链路成功、幂等重放未新增任务、无效 HMAC 被拒 | completion rate、p95、长期 exactly-once、业务接受率 |
| 历史单项目植入缺陷评测 | Finder/Verifier 机制的工程迭代信号 | 真实团队 accepted rate、时间节省或跨仓泛化 |
| Week 6 fake/synthetic 安全探针 | 冻结控制面回归 | 生产攻击抵抗力或 unauthorized publish 生产率 |
| Phase 8 synthetic 训练/标注闭环 | artifact、split、训练和 freeze 协议可运行 | 模型质量提升、真实标注一致率或业务收益 |
