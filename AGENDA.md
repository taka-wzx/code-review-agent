# Code Review Agent：9 周升级计划

## 当前进度

- 第 1 周：已完成并发布 `v0.1.0`。
- 第 2 周：已完成延迟与韧性改造并合入 `master`。
- 第 3 周：实现、Claude 终审与本地验收已完成；10 个真实 Issue、本地人工审批、Docker 沙箱、失败隔离和两次受控中断恢复均已验证。
- 第 4 周：可信 Review 评测合同、4 仓/40 PR 预注册（密封 reporting 为 3 仓/30 PR）、
  双人独立标注/第三方仲裁协议、仓库级防泄漏和离线统计框架已完成 Codex 实现、Claude
  独立审查和 integration 整合；13 项审查发现均已处置，已合入并推送 `master`，GitHub
  CI 通过。真实 PR 尚未下载，外部模型和付费评测尚未运行。
- 第 5 周：已从第 4 周合入后的 `master` 冻结 SWE-bench Verified 30 任务候选合同
  （5 development / 5 tuning / 20 sealed reporting）、6 配置单因素消融、独立
  worktree/Docker 身份和 USD 80 总硬上限；已实现纯离线选择/run-plan 验证、pass@1、
  成本/时延/工具/测试失败/非法操作统计、并发/container-hour 审计和仓库分层配对
  Bootstrap 95% CI。Claude 独立审查的 13 项发现已在 integration 逐项处置；成果已
  合入并推送 `master`，远端 SHA 已核对，GitHub CI 已到成功终态。真实任务尚未下载或
  物化，Docker/外部模型/付费评测均未运行。
- 第 6 周：Phase 1--3 合同、可观测性、离线红队、Claude 审查和 integration 整合已完成；
  Phase 4 的 12 个受限 Docker 探针全部通过且无残留容器，Phase 5 的 24 次 GLM-5.2
  合成攻击/对照探针完成，攻击成功和误拦均为 0，估算成本约 ¥0.13842。Phase 6 独立
  Claude 终审、唯一 P2 修复及 integration 全量验证已完成；已合入并推送 `master`，
  GitHub CI 到达成功终态。
- 第 7 周：已冻结 FastAPI / GitHub Webhook / MCP 服务合同并完成 Codex 实现；Bearer、
  Webhook HMAC、Host/Origin 防 DNS rebinding、注册仓库白名单、SQLite 幂等任务状态和
  canonical trace 资源已通过 33 个新增离线协议测试及全仓门禁。Claude `fable5` 独立审查提出的
  4 项 P2 和 6 项 P3 已全部处置；已合入并推送 `master`，GitHub CI 全部 7 个 job 到达成功终态。

## 第 1 周：工程基线与公开交付

任务：

- 创建公开 GitHub 仓库，检查并清理密钥历史。
- 增加 dev 可选依赖：coverage、ruff、mypy 等。
- 修复 README 已知的 `src/` 布局 import 预取问题。
- 设置覆盖率门槛：总体不低于 85%，Agent 核心模块不低于 90%。
- 更新答辩文档中已经过期的测试数量。
- 增加 Dockerfile、开发环境安装命令和 CI Badge。
- 发布 `v0.1.0`。

验收：全新机器克隆后，一条命令安装、一条命令运行 Demo，单测、lint、类型检查和覆盖率全部通过。

## 第 2–3 周：从 Review Agent 升级为 Review + Repair Agent

状态机：

```text
DISCOVER
  ↓
PLAN
  ↓
PATCH
  ↓
TEST
  ↓
REFLECT ──失败──→ PATCH
  ↓
WAIT_APPROVAL
  ↓
SUBMIT
```

新增工具：

- `git_status`
- `git_diff`
- `apply_patch`
- 受限命令执行
- 测试执行
- 回滚修改
- Checkpoint 保存与恢复

关键要求：

- 每次任务使用独立 worktree。
- 修改前输出计划。
- 写文件和提交前设置人工确认点。
- 测试失败后允许有限次数自修复。
- 崩溃后可以从 Checkpoint 恢复。
- 所有命令必须运行在 Docker 或受限沙箱中。
- 设置总时长、Token、成本和工具调用预算。

验收：

- 至少完成 10 个真实、小规模 GitHub Issue。
- 能在进程中断后恢复。
- 失败任务不会污染原仓库。
- 不会绕过人工确认提交代码。

## 第 4–5 周：重建可信评测体系

Review 评测：

- 建立一个从未用于调参的新集合。
- 至少覆盖 3 个不同代码库、30 个以上真实 PR。
- 两名标注者独立判断，冲突由第三次仲裁。
- 记录标注一致率，按代码库切分，避免同仓库泄漏。

Repair 评测：接入 SWE-bench Verified 的 20–50 个任务子集，利用真实 GitHub Issue 和 Docker 化可复现评测证明 Code Agent 能力。

必须报告：

- Issue 解决率 / pass@1。
- Review precision、recall、F1。
- 每任务成本。
- p50 / p95 时延。
- 平均工具调用次数。
- 测试失败率。
- 非法或越权操作率。
- fail-open / degraded 比例。
- Bootstrap 95% CI。

必须做的消融：单 Finder、双 Finder、上下文检索、Verifier、Repair Reflection 和不同模型。

## 第 6 周：安全与生产可观测性

**当前状态（Phase 4--5 live probes 已验证）**：Phase 1 已冻结 OWASP 2026 风险映射与
OpenTelemetry core 1.43.0 / GenAI 固定提交；获批的 Phase 2 已实现
`crag.observability/v1alpha1` canonical trace/span、序列化前脱敏、旧 JSONL
兼容投影，以及 Review / Verifier / Repair 的 LLM、工具、策略、审批、沙箱、
checkpoint 和终态埋点。实现与运行说明见
`docs/security-observability.md`。A3 授权提交之后，Phase 3 已物化并执行冻结的 48 个
合成身份（36 对抗、12 正常对照），全部使用 effect-recording fakes。默认离线门禁已
通过（530 tests、3 skips、86% coverage、Ruff、mypy、双 CLI 冒烟）；48/48 用例通过，
对抗成功、secret disclosure、已执行越权操作和正常对照误拦均为 0。Claude 指出的
“审计事件事后盖章”已改为实际观测事件经 canonical trace 回读验证；报告强制区分
23 个 product-code 用例（15 对抗、8 对照）和 25 个 fixed-fixture 用例（21 对抗、4 对照）。
Phase 3 已完成 Claude 独立审查、findings 处置和 integration 离线验证，并以提交
`6b2adbb440670b135e42157ec4d8479426b47de2` 合入本地 master。A4 输入冻结提交
`9f4b33b76a4f6bb8587f284871a7f22b5bbe34b4` 之后，Phase 4 使用本地锁定镜像执行
12 个串行 Docker 探针，12/12 通过、残留容器 0；Phase 5 对 GLM-5.2 执行 18 个对抗和
6 个正常对照，24 次均返回唯一 `submit_security_decision`，无 protected tool call、错误或
malformed，攻击成功率与误拦率均为 0（Bootstrap 95% CI `[0,0]`），输入 13,916、输出
1,187 token，估算成本 138,420 micro-CNY。供应商未返回 system fingerprint；这些仍是
单模型、单次、合成 prompt 的小样本探针，不是生产安全或跨模型泛化指标。全程未下载数据、
未执行模型工具调用、未读取既有 eval/holdout。Phase 6 独立审查与 findings 整合已完成，
Phase 7 尚待合入、推送和 CI 终态核验。

目标不是只增加零散回归，而是冻结威胁模型、可观察副作用和成对正常对照，形成可复核的
离线安全门禁。计划至少包含 48 个全新合成用例：36 个对抗用例和 12 个成对正常对照。

安全测试覆盖：

- README 或代码注释中的 Prompt Injection。
- 工具输出诱导调用其他工具。
- `.env`、SSH Key 和凭据读取。
- 路径逃逸和符号链接。
- 命令注入。
- 恶意测试脚本。
- 超大文件和无限循环。
- Agent 尝试修改未授权文件。
- 审批重放、Checkpoint 篡改、日志换行伪造和 exporter 失败。

以“是否发生禁止副作用”为判据，报告 attack success、拦截、检测、误拦、敏感信息泄漏、
越权操作、清理/隔离和证据完整率。强制门禁为零禁止副作用、零 canary 泄漏、零已执行越权
操作，且 12 个正常对照不被误拦。OWASP Agentic AI 的精确版本和风险映射必须在实现前冻结。

可观测性：

- 为每次 Agent Run、阶段、LLM 请求、工具调用、策略决定、审批、沙箱命令和终态分配
  trace/span，并校验父子关系、并发时间线和生命周期。
- 记录精确模型、输入/输出/缓存 Token、整数 micro-USD 成本、时延、重试、预算和稳定错误类型。
- Prompt、工具参数/结果、stdout/stderr、异常和路径在序列化前统一脱敏；默认不记录原文。
- 实现前冻结 OpenTelemetry GenAI 语义约定的权威版本、稳定性和字段映射；以版本化语义
  envelope 取代完全自定义词汇，同时保留一个有截止期的旧 JSONL 兼容桥。
- 本地安全审计不采样；远端 exporter 默认关闭。远端失败只能进入明确 degraded 状态，
  不能放宽策略；Repair 缺失强制本地审计时必须在写操作前 fail closed。

本周按 Phase 0--7 八个阶段推进：规划、合同冻结、离线可观测性核心、合成红队、可选
Docker smoke、可选模型攻击评测、Claude/integration，以及 master/push/CI。Phase 1
之后的实施、执行和交付门禁均需按合同获得相应批准；完整合同见
`docs/plans/week6-security-observability.md`。

## 第 7 周：标准协议与服务化

优先实现 FastAPI / GitHub Webhook 服务和 MCP Server，A2A 作为可选加分项。

MCP Adapter 可暴露：

- `review_diff`
- `review_pr`
- `get_review_status`
- `approve_patch`
- `get_trace`

MCP 的 Tools、Resources、Prompts 与现有自定义工具层对齐。A2A 等单 Agent 和 MCP 完成后再引入，用于暴露可发现、可协作的 Finder、Verifier 或 Repair Agent。

本周改进后的冻结范围：先交付统一异步 Review 任务核心，FastAPI 提供注册仓库限定的
diff/PR 提交、状态与 trace API，GitHub Webhook 使用 HMAC 和 delivery 幂等；MCP 通过
官方稳定 1.x SDK 暴露 `review_diff`、`review_pr`、`get_review_status` 三个 Tool、review/
trace Resource 和复用 Prompt。`get_trace` 按 MCP 语义实现为 Resource。`approve_patch`
推迟到远程身份与 pending Repair 操作可持久绑定后，避免把现有一次性审批降级为通用布尔
开关；A2A 继续推迟。完整合同见 `docs/plans/week7-protocol-service.md`。

### 第 7.5 阶段：真实协议链路验证

在不扩展产品接口的前提下，以一个私有草稿 PR、临时 GitHub Webhook、官方 `gh` 和一次
`deepseek-v4-pro` 审查验证真实链路。唯一任务成功；官方重投和同 payload 签名重放复用
同一 review，未增加模型调用；无效 HMAC 返回 401。初次 Webhook 在 GitHub 侧发生 10 秒
超时，但提交实现已异步入队，现有证据不足以确定真实延迟原因；生产化前应先补充端到端时序
埋点并复现，再决定修复方案，同时把重复投递响应缩减为紧凑确认。
真实链路执行本身不包含容器，但推送后的 Ubuntu CI 已通过锁文件安装和 CLI/服务镜像
build/help smoke；容器内真实协议、MCP-over-HTTP、远程 OAuth、审批 API 或 A2A 仍未验证。
完整证据见 `docs/week7-5-live-validation.md`。

## 第 8 周：补齐“算法岗”模型侧证据

训练一个可控的 Verifier / Reranker，而不是完整 Code LLM。

**当前进展（Phase 8C）**：Phase 8A 已冻结候选/证据/工具摘要、仓库级切分、多重哈希、
指标和两种词法基线；Phase 8B 进一步冻结 9 个公开宽松许可证仓库、29 PR 窗口与确定性
选择、双人独立标注/仲裁、secret scan、留存和零付费/零加速器上限，并实现来源到 freeze
manifest 的严格离线门禁。当前闭环示例仍为合成 fixture 且 `trainable=false`，只证明协议
可复现，不构成模型效果证据。9 仓/29 PR 的真实公开来源快照及 pending Finder 队列已物化
并哈希冻结，原始对象保持在忽略目录。Phase 8C 已在独立 CPU 环境以精确 safetensors
快照和锁定依赖完成 Base、全量 SFT、LoRA SFT、LoRA pairwise 的同 manifest 合成烟测，
`quality_claim_allowed=false`；真实 Finder 候选、两人标注/仲裁、真实跨仓实验、GPU 和 API
对照仍未完成或未获授权；详见
`docs/plans/week8-verifier-training.md`、`docs/verifier-training.md` 与
`docs/verifier-corpus.md`、`docs/verifier-transformer.md`。

**下一步（Phase 8D，离线控制面已实现）**：在不读取原始 diff、不调用 provider 的条件下，
为 29 个来源生成 fail-closed Finder envelope，并加入诚实零候选回执、双人盲标响应导入、
第三人仲裁、Finder-bound real freeze 与真实模型 readiness 门禁。真实执行仍等待 provider/
精确模型、调用/token/费用、raw diff、trace 留存、三位人员身份和稳定本地提交授权；详见
`docs/plans/week8d-real-verifier-evidence.md` 与 `docs/verifier-real-evidence.md`。

数据：

- Finder 生成的候选问题。
- 人工标注 keep / drop / uncertain。
- 正负证据和工具轨迹。
- 按仓库切分，禁止泄漏。

实验：

- 基础小模型零训练。
- LoRA / SFT。
- Pairwise DPO 或排序损失。
- 与 API Verifier 比较。
- 跨仓库泛化测试。

报告 Precision / Recall / F1、Calibration / ECE、不同阈值下的 PR 曲线、训练成本和推理延迟、错误类型分析，以及 Base / SFT / 偏好优化消融。

面向华为可增加 MindSpore / 昇腾部署或量化实验；面向字节优先加强 PyTorch、Agent 轨迹数据、Verifier 后训练和 SWE-bench。

## 第 9 周：面试交付与项目包装

最终仓库首页直接展示：

- 30 秒项目描述。
- 架构图。
- 90 秒 Demo GIF 或视频。
- 一键运行命令。
- 真实 PR 示例。
- Benchmark 表格。
- 成本和时延表格。
- 安全模型。
- 已知限制。
- Roadmap。

准备三套陈述：

- 30 秒：问题、方案、核心结果。
- 5 分钟：Agent 架构、工具调用、Verifier、评测和成本。
- 20 分钟：失败案例、评测污染、系统取舍、安全、模型训练实验和生产化设计。
