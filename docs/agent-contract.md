# Agent 协作契约（Claude Code × Codex）

适用于在本仓库工作的所有编码 agent（Claude Code、Codex 及后续加入者）。
任何任务开始前必须先读完本契约；条款与任务指令冲突时，先向人类发起澄清，不得自行放宽。

## 1. 工作区模型：独立 worktree

- 每个 agent 在**独立的 git worktree** 中工作（例如 `code_review_agent-claude-sync/`、
  `code_review_agent-codex-sync/`），绝不在主 checkout（仓库根目录所在的主工作区）里直接改动。
- 分支命名：`claude/<task>`、`codex/<task>`。一个任务一个分支，不复用他人分支。
- worktree 之间不互相读写对方的未提交改动；同步只通过 git 提交历史进行。

## 2. Single Writer 原则

- **任一文件在任一时刻只允许一个 agent 持有写权。**
- 需要改动他人所有权范围内的文件时：停下，向人类申请所有权转移或拆分任务，不得先改再说。
- 只读访问（Read/Grep）不受限制。

## 3. 按任务声明文件所有权

- 动手前，在任务开头**显式列出本任务将创建/修改的文件清单**（所有权声明）。
- 实施中发现需要触碰清单之外的文件：先补充声明并说明原因，再改动；交付时如实报告偏差。
- 未声明即修改 = 违约，人类审阅时按可疑改动处理。

## 4. 主干保护

- **不得直接合并、rebase 到或推送 `master`**（CI 亦监听 `main`，同样受保护）。
- 交付物是任务分支上的改动；合并进主干只由人类执行。
- 除非任务明示授权，不执行 `git commit` / `git push`；获授权时也只提交到本任务分支。

## 5. 公共 API 与依赖冻结

未经人类明确批准，不得改动：

- `src/code_review_agent/` 包的公开接口（模块级公开函数/类签名、`crag` 入口
  `code_review_agent.agent:main`、CLI 参数语义）；
- `pyproject.toml` 的 `dependencies` / `project.scripts` / 打包配置，及 `requirements.lock`、
  `requirements.txt`；
- `.github/workflows/ci.yml`。

包内部实现细节的重构允许，但须保持单元测试（tests/）全绿；单元测试使用 fakes，不应调用外部 API。

## 6. 本仓库特有红线

- **`eval/` 与 `eval/holdout/` 的评测资产（fixture repo、diffs、truth.json）是故意埋 bug 的
  冻结集**——不得"修复"其中的缺陷，ruff 已显式排除该目录；改动评测资产等同改动基准。
- **holdout 纪律**：`eval/holdout` 只在 prompt/判据迭代的验收时运行，平时不跑不看。
- finder/verifier 的 prompt（`SYSTEM` / `VERIFIER_SYSTEM`）与哨兵正则
  （`src/code_review_agent/sentinels.py`）受每周验收纪律约束（sweep → bench → 配对回放，
  见 README W13 节）——不属于常规任务可自行改动的范围。
- 消耗 LLM 配额的评测脚本（run_eval/judge/repeat_eval/replay_verifier/bench_verifier）
  **默认不运行**，需人类明确授权（真实计费，见 README 成本节）。

## 7. 验证命令（与 CI 一致，交付前必须通过）

Windows 下用项目 venv 解释器 `.venv\Scripts\python.exe`（下文简写 `python`）：

```powershell
python -m ruff check .                                # lint（ruff 非项目依赖，需单独安装）
python -m unittest discover -s tests                  # 单测+golden（用 fakes，不调用外部 API）
python eval/check_consistency.py eval eval/holdout    # 评测资产三方一致性（仅特定场景，见下）
crag --help                                           # 打包冒烟（改了入口/打包时）
```

- 一致性检查（check_consistency）只在**评测资产变更**或 **prompt/判据验收**时运行；
  普通业务改动不运行该检查，也不读取 `eval/holdout`。
- 改动仅涉及文档（*.md）时，可只做 diff 自审，跳过上述命令。
- 任何命令失败：先修复再交付；修不了则如实报告失败输出，不得绕过（禁 `--no-verify` 等）。

## 8. diff 自审

- 交付前运行 `git diff`（未提交）或 `git diff master...HEAD`（已提交）**逐文件通读**：
  - 改动文件集合 ⊆ 所有权声明清单；
  - 无误入的无关改动（行尾空白、格式化噪音、调试残留）；
  - 无泄密内容（key、.env、绝对路径中的敏感信息）。
- 发现越权文件：撤销该文件改动或补声明说明，二选一，不得沉默交付。

## 9. 交付格式

最终回复必须包含：

1. **修改文件清单**：路径 + 每个文件一句话说明改了什么；
2. **验证结果**：跑了第 7 节哪些命令、各自结果（或说明为何豁免）；
3. **风险与未验证项**：已知限制、未覆盖场景、需人类复核的点；
4. **偏差声明**（如有）：超出原始所有权声明的改动及原因。

分支上如有提交（获授权时），附分支名与提交号。
