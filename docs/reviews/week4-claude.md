# Week 4 可信 Review 评测体系 — Claude 独立审查报告

## 1. 审查范围

- 基线（本地最新 master）：`9564cc817d5d0639b6c31cf4bde540594b38382d`
- Codex 交接提交：`d7aa90ac359432029bf86ba776a443427534eba0`
- 审查分支：`claude/week4-trusted-review-evaluation-review`（HEAD 即交接提交，工作区干净）
- 审查对象：交接提交的全部 12 个变更文件（AGENDA.md、README.md、
  docs/plans/week4-trusted-review-evaluation.md、docs/trusted-review-evaluation.md、
  tests/test_trusted_review_eval.py、trusted_review/cohort-plan.json、
  trusted_review/examples/{annotations,runs}.jsonl、
  trusted_review/schemas/{annotations,cohort,runs}.schema.json、trusted_review_eval.py），
  以及只读参照 AGENTS.md、docs/agent-contract.md、pyproject.toml、scripts/verify.py。
- 变更文件集合与任务合同（docs/plans/week4-trusted-review-evaluation.md「File
  ownership」）声明的 Codex 所有权清单**完全一致**，无越权文件。
- 审查方式：逐行阅读 + 离线可复现探针脚本（仅 import `trusted_review_eval`
  与其测试辅助函数构造合成数据，未触碰 `eval/**`、未联网、未调用任何模型）。

## 2. 总体结论：**有条件通过**

评测仪器整体质量高：校验一贯 fail-closed，未知字段拒绝，双标/仲裁/时间线/哈希
绑定/单 run 覆盖等关键约束均由代码强制且有测试；40 个专项测试与全仓离线验证
（391 测试、覆盖率 85% 门禁、ruff、mypy）全部通过；文档与实现口径基本一致，且
诚实声明"仪器完成 ≠ 效果达成"。

条件：**F-1（P1）必须在任何真实 sealed reporting 运行前修复**——当前协议对
"重复的 novel 发现"既无合法标注出口、也无去重计分，precision 可被无上限抬高。
F-2/F-3/F-4（P2）应在 cohort materialize 之前处理，否则"确定性选择"和"冻结时间
线"的可信度主要依赖人工纪律而非可验证证据。

## 3. Findings（按严重度排序）

### F-1（P1）重复 novel 发现逐条计 TP，precision 可被通胀，且标注协议无合法出口

- 文件：[trusted_review_eval.py:1021-1023](../../trusted_review_eval.py#L1021-L1023)
  （`novel_valid` 直接 `tp_findings += 1`）；
  [trusted_review_eval.py:1024-1030](../../trusted_review_eval.py#L1024-L1030)
  （`duplicate` 要求 `gold_id` 必须在冻结 gold 集内）；
  [trusted_review_eval.py:839-851](../../trusted_review_eval.py#L839-L851)
  （`fingerprint_sha256` 仅做格式校验，同 run 内重复指纹不拒绝、不参与任何去重）；
  协议定义见 [docs/trusted-review-evaluation.md:117-129](../../docs/trusted-review-evaluation.md)（阶段 4 标签表）。
- 触发条件（已用探针复现）：同一 run 提交两条 `finding`，`fingerprint_sha256`
  完全相同，双标注者均只能标 `novel_valid`。结果被完整接受：
  `tp_findings=32, fp_findings=0, novel_valid=2, precision=1.0`。
  反向探针：将第二条标为 `duplicate` 并引用第一条 novel 发现的 ID，
  `score_review_runs` 抛出
  `duplicate finding ... references invalid gold`——即协议上**无法**把 novel
  重复标成重复。六个 finding 标签里，novel 重复项唯一"不撒谎"的选择就是再标一次
  `novel_valid`，于是每条重复都进 TP。
- 实际行为：gold 命中有"每 PR 至多记一次 + 后续必须 duplicate 且 duplicate 计
  FP"的完整防重复语义（1011-1030 行），novel 发现完全没有对应机制。
- 期望行为：与 gold 对称——同一缺陷的重复 novel 报告至多记一次 TP，其余进 FP。
- 影响：headline precision/F1 可被系统性抬高（agent 把同一个非 gold 缺陷在多个
  行位/文件重复报告是真实常见行为，无需恶意）；同时诚实标注者被迫产出失真标签，
  agreement 统计也随之失真。这直接击穿"可信 precision"的目标。
- 最小修复方向：三选一或组合——
  1. 扩展 `duplicate` 语义：`gold_id` 允许引用同 run 内已标 `novel_valid` 的
     finding_id（或新增 `duplicate_of_novel` 标签），计 FP；
  2. 在 `_validate_run_row` 拒绝同 run 内重复的 `fingerprint_sha256`；
  3. 计分时对 novel_valid 按 fingerprint 去重，重复者计 FP。
  任一方案都需要同步更新 schema、阶段 4 文档表格和新增负例测试。

### F-2（P2）cohort_seed 在选择窗口完全成为历史后才落定，"确定性选择"可被 seed-shopping 绕过

- 文件：[trusted_review/cohort-plan.json:4-8](../../trusted_review/cohort-plan.json)
  （`cohort_seed` 已提交，`selection_window` 为 2024-01-01 至 2026-01-01，而预注册
  发生在 2026-07）；选择规则见
  [docs/trusted-review-evaluation.md:45-62](../../docs/trusted-review-evaluation.md)。
- 触发条件：四个目标仓库均为公开仓库，窗口内 merged PR 集合在预注册时已是可完整
  枚举的历史数据。任何人都可以离线枚举候选集，对不同 seed 计算
  `SHA256(seed + "\n" + pr_id)` 排名，挑一个"前 10 名看起来有利"的 seed 再写进
  预注册——产物与诚实流程逐字节不可区分。
- 实际行为：代码只校验 seed 是 64 位十六进制
  （[trusted_review_eval.py:303](../../trusted_review_eval.py#L303)），文档未规定
  seed 的生成方式与来源证明。另外该 seed 值与示例
  [trusted_review/examples/runs.jsonl:1](../../trusted_review/examples/runs.jsonl)
  中的 `fingerprint_sha256` 逐字符相同，说明它是复制粘贴而来，提供不了任何
  provenance（另见 F-12）。
- 期望行为：seed 不可被选择者自由挑选——例如规定
  `seed = SHA256(基线 master commit SHA + 固定串)` 并写入预注册，或使用预注册
  之后才产生的公共熵源；至少在文档中声明生成程序使其可复核。
- 影响：整套"哈希排名取前 10"的反挑选设计在敌手模型下失效；对诚实执行者影响
  为零，因此定 P2 而非 P1。
- 最小修复方向：在 cohort-plan 与协议文档中补 seed derivation 规则（推荐可复算
  公式），materialize 校验时复算比对。

### F-3（P2）gold 冻结与 run 时间线完全依赖自报时间戳，哈希链无外部时间锚点

- 文件：[trusted_review_eval.py:565-568](../../trusted_review_eval.py#L565-L568)
  （gold 标注须早于 `gold_frozen_at`）、
  [trusted_review_eval.py:889-890](../../trusted_review_eval.py#L889-L890)
  （run 须晚于 `gold_frozen_at`）——两侧比较的都是输入文件里的字符串时间戳。
- 触发条件：先看 Agent 输出、再补写/改写 gold 标注，然后重算
  `gold_annotation_set_sha256` 写回 cohort、重排 `created_at`/`gold_frozen_at`/
  `started_at`。所有内部哈希（逐 PR gold set hash、report 的三个 input hash）
  会重新自洽，`report` 命令无从察觉。
- 实际行为：哈希绑定证明的是"输入文件自提交给 CLI 后未被篡改"，不证明"gold 在
  run 之前冻结"这一时间断言。文档的防泄漏清单
  （[docs/trusted-review-evaluation.md:284-297](../../docs/trusted-review-evaluation.md)）
  把"gold 在任何 reporting run 前冻结"列为人工确认项，但没有任何机制产生可审计
  证据。
- 期望行为：materialized cohort（含全部 `gold_annotation_set_sha256`）与
  annotation 文件的 SHA-256 必须在任何 reporting run 之前进入一个外部不可回改的
  锚点——最直接的是提交进 git 历史（run 行里已有 `source_commit` 字段可顺带绑定
  "该 commit 的树里包含此 cohort hash"）。
- 影响：这是离线验证器的固有信任边界，无法全靠代码封死，但当前文档连"必须先
  commit 冻结哈希再跑 run"的操作步骤都没有写成硬性流程。
- 最小修复方向：在协议文档把"冻结 = git commit cohort+annotations 哈希"写为
  必经步骤并纳入防泄漏清单；可选地让 run 行携带 `cohort_sha256` 由
  `validate_runs` 校验一致。

### F-4（P2）确定性选择规则不可机器复核：selection log 无格式、无验证命令

- 文件：[trusted_review_eval.py:416-418](../../trusted_review_eval.py#L416-L418)
  （`selection_log_sha256` 只校验是 64 位十六进制或 null）；规则描述见
  [docs/plans/week4-trusted-review-evaluation.md:117-134](../../docs/plans/week4-trusted-review-evaluation.md)。
- 触发条件：materialized cohort 只需给出 30 个 PR 与一个 log 哈希。工具不复算
  `SHA256(cohort_seed + "\n" + canonical_pr_id)` 排名，也没有 selection log 的
  schema/加载器，无法验证"这 10 个确实是候选集中哈希最小的 10 个"以及排除决策
  是否事后偏置（排除规则含主观判断，是比 seed 更宽的挑选通道）。
- 实际行为：确定性选择目前是纯文档承诺 + 一个外部文件的哈希占位。
- 期望行为：定义 selection log 的 JSONL 契约（候选 PR、纳入/排除结论与理由、
  排名哈希），并提供 `verify-selection` 子命令：校验 log 字节哈希等于
  `selection_log_sha256`，复算排名并比对 manifest 的 PR 集合。
- 影响：审计者现阶段无法独立确认选择未被 cherry-pick；与 F-2 叠加时挑选空间
  更大。
- 最小修复方向：如上，最少也应在文档规定 log 的必备字段，使人工审计可执行。

### F-5（P3）repository_macro / pr_macro 静默丢弃未定义分量，且不报告参与数

- 文件：[trusted_review_eval.py:970-972](../../trusted_review_eval.py#L970-L972)
  （`_mean_defined` 丢 None）、
  [trusted_review_eval.py:1164-1171](../../trusted_review_eval.py#L1164-L1171)。
- 触发条件（已复现）：令 `reporting/alpha` 全部 PR 无 gold 且无 finding
  （precision/recall 均 None），macro 输出
  `{"precision": 1.0, "recall": 1.0, "f1": 1.0}`——实际只有 2/3 仓库参与平均，
  输出中没有任何参与计数。
- 实际行为：宏平均的分母随 None 静默收缩；审计者只能靠自查 `by_repository`
  发现。
- 期望行为：macro 块附带 `defined_repositories` / `defined_prs` 计数，或在任一
  分量未定义时将 macro 置 null 并给 reason。
- 影响：极端子集下宏指标可能被误读偏高；headline micro 不受影响，故 P3。
- 最小修复方向：在 macro 字典中并列输出参与计数；补一个含 None 分量的测试。

### F-6（P3）bootstrap 分位数取法与文档及自身 `_percentile` 不一致（偏保守）

- 文件：[trusted_review_eval.py:1195-1199](../../trusted_review_eval.py#L1195-L1199)
  （low 取 `floor((n-1)*α/2)`、high 取 `ceil((n-1)*(1-α/2))` 的次序统计量）对比
  [trusted_review_eval.py:1066-1078](../../trusted_review_eval.py#L1066-L1078)
  （latency/tool 分位数用线性插值）；文档口径"取 2.5% 和 97.5% 百分位"见
  [docs/trusted-review-evaluation.md:257-268](../../docs/trusted-review-evaluation.md)。
- 触发条件：n=10000 时实际取 quantile 0.02490 与 0.97510（复算确认），区间比
  名义 95% 略宽；defined replicates 少时（如 20）会退化为全距。
- 实际行为/影响：方向保守（不会虚增显著性），确定性可复现，无 off-by-one 越界；
  但同一文件内存在两种分位数定义，且与文档字面口径有偏差。
- 期望行为：统一用同一插值定义，或在文档注明"保守取整的次序统计量"。
- 最小修复方向：文档加一句实现口径说明即可；改实现则需同步测试。

### F-7（P3）`unresolved_subjects` / `malformed_subjects` 恒为 0，属死字段

- 文件：[trusted_review_eval.py:740-741](../../trusted_review_eval.py#L740-L741)。
- 触发条件：任何未解决/畸形 subject 在 `resolve_annotations` 阶段直接抛
  ValidationError，`_agreement_block` 根本不可能收到非零计数。
- 实际行为：报告里两个硬编码 0 字段暗示"统计过且为零"，实际是"fail-closed 使
  其不可能非零"。计划文档（plan「Annotation agreement statistics」）承诺报告
  该数字，读者可能误以为框架容忍并计数这类 subject。
- 期望行为：删除字段，或在文档/字段名标明恒为零的语义（fail-closed）。
- 影响：仅误导审计阅读，不影响数值。
- 最小修复方向：文档一句话说明，或去掉字段。

### F-8（P3）`selected_at` 允许早于 `merged_at`

- 文件：[trusted_review_eval.py:381-383](../../trusted_review_eval.py#L381-L383)
  （只检查 `selected_at >= window.start`）。
- 触发条件（已复现）：PR `merged_at=2025-06-01`、`selected_at=2024-02-01`，
  materialized 校验通过——"在 PR 合并前就选中了它"这一自相矛盾的时间线被接受。
- 实际行为：无 `selected_at >= merged_at` 交叉检查。
- 期望行为：拒绝 `selected_at < merged_at`。
- 影响：单独看只是元数据自洽性漏洞；但它削弱了时间线字段整体的可信度
  （与 F-3 相关）。
- 最小修复方向：materialized 分支加一行比较 + 一个负例测试。

### F-9（P3）时间戳接受非规范 ISO 紧凑形式

- 文件：[trusted_review_eval.py:217-227](../../trusted_review_eval.py#L217-L227)。
- 触发条件（已复现）：`"20260102T010000Z"` 被 `parse_timestamp` 接受
  （Python 3.11+ `fromisoformat` 支持紧凑格式）；`"...+00:00Z"` 正确拒绝。
- 实际行为：同一时刻存在多种可接受拼写。gold set hash 按原始字符串参与
  canonical JSON，拼写不同会哈希不同——方向上 fail-closed，安全；主要是校验
  严格性与 schema `pattern: "Z$"` 的宽松互相叠加，规范性不足。
- 期望行为：锚定单一规范形式（如强制 `YYYY-MM-DDTHH:MM:SSZ` 正则）。
- 影响：低；不改变任何统计。
- 最小修复方向：`parse_timestamp` 前置一个格式正则。

### F-10（P3）允许 gold 候选被两名标注者同时标 `discovered=false`

- 文件：[trusted_review_eval.py:489-492](../../trusted_review_eval.py#L489-L492)
  （只校验类型，不校验"至少一人发现"）。
- 触发条件（已复现）：某 gold 候选两条独立标注 `discovered` 均为 false，
  `resolve_annotations` 通过。
- 实际行为：协议规定候选集是 A/B 发现的 union、coordinator 只合并身份不造候选
  （[docs/trusted-review-evaluation.md:77-88](../../docs/trusted-review-evaluation.md)），
  但"无人发现的候选"（即第三方注入）不会被拒绝，且它照常进入 recall 分母。
- 期望行为：每个 gold 候选至少一条独立标注 `discovered=true`。
- 影响：为"注入外部知识构造 gold"留了未校验通道；诚实流程不受影响。
- 最小修复方向：在按 subject 分组校验处加一条断言 + 负例测试。

### F-11（P3）JSON Schema 仅为参考文档：无实例校验路径，测试只验证其能被解析

- 文件：[tests/test_trusted_review_eval.py:915-944](../../tests/test_trusted_review_eval.py#L915-L944)
  （仅 `json.loads` schema 文件 + 用 **Python** 校验器验证示例）；三个 schema
  文件全部未被任何运行时代码引用。
- 实际行为：受 stdlib-only 约束，CLI 不做 JSON Schema 校验，权威语义完全在
  Python。抽查确认 schema 约束处处**弱于或等于** Python（label/severity/
  discovered 的条件约束、source 数组 0-或-2、唯一性、覆盖率、diversity 均只在
  Python 中），方向安全（schema 放行的会被 CLI 拒绝，不会反向漏放）；另外
  draft 2020-12 中 `format: "date-time"` 默认仅为注解不做断言，时间戳在 schema
  层实际只被 `pattern: "Z$"` 约束。
- 期望行为：文档明确"Python 校验为规范性权威，schema 为弱化的互操作参考"；
  可选用 `if/then` 补齐条件约束。
- 影响：第三方若仅用 schema 生成/预检数据，会产出被 CLI 拒绝的行；无指标风险。
- 最小修复方向：两份文档各加一句权威性声明。

### F-12（P3）示例与预注册数据卫生：cohort_seed 被复用为示例指纹

- 文件：[trusted_review/cohort-plan.json:4](../../trusted_review/cohort-plan.json)
  与 [trusted_review/examples/runs.jsonl:1](../../trusted_review/examples/runs.jsonl)
  的 `fingerprint_sha256` 完全相同
  （`2f7eb8f0...89f12a`）。
- 实际行为：正式预注册的 seed 与合成示例数据共享同一 magic 值，坐实 seed 无
  provenance（见 F-2），也容易让后来者误以为两者有语义关联。
- 期望行为：示例哈希用明显的占位值（如 `1111...`，同文件其他字段的风格）；seed
  按 F-2 给出生成规则。
- 影响：无数值影响；与 F-2 叠加降低预注册可信度。
- 最小修复方向：替换示例指纹；随 F-2 一并补 seed 来源。

### F-13（P3）测试缺口：三条已实现的 fail-closed 路径无回归测试

- 文件：[tests/test_trusted_review_eval.py](../../tests/test_trusted_review_eval.py)。
- 缺失的负例（行为均已由探针确认正确，仅缺测试锁定）：
  1. run 中存在无任何标注的 finding →
     `finding ... lacks final independent/adjudicated labels`
     （[trusted_review_eval.py:1004-1005](../../trusted_review_eval.py#L1004-L1005)）；
  2. JSONL 行级语法错误 → `...:N is not valid JSON`
     （[trusted_review_eval.py:277-285](../../trusted_review_eval.py#L277-L285)）；
  3. 标注引用 calibration PR → `annotation references non-reporting or unknown PR`
     （[trusted_review_eval.py:553-554](../../trusted_review_eval.py#L553-L554)）。
- 影响：这些语义目前正确，但未来重构可能悄悄退化。
- 最小修复方向：各加一个 assertRaisesRegex 用例。

## 4. 验证命令及结果

| 命令 | 结果 |
| --- | --- |
| `git branch --show-current` / `git rev-parse HEAD` / `git status --short --branch` | 分支、HEAD（d7aa90a…）、干净工作区均与交接信息一致 |
| `$env:PYTHONPATH=<worktree>\src; .venv python -B -m unittest tests.test_trusted_review_eval -v` | **40 个测试全部通过**（0.19s） |
| `.venv python scripts\verify.py`（未加 `--eval-assets`） | 首次运行失败：test_week3_tools 2 个 ERROR，原因是 venv 的 editable 安装把 `code_review_agent` 解析到 **integration-week3 worktree** 的旧 `sandbox.py`（缺 `_path_has_symlink_or_reparse_component`）。设置 `PYTHONPATH=<worktree>\src` 后重跑：**Ruff 通过、391 测试通过（3 跳过）、覆盖率 TOTAL 85% 达标、mypy 通过、两个入口冒烟通过、All offline validation passed**。该失败是共享 venv 指向他处 worktree 的环境问题，非本交接提交缺陷（本 worktree 的 `sandbox.py` 含该属性）。 |
| `git diff --check 9564cc8… d7aa90a…` | 无空白错误，退出码 0 |
| `git diff --name-status 9564cc8… d7aa90a…` | 12 个文件，与合同所有权清单一致 |
| `python -B trusted_review_eval.py validate-cohort --cohort trusted_review\cohort-plan.json` | 通过：3 reporting 仓 / 30 计划 PR、1 calibration 仓 / 10 PR、`valid: true`、退出码 0 |
| 离线探针脚本（scratchpad，合成数据） | 复现 F-1（novel 重复 `tp=32, precision=1.0`；duplicate-of-novel 被拒）、F-5、F-8、F-9、F-10；确认无标注 finding、空 annotations、坏 gold hash 等均正确 fail-closed |

## 5. 已检查且未发现缺陷的高风险边界

- calibration/reporting 仓库不相交、角色一致性、重复仓库/PR/snapshot 拒绝；
  materialized 逐仓精确计数、≥3 仓/≥30 PR、size band/change type/review-comment
  多样性（多样性仅 materialized 时校验，位置正确）。
- 双人独立标注：恰好两条、两人不同、全局同一对标注者；仲裁者为全局同一第三人、
  不得与标注者重合；一致且非 uncertain 时禁止事后仲裁；仲裁必须晚于两条独立标注
  并逐条绑定其 annotation_id 与 canonical SHA-256（改写源标注即 fail）。
- gold set 哈希：含仲裁行、按 annotation_id 排序、canonical JSON、空集也绑定
  空数组哈希；逐 reporting PR 与 manifest 比对。
- 计分语义：matched 每 gold 至多一次、二次命中必须 duplicate 且 duplicate 计
  FP、orphan duplicate 拒绝、unscorable/invalid 计 FP、novel_valid 不进 recall
  分母；failed run 强制无 findings、其 gold 全部计 FN（recall 受罚而 precision
  分母不被伪造）；零分母一律 JSON null。
- run 约束：恰好覆盖全部 reporting PR、每 PR 一条、snapshot 逐 PR 绑定、30 条
  headline run 共享单一 source/model/pricing/runtime 身份、purpose 只接受
  final_report、tuning 等四种 purpose 全局拒绝、run 必须晚于 gold freeze、
  finding_id 全局唯一、路径禁绝对/父目录。
- telemetry：分母为全部 attempted run（含 failed）；NaN/负数/组件总数不一致
  拒绝；latency 与起止时间交叉校验；test_status 与越权事件统计口径正确。
- bootstrap：按仓库分层、按 PR 重采样（非按 finding）、seed 确定、输入顺序无
  关（测试与读码均确认）；<2 PR 时显式给 reason。
- 路径防护：任何路径组件精确等于 `eval`/`holdout`（casefold、resolve 后）在
  读写前拒绝，输入输出两侧都过 `_reject_forbidden_path`；`eval_data` 之类正常
  路径不误伤。本审查过程未读取、未枚举、未哈希任何 `eval/**` 内容。
- CLI：ValidationError 统一 `error:` + 退出码 2，成功 0，错误信息含定位（文件、
  行号、字段路径），适合自动化断言。

## 6. 剩余风险与后续建议

1. **自报数据的信任边界**（与 F-3/F-4 相关）：工具只能验证提交给它的文件内部
   自洽。"跑 N 次挑最好一次提交"、"事后重标 gold 再重算哈希"在纯离线校验下
   原理上不可检出；防线只能是外部锚点（git 提交冻结哈希）+ selection log 审计
   + 操作纪律。建议把这两步写成协议的硬性步骤（见 F-3/F-4 修复方向）。
2. **排除规则的主观性**：dependency-only/generated-only 等排除判断先于哈希排名，
   是比 seed 更宽的隐性挑选通道；selection log 契约落地前无法审计。
3. **`previously_used` / `author_is_benchmark_implementer` 为自我声明**：代码只
   能拒绝 `true`，声明真实性需人工核查（比对旧 eval、prompt、Week 3 pilot 使用
   过的仓库/PR 清单）。
4. **配置层面的挑选**：`validate_runs` 按 `--config-id` 过滤，同一文件可携带多
   配置。单次报告内无法挑选，但"跑多配置、看完结果再宣称其中之一是预注册主
   配置"仍需靠预注册纪律；建议主配置 ID 在 materialize 前写入 cohort 或计划
   文档。
5. **环境**：共享 venv 的 editable 安装当前指向 integration-week3 worktree，
   任何不设 `PYTHONPATH` 的验证都会测到别处的旧包（本次 verify.py 首跑失败即
   此因）。集成阶段建议在目标 worktree 重装 editable 或统一设置 `PYTHONPATH`，
   避免"验证通过/失败"归因错位。
6. **小样本声明**：文档已诚实说明 30 PR/3 仓的推断边界（bootstrap 不覆盖仓库
   选择不确定性、模型漂移、标注盲区），集成时建议保留这些限制表述，不要在
   README 汇总时弱化。

## 7. 审查合规声明

- 未读取、未枚举、未搜索、未哈希 `eval/**` 与 `eval/holdout/**`；
  `scripts\verify.py` 以默认模式运行（不含 `--eval-assets`，该模式不触碰评测
  资产）。
- 未联网、未下载数据、未安装依赖、未获取真实 PR。
- 未调用外部模型/agent/付费评测。
- 未修改 Codex 提交中的任何代码、schema、示例、测试与文档；本分支唯一改动为
  本报告文件。
- 未 push、未合并 master、未使用任何破坏性 git 命令。
