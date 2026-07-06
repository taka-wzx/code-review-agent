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

## 结构

- **agent loop**（agent.py）：`run_review()` — 调 API → 有 `tool_calls` 就执行、把 `role:"tool"` 结果回填 → 循环直到模型提交合法的 `submit_review`
- **工具**（tools.py，finder/verifier 共用）：`read_file`（路径逃逸检查、大文件按 `start_line` 续读、文件不存在时返回候选路径/"仓库里确实没有"——错误信息可恢复）+ `search_repo`（字面量全仓 grep,追 import/查符号是否存在）+ `submit_review`
- **结构化输出**：把「提交结果」做成 `submit_review` 工具、schema 当函数参数——不依赖任何厂商专有的 JSON 模式，跨 DeepSeek/GLM 通用；payload 过 `validate_review` 结构校验，非法则把问题回填重试（W6）
- **护栏**：`MAX_STEPS=10`、坏 submit 上限 2、同参数重复工具调用短路、`temperature=0`、每次请求 120s 超时（SDK 自动重试两次）、工具失败回可恢复的 `Error:` 文本而不是崩
- **trace**（tracelog.py）：JSONL 事件流（llm_response/tool/submit_rejected/review/verdicts），`agent.py --trace PATH` 手动开，`run_eval.py` 自动写 `<results>/traces/<name>.jsonl`（W6）

## 评测（W1 人工基线 + W2 自动打分 + 扩集）

```powershell
.venv\Scripts\python.exe run_eval.py [--no-context] [--results-dir DIR]   # 跑评测集
.venv\Scripts\python.exe judge.py [--results-dir DIR]                     # LLM-judge 打分
.venv\Scripts\python.exe eval\check_consistency.py                        # 校验评测资产一致性
```

- **评测集**：16 diffs / 30 埋点，源自 pingpong 项目**真实 bug 蒸馏**（dt 感知门、单位/量纲、
  死旗标、降采样过滤、COR 标定……），含 2 个**无埋点陷阱用例**（干净代码/纯重构）专测误报、
  1 个**信息缺失用例**（d16：import 仓库中不存在的模块）专测"编造 vs 诚实报告"
- ground truth 在 `eval/truth.json`（每个埋点附**命中标准**）；diff 由 pre/post 机械生成，
  `check_consistency.py` 保证 diff↔fixture↔truth 三方一致
- judge 裁决经结构化校验 + 自动重试；校准：埋点判定 9/9 与人工一致（W2，见 cases.md）

## 上下文检索（W3）

`context.py`：解析 diff → 主动预取约定文档（CLAUDE.md）、改动文件全文、函数调用方片段（纯符号级检索，无向量库，预算 cap 28k chars）。`--no-context` 做 ablation（V0=agent 被动按需 read_file，V1=主动预取+按需工具）。

| 版本（n=30，W6 重跑） | recall | precision | FP | noise |
|---|---|---|---|---|
| V0 被动工具 | **0.900 (27/30)** | 0.419 | 0 | 36 |
| V1 +主动检索 | 0.800 (24/30) | 0.348 | 0 | 43 |
| V2 +verifier | 0.800 (24/30) | **0.793** | 0 | **6** |

- **V2 主线不变**：`verifier.py` 独立二次复核（keep/drop 每条 finding，temperature=0，fail-open，
  被丢条目带 `drop_reason` 落盘可审计）仍是 precision 引擎（0.35→0.79，noise -86%），
  陷阱用例 d12 findings 归零
- **W6 重跑新信号**：V1 主动检索 recall 反降（0.90→0.80）——trace 归因：预取让 finder 探索变浅、
  提早交卷（d7 上 8 步 3 read → 4 步 1 read），疑与 W6 新增"step budget"提示词叠加；
  n=1 未排除方差，W7 复验（见 cases.md W6 重跑节）
- F1 视角：V0 0.57 → V1 **0.49** → **V2 0.80**（旧 28 埋点表 0.57/0.59/0.83，口径不可直接比）

## W6：工具层 + 可靠性补齐（对照 agent 路线文章逐项）

- finder 输出 schema 校验+重试（此前 submit_review payload 不校验直接用）
- `search_repo` 工具——agent 可按需追 import/查符号（对着 d7 屡漏的架构缺口）
- verifier 挂同款只读工具，"依赖未见代码的 finding 必须先查再判"（对着 d5 错杀:
  没有查证手段的 Reflection 只能猜）
- trace 落盘 + finder temperature=0 + 重复调用短路（对着 d7 run 方差不可归因）
- 新用例 d16 测"信息缺失时编造 vs 诚实报告"（评估文章:"错误失败"最危险）
- 30 埋点三版 ablation 已全量重跑（2026-07-06）：d16 三版全中、零编造✔；d7 仍漏✘
  （search_repo 在手但预取模式下不去用）；verifier 新错杀 d1 空列表除零
  （"search 证实无调用方→投机"）——完整拆账见 eval/cases.md「W6 全量重跑」节

## 限制

- 工具全只读（read_file/search_repo）；不跑 linter/测试
- 主动预取仍不追 import——现在靠 agent 用 search_repo 按需补,预取层缺口保留
- 主动预取在当前提示词下压低探索深度、伤 recall（W6 重跑 V1 0.90→0.80）——单次 run 证据,
  W7 需每版 n≥3 复验后再动 prompt
- verifier 的"投机建议"判据会误伤真 bug（W5 杀 d5 领域死区、W6 重跑杀 d1 空列表除零——
  查到"无调用方"后判投机,带工具只是让错杀更有依据）；prompt 迭代需先建 held-out 集防过拟合
