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
  成本/时延/工具/测试失败/非法操作统计和仓库分层配对 Bootstrap 95% CI。真实任务尚未
  下载或物化，Docker/外部模型/付费评测均未运行，等待 Claude 独立审查与 integration。

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

安全测试至少覆盖：

- README 或代码注释中的 Prompt Injection。
- 工具输出诱导调用其他工具。
- `.env`、SSH Key 和凭据读取。
- 路径逃逸和符号链接。
- 命令注入。
- 恶意测试脚本。
- 超大文件和无限循环。
- Agent 尝试修改未授权文件。

将 OWASP Agentic AI 中的行为劫持、工具误用等风险落实为可执行红队测试。

可观测性：

- 为每次 Agent Run、LLM 请求和工具调用分配 trace/span。
- 记录模型、缓存 Token、成本、延迟、重试和错误类型。
- 对 Prompt 和工具结果做可配置脱敏。
- 采用 OpenTelemetry GenAI Agent、Tool Call、Token Usage 等语义字段替换完全自定义的 JSONL 口径。

## 第 7 周：标准协议与服务化

优先实现 FastAPI / GitHub Webhook 服务和 MCP Server，A2A 作为可选加分项。

MCP Adapter 可暴露：

- `review_diff`
- `review_pr`
- `get_review_status`
- `approve_patch`
- `get_trace`

MCP 的 Tools、Resources、Prompts 与现有自定义工具层对齐。A2A 等单 Agent 和 MCP 完成后再引入，用于暴露可发现、可协作的 Finder、Verifier 或 Repair Agent。

## 第 8 周：补齐“算法岗”模型侧证据

训练一个可控的 Verifier / Reranker，而不是完整 Code LLM。

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
