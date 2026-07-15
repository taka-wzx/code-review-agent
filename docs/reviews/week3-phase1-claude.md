# Week 3 Phase 1 独立安全与正确性审查（Claude Code）

- 审查分支：`claude/week3-phase1-review`
- 审查对象：Codex Phase 1 handoff `c3cc08b6f301504ddd978aaef623a9b9b977f8fc`
  （相对起点 `0f437209ff80db4eee82e470cfef93fb92fd3c1a`）
- 审查范围：`repair_state.py`、`repair_budget.py`、`repair_approval.py`、
  `repair_checkpoint.py` 及 `tests/test_week3_state.py`、`tests/test_week3_recovery.py`
- 审查方法：逐行通读四个模块与全部测试，对照
  `docs/plans/week3-review-repair-agent.md` 的状态机/预算/批准/checkpoint 契约条款，
  针对状态越权、预算绕过、批准重放、路径范围扩大、恢复不安全、损坏快照接受、
  并发记录损坏与原仓库污染八类威胁逐项推演；确认的缺陷在授权路径内修复并补最小回归测试。
- 结论：**发现并修复 6 组真实缺陷（详见下文），其余检查项确认无问题**；
  Phase 1 组件在修复后满足契约的 fail-closed 要求。单进程锁问题**不构成 Phase 1 阻断**
  （见"不修复项" R1），但列为 Phase 4 resume 落地的前置条件。

## 一、已修复缺陷

### F1（高）repair_state.py — SUBMIT 无法 fail closed

- 位置：`_ALLOWED_TRANSITIONS[RepairState.SUBMIT]`（原为 `{WAIT_APPROVAL}`）。
- 证据：契约要求 SUBMIT 阶段 revalidate status/diff/budget/approval，且
  "An unsafe condition … must fail closed"、CANCELLED 为通用终态。原实现里 SUBMIT
  的唯一合法出口是 WAIT_APPROVAL：revalidation 发现原 checkout 被改动、预算耗尽或
  用户取消时，要么被迫回 WAIT_APPROVAL（语义错误：这不是"commit 命令失败"），
  要么诱导上层绕过转换校验强改状态（契约明令禁止 coerce）。
- 修复：SUBMIT 出口改为 `{WAIT_APPROVAL, FAILED, CANCELLED}`。
- 回归测试：`test_submit_can_still_fail_closed_or_cancel`、
  `test_every_non_terminal_state_can_fail_closed_and_cancel`（不变量：所有非终态
  必须能到达 FAILED 与 CANCELLED）。

### F2（高）repair_state.py — 恢复时可伪造非法历史路径

- 位置：`RepairStateMachine.__post_init__`。
- 证据：原实现只校验 `history[-1] == state`，恢复/重建时可构造任意非法路径；
  原测试 `test_submit_can_return_for_new_commit_approval` 自己就构造了非法历史
  `[DISCOVER, SUBMIT]`（DISCOVER→SUBMIT 不是合法转换）且被接受——即"审计记录"
  可以声称走过一条从未被校验过的路径。
- 修复：构造时强制（1）history 首元素为 DISCOVER；（2）逐对相邻转换合法
  （复用 `validate_transition`，非法即抛 `IllegalTransitionError`）；（3）末元素
  等于当前 state。另将 state/history 元素强制转换为 `RepairState` 成员，未知值抛
  `ValueError` 而非裸 `KeyError`（`validate_transition`/`allowed_targets` 同样收紧）。
- 测试同步：原测试改为提供完整合法历史（该用例验证的"SUBMIT 失败可回
  WAIT_APPROVAL"语义保持不变）；新增
  `test_restored_history_cannot_encode_an_illegal_path`、
  `test_restored_history_must_start_at_discover`、
  `test_unknown_state_values_are_rejected`。
- 说明：这要求 Phase 4 resume 用 journal 重建完整合法历史（或显式走 DISCOVER
  重入流程），而不是拼一个两元素历史，这正是契约"恢复不得伪造"的方向。

### F3（高）repair_approval.py — writable path 校验存在多个绕过

- 位置：原 `_normalize_paths`。
- 证据（逐项）：
  1. 冒号只检查首段：`src/mod.py:stream`（NTFS 备用数据流）与后段盘符通过校验；
     Windows 上写 ADS 可在已批准文件上隐藏内容。
  2. `.git` 只检查首段：`vendor/.git/hooks/pre-commit` 通过校验——worktree 内嵌套
     git 目录可被注入 hook/config。
  3. Windows 尾点/尾空格别名：`src/mod.py.` 打开的是 `src/mod.py`——批准的字符串
     与实际写入的文件不一致，构成路径范围扩大。
  4. 设备名：`nul`、`src/CON.txt` 在 Windows 上寻址设备而非文件。
  5. 字符串冒充列表只在 `from_dict` 拦截：直接构造
     `ApprovalBinding(writable_paths="ab")` 会把字符串逐字符拆成路径 `("a","b")`。
  6. 大小写重复：`("src/App.py","src/app.py")` 在 Windows 上是同一文件，
     原 uniqueness 检查放行。
- 修复：新增组件级校验 `_validate_path_part`（冒号任意位置、尾点/尾空格、任意深度
  `.git`、Win32 保留设备名 stem），公共函数 `normalize_repo_paths` 拒绝
  str/bytes 冒充序列并做 casefold 唯一性检查；"至少一个路径"检查移入 WRITE 分支，
  语义不变。
- 回归测试：`test_unsafe_write_paths_are_never_bindable` 扩展 7 个新向量（含 UNC
  `//server/share`）、`test_case_aliased_duplicate_paths_are_rejected`、
  `test_directly_constructed_string_path_list_is_rejected`。

### F4（高）repair_checkpoint.py — run_id 尾点别名可跨 run 覆盖快照；设备名 run_id

- 位置：`_RUN_ID` 正则与 `CheckpointStore._validate_run_id` / `RepairCheckpoint.__post_init__`。
- 证据：原正则 `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` 允许尾点。Windows 会剥离尾点，
  `run.` 与 `run` 落到同一目录：`load("run.")` 会被 run_id 一致性检查拦下，但
  `save()` 没有等价检查——run_id 为 `run.` 的 checkpoint 会**直接覆盖** run `run`
  的快照与 journal（跨 run 记录污染）。`nul`/`CON`/`com1.log` 等设备名 run_id 则
  寻址设备路径。
- 修复：正则收紧为不允许尾点；新增模块级 `_require_valid_run_id`（含保留设备名
  stem 拒绝），dataclass 与 store 共用同一校验（原先两处校验行为不一致：dataclass
  对非 str run_id 会抛 `TypeError` 而非 `ValueError`）。
- 回归测试：`test_windows_aliasing_run_ids_are_rejected`。

### F5（中）repair_checkpoint.py — 快照 writable_paths 完全不校验

- 位置：`RepairCheckpoint`（`_paths` 只检查"字符串列表"）。
- 证据：载入的 checkpoint 可携带 `../escape.py`、`.git/config`、ADS 冒号等路径且
  被接受；该字段正是未来 resume/rollback 的路径范围来源，属于"损坏快照被接受→
  路径范围扩大"链条的第一环。
- 修复：`__post_init__` 用与批准完全相同的 `normalize_repo_paths` 规范化校验
  （允许空元组；非序列类型显式拒绝）。`from_dict` 路径上违规载荷统一表现为
  `CheckpointCorrupt`。
- 回归测试：`test_checkpoint_writable_paths_cannot_escape_the_worktree`。
- 附带：`state` 字段在直接构造时也强制转换为 `RepairState` 成员（原先传入字符串
  会在 `to_dict` 时才 `AttributeError`）。

### F6（中）repair_budget.py — 截断快照静默重置预算；数值字段接受字符串

- 位置：`BudgetManager.from_dict`、`_positive_finite`/`_nonnegative_finite`。
- 证据（两点）：
  1. `BudgetLimits(**data["limits"])` / `BudgetUsage(**data["usage"])` 对**缺失键**
    静默取 dataclass 默认值：删掉 `usage.tool_calls` 键即把计数重置为 0，删掉
    `limits.total_tokens` 即把限额放宽回默认——构成预算绕过（恢复路径上的
    fail-open）。
  2. 数值校验用 `float(value)` 探测，可解析字符串（如 `"1800"`、`"0.0"`）通过校验
    后**以字符串形式驻留**在账本里，直到深处算术/比较才 `TypeError`，而不是在恢复
    边界干净拒绝。
- 修复：新增 `_complete_section`（快照 limits/usage 必须携带全部字段，缺失即
  `invalid budget snapshot`；`LLMReservation` 无默认值天然覆盖）；新增
  `_real_number` 类型闸（bool 与非 int/float 一律拒绝）。
- 回归测试：`test_truncated_snapshot_sections_cannot_reset_budget`、
  `test_snapshot_with_non_numeric_values_cannot_restore`。

## 二、确认无问题的检查项

按任务检查单逐项核对，以下为审查通过、无需改动的结论：

- **状态机**：合法转换表与契约工作流一致（DISCOVER→PLAN→PATCH→TEST→REFLECT→
  {PATCH|WAIT_APPROVAL}→SUBMIT）；非法转换抛 typed `IllegalTransitionError` 且
  state/history 不变（测试覆盖）；FAILED/CANCELLED 出度为空、真正终止。
- **预算-并发**：`reserve_llm` 在锁内"先检后订"，token 与 cost 均计入
  已用+已预留+新预留，双线程并发只有一单成功（测试覆盖）；elapsed/tool-call/
  repair-attempt 均先检查后修改，超限时计数不变（测试覆盖）。
- **预算-对账**：实际用量超预留时真实消耗被保留、`_accounting_failure` 置位后
  **永久 fail closed**（含序列化恢复后，测试覆盖）；未知/重复 reconcile、重复
  cancel 抛 `BudgetAccountingError`；重复 reservation id 在构造时拒绝；崩溃时未
  结算 reservation 随快照恢复并继续占用预算，可显式 cancel 释放（测试覆盖）；
  恢复时用量超限在构造期即抛 `BudgetExceeded`（fail closed）。
- **批准**：WRITE 绑定 run/checkpoint/base/diff/plan/paths/单次 patch_attempt，
  COMMIT 绑定 run/checkpoint/base/final diff/test hash/commit message；两类字段
  互斥严格校验；过期（含 `>=` 边界）、重放（已消费）、错 nonce/diff/checkpoint
  一律拒绝；`consume` 返回新记录、序列化往返保持 consumed 状态；NaN/Infinity
  时间戳在构造期拒绝（不存在永不过期批准）。
- **Checkpoint 原子性**：独占临时文件 + flush + fsync + `os.replace` + 目录
  fsync（尽力）；replace 失败保留上一份有效快照并清理临时文件（测试覆盖）；
  schema 版本、checksum（常数时间比较）、envelope 类型、payload 类型、NaN
  （`allow_nan=False` 双向拒绝）、非法 state、run-id traversal 全部 fail closed；
  `load` 校验快照 run_id 与目录一致。
- **Journal**：逐行 canonical JSON + fsync；截断/损坏行显式抛
  `CheckpointCorrupt`（含行号），不静默跳过（测试覆盖）；单进程并发追加 200 条
  记录完整（测试覆盖）。
- **原仓库污染**：Phase 1 组件不接触任何 git 仓库；checkpoint 状态根由调用方
  注入、独立于目标 repo；本审查未发现任何会写入原 checkout 的路径。
- **测试质量**：现有测试无删除、无弱化（唯一改动是 F2 所述用例的非法历史改为
  合法历史，其验证目标不变）；新增 11 个回归测试均针对修复的失败模式而非复述实现。

## 三、审查后不修复项（理由与去向）

- **R1 单进程锁、无跨进程互斥——不构成 Phase 1 阻断**。`CheckpointStore` 的
  RLock 与 `BudgetManager` 的 Lock 只覆盖线程。Phase 1 没有任何生产调用方，也就
  不存在第二个进程打开同一 state_root 的场景，因此不阻断本阶段验收。但两个进程
  并发 resume 同一 run-id 会交错写快照/journal（journal 追加在 Windows 上无
  O_APPEND 原子性保证），**必须在 Phase 4 resume CLI 落地前**加跨进程互斥
  （建议 run 目录内 O_EXCL lockfile 或 msvcrt/fcntl 文件锁），并把"锁被占用"
  视为 fail closed。
- **R2 批准消费的持久化时序**。`ApprovalRecord.consume` 是纯函数式的：调用方拿到
  consumed 副本后必须先写 checkpoint 再执行受保护操作，否则崩溃后 resume 读到的
  仍是未消费记录，存在重放窗口。这是契约"save before and after approval
  consumption"的 orchestrator 义务，数据结构层无法单方面强制。同理，"SUBMIT 失败
  回 WAIT_APPROVAL 不复用旧批准"成立的前提是 orchestrator 在发起 commit 命令**前**
  持久化 consumed 记录——Phase 4 必须按此顺序实现，并且消费时的 expected binding
  必须由运行时快照重建（不得像单元测试那样直接传 `record.binding` 自证）。
- **R3 checksum 是完整性校验而非真实性校验**。能改 checkpoint 文件的人可以重算
  sha256。契约只要求 checksum，防篡改需要密钥管理（HMAC），超出 Phase 1 范围；
  信任边界为本机文件系统权限。
- **R4 `save()` 不强制 sequence 单调**。陈旧写者可用旧 sequence 覆盖新快照。单
  进程单 orchestrator 下风险低，且契约未规定该不变量；建议与 R1 的跨进程锁一并
  处理（持锁读旧快照比对 sequence）。
- **R5 `assert_matches` 的 worktree 比较用 `normcase+abspath` 而非 `realpath`**，
  symlink 别名不检测；六元组字段（repository/base/branch/worktree/status/diff）
  已满足契约列举项。resume 侧应传入 realpath 归一后的路径。
- **R6 checkpoint/journal 内容无大小上限与 secret 扫描**。契约要求"不存储含密钥的
  prompt 与无界输出"，这是语义层约束，须由 orchestrator 在写入前执行（限幅、
  脱敏）；数据层已保证非 JSON 化数据（含 NaN）拒绝。
- **R7 budget limits 随快照恢复生效**。被改大的 limits 会被 `from_dict` 接受
  （checksum 见 R3）。Phase 4 resume 应把恢复出的 limits 与任务契约限额比对，
  不一致即隔离。
- **R8 Windows 8.3 短名（如 `GIT~1`）理论上可指代 `.git`**。属于文件系统解析层
  别名，需要 Phase 2 sandbox 的路径 resolve（realpath + 前缀确认)兜底，字符串层
  无法完备防御。

## 四、验证记录

解释器：`E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe`
（只读复用），`PYTHONPATH` 指向本 worktree `src/`。

| 命令 | 结果 |
| --- | --- |
| `python -m unittest tests.test_week3_state tests.test_week3_recovery -v` | 修复前基线 29 通过；修复后 **40 通过**（新增 11） |
| `python -m ruff check <四个 src 模块 + 两个测试文件>` | 通过 |
| `python -m mypy <四个 src 模块>` | 通过（4 files, no issues） |
| `python scripts\verify.py`（未传 `--eval-assets`） | 全部通过：ruff 全仓 / **230 tests OK** / 覆盖率 **93%**（门禁 85%）/ mypy / 双入口 `--help` 冒烟 |

未调用任何外部 LLM API，未运行付费评测，未读取 `eval/holdout`，未联网安装依赖。

## 五、所有权与偏差声明

- 改动文件严格限于授权清单：四个 repair 模块、两个 Week 3 测试文件、本报告。
- 接口偏差（均为授权模块内的**新增**，未触碰任何冻结接口）：
  - `repair_approval.py` 新增公共名 `normalize_repo_paths`、
    `WINDOWS_RESERVED_DEVICE_NAMES`；私有 `_normalize_paths` 并入公共函数
    （无外部引用，测试已同步）。
  - `repair_checkpoint.py` 新增对 `repair_approval` 的导入（单向，无环）。
  - `_ALLOWED_TRANSITIONS[SUBMIT]` 集合扩大（F1）；`RepairStateMachine` 构造
    校验收紧（F2）——需要 Codex 在 Phase 4 resume 中按 F2 说明重建历史。
  - 其余既有类型/函数名称、签名与语义保持不变。
