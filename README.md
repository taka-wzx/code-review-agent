# code-review-agent

最小 code review agent loop（W0 里程碑）。喂一段 unified diff → Claude 审查（可用 `read_file` 工具自己读仓库文件补上下文）→ 输出结构化 JSON review。

## 运行

```powershell
# 1. 设 API key（在 https://platform.claude.com 创建）
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# 2. 跑内置样例（sample.diff 里埋了几个真实 bug，看它能不能抓到）
E:/miniconda3/envs/tracker/python.exe agent.py sample.diff

# 3. 审查真实 commit
cd e:\shiyan\pingpong_tracker
git show HEAD --format="" > ..\code_review_agent\real.diff
cd ..\code_review_agent
E:/miniconda3/envs/tracker/python.exe agent.py real.diff --repo e:\shiyan\pingpong_tracker
```

## 结构（~150 行，全在 agent.py）

- **agent loop**：`run_review()` — 调 API → `stop_reason == "tool_use"` 就执行工具、把 `tool_result` 回填 → 循环直到 `end_turn`
- **工具**：`read_file`（带 repo root 路径逃逸检查）
- **结构化输出**：`output_config.format` + JSON schema，保证最终回答是合法 JSON
- **护栏**：`MAX_STEPS=8`、每次请求 120s 超时（SDK 自动重试 429/5xx 两次）、工具失败回 `is_error` 而不是崩

## 限制（W0 就该有的限制）

- 只有一个工具；不跑 linter/测试
- 没有 eval（W1 做）、没有 trace 落盘（W4 做）
- 大 diff 不做压缩（W3 做）
