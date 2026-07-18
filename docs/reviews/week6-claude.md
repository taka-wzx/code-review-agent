# Week 6 Phase 2 安全与生产可观测性 — Claude 独立审查报告

## 1. 审查范围与方法

- 实现审查基线（Phase 2 授权提交）：`67ad660972bf433b7c6a900ba7cf2711d7bb2a14`
- 交接提交：`8d52f9bfb745a2729e8ebcba053be844fc1dfca7`
  （区间含 `ac50f78` "docs: complete week 6 telemetry profile"，即 A2 profile
  修正提交先于实现提交，时序符合合同）
- 审查分支：`claude/week6-security-observability-review`（HEAD 即交接提交，
  工作区起始干净）
- 审查对象：`67ad660...8d52f9b` 全部 19 个变更文件——
  `src/code_review_agent/{observability,redaction,tracelog}.py`（新增/重写）、
  `{agent,agentloop,orchestration,verifier,repair,repair_tools}.py`（埋点）、
  `tests/{test_observability,test_redaction}.py`（新增）、`tests/test_week3_repair.py`、
  `security_redteam/phase1-profile.json` 与其 schema、
  `docs/plans/week6-security-observability{,-phase1}.md`、
  `docs/security-observability.md`（新增）、`README.md`、`AGENDA.md`；
  只读参照 AGENTS.md、docs/agent-contract.md、pyproject.toml、
  `tools.py` / `repair_state.py` / `sandbox.py` 等未变更调用方、
  仓库根 `cost_report.py` / `bench_verifier.py` 等 legacy trace 消费方。
- 变更文件集合 ⊆ Phase 1 附件冻结的 Phase 2–3 Codex Single Writer 清单，
  **无越权文件**；pyproject.toml、CI、Dockerfile、`eval/**`、Week 3/4/5
  checkpoint/approval/sandbox/评测证据格式均未被触碰。
- 方法：逐行阅读全部新增模块与埋点 diff；对可疑行为编写独立复现脚本
  （置于会话 scratchpad，未写入仓库）逐项实证；运行任务允许的全部验证命令；
  按 Phase 1 附件的规范化算法独立复算 4 个冻结文件哈希。

## 2. 总体结论：**有条件通过**

可观测性核心质量高：ID/生命周期/时间/父子/环包络校验全面且 fail-closed，
脱敏在任何序列化、console、exporter 之前执行且有 validator 兜底扫描，
Repair 强制本地 audit sink 的初始化时序正确并有测试锁定，可选 exporter
首次失败即熔断且留下本地 degraded 证据、不触碰任何策略。冻结哈希、
profile 字段与运行时实现一致。全部验证命令通过（509 测试 / 3 跳过 /
覆盖率 86% / Ruff / mypy / 双入口冒烟）。

条件：F-1（P2，span 收尾校验失败会楔死 tracer、丢失 root 审计记录并在
Repair 异常路径顶替原始异常）与 F-2（P2，POSIX 绝对路径脱敏枚举不完整，
与冻结的 `absolute_host_path: forbidden` 条款偏差）应在 integration 处置
（修复或书面接受并记录理由）后合入。两者当前触发面都不在已交付的默认
埋点路径上，方向均为 fail-closed / 信息面收窄，故不构成拒绝理由。

## 3. 命令及实际结果

全部命令在本 worktree 内执行，解释器与 PYTHONPATH 显式固定：

```powershell
$wt = (Resolve-Path '.').Path
$env:PYTHONPATH = "$wt\src;$wt\tests"
$python = 'E:\shiyan\code_review_agent\.venv\Scripts\python.exe'
```

| 命令 | 实际结果 |
| --- | --- |
| `git branch --show-current` | `claude/week6-security-observability-review` |
| `git rev-parse HEAD` | `8d52f9bfb745a2729e8ebcba053be844fc1dfca7` |
| `git status --short --branch` | 干净（仅本报告为后续新增） |
| `git diff --check 67ad660...8d52f9b` | 通过（exit 0，无空白错误） |
| `git diff --name-status 67ad660...8d52f9b` | 19 个文件：4 A（observability.py、redaction.py、tests/test_observability.py、tests/test_redaction.py、docs/security-observability.md 中 5 项为 A，其余 M），清单见上节 |
| 4 个冻结文件 UTF-8/LF SHA-256 独立复算 | 与 Phase 1 附件表**逐一相符**（phase1-profile `8f0fd43a…`、case-plan `b5bc761d…`、profile-schema `da5d7229…`、case-plan-schema `a294dc48…`；case-plan 及其 schema 未变更） |
| `unittest tests.test_observability tests.test_redaction tests.test_week2_orchestration tests.test_golden tests.test_cli tests.test_week3_tools tests.test_week3_repair -v` | **Ran 180 tests in 24.3s — OK (skipped=1)** |
| `ruff check`（任务列出的 12 个路径） | **All checks passed!**（exit 0） |
| `mypy src/code_review_agent` | **Success: no issues found in 23 source files**（exit 0） |
| `python scripts\verify.py`（未加 `--eval-assets`） | **exit 0，"All offline validation passed."**；内含 `Ran 509 tests — OK (skipped=3)`，分支覆盖率 `TOTAL … 86%`（≥85 门禁），Ruff、mypy、`python -m code_review_agent --help` 与 `crag --help` 冒烟全部通过 |
| 复现脚本（scratchpad，6 个探针） | 6 项全部按下文 findings 所述复现 |

注：verify.py 曾另跑一次并用管道截取输出，因 `Select-Object -First` 提前关闭
管道产生虚假 exit 255；改为落盘重跑后 exit 0，与首次直跑一致。该伪影与被审
代码无关。

## 4. Findings（按严重度排序）

### P1

**none。** 未发现权限放宽、秘密/内容在默认路径泄漏、越权写入或冻结接口被
破坏的缺陷。

### P2

#### F-1 `Span.end()` 内部校验失败使 span 永久滞留：tracer 无法关闭、root 审计记录丢失，且在 Repair 异常路径顶替原始异常

- 文件：[observability.py:436-467](../../src/code_review_agent/observability.py#L436-L467)
  （`end()` 先置 `_end_time_ns` 再 `_snapshot()` 校验，校验抛异常时 span 已
  `ended=True` 但从未从 `_open` 注销、从未导出）；
  [observability.py:393-405](../../src/code_review_agent/observability.py#L393-L405)
  （属性满 56 个用户槽位后 `set_attributes` 静默逐出新 key——包括 `end()` 在
  error 分支写入的 `error.type` / `crag.error.category`）；
  [observability.py:716-721](../../src/code_review_agent/observability.py#L716-L721)
  （`close()` 见到未注销 span 即抛 `SpanLifecycleError`）；
  [repair.py:3388-3396](../../src/code_review_agent/repair.py#L3388-L3396)
  （异常 handler 中 `trace.close(...)` 的次生异常会替换原始异常，且使同
  handler 内的 `_assert_original_checkout_unchanged` 不再执行）。
- 复现（实测）：向一个 span 写满 56 个属性后
  `end(status="error", error_type="SyntheticFailure", …)` →
  `TelemetryValidationError: error span lacks error.type`；随后
  `tracer.close()` → `SpanLifecycleError: cannot close tracer with open child
  spans`。同类触发面还有：`llm.request` span 缺 `gen_ai.provider.name`
  （见 F-6，已实测）、墙钟回拨导致 `end < start`。
- 影响：(a) root record 永远不会写入本地 JSONL，`validate_trace` /
  `load_span_records` 对该文件必然失败——本地审计证据不完整，违背失败语义
  中"单条有界本地 fallback 记录"的意图；(b) 在 `_run_repair_contract` 的
  异常路径上，原始异常被 `SpanLifecycleError` 顶替，且该 handler 内的
  original-checkout 不变性断言被跳过（函数末尾 3423 行的断言在异常重抛后
  不可达）。方向是 fail-closed（不放宽任何权限），但破坏审计完整性与异常
  归因。
- 现状评估：默认埋点单 span 属性最多 ~15 个，provider 在 CLI 流中保证非空，
  故已交付路径不会触发；这是生命周期不变量缺陷而非现网缺陷。
- 建议：`end()` 的 error 分支绕过容量逐出直接写入 `error.type` /
  `crag.error.category`（或为其保留槽位）；`_snapshot()` 校验失败时把 span
  从 `_open` 注销并降级为一条有界的本地 fallback 记录；`trace.close()` 在
  repair 异常 handler 内 wrap try/except，保证 original-checkout 断言总被执行、
  原始异常不被顶替。

#### F-2 绝对路径脱敏为顶级目录枚举式，POSIX 常见根目录不在列，偏离冻结的 `absolute_host_path: forbidden`

- 文件：[redaction.py:106-110](../../src/code_review_agent/redaction.py#L106-L110)
  （`_ABSOLUTE_PATH` 仅匹配盘符、UNC 与
  `/Users|home|root|etc|var|tmp|mnt|opt|srv|proc|sys|dev`）；
  [redaction.py:349-367](../../src/code_review_agent/redaction.py#L349-L367)
  （validator 的 `contains_forbidden_content` 用同一正则——同源同盲区，
  没有第二道防线）。
- 复现（实测）：`/usr/lib/python3.12/site-packages`、`/bin/bash`、
  `/media/user/disk` 均原样通过 `sanitize_value` 且
  `contains_forbidden_content` 返回 False；`/home/user/x`、`C:\Users\…` 被
  正确拦截。
- 影响：`/usr`、`/bin`、`/sbin`、`/lib`、`/run`、`/media`、`/snap`、`/boot`
  等根下的绝对路径可进入本地记录与可选 exporter。多数是无身份信息的系统
  路径，但 `/media/<用户名>/…`、`/run/media/<用户名>/…` 可直接携带用户名。
  Phase 1 profile 冻结条款为 `"absolute_host_path": "forbidden"`，未按枚举
  子集让步，属合同偏差。到达面有限（内容承载键已被阻断，路径主要经由
  文件名/模型输出类字符串进入属性），故 P2 而非 P1。
- 建议：把正则改为"泛化绝对路径形状"（`^/[^/]+(/|$)` 加已知安全白名单，或
  至少补全 `usr|bin|sbin|lib|lib64|run|media|boot|snap|srv`），并为
  validator 提供与 redactor 不同源的判定或显式共享测试向量。

### P3

#### F-3 run_id 未校验即拼入强制 audit sink 路径：非法 run_id 的审计文件在拒绝前已创建于 state_root 之外并遗留

- 文件：[repair.py:3272-3284](../../src/code_review_agent/repair.py#L3272-L3284)
  （`Trace(state_root / values["run_id"] / …)` 直接使用契约原文）；
  [repair.py:432-437](../../src/code_review_agent/repair.py#L432-L437)
  （`_validate_run_id` 只在其后的 `RepairWorktreeManager.create` 内执行）；
  [tracelog.py:143-159](../../src/code_review_agent/tracelog.py#L143-L159)
  （`Trace.__init__` 先建文件再构造 Tracer，Tracer 的 `_RUN_ID` 校验发生在
  文件创建之后）。
- 复现（实测）：`run_id=".."` → `Tracer` 抛
  `TelemetryValidationError: run_id is not a bounded stable identifier`，
  但 `observability-*.jsonl` 已创建在 `state_root` 的**父目录**并遗留。
- 影响：repair 契约是人审输入且 state_root 本就由操作者指定，无权限增益；
  但 `docs/security-observability.md` 声明的 `<state_root>/<run_id>/` 布局
  保证被违反，且拒绝路径遗留孤儿文件。
- 建议：在构造 Trace 之前调用 `_validate_run_id(values["run_id"])`。

#### F-4 `aggregate_trace` 把"观测到的 0 token"降级为 `total_tokens=None`

- 文件：[observability.py:1084-1104](../../src/code_review_agent/observability.py#L1084-L1104)
  （`standard_total_tokens or extension_tokens_observed` 用值的真值性而非
  observed 标志判断）。
- 复现（实测）：span 携带 `input_tokens=0, output_tokens=0` →
  聚合结果 `input_tokens=0, output_tokens=0`，但 `total_tokens=None`。
- 影响：与"absent 才是 unknown、观测值不伪造也不丢弃"的口径不一致——
  这是把观测到的 0 错报为 unknown（合同禁止的是反方向的伪造 0，故仅 P3）。
- 建议：条件改用 `observed["input_tokens"] or observed["output_tokens"]
  or extension_tokens_observed`。

#### F-5 敏感文件名分量表缺 `*.pem` / `*.key` / `*.ppk` 形状

- 文件：[redaction.py:113-126](../../src/code_review_agent/redaction.py#L113-L126)、
  [redaction.py:178-191](../../src/code_review_agent/redaction.py#L178-L191)。
- 复现（实测）：`secrets.pem`、`server.key`、`deploy_key` 原样通过；
  `id_rsa`、`.env` 正确拦截。
- 影响：`read_file` 黑名单（tools.py）挡住文件**内容**读取，但含密钥形状的
  **文件名**字符串仍可进入 trace 属性。仅元数据级泄漏（暴露密钥文件存在与
  位置），且 `-----BEGIN … PRIVATE KEY-----` 内容形状本身会被值检测拦截。
- 建议：为 `_sensitive_relative_path` 增加后缀判定（`.pem`/`.key`/`.ppk`/
  `.p12`/`.pfx`），与 tools.py 黑名单对齐。

#### F-6 `llm.request` span 在模型对象缺 `provider` 时必然收尾失败（测试用 FakeModel 补属性掩盖了该路径）

- 文件：[repair.py:2719-2739](../../src/code_review_agent/repair.py#L2719-L2739)
  （`"gen_ai.provider.name": getattr(self.model, "provider", None)`——None 被
  sanitizer 丢弃）；
  [observability.py:837-859](../../src/code_review_agent/observability.py#L837-L859)
  （validator 要求 llm.request 必须携带非空 provider）；
  [tests/test_week3_repair.py:419-421](../../tests/test_week3_repair.py#L419-L421)
  （FakeModel 显式加 `provider="fake"`）。
- 复现（实测）：provider 为 None 的 llm.request span `end()` →
  `TelemetryValidationError: llm.request span lacks required semantic
  attributes`，随后进入 F-1 的楔死路径。
- 影响：已交付 CLI 流中 `runtime_provider` 有默认值且与契约强校验一致，
  不会触发；风险在编程调用方（如未来 swebench adapter）构造
  `OpenAIRepairModel(provider=None)`（参数默认值即 None）。fail-closed 方向
  正确，但失败形态是 F-1 的楔死而非清晰报错。
- 建议：`OpenAIRepairModel` 将 provider 设为必填，或 `_call_model` 在
  provider 缺失时显式回退 `"unknown"` 字符串并标记 degraded。

#### F-7 `discover_source_commit` 在非 editable 安装场景可能把外层无关仓库的 HEAD 当作 source_commit

- 文件：[observability.py:179-200](../../src/code_review_agent/observability.py#L179-L200)
  （`Path(__file__).resolve().parents[2]` 对 site-packages 布局落在 venv 内，
  `git -C` 向上搜索会命中包裹 venv 的任意 git 仓库）。
- 影响：来源证据错误标注且 `telemetry_mode` 记 normal 而非 degraded，
  违背 A1 "unknown 仅在无法发现 checkout 时"的语义方向（记录了一个**错的**
  commit 比 unknown 更糟）。本项目当前全部为 editable/worktree 用法，
  `PYTHONPATH` 显式指向本 worktree 时结果正确。
- 建议：校验 `parents[2]` 下存在本包源码布局（如 `src/code_review_agent`）
  或比对 `git rev-parse --show-toplevel` 与包路径的包含关系，不匹配则返回
  `unknown`。

#### F-8 读取端对恶意 trace 文件无深度/大小上限：深嵌套触发未受控 `RecursionError`，超大文件整读内存

- 文件：[observability.py:956-982](../../src/code_review_agent/observability.py#L956-L982)
  （`load_span_records` 全文件 `read_text`）；
  [redaction.py:349-367](../../src/code_review_agent/redaction.py#L349-L367)
  （`contains_forbidden_content` 无深度上限递归；`_canonical_json` 同理）。
- 影响：写入端由 sanitizer 保证深度 ≤8、单记录 ≤64 KiB，正常自产文件安全；
  但 validator 的自我定位是"拒绝坏记录"，对手工构造的 10k 层嵌套文件抛
  `RecursionError` 而非 `TelemetryValidationError`，属不干净拒绝。仅影响
  操作者主动加载不可信文件的场景。
- 建议：`validate_span_record` 显式校验嵌套深度（复用 MAX_NESTED_DEPTH），
  load 前检查文件大小上限。

#### F-9 串行回退路径的 `llm.request` span 直接挂 root，偏离冻结层级图中"chat 在 stage 之下"

- 文件：[tracelog.py:176-194](../../src/code_review_agent/tracelog.py#L176-L194)
  （无当前 span 时父级默认 root）；触发点如 verifier tiebreak pass C
  （[verifier.py:215-217](../../src/code_review_agent/verifier.py#L215-L217)，
  在 verifier stage span 退出后串行执行）。
- 影响：trace 仍单根、父子有效、可聚合；只是这些请求不归属任何
  `agent.stage`，与 profile 层级示意不完全一致。validator 不校验父 span 的
  operation 类型，故不报错。
- 建议：为 tiebreak/单 lane 降级路径补一个 stage span，或在文档中注明该
  层级图为"典型形状"而非硬校验。

#### F-10 `crag.cost.pricing_revision` 从未写入

- 文件：[repair.py:2793-2851](../../src/code_review_agent/repair.py#L2793-L2851)
  （写入 `crag.cost.micro_usd` 与 `crag.cost.settlement="reconciled"`，未写
  pricing revision；契约中 `pricing_id` 可用未用）。
- 影响：合同"整数 micro-USD、pricing revision、settlement status"三件套落了
  两件。字段在 profile 中为 conditional，缺席合法，但削弱成本证据的
  可复算性。
- 建议：在模型 span 上附 `crag.cost.pricing_revision=values["pricing_id"]`。

#### F-11 仓库根评测脚本复用固定 trace 路径，在 O_EXCL 新语义下重跑会硬失败

- 文件：[bench_verifier.py:181](../../bench_verifier.py#L181)、
  [run_eval.py:64](../../run_eval.py#L64)、[judge.py:270](../../judge.py#L270)、
  [replay_verifier.py:156](../../replay_verifier.py#L156)（均
  `Trace(<结果目录固定文件名>)`）；
  [observability.py:227-238](../../src/code_review_agent/observability.py#L227-L238)
  （已存在路径直接拒绝）。
- 影响："不覆盖既有审计文件"是合同要求的正确行为，README 也已声明
  "路径必须是新文件"；但这四个脚本（本任务写权之外、默认不运行）未同步，
  向同一结果目录重跑将在首个已存在 trace 文件处失败。属于合同允许的
  "拒绝既往不安全输入"的有意变更，缺一条针对评测脚本的迁移说明。
- 建议：integration 时在计划文档记一条迁移注记；后续获授权改脚本时改为
  每次运行生成唯一子目录/文件名。

## 5. 合同与字段映射一致性结论

- **冻结哈希**：4 个机器可读输入的 UTF-8/LF SHA-256 独立复算与 Phase 1 附件
  完全一致；case-plan 及其 schema 未被触碰；A2 修正走了带哈希更新的合同
  修正提交，且如实记录 `runtime_results_observed_before_amendment:true`。
- **envelope**：`_snapshot` 注入的 8 个信封属性与 profile 的 8 个 required
  扩展字段一一对应（schema.version、run.id、source.commit、redaction 的
  policy_version/count/omitted_count/truncated、telemetry.mode），
  `_ENVELOPE_ATTRIBUTE_SLOTS = 8` 与之匹配。
- **限额**：record 64 KiB、attributes 64、key 128 B、string 1024、array 32、
  depth 8、events 128、links 32、本地文件 64 MiB 全部与 profile 数值一致并被
  校验/测试覆盖。`export_retry ≤2 次/2000ms` 上限之下实现选择了更严格的
  "首败即熔断（0 重试）"，合规。`export_queue_records=1024` 无对应队列
  （可选 exporter 为同步直调），上限平凡满足。rotation 未实现，与
  "disabled-until-explicit-path-and-policy" 默认一致；超过文件上限抛
  `TelemetryWriteError` 而非覆盖/丢弃，符合"越限是审计失败不是删证据许可"。
- **span 映射**：8 个 operation 的 span 命名与 kind 全部按 profile 落地
  （`invoke_agent code-review-agent`、`chat {model}`（CLIENT）、
  `execute_tool {tool}`、`crag.stage/policy/sandbox/checkpoint {…}`、
  `crag.telemetry.export`）。
- **GenAI 字段**：llm span 记录 provider/model、response id/model/
  finish_reasons、max_tokens/temperature、五类 usage token；required 语义
  属性（agent.run 三件、llm.request 三件、tool.execute 两件）由 validator
  强制。5 个原始内容字段（input/output messages、system_instructions、
  tool.call.arguments/result）被 `FORBIDDEN_CONTENT_FIELDS` + 阻断键双重
  hard-disable，v1alpha1 无任何 verbose 开关，有测试锁定。
- **error 分类**：`crag.error.category` 14 值枚举与 profile 完全一致；
  `error_category_for_exception` 只看异常类型名不看 message。
- **provider 值**：deepseek/glm 小写，`Trace.event("meta")` casefold，合规。
- **观测缺失语义**：None 值在 sanitizer 顶层被丢弃、`unknown-if-absent`
  token 不发明 0（有测试与我方复核确认）；仅提供 total 的 Repair 路径用
  `crag.usage.total_tokens` 且与逐分量互斥，符合
  `conditional-when-provider-only-supplies-total`。例外见 F-4（观测到的 0
  反被降级 None）与 F-10（pricing_revision 未落）。
- **retry 语义**：单逻辑 llm span 覆盖 SDK 内部全部传输重试，符合冻结
  语义；但 SDK 重试对本实现不可见，`crag.retry.*` 事件当前从不产生——
  未观测即未记录，不伪造，与合同一致，记为已知观测局限。
- **兼容窗口**：新写入仅 canonical（dual-emit 无、默认关）；`iter_events`
  同时读旧 flat JSONL（原样透传）与 canonical（`legacy.*` 事件投影），
  cost_report/repeat_eval/bench_verifier 消费的 `llm_response`/`tokens_in`/
  `tokens_out`/`cache_hit`/`cache_miss` 键在 `_SAFE_TOKEN_KEYS` 中被保留，
  投影可用；0.3.0 前不得移除的声明在 profile 与文档中一致。见 F-11 的
  重跑行为注记。

## 6. 脱敏、并发、失败语义与 Repair audit 时序结论

- **脱敏顺序**：所有属性/事件在**进入 span 时**即经 `sanitize_attributes`；
  serializer 仅见已脱敏数据；`validate_span_record` 末尾再做
  `contains_forbidden_content` 兜底扫描 + 64 KiB 上限；可选 exporter 收到的
  是已验证记录的 deepcopy。顺序满足"先脱敏后编码/落盘/导出"。
  Prompt/diff/工具参数与结果/stdout/stderr/异常消息经三重机制拦截：
  埋点只记 bytes/counts（agentloop、repair_tools）；阻断键表覆盖
  `args`/`arguments`/`problems`/`stdout`/`stderr`/`message`/`diff`/`prompt`
  等 legacy 事件残留键（tools.py 的 `tev("tool", args=…)` 与 verifier 的
  `problems=…` 实测被丢弃）；异常对象仅序列化为 `[OMITTED:<类型名>]`。
  console 输出同轮收紧（`_safe_command_error` 分类替代原始 stderr、
  submit/verifier 失败只打印计数、repair CLI 只打印异常类型名）。
  canary/Bearer/sk-/ghp_/AKIA/URL-userinfo/`password=` 形状及相邻拆分秘密
  （list/dict/attributes 三处拼接检测）实测全部拦截。控制字符与 Unicode
  C 类归一为 U+FFFD，换行折叠为空格——单逻辑记录无法伪造第二条 JSONL 行。
  余留缺口见 F-2（POSIX 根枚举）与 F-5（pem/key 文件名）。
- **并发与 context propagation**：`run_parallel_pair` 在进入 stage span 后
  `copy_context()` 分发到两个 lane，两条 lane 的 llm/tool span 成为同一
  stage span 的兄弟子节点（`tests/test_observability.py`
  `test_review_vertical_path_has_parallel_stage_and_llm_children` 锁定）；
  `Trace.span` 在无上下文线程中回退挂 root，杜绝意外多根。Tracer 内部
  `RLock` + 每 span `RLock`，200 事件 4 线程并发测试无丢失。ContextVar 每
  lane 独立拷贝，无跨 lane 污染。
- **失败语义**：可选 exporter 首败即按对象熔断（`_export_optional`），根 span
  与非根 span 两条路径都会写入 `crag.telemetry.export_failed` 事件、置
  `telemetry_mode=degraded` 并（非根路径）产出一条 error 状态的
  `telemetry.export` span；`_recording_export_failure` 防递归（深度 1）。
  熔断只影响遥测，不触碰 tool/policy/approval/sandbox 对象——exporter 在
  构造函数注入，仓库/模型内容无配置通道（profile
  `repository_content_may_configure_export:false` 与实现一致）。本地
  `TelemetryWriteError` 沿 span 收尾向上传播成硬失败，不存在静默继续写入
  的路径。例外形态见 F-1。
- **Repair 强制 audit sink 时序**：`_run_repair_contract` 中 `Trace(...)` 的
  构造先于 cohort ledger、`worktree_root.mkdir`、`DockerWorktreeBackend`、
  任务 worktree 创建、checkpoint 首存、sandbox 构建、模型构造、审批与
  orchestrator.run；O_EXCL + 0o600 创建（平台支持处），已存在文件一律拒绝
  （测试 `test_existing_audit_file_is_never_overwritten` 锁定字节不变）。
  测试 `test_repair_contract…` 断言 `setup_order == ["trace", "worktree"]`。
  Review 侧 tracing 保持 opt-in，Repair 侧无禁用通道。本地初始化失败即
  构造抛错，任何受保护操作不会开始；审批决定不依赖任何遥测状态。
  终态（SUBMIT/FAILED/CANCELLED）、审批（write/commit approval 的
  policy.decision + approval 双属性）、每次状态迁移、每次 checkpoint save、
  sandbox 命令的 exit/timeout/truncation/bytes 全部入 trace，
  `aggregate_trace` 可交叉核算 token/cost/时延/工具/策略/degraded/fail_open
  计数（`test_happy_path_emits_cross_checkable_canonical_trace` 并断言补丁
  内容不在序列化输出中）。

## 7. Remaining risks

1. F-1/F-6 组合意味着任何未来埋点若写满属性或漏掉必需语义属性，故障形态
   是"楔死 + 丢 root 记录"而非清晰的单条降级记录——在 Phase 3 增加埋点前
   宜先修复。
2. 绝对路径与敏感文件名检测是枚举式的（F-2/F-5），对未来新增平台
   （如 WSL、`/snap`）需要维护；validator 与 redactor 同源，检测缺口不会被
   第二层捕获。
3. `crag.retry.*` 语义已冻结但当前无产生点，SDK 内部重试不可观测；后续若
   实现自管重试需补事件，否则 retry 统计恒为 0（真实为未观测）。
4. 层级图中 `chat/execute_tool 必在 stage 之下`未被 validator 强制（F-9），
   Phase 3 若以层级作断言依据需注意串行回退路径。
5. 评测脚本重跑行为变更（F-11）在获授权修改脚本前是已知操作性限制。
6. Windows 上 0o600 基本不生效（ACL 继承），"restrictive where supported"
   的表述如实，但本平台的实际保护弱于 POSIX。
7. 本审查为静态阅读 + 离线 fake 验证；未运行真实 provider、Docker 或任何
   付费路径，生产端到端行为（真实 SDK usage 对象字段形状、GLM 响应差异）
   未验证。

## 8. 合规确认

- 未读取、枚举、搜索、哈希或验证 `eval/**`、`eval/holdout/**`；未运行
  `scripts/verify.py --eval-assets`（verify.py 仅以默认参数运行）。
- 未联网、未下载或安装任何依赖；未调用外部模型/agent/服务；未启动 Docker；
  未运行付费评测或真实安全评测。
- 未物化 `security_redteam/cases.jsonl`；未修改或重新生成任何 frozen
  case-plan/hash（4 个冻结文件仅做只读哈希复算，字节未动）。
- 未执行 reset/checkout 覆盖/clean/rebase/merge/push。
- 唯一写入文件为本报告 `docs/reviews/week6-claude.md`；复现脚本位于会话
  scratchpad（仓库外），未提交。
- 冻结接口核对：profile `crag.observability/v1alpha1` 未变；redaction policy
  `week6-redaction-v1` 未变；原始 GenAI 内容字段 hard-disabled 且无开关；
  Repair 本地 audit sink 强制且先于受保护变更；远端 exporter 默认禁用；
  Phase 3 未被物化或执行；旧 JSONL 读取兼容保留；Week 3/4/5
  checkpoint/approval/sandbox/评测证据格式未被更改（test_week3_repair 仅
  增量断言，180 项全过）。

## 9. 最终结论

**有条件通过。**

- P1：无。
- P2（F-1、F-2）：应在 integration 阶段修复或书面处置——两者均 fail-closed
  方向、不在默认交付路径触发，不阻塞交接审查本身，但应在 Phase 3 开始增加
  埋点/用例之前解决。
- P3（F-3 ~ F-11）：建议随 integration 或后续小步修复/记录，均不影响本轮
  验收结论。
- 全部验证命令实测通过；变更范围合规；冻结合同、字段映射、脱敏顺序、
  并发传播、失败语义与 Repair audit 时序与 Phase 1 冻结内容一致。
