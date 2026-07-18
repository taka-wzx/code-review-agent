# 可信 Review 评测协议

这套协议解决的不是“再跑一次旧基准”，而是把 Review Agent 的泛化结论建立在一批从未用于
调参的真实 PR 上。第 4 周交付的是预注册、标注流程、数据契约、完整性校验和统计框架；
真实 PR 尚未下载，外部模型和付费评测也尚未运行。

任务合同见 `docs/plans/week4-trusted-review-evaluation.md`，机器可读的预注册见
`trusted_review/cohort-plan.json`。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 3 个 reporting 仓库、30 个真实 PR 的选择计划 | 已预注册，未下载 |
| 独立 calibration 仓库、10 个真实 PR 的选择计划 | 已预注册，未下载 |
| 双人独立标注与第三方仲裁协议 | 已定义 |
| cohort / annotation / run schema | 已实现 |
| precision / recall / F1 与分仓统计 | 已实现 |
| PR 级、仓库内分层 Bootstrap 95% CI | 已实现 |
| 成本、时延、工具调用、fail-open / degraded | 已实现 |
| 防泄漏和调参污染检查 | 已实现 |
| 真实 PR 数据、真实模型结果、最终数值 | 未授权、未产生 |

因此，当前代码能证明“仪器按预注册口径工作”，不能证明 Agent 已在 30 个真实 PR 上达到
任何效果。

## 集合结构

预注册使用仓库作为不可拆分的角色边界：

| 角色 | 仓库 | PR 数 | 可用于 |
| --- | --- | ---: | --- |
| calibration | `pallets/click` | 10 | 标注员训练、流程试跑、输入格式排错 |
| reporting | `pallets/flask` | 10 | 最终密封评测 |
| reporting | `psf/requests` | 10 | 最终密封评测 |
| reporting | `encode/httpx` | 10 | 最终密封评测 |

reporting 三个仓库合计 30 个 PR。calibration 的仓库和 PR 不进入 headline 指标，也不能与
reporting 共享仓库。这样即使 calibration 阶段发生提示词、阈值或标注指南调整，也不会把同一
仓库的约定、命名模式和实现习惯泄漏到最终评测。

这些仓库名称是选择目标，不代表数据已存在于本地。若后续授权采集时某个目标不可用，必须在
查看任何该仓模型结果前修改预注册并记录原因。

## 选择流程

授权联网后，对每个仓库独立执行相同步骤：

1. 枚举预注册时间窗内已合并、非 draft 的 PR。
2. 在看任何 Agent 结果前应用排除规则：
   dependency-only、generated-only、documentation-only、vendored、受安全禁运限制、
   无法固定 base/head/merge SHA、无法离线复现上下文。
3. 将剩余 PR 规范化为 `owner/repo#number`。
4. 计算
   `SHA256(cohort_seed + "\n" + canonical_pr_id)`。
5. 按哈希升序取每仓前 10 个，而不是人工挑“看起来容易”或“肯定有 bug”的 PR。
6. 固定 base/head/merge SHA、diff SHA-256、离线 snapshot SHA-256 和 selection log
   SHA-256。
7. 在任何 Agent 输出产生前完成 gold 标注并写入 `gold_frozen_at`。

选择日志必须保留所有候选的纳入/排除结论及理由。最终 manifest 只引用内容哈希；原始仓库
快照和未脱敏标注保存在受访问控制的数据根，不提交进普通开发路径。

## 标注流程

### 角色

- A、B：两名独立标注者；
- C：第三方仲裁者；
- coordinator：只合并候选身份，不决定对错。

A 和 B 看相同的离线 snapshot、PR 意图和仓库约定，但看不到彼此标签、Agent 名称、模型、
prompt、ablation 和历史结果。展示顺序应分别随机化。

### 阶段 1：独立发现

A 和 B 分别从零审查 PR，提出具体缺陷。每个候选应至少包含：

- PR ID、文件和最小行范围；
- 问题主张；
- 可执行的失败机制；
- 代码或约定证据；
- 严重度；
- 后续 finding 的匹配标准。

coordinator 对 A/B 候选做 identity 合并，形成 union，但不判定有效性。每条
`gold_candidate` 的两份 annotation 通过 `discovered: true/false` 保留是否由该标注者独立
发现。框架报告两个 discovery set 的 Jaccard 和 set-F1。

### 阶段 2：独立有效性判断

A 和 B 对 union 中每条候选分别打：

- `valid_defect`
- `not_defect`
- `uncertain`

两份标签必须都落盘后才能进入仲裁。框架按总体和仓库报告 exact agreement、Cohen's kappa
和需要仲裁的比例。

### 阶段 3：仲裁

满足任一条件必须由 C 仲裁：

- A/B 标签不同；
- 任一人选择 `uncertain`；
- 对 system finding，A/B 指向了不同 gold ID。

C 必须是第三个人，最终标签不能仍是 `uncertain`。如果 A/B 已一致且非 uncertain，反而添加
仲裁记录会被拒绝，防止“事后覆盖”原始共识。

只有在 `gold_frozen_at` 前最终为 `valid_defect` 的 gold unit 进入 recall 分母。

### 阶段 4：Agent finding 判断

gold 冻结、配置冻结、Agent 运行完成后，A/B 再独立判断每条 finding：

| 标签 | 含义 | precision | recall |
| --- | --- | --- | --- |
| `matched` | 命中一个冻结 gold | TP | 对该 gold 记一次命中 |
| `novel_valid` | 是真实缺陷，但不在冻结 gold | TP，单列 | 不扩展冻结分母 |
| `invalid` | 不成立或不可行动 | FP | 无 |
| `duplicate` | 重复报告已命中的 gold | FP | 不重复计数 |
| `unscorable` | 输出损坏或证据不足 | FP | 无 |
| `uncertain` | 独立标注阶段尚不能决定 | 必须仲裁 | 必须仲裁 |

`novel_valid` 的设计避免封闭世界把新发现一律罚成 FP，同时又禁止看完 Agent 输出后扩大 gold
并抬高 recall。一条 gold 每个 PR 最多被一个 `matched` finding 计分；后续重复项必须标成
`duplicate`。

## 数据文件

### Cohort JSON

schema：`trusted_review/schemas/cohort.schema.json`

关键字段：

- `cohort_id` / `cohort_seed`
- `selection_window`
- `repositories[]`: `slug`、`role`、`target_prs`
- `prs[]`: PR identity、Git SHA、merge 时间、diff/snapshot hash、changed lines、change type、
  是否有人类 review comment、无作者/历史使用污染的 attestation、标注完成证明
- `gold_frozen_at`
- `selection_log_sha256`

预注册阶段允许 `prs: []`、freeze/hash 为 `null`；`report` 命令要求完整 materialized cohort，
逐仓 PR 数必须精确等于 target。

`gold_annotation_set_sha256` 的固定算法是：取该 PR 的全部 `gold_candidate` JSONL 行，按
`annotation_id` 排序，再对 UTF-8 canonical JSON（key 排序、无多余空白、保留 Unicode）计算
SHA-256。即使一个 PR 最终没有候选，也要绑定 canonical 空数组的哈希。报告时会重新计算并
逐 PR 比较，防止 gold freeze 后增删或改写标注。

### Annotation JSONL

schema：`trusted_review/schemas/annotations.schema.json`

一行是一名标注者对一个 subject 的记录。`subject_kind` 为 `gold_candidate` 或
`system_finding`；同一个 subject 必须有两名固定 annotator 的独立记录，必要时再有一条
adjudicator 记录。

一次 `report` 输入的 system-finding annotation 必须只属于所选 `config_id` 的 runs；不同
ablation 使用独立、分别哈希的 annotation JSONL，避免 finding identity 混用。

每条记录还必须带非空 rationale、evidence SHA-256；gold 标注带 severity。独立记录的
`source_annotation_*` 为空，仲裁记录必须同时引用两条独立 annotation ID 和它们各自的
canonical SHA-256，避免仲裁结论与后来被改写的源标签脱钩。

格式示例见 `trusted_review/examples/annotations.jsonl`。示例只展示行格式，不是完整 cohort，
也不用于任何指标。

### Run JSONL

schema：`trusted_review/schemas/runs.schema.json`

每行对应一个 PR 上的一次冻结配置运行，包含：

- `run_id`、`pr_id`、`config_id`、`source_commit`
- exact provider/model ID、pricing revision、runtime config SHA-256
- 与 cohort 逐 PR 一致的 snapshot SHA-256
- `purpose`
- 起止时间和 `latency_seconds`
- `status`: `ok` / `degraded` / `fail_open` / `failed`
- `cost_microusd`
- 总工具调用及组件拆分
- Repair 场景可选用的 test status 和越权事件数
- finding identity、fingerprint、相对路径和行号

`cost_microusd` 使用整数，避免浮点货币累计误差。组件调用数存在时必须恰好等于总工具调用。
`failed` 必须不可评分且不能带 findings；其他状态必须可评分。

同一 `config_id` 的 30 个 headline runs 必须共享同一 source commit、provider、model、
pricing revision 和 runtime config hash；任一 PR 的 snapshot hash 不匹配也会整体失败。

## 本地命令

验证尚未 materialize 的预注册：

```powershell
python trusted_review_eval.py validate-cohort `
  --cohort trusted_review\cohort-plan.json
```

验证已 materialize 的 cohort：

```powershell
python trusted_review_eval.py validate-cohort `
  --cohort X:\sealed\cohort.json `
  --materialized
```

生成最终报告：

```powershell
python trusted_review_eval.py report `
  --cohort X:\sealed\cohort.json `
  --annotations X:\sealed\annotations.jsonl `
  --runs X:\sealed\runs.jsonl `
  --config-id frozen-v1 `
  --bootstrap 10000 `
  --seed 20260718 `
  --output X:\sealed\reports\frozen-v1.json
```

工具只读本地 JSON/JSONL，使用标准库，不启动 subprocess，不访问网络，不调用 SDK 或模型。
任何路径组件精确为 `eval` 或 `holdout` 时，在打开文件前拒绝。

报告的 `generated_at` 记录实际生成时间，因此完整文件字节每次会不同；除该字段外，固定输入、
`config_id`、bootstrap 次数和 seed 的计算区块不受输入行顺序影响并可复现。

## 指标口径

每个 PR 先计算：

```text
TP_findings = matched + novel_valid
FP_findings = invalid + duplicate + unscorable
TP_gold     = 被 matched 的唯一冻结 gold 数
FN_gold     = 冻结 gold 总数 - TP_gold

precision = TP_findings / (TP_findings + FP_findings)
recall    = TP_gold / (TP_gold + FN_gold)
F1        = 2PR / (P + R)
```

headline 是 reporting 30 PR 的 micro 指标。报告同时给出：

- 每仓 micro；
- 三仓 macro；
- PR macro；
- 所有分子、分母和特殊标签计数。

某一分母为零时对应指标是 JSON `null`。hard failure 虽不可评分，仍将该 PR 的全部冻结 gold
计入 FN，避免只对成功运行计算 recall；它没有 findings，因此不会伪造 precision 分母。

### Bootstrap 95% CI

最终报告默认 10,000 次 seeded percentile bootstrap：

1. 保持三个 reporting 仓库不变；
2. 在每个仓库内部对该仓 10 个 PR 有放回抽 10 个；
3. 合并三个仓的样本；
4. 重新计算完整 micro precision / recall / F1；
5. 取 2.5% 和 97.5% 百分位。

采样单位是 PR 而不是 finding，因此同一 PR 内相关的 findings 和 gold 不会被拆散；仓库内分层
保持每个仓的固定权重。seed、replicate 数和 defined replicate 数全部进报告。

### 资源与可靠性

所有 attempted PR 都进入运行统计：

- cost：total、mean、p50、p95、max、per-scorable-PR；
- latency：mean、p50、p95、max；
- tool calls：total、mean、p50、p95、max、组件拆分；
- scorable、degraded、fail-open、hard failure 比例；
- Repair telemetry 存在时的 test failure；
- unauthorized operation 事件数、受影响 run 数和 run rate。

`degraded` 和 `fail_open` 是互斥 primary status。认证或限流等账号级错误必须保持 hard failure，
不能包装成 fail-open。

## 防泄漏清单

在最终报告前逐项确认：

- [ ] cohort repo 与现有 prompt、测试、例子、旧 eval、Week 3 issue pilot 没有重用；
- [ ] calibration 和 reporting 仓库集合交集为空；
- [ ] 选择日志是在任何 Agent 输出前冻结；
- [ ] gold 是在任何 reporting run 前冻结；
- [ ] `purpose` 不是 tuning / prompt selection / sentinel design / threshold search；
- [ ] source commit、精确模型 ID、provider、价格版本和 runtime config 已冻结；
- [ ] 所有 ablation 在打开 reporting 结果前预注册；
- [ ] 每个 PR/config 只有一个 headline run；基础设施失败仍作为 failure，未用成功重跑覆盖；
- [ ] reporting 结果未用于修改 prompt、sentinel、阈值、模型或上下文策略；
- [ ] 报告记录 cohort、annotations、runs 三份输入的原始字节 SHA-256。

## 消融

后续获准的付费阶段只运行预注册配置：

1. 单 Finder；
2. 双 Finder；
3. context retrieval off/on；
4. Verifier off/on；
5. Review-only / Repair Reflection；
6. exact model A / exact model B。

所有配置使用相同 snapshot 和 gold。结果用于解释机制，不允许在 reporting 集上挑赢家再宣称
它是预先选定的主模型。

v1 不支持用重跑覆盖 headline failure。若未来确实需要区分“基础设施重跑”，必须在看结果前
先扩展 schema 和预注册，让全部 attempt 都进入成本、时延和失败率分母；不能只保留成功的一次。

## 已知限制

- 30 PR / 3 仓仍是小样本；仓库宏平均只能描述这三个项目。
- PR 内独立发现不是“所有真实缺陷”的证明；`novel_valid` 数量必须单列。
- Cohen's kappa 会受类别不平衡影响，因此必须和 exact agreement、原始 contingency 一起看。
- 仓库内 PR bootstrap 表达 PR 抽样不确定性，不覆盖模型服务漂移、标注系统性盲区或仓库选择
  不确定性。
- 当前没有真实数据和模型结果；任何效果数字必须等用户授权采集、标注和最终运行后再填写。
