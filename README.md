# code-review-agent

最小 code review agent loop（W0 里程碑）。喂一段 unified diff → Claude 审查（可用 `read_file` 工具自己读仓库文件补上下文）→ 输出结构化 JSON review。

## 运行

Provider 无关：走 OpenAI 兼容接口，`LLM_PROVIDER` 切换 `deepseek`（默认）/ `glm`。

```powershell
# 0. 建项目独立虚拟环境 + 装依赖（只做一次）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 1. 设 provider + key
$env:LLM_PROVIDER = "deepseek"          # 或 "glm"
$env:DEEPSEEK_API_KEY = "sk-..."        # glm 则设 $env:GLM_API_KEY

# 2. 跑内置样例（sample.diff 里埋了几个真实 bug，看它能不能抓到）
.venv\Scripts\python.exe agent.py sample.diff

# 3. 审查真实 commit
cd e:\shiyan\pingpong_tracker
git show HEAD --format="" > ..\code_review_agent\real.diff
cd ..\code_review_agent
.venv\Scripts\python.exe agent.py real.diff --repo e:\shiyan\pingpong_tracker
```

## 结构（~150 行，全在 agent.py）

- **agent loop**：`run_review()` — 调 API → 有 `tool_calls` 就执行、把 `role:"tool"` 结果回填 → 循环直到模型调用 `submit_review`
- **工具**：`read_file`（带 repo root 路径逃逸检查）+ `submit_review`（提交最终结果）
- **结构化输出**：把「提交结果」做成 `submit_review` 工具、schema 当函数参数——不依赖任何厂商专有的 JSON 模式，跨 DeepSeek/GLM 通用
- **护栏**：`MAX_STEPS=8`、每次请求 120s 超时（SDK 自动重试两次）、工具失败回 `Error:` 文本而不是崩

## 评测（W1 人工基线 + W2 自动打分）

```powershell
.venv\Scripts\python.exe run_eval.py    # 跑评测集 -> eval/results/*.json
.venv\Scripts\python.exe judge.py       # LLM-judge 自动打分 -> recall/precision + scores.json
```

- ground truth 在 `eval/truth.json`（每个埋点附**命中标准**，固化人工打分时的微妙判例）
- judge 裁决经结构化校验（id 齐全/索引不越界/无重叠/全覆盖），不合法自动带错误重试一次
- **judge 校准**：埋点命中判定 9/9 与人工一致，分类 11/12（详见 `eval/cases.md`）

## 上下文检索（W3）

`context.py`：解析 diff → 主动预取约定文档（CLAUDE.md）、改动文件全文、函数调用方片段（纯符号级检索，无向量库，预算 cap 28k chars）。评测用 fixture 仓库 `eval/repo/`；`--no-context` 关掉检索做 ablation。

| 版本 | recall | precision | noise |
|---|---|---|---|
| V0 无上下文 | 0.78 (7/9) | 0.58 (7/12) | 5 |
| V1 +主动检索 | **1.00 (9/9)** | **0.82 (9/11)** | 2 |

两个 miss（§5.1 符号投影、缺 import）均为上下文饥饿型，检索后全部命中。注：n=9 的合成集，见 cases.md 的过拟合注脚。

## 限制

- 只有 read_file 一个执行工具；不跑 linter/测试
- 无 verifier 复核——style 噪音无人拦（W5 做）
- 评测集小且合成（扩真实用例是 W5 前置）；没有 trace 落盘（W6 做）
