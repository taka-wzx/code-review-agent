# Week 5 可信 SWE-bench Repair 评测框架 — Claude 独立审查报告

## 1. 审查范围

- 原始 master 基线：`afb6e1fa85701d7b6af5b16198c9dd992740a03d`
- Codex 合同提交（先冻结合同与计划 JSON）：`d12169797eef8327d25f1ed0f5b27bc7a8f21727`
- Codex 实现提交：`a72ad9fc79b535276775d0ceeca5fc9b88bc012f`
- 交接提交（仅补充计划文档 delivery record）：`3bf3e60518a30842eb47383ee3209cf766522fc5`
- 审查分支：`claude/week5-swebench-repair-evaluation-review`（基于交接提交，工作区干净）
- 审查对象：交接 diff 的全部 16 个变更文件（AGENDA.md、README.md、
  docs/plans/week5-swebench-repair-evaluation.md、docs/swebench-repair-evaluation.md、
  swebench_repair_runner.py、swebench_repair_eval.py、
  swebench_repair/{cohort-plan,config-plan}.json、
  swebench_repair/schemas/{cohort,run-plan,runs}.schema.json、
  swebench_repair/examples/{synthetic-cohort.json,synthetic-run-plan.json,synthetic-runs.jsonl}、
  tests/test_swebench_repair_{runner,eval}.py），
  以及只读参照 AGENTS.md、docs/agent-contract.md、pyproject.toml、scripts/verify.py。
- 变更文件集合与任务合同「File ownership」声明的 Codex 所有权清单**完全一致**，
  无越权文件；pyproject.toml、CI、`src/code_review_agent/`、既有 `eval/**` 均未被触碰。
- 提交时序符合预注册纪律：合同与未物化计划（d121697）先于实现（a72ad9f）冻结。
- 审查方式：逐行阅读全部新增代码/测试/schema/文档 + 仅运行任务允许的验证命令。
  未构造额外探针脚本（本次发现的 F-1 可由纯代码路径推理确证，见下）。

## 2. 总体结论：**有条件通过**

离线仪器整体质量高：JSON 严格解析（重复键/NaN/未知字段拒绝）、选择 seed 与
rank 的确定性复算、run-plan 用"从不可变输入完整重生成再逐字节对比"方式校验、
120 行矩阵的唯一身份/单 run 覆盖/隔离证据/官方 evaluator 一致性全部 fail-closed，
且 67 个专项测试与全仓离线验证（470 测试、覆盖率 85% 门禁、ruff、mypy、双 CLI
冒烟）全部通过。文档如实声明"零真实任务、零可报告数字"。

条件：**F-1（P1）必须在 acquisition/materialize 之前修复**——当前选择日志校验
存在自相矛盾的角色约束，任何被分配角色的仓库只要含一条 ineligible 候选行，
`verify-selection` 就永久失败；真实 SWE-bench Verified 数据几乎必然触发，而唯一
离线不可检测的"绕过"恰恰是从日志里删掉 ineligible 行，这会反向破坏"记录每一个
候选及排除原因"的审计纪律。F-2～F-6（P2）应在冻结 attestation/编写真实执行
adapter 合同之前处理，否则部分指标分母与消融单因素性只依赖 adapter 自觉而非
可验证证据。

## 3. Findings（按严重度排序）

### F-1（P1，确定缺陷）选择日志角色校验自相矛盾：被分配仓库含 ineligible 行必然验证失败，并激励删行

- 文件：
  [swebench_repair_runner.py:747-750](../../swebench_repair_runner.py#L747-L750)
  （`_validate_selection_row`：`eligible=false` 的行**必须** `selected=false` 且
  `role=null`，否则拒绝）；
  [swebench_repair_runner.py:674-683](../../swebench_repair_runner.py#L674-L683)
  （交叉校验对**所有**行执行 `row["role"] != expected_role_by_repo.get(repo)` →
  被分配角色仓库的每一行都必须携带该仓库的 role）。
- 触发条件（最小复现）：物化后的选择日志中，任一被分配 reporting/tuning/
  development 的仓库含一条 `eligible=false` 的行（如 `exclusion_reason="flaky"`）。
  该行按 747-750 必须 `role=null`，又按 674-683 必须 `role=<仓库角色>`，两条约束
  互斥 → `validate_selection` 必然抛 `selection role mismatch`，连带
  `validate-plans`（materialized）与 `generate-run-plan` 全部失败。
- 真实性论证：SWE-bench Verified 单仓候选任务通常远多于 5 个，eligibility 筛查
  含 flaky/资源上限/镜像可得性等客观排除项；被选中仓库全部候选恰好 100% eligible
  的概率极低。现有测试掩盖了该缺陷——fixture
  [tests/test_swebench_repair_runner.py:56-97](../../tests/test_swebench_repair_runner.py#L56-L97)
  的所有候选行都是 `eligible=true`，且给未选中行也统一填了仓库 role。
- 一致性冲突：操作文档
  [docs/swebench-repair-evaluation.md:80-84](../../docs/swebench-repair-evaluation.md)
  说选择日志行携带 "assigned `role` or null"——按文档语义，未选中/未分配行填
  null；但代码要求被分配仓库的 eligible 未选中行（rank 第 6+ 名）也必须填仓库
  role，否则同样报 `selection role mismatch`。文档、代码、fixture 三方口径不一致。
- 影响：
  1. 物化阶段 fail-closed 死锁（方向安全，不直接污染 pass@1）；
  2. **审计激励扭曲**：离线校验器无法拿到 manifest 全集，从日志中静默删除被分配
     仓库的 ineligible 行是唯一能让校验通过、且离线不可检测的做法——这正是
     合同要求"每个候选与排除原因都必须入日志"想禁止的行为；
  3. 或者迫使 acquisition 阶段公开修改协议（合同允许，但代价高）。
- 建议修复（择一并同步文档/schema/fixture）：
  1. 674-683 的角色交叉校验只对 `eligible=true` 的行执行（ineligible 行保持
     `role=null`）；
  2. 或改为"role 只在 `selected=true` 时等于仓库角色，否则必须为 null"，并同步
     修改 fixture 中未选中行的 role。
- 建议回归测试：被分配仓库含一条 `flaky` 排除行的选择日志必须通过校验；
  未分配仓库（rank 第 7+ 名）的 eligible 行 `role=null/selected=false` 必须通过；
  被分配仓库的 ineligible 行若声称 `role=<仓库角色>` 必须被拒。

### F-2（P2，确定缺陷）`operations_total` 无下界交叉校验，非法操作事件率分母可被低报

- 文件：[swebench_repair_eval.py:515-537](../../swebench_repair_eval.py#L515-L537)。
  唯一约束是 `unauthorized <= operations_total`；`operations_total` 与
  `tool_calls`、`test_commands_total` 之间没有任何关系校验。
- 触发条件：一条 run 记录报 `tool_calls=150, test_commands_total=10,
  operations_total=1, unauthorized_operations=1`——通过全部校验，事件率
  1/1=100%；反向地，`operations_total=1, unauthorized_operations=0` 也通过，
  把分母做小或做大都不会被拒。
- 影响：合同定义"unauthorized-operation event rate = rejected policy events /
  all attempted tool/command operations"
  （[docs/plans/week5-swebench-repair-evaluation.md:366-367](../../docs/plans/week5-swebench-repair-evaluation.md)）
  的分母完全自报且不受约束，事件率可被任意方向扭曲；这与"分子、分母、预算校验
  准确"的验收标准不符。
- 建议修复：至少强制 `operations_total >= tool_calls + test_commands_total`，
  并在文档里写明 `operations_total` 的构成口径（工具调用 + 测试命令 + 其他
  何种操作）。
- 建议回归测试：`operations_total < tool_calls` 的记录必须被拒。

### F-3（P2，合同-代码语义矛盾 + adapter 陷阱）任何 `unauthorized>0` 强制终态 `policy_violation`

- 文件：[swebench_repair_eval.py:538-546](../../swebench_repair_eval.py#L538-L546)。
  `unauthorized_operations > 0` ⇒ `status` 必须恰为 `policy_violation`（且
  unresolved）；反之 `policy_violation` 必须有事件。
- 矛盾点：Week 3 的 preflight/审批模型允许"单次操作被拒绝，run 继续执行"；合同
  也据此定义了**事件率**（每 run 可有多次被拒事件）与**任务率**两个指标。但按
  本校验，一条被拒事件的 run 永远不能以 `completed`/`timeout`/`budget_exhausted`
  等真实终态入档——adapter 只能把"拒绝过一次但正常跑完并通过官方 evaluator"的
  run 谎报为 `policy_violation` 终态。resolved=false 的结论与合同一致（pass@1
  不受污染），但 `status_counts`、hard-failure/timeout 归因统计被系统性失真，
  且 adapter 若如实记录 `completed` 会导致整个 cohort 验证失败。
- 建议修复：二选一并写入合同——
  1. 保留严格语义：明确规定运行时任何一次策略拒绝立即终止 run（则 Week 3
     "拒绝后继续"路径必须在 adapter 中禁用，事件率指标意义弱化）；
  2. 放宽校验：允许 `unauthorized>0` 与任意终态共存，仅保留
     "`unauthorized>0` ⇒ `resolved=false`"（safely_resolved 已隐含）与
     "`policy_violation` ⇒ `unauthorized>0`"。
- 建议回归测试：按选定语义，覆盖"被拒一次 + completed + evaluator 通过 +
  resolved=false"这一组合（接受或拒绝，二者必居其一且有测试锁定）。

### F-4（P2，未来真实执行风险）预算上限用整档拒绝而非"允许超限终态"，激励对资源指标截断

- 文件：[swebench_repair_eval.py:520-529](../../swebench_repair_eval.py#L520-L529)
  （cost/tokens/latency/tool_calls/test_total 超预算即拒收整份 runs.jsonl）；
  [swebench_repair_eval.py:485-489](../../swebench_repair_eval.py#L485-L489)
  （latency 与时间戳差值容差 ±999ms）。
- 触发条件：真实执行中 `timeout`/`budget_exhausted` 终态的**观测值**经常略超
  上限（进程 kill 延迟、计费尾差、最后一条命令的输出计量）。例如 wall 时间
  3,601,200ms 的 timeout run：如实记录 → 全 cohort 验证失败；唯一出路是把
  latency clamp 到 3,600,000 并同步伪造 completed_at——即系统性向下截断 p95
  时延/成本分布。失败 run 本身必须留在分母（合同硬性要求），所以"丢弃该 run"
  不是选项。
- 影响：不污染 pass@1 分母，但污染资源指标的尾部（p95/max），并把"如实记录"
  与"能通过校验"置于对立。
- 建议修复：为 `timeout`/`budget_exhausted` 终态定义明确语义——允许观测值超限
  （如上限 +5% 宽限带）或规定 clamp 语义并新增 `clamped: true` 证据字段；写入
  合同与 adapter 规范。
- 建议回归测试：timeout 终态 latency=3,600,500ms 的记录按选定语义被接受
  （或带 clamp 标记被接受）。

### F-5（P2，确定缺口）run 记录缺少观测的 repair 尝试计数，`no_reflection` 消融单因素性无证据可验

- 文件：run-plan 侧
  [swebench_repair_runner.py:1213-1218](../../swebench_repair_runner.py#L1213-L1218)
  把 `no_reflection` 的 `repair_attempts` 预算置 0；但 runs 记录
  （[swebench_repair/schemas/runs.schema.json](../../swebench_repair/schemas/runs.schema.json)
  与 [swebench_repair_eval.py:405-435](../../swebench_repair_eval.py#L405-L435)
  的 `_exact_keys` 键集）**没有任何观测的 repair-attempt 计数字段**；同理
  `command_seconds`（600s/命令）与 `command_output_bytes`（1MiB/命令）预算也无
  对应观测计数器。
- 影响：一个实际执行了 2 次 reflection 的 "no_reflection" run 在证据层面不可
  检测（只能靠 tool_calls 间接怀疑）；消融矩阵"真正单因素"的关键差异变量恰好
  是唯一没有 fail-closed 证据绑定的变量。per-command 预算同样只能靠 adapter
  自觉。
- 建议修复：runs schema 增加 `repair_attempts_used`（整数，校验
  `<= budget.repair_attempts`，`no_reflection` 行即强制 0）；可选增加
  `max_command_seconds_observed`/`max_command_output_bytes_observed`。
- 建议回归测试：`no_reflection` 记录 `repair_attempts_used=1` 必须被拒。

### F-6（P2，信任边界）`size_band` 是无客观定义的自由输入，却参与 allocatable 判定，可静默改变 reporting 仓库集合

- 文件：[swebench_repair_runner.py:647-656](../../swebench_repair_runner.py#L647-L656)
  （首 5 个 task rank 覆盖 <2 个 size band 的仓库整体不可分配）；band 值本身仅做
  枚举校验（[swebench_repair_runner.py:755-757](../../swebench_repair_runner.py#L755-L757)）。
- 触发条件：数据控制者在生成选择日志时把某仓库前 5 任务的 band 全部标成
  `small`，该仓库即被挤出六仓分配序列，后续仓库依 rank 顺次顶替——**在 seed、
  rank、hash 全部合法复算通过的前提下**改变了 reporting/tuning/development 的
  仓库构成。`eligible` 布尔有同样性质，但合同至少为其规定了固定的排除原因枚举
  与"两次 acquisition-only 可复现性检查"；而 band 的推导公式在合同/文档中完全
  未定义（plan 只写 "coarse size-band quotas"），离线校验器与外部审计者都没有
  可复核的客观基准。
- 影响：选择的"outcome-blind 确定性"退化为"给定日志内容的确定性"；band 是当前
  唯一既影响选择结果、又没有任何预注册推导规则的输入。
- 建议修复：在 acquisition 合同中预注册 band 的客观推导公式（例如按官方
  manifest 中 patch/测试规模的固定分位数切分），并把公式与输入字段写入
  cohort.dataset 绑定，供审计复算。
- 建议回归测试：物化后由 manifest 字段复算 band 并与日志逐行对比（acquisition
  阶段脚本）。

### F-7（P3，缺失校验）并发上限与 container-hour 上限离线不可审计

- 文件：[swebench_repair_eval.py:466-489](../../swebench_repair_eval.py#L466-L489)
  校验了单条 run 的时间戳次序，但没有跨记录的区间重叠检查；runs 记录亦无 judge
  容器时长字段。
- 影响：`maximum_parallel_runs=2`（冻结策略）被违反时证据完全合法；120
  container-hour 上限（含 judge 时间）无法从证据复核。并发超限会经资源竞争污染
  时延指标。
- 建议修复：eval 侧按 `started_at`/`completed_at` 做扫描线检查，任一时刻在途
  run > 2 即拒绝；runs 记录增加 judge 起止时间戳。

### F-8（P3，未来真实执行风险）trace/checkpoint/evaluator-output 全局唯一性可能误伤合法重复

- 文件：[swebench_repair_eval.py:603-613](../../swebench_repair_eval.py#L603-L613)。
- 触发条件：两个配置在同一任务上产出**字节相同的 patch**（同模型、简单单行修复
  时常见），若 adapter 对 evaluator 输出做了去时间戳的规范化，两次官方评测输出
  字节可能相同 → 合法 cohort 被拒。同理两条"worktree 创建前即失败"的 run 若
  trace 序列化不含 run 身份，空 trace 哈希相同 → 被拒。
- 影响：fail-closed 方向（不污染指标），但会在真实执行中造成不可恢复的整档
  失败——而重跑又被"不得静默重试"禁止。
- 建议修复：在 adapter 规范中强制 trace/checkpoint/evaluator 输出嵌入
  `run_id`（使哈希天然唯一），并在文档记录该前提。

### F-9（P3，部分实现的合同承诺）状态与计数器缺少语义交叉校验

- 文件：[swebench_repair_eval.py:457-546](../../swebench_repair_eval.py#L457-L546)。
- 现状：`status="test_failure"` 但 `test_commands_failed=0` 通过；`cost>0` 而
  `tokens_total=0` 通过；`model_failure` 但 `tool_calls=150` 通过。合同声称
  "internally inconsistent telemetry is rejected"
  （[docs/plans/week5-swebench-repair-evaluation.md:373-374](../../docs/plans/week5-swebench-repair-evaluation.md)），
  当前只实现了其中的预算/时间戳/evaluator/隔离子集。
- 建议修复：按状态枚举补一张"状态 ⇒ 计数器不变量"表（至少
  `test_failure ⇒ test_commands_failed>0`），或在文档中明确收窄该承诺的范围。

### F-10（P3，schema/文档与代码分歧）

- run-plan schema 的 `task_branch` 模式 `^repair/[a-z0-9-]+$`
  （[swebench_repair/schemas/run-plan.schema.json:259-262](../../swebench_repair/schemas/run-plan.schema.json)）
  严于 Python 校验（仅 `startswith("repair/")` 且不含 `..`，
  [swebench_repair_runner.py:1332-1334](../../swebench_repair_runner.py#L1332-L1334)）；
  schema 的 `repository` 模式允许尾随 `-`/`.` 且无长度上限，宽于 Python 的
  `REPOSITORY` 正则。文档已声明 Python 为规范实现，故仅为互操作口径漂移。
- 计划文档写分支名为 `repair/<instance>-<run-id>`
  （[docs/plans/week5-swebench-repair-evaluation.md:292-293](../../docs/plans/week5-swebench-repair-evaluation.md)），
  实际为 `repair/<slug≤32>-<identity前12位>`
  （[swebench_repair_runner.py:1235-1238](../../swebench_repair_runner.py#L1235-L1238)）。
- 计划文档称 "salted path identity hash"
  （[docs/plans/week5-swebench-repair-evaluation.md:305-306](../../docs/plans/week5-swebench-repair-evaluation.md)），
  实现是**无盐**的域分隔确定性 token
  `SHA256(PATH_DOMAIN + identity)`（[swebench_repair_runner.py:1240-1242](../../swebench_repair_runner.py#L1240-L1242)）。
  它不以宿主路径为输入，因此无路径泄漏（这点是好的），但"salted"措辞失准，
  且它证明的是"计划了一个路径 token"而非"观测到的 worktree 路径的承诺值"。
  建议统一措辞，或在 runs 记录中增加真正的 salted 观测路径承诺。

### F-11（P3，可复现性）Bootstrap 跨 Python 版本确定性依赖 `random.choice` 的实现细节

- 文件：[swebench_repair_eval.py:749-763](../../swebench_repair_eval.py#L749-L763)。
- Python 只对 `Random.random()` 的跨版本序列做官方保证；`choice`/`_randbelow`
  的映射自 3.2 以来稳定但未被承诺。报告记录了 method/seed/replicates，未记录
  Python 版本与 RNG 实现。若未来解释器改变映射，同 seed 复算得到不同 CI 而无
  任何告警。
- 建议修复：报告中记录 `sys.version_info` 与 RNG 实现标识；或改用自实现的
  确定性整数流（如对 `sha256(seed||counter)` 取模的拒绝采样）。

### F-12（P3，防御性缺口）路径防护的 Windows 别名情形未锁定；bootstrap 早退分支存在不可达 KeyError

- [swebench_repair_runner.py:122-133](../../swebench_repair_runner.py#L122-L133)
  的 `safe_artifact_path` 依赖 `Path.resolve()` 归一化。对已存在目录，Windows
  尾点（`eval.`）与 8.3 短名（`EVAL~1`）通常会被 realpath 归一化为真名而被
  拦截，但该行为没有测试锁定，属于平台相关的假设（本次按只读纪律未做实机
  探针）。casefold 已覆盖大小写别名；符号链接经 resolve 覆盖。
- [swebench_repair_eval.py:740-748](../../swebench_repair_eval.py#L740-L748)
  少于 2 个任务时返回空 `paired_pass_at_1_delta`，而
  [swebench_repair_eval.py:853-856](../../swebench_repair_eval.py#L853-L856)
  无条件按 config_id 取下标 → KeyError。密封流程中不可达（120 行矩阵恒有 20 个
  primary 任务），但 `build_report` 是公开函数，建议改为显式抛
  `EvaluationValidationError` 或返回带 reason 的 null 区间。
- 建议回归测试：`eval.`/大小写混合路径的拒绝测试（在临时目录构造，不触碰真实
  `eval/`）；`build_report` 对退化输入的行为测试。

### F-13（P3，测试覆盖缺口清单）

以下高风险路径当前无测试（其中第 1 条掩盖了 F-1）：

1. 选择日志含 ineligible 行（任意仓库）；
2. 存在 rank 第 7+ 名未分配仓库的日志（`role=null` 全量行）；
3. `swebench_repair_eval.py` 的 CLI `main()`（validate-runs/report/--out 路径
   防护，`_write_json` 的 eval/holdout 拒绝在 eval 侧无直接测试）；
4. `checkpoint_sha256` 重复（trace 与 evaluator output 有测试，checkpoint 无）；
5. `worktree_created=true` 且 `container_started=false` 的中途失败形态
   （`writable_mounts=0`、cleanup=removed）；
6. 预算恰好等于上限的边界值（cost=500000、latency=3600000、tool_calls=150
   应通过）；
7. run plan `validate_run_plan` 对独立构造（非再生成）合法计划的接受路径。

## 4. 已重点检查、未发现问题的高风险路径

对应任务书的必查清单，以下项目经逐行审查与测试运行确认无缺陷：

1. **确定性与 outcome-blind 选择**：seed 严格由 base commit 派生并复算
   （runner:212-226、397-399，测试锁定具体值）；repo/task rank 由 seed 复算，
   任何篡改 rank/selected/role/字节哈希均被拒（测试覆盖）；选择日志行序无关
   （测试覆盖）；forbidden 仓库必须显式 `forbidden_repository` 记录，静默丢弃
   或混入物化 cohort 均被拒。事后换任务被"cohort tasks 集合 == 确定性 selected
   集合"（runner:685-689）钉死。
2. **仓库隔离与 4 仓×5 任务**：validate_cohort 强制 30 任务、5/5/20 角色、仓库
   角色不相交、reporting ≥4 仓且每仓 ≥3；validate_selection 进一步把 reporting
   钉死为 rank 前 4 仓各取前 5 任务，tuning/development 各 1 仓 5 任务——两层
   合取后不存在 6+6+5+3 之类的松弛解。Week 3/4 四个仓库在 forbidden 列表中。
3. **唯一身份**：120 行 run/branch/worktree/path-token/container/judge/state 全部
   由域分隔 SHA-256（96-bit 截断）派生，计划与 runs 两侧都做唯一性断言；
   task/config 对唯一；run plan 不含宿主绝对路径（测试断言）。
4. **fail-closed 隔离证据**：network≠none、root、cap 未 drop、可写挂载≠预期、
   原 checkout 改变、worktree 创建后缺 removed/quarantined 清理证据、quarantine
   与状态不一致、judge 先于 Agent 容器（布尔层面）均被拒；worktree 未创建时
   仅允许 hard/sandbox failure 且必须零活动、`cleanup=not_created`，并保留分母。
5. **evaluator 一致性**：`official_resolved` 必须等于
   `exit_code==0 ∧ F2P 全过 ∧ P2P 全过`；总数必须等于冻结计划的官方计数；
   未尝试则一切结果字段必须为 null/0/false；`resolved` 是九条件合取的重算值，
   任何矛盾（含"官方通过但清理未证明"的 quarantine 情形）都被拒，测试覆盖。
6. **分母纪律**：记录数必须恰等于 120 且 run_id 集合恰等于计划集合——漏跑、
   重复、替换（不同 run_id）皆不可能；`validate-runs`/`report` 前置同一套校验，
   报告 primary 分母恒为 20（测试锁定 10/20/0.5）。
7. **统计口径**：所有率带整数分子/分母，零分母输出 null（测试覆盖）；成本为
   整数 micro-USD，非有限数在 JSON 层即被拒；报告绑定 cohort/config/run-plan
   规范哈希与 selection/runs 精确字节哈希，`created_at` 不得早于最晚
   `recorded_at`（测试覆盖）。
8. **Bootstrap**：按 reporting 仓库分层、按任务重采样、六配置共享同一抽样
   （配对成立）、固定 seed 确定且行序无关（均有测试）；CLI 报告路径不暴露
   replicates 降级参数，10000 次下限由 config 校验强制。
9. **消融矩阵**：六配置与 primary 的单因素差异由 `CONFIGURATION_SPECS` 白名单
   逐字段强制，预注册顺序强制，模型 A/B 必须绑定不同 (provider, model)；
   `models_frozen=false` 时槽位必须全 null 且拒绝生成 run plan——不可能编造
   模型身份。`no_reflection` 同时把 retry 预算置 0 是合同预注册的联动，不构成
   第二因素。
10. **JSON 信任边界**：重复键、NaN/Infinity、未知键、非规范时间戳（含闰秒/
    非法日期/非 Z 时区）、控制字符、非小写 hex、非规范 repo 名均被拒；
    eval/holdout 路径拒绝发生在 `resolve()` 之后、任何 read/write 之前，
    大小写与符号链接情形已覆盖（Windows 别名残余见 F-12）。
11. **诚实性**：README/AGENDA/两份文档一致声明零下载、零 Docker、零模型调用、
    零可报告数字；合成示例明确标注不构成基准证据且确实无法通过 120 行交叉
    校验；全部新增文件中未发现凭据、真实 SWE-bench 内容、gold patch 或结果
    声明。
12. **缺少真实执行 adapter 是否阻塞发布**：不阻塞本周"评测仪器"交付（文档已
    如实定位），但阻塞任何真实 benchmark 结论；F-3/F-4/F-5/F-8 应作为 adapter
    合同的前置输入解决，否则 adapter 将在真实执行中被迫在"如实记录"与"通过
    校验"之间二选一。

## 5. 验证命令与结果

均在本 worktree 以共享 venv 解释器执行，`PYTHONPATH` 显式固定为
`<本worktree>\src`：

| 命令 | 结果 |
| --- | --- |
| `python -m unittest tests.test_swebench_repair_runner tests.test_swebench_repair_eval -v` | **67 个测试全部通过**（2.07s） |
| `python -m ruff check swebench_repair_runner.py swebench_repair_eval.py tests\test_swebench_repair_runner.py tests\test_swebench_repair_eval.py` | 通过（exit 0） |
| `python -m mypy swebench_repair_runner.py swebench_repair_eval.py` | 通过：2 个源文件无问题 |
| `python scripts\verify.py`（无 `--eval-assets`） | **全部通过**：Ruff 通过；470 测试通过、3 个环境跳过；覆盖率 85%（达 `fail_under=85`）；mypy src 21 文件通过；`python -m code_review_agent --help` 与 `crag --help` 冒烟通过 |
| `python -B swebench_repair_runner.py validate-plans --cohort swebench_repair\cohort-plan.json --config swebench_repair\config-plan.json` | `valid:true, materialized:false, selected_tasks:0, configurations:6, planned_reporting_attempts:120, models_frozen:false`，与文档预期完全一致 |
| `git diff --check afb6e1f… 3bf3e60…` | 无空白错误（exit 0） |
| `git diff --name-status afb6e1f… 3bf3e60…` | 16 个文件，全部在 Codex 所有权清单内（见第 1 节） |

与 Codex delivery record 声称的验证结果**逐项吻合**（67 专项测试、470 全量
测试、85% 覆盖率、ruff/mypy 通过、unmaterialized 校验事实）。

## 6. 未运行项目及原因

- `scripts/verify.py --eval-assets`、`eval/check_consistency.py`：合同禁止读取
  既有 `eval/**`；
- `run_eval.py` / `judge.py` / `repeat_eval.py` / `replay_verifier.py` /
  `bench_verifier.py`：消耗 LLM 配额，未获授权；
- 任何 Docker、网络、SWE-bench 下载、依赖安装、外部模型调用：任务书严禁；
- `generate-run-plan` / `verify-selection` / `validate-runs` / `report` 的真实
  数据路径：无物化数据（by design），其行为经 67 个单测的合成数据路径验证；
- F-12 所述 Windows 路径别名实机探针：任务书只允许列名的验证命令，故以静态
  分析代替并降级为 P3 测试建议。

## 7. Remaining risks

1. **自报证据边界**（合同已承认，重申量化）：eligible 布尔、size band（F-6）、
   全部计数器、容器安全布尔、时间戳均由未来 adapter/数据控制者自报；离线校验
   只能证明"内部一致 + 与冻结计划绑定"，不能证明与现实一致。冻结先于运行的
   时序、以及"整个 120 矩阵密跑两遍挑一遍提交"的矩阵级 run-shopping，都只有
   预注册的外部审计者可以排除。
2. **选择日志完整性不可离线证明**：校验器拿不到 manifest 全集，无法发现日志
   整行缺失（与 F-1 的删行激励叠加时危害最大）。acquisition 阶段应把"日志行数
   == manifest 任务数"写入审计清单。
3. **judge 容器证据薄弱**：仅有名称与 started 布尔，无 judge 侧网络/挂载/时长
   证据（F-7/F-8 相关）；"evaluator 在 patch 冻结后运行"目前只由哈希绑定与
   布尔次序近似表达。
4. **统计功效**：20 任务/4 仓的 CI 会很宽（合同已声明）；分层 bootstrap 在
   每仓 n=5 下对 p95 类指标的区间解释力有限，发布时应突出区间而非点估计。
5. **共享 venv 的 editable 安装**：验证依赖显式 PYTHONPATH 纪律，跑错 worktree
   的静默风险仍在（本次已按规程固定）。

## 8. 合规确认

- 未联网、未下载任何数据集、未安装依赖；
- 未调用任何外部模型/agent/付费服务；
- 未启动 Docker、未运行任何真实或大规模评测；
- 未读取、枚举、搜索、哈希或验证既有 `eval/**`、`eval/holdout/**`（ruff/verify
  按其自身配置排除 eval；本审查未直接打开任何该目录文件）；
- 未运行 `--eval-assets`、`check_consistency`、`run_eval`、`judge`、
  `repeat_eval`、`replay_verifier`、`bench_verifier`；
- 未修改任何实现/测试/schema/计划/README/AGENDA/AGENTS/pyproject/master——
  本分支唯一写入路径为 `docs/reviews/week5-claude.md`；
- 未 push、未合并 master、未改变父提交与 Codex 交接提交。
