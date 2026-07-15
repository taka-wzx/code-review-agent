# CLAUDE.md

**开始任何任务前，先完整阅读 [docs/agent-contract.md](docs/agent-contract.md)（多 agent 协作契约）并遵守。**
本文件只做入口指引，契约条款以 agent-contract.md 为准。

## 项目速览

- 两段式（finder + verifier）code review agent，OpenAI 兼容 API（DeepSeek/GLM），带可复现评测台架。
- 运行时代码在 `src/code_review_agent/`（src/ 布局，`pip install -e .` 后获得 `crag` 命令）；
  评测脚本（run_eval/judge/repeat_eval/replay_verifier/bench_verifier/cost_report）在仓库根。
- 架构、指标与各周迭代记录见 `README.md`；评测归因细节见 `eval/cases.md`。

## 硬性约束（详见契约）

- 只在自己的独立 worktree 分支工作（`claude/*` / `codex/*`），不得直接合并 `master`。
- 动手前声明本任务的文件所有权；不得擅改公共 API、pyproject 依赖与 `eval/` 评测资产。
- 交付前跑契约中的验证命令并完成 diff 自审。
