# Changelog

本文件记录 code-review-agent 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

Week 2「延迟韧性」：审查通道阶段内并行 + 全程协作式软截止。公开接口、CLI、review JSON 结构、prompt/哨兵/评测资产均无变化。改动已在本地离线验证（190 测试 / 96% 分支覆盖率 / Ruff / mypy / 双 CLI 冒烟 / eval+holdout 一致性，零 LLM 调用），**尚未在 GitHub CI 上运行，也未做真实 provider 延迟基准**。

### Added

- **并行审查通道编排**（新模块 `orchestration.py`）：finder 锚定/采样两跑与 verifier A/B 两 pass 分别用两个线程阶段内并行（两阶段之间仍串联）；`run_parallel_pair` 收敛两 lane 的结果与异常后按原有优先级重放，保持锚定失败致命、采样/单 pass 失败降级、双 pass 失败 fail-open、AuthenticationError/RateLimitError 显式穿透的既有语义
- **全 review 协作式延迟预算**：`run_review` 在上下文构建前启动 300 秒 monotonic 软截止，贯穿全部 finder/verifier loop；截止后不再发起新的 LLM 请求，每个新请求的 timeout 取剩余预算与原有 120s 单请求上限的较小值（软截止不强杀已在途的同步请求）
- **并行阶段 / 截止 trace 遥测**：新增事件 `parallel_stage_started`、`parallel_stage_finished`（含阶段耗时与各 lane 异常类型）、`deadline_exhausted`（含组件与已完成步数）；既有事件的字段不变
- **并发与超时回归测试**（`tests/test_week2_orchestration.py`，12 个，零 API）：barrier 验证两 lane 真实重叠、截止后零新请求、锚定截止致命 / finder2 截止降级 / verifier 截止 fail-open、全阶段共享同一 deadline、并发 trace 行完整性

### Changed

- finder 采样跑（finder2）不再等待锚定跑完成，随锚定跑并行提前启动——锚定快速失败时采样跑可能已发出请求（golden 测试已固化该行为）；正常路径的请求数与内容不变
- trace 写入线程安全化：`Trace.event()` 行级加锁写入 + flush，并发 lane 不会交错破坏 JSONL 行；`cost_report` / `repeat_eval` / `bench_verifier` 按事件字段无序聚合，不受并行交错影响
- 本地验证基线更新：190 个零 API 测试 / 96% 分支覆盖率（Week 1 为 178 / 95%），mypy 覆盖 14 个源文件

## [0.1.0] - 2026-07-15

首个可安装版本（`pip install -e .` → `crag` 命令），已以 v0.1.0 Release 发布于私有 GitHub 仓库（未公开）。

### Agent 架构

- **Finder/Verifier 两阶段架构**：Finder 双跑（temp=0 锚定 + temp=0.7 采样）召回候选缺陷，结构化去重（token-set Jaccard 双档阈值）+ 文件级 scope 过滤（diff 外发现降级 `out_of_scope`）；Verifier 双 pass 独立证据复核（候选倒序去相关，drop 需 2/2 票），分歧保留标 `uncertain` 并附少数派理由
- **Sentinel 哨兵**（四族，纯正则零 LLM 成本）：drop 理由命中"prompt 禁止话术 × 受保护缺陷类别"合取时降级 uncertain 而非丢弃；失败方向安全（失配只会不救），救活条目带 `[sentinel:tag]` 可审计
- **共用 agent loop 引擎**：结构化输出做成 `submit_review` tool call（schema 当函数参数，不依赖厂商专有 JSON mode）；坏载荷校验回填重试（上限 2）、`MAX_STEPS=10`、末步撤工具催交
- **Provider 无关**：OpenAI 兼容接口，`LLM_PROVIDER` 切换 DeepSeek（默认）/GLM，`LLM_MODEL` 可锁定快照；账号级异常（认证/限流）穿透响亮失败，基础设施类异常走降级语义（finder2 退单跑、verifier degraded/failed_open 并在输出标注 `verifier_status`）

### Repo 工具与安全防护

- 只读工具三件套：`read_file`（大文件按 `start_line` 续读、文件不存在返回候选路径）、`search_repo`（字面量全仓 grep、目录剪枝遍历）、`run_linter`（pyflakes 静态检查，不执行代码）
- 敏感文件防护：路径逃逸检查（`resolve()` + `is_relative_to`），`_refuse_read` 黑名单拦截 `.env*`/`*.pem`/`*.key`/`id_rsa*`/`credentials*` 等，带回归测试保证密钥值不进错误文案
- 可靠性护栏：同参数重复调用短路、连续 3 次搜索 miss 注入提示、工具异常转可行动的 `Error:` 文本、每请求 120s 超时 + SDK 重试

### 主动上下文检索与 src-layout 修复

- `context.py` 预取：约定文档（CLAUDE.md 等）+ 改动文件全文 + in-project import 追踪 + 调用方片段，符号级检索无向量库，全 pack 预算封顶 28k 字符
- **修复：import 追踪支持 src/ 布局**——此前 `pkg.mod` 只按仓库根解析，src/ 布局包内 import 静默走 external 分支、定义不被预取；现按 importing 文件位置选择搜索根（`src/` 优先或回退仓库根），flat 与 src-layout 均可解析，缺失的项目内 import 输出显式 note，外部/标准库 import 静默（新增 4 个回归测试覆盖以上矩阵）

### 可观测与集成

- **JSONL trace**：llm_response/tool/submit_rejected/review/verdicts 全事件流落盘，含 provider/model 元数据与 token/cache 计量（供 `cost_report.py` 出真实计费报表）
- **GitHub PR review payload / dry-run**：`github_review.py` 行号映射 + 行内评论载荷构建；`--pr N --post-dry-run` 打印 `gh api` 命令与完整载荷不发送；live `--post` 发帖前 fail-fast 校验（真实发帖待远程仓库验证）
- git 集成：`--commit [SHA]` / `--uncommitted` / `--pr N` 三种 diff 来源；输出 JSON（含 dropped/uncertain/out_of_scope 审计通道）或 `--format md` 的 PR 可贴 Markdown

### 离线评测与 holdout

- 公开集 16 diffs / 30 埋点（真实项目 bug 蒸馏，含 2 陷阱用例 + 1 信息缺失用例），holdout 6 diffs / 7 埋点独立副本；`eval/check_consistency.py` 保证 diff↔fixture↔truth 三方一致
- 台架：LLM judge 结构化裁决（GLM 交叉重判 90/90 一致）、`repeat_eval.py` n 次重复跑 + per-bug 翻转表 + bootstrap CI、`replay_verifier.py` 配对回放（verifier 迭代省 ~60% 成本）、`bench_verifier.py` 冻结 case 断言、`cost_report.py` 缓存感知计价
- 评测脚本调用付费 API，默认不运行；评测资产为故意埋 bug 的冻结集，ruff 显式排除

### 质量与工程化

- **测试：178 个零 API 测试全部通过；分支覆盖率 95%，门禁 `fail_under=85`**（2026-07-15 本机 Windows 11 / Python 3.13 独立 venv 重新验证，非沿用历史数字）——golden 测试（FakeClient 锁请求序列与 trace 事件流）+ 纯函数单测 + 回归测试（本版新增 CLI 参数路径、工具协议、provider 构建、context 布局解析共 3 个测试文件）
- **Python 3.10–3.13** 支持声明与 CI 矩阵（Linux 3.10–3.13 + Windows 3.11）
- **mypy**：非空严格配置（`check_untyped_defs`/`no_implicit_optional` 等）对 `src/code_review_agent` 全包通过，本版补运行时类型修复（ProviderConfig TypedDict、severity 非字符串防御、sentinel family 空值守卫）且保持行为不变
- **Ruff**（E/F/W）通过；`.[dev]` extra 一键安装全部本地验证工具；`scripts/verify.py` 单入口串起 lint→测试+覆盖率→mypy→双 CLI 冒烟→（可选）评测资产一致性，本地与 CI 同一入口
- **Docker packaging**：`python:3.13-slim` 基底、非 root 用户、只 COPY 源码与元数据，`.dockerignore` 排除 `.env*`/密钥/VCS/trace/评测结果；CI 增 container-smoke job（本机无 Docker 未验证构建，状态为等待 GitHub CI 验证）

### Known limitations

- 评测为单项目人工植入基准（16+6 diffs / 30+7 埋点），真实代码库泛化仅有一次 3-commit 抽查，数字是工程迭代信号而非通用效果
- judge 与被测 agent 同模型（交叉重判已收窄但共享盲区无法排除）；holdout 被反复用于验收，实际为第二开发集；n=3 无显著性检验
- Sentinel 正则与特定模型族的措辞耦合，换 provider/改 prompt 需重跑 sweep
- 实际审查需第三方模型 API 与费用（W14 实测均值约 ¥0.11/review）；模型 id 是服务端别名，存在漂移变量
- 工具全部静态只读，不执行被审代码、不跑测试；封闭世界假设使 precision 有偏低估
- Docker 构建与全矩阵 CI 在本版发布时未实际运行（随后 Week 1 交付合入 master，私有 GitHub 仓库的 CI 含容器冒烟已运行通过，见 README）；live PR post 未做过真实发帖
