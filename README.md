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

## 评测（W1 人工基线 + W2 自动打分 + 扩集）

```powershell
.venv\Scripts\python.exe run_eval.py [--no-context] [--results-dir DIR]   # 跑评测集
.venv\Scripts\python.exe judge.py [--results-dir DIR]                     # LLM-judge 打分
.venv\Scripts\python.exe eval\check_consistency.py                        # 校验评测资产一致性
```

- **评测集**：15 diffs / 28 埋点，源自 pingpong 项目**真实 bug 蒸馏**（dt 感知门、单位/量纲、
  死旗标、降采样过滤、COR 标定……），含 2 个**无埋点陷阱用例**（干净代码/纯重构）专测误报
- ground truth 在 `eval/truth.json`（每个埋点附**命中标准**）；diff 由 pre/post 机械生成，
  `check_consistency.py` 保证 diff↔fixture↔truth 三方一致
- judge 裁决经结构化校验 + 自动重试；校准：埋点判定 9/9 与人工一致（W2，见 cases.md）

## 上下文检索（W3）

`context.py`：解析 diff → 主动预取约定文档（CLAUDE.md）、改动文件全文、函数调用方片段（纯符号级检索，无向量库，预算 cap 28k chars）。`--no-context` 做 ablation（V0=agent 被动按需 read_file，V1=主动预取+按需工具）。

| 版本（n=28） | recall | precision | FP | noise |
|---|---|---|---|---|
| V0 被动工具 | 0.857 (24/28) | 0.421 | 3 | 30 |
| V1 +主动检索 | **0.893 (25/28)** | 0.446 | **0** | 31 |

检索修复约定/调用方依赖型 miss（符号投影、自举死区）并消灭全部误报；**瓶颈已从 recall 移到 noise**（30+ 条 style/投机建议把 precision 摁在 0.45）——W5 verifier 的靶子。失败分析详见 `eval/cases.md`。

## 限制

- 只有 read_file 一个执行工具；不跑 linter/测试
- 检索不追 import（d7 死旗标 bug 两版都漏的根因）——已知架构缺口
- 无 verifier 复核——noise 30+ 无人拦（W5 做）
- agent 未锁 temperature，run-to-run 有方差（d7-gap-connect 一版抓到一版漏）
- 没有 trace 落盘（W6 做）
